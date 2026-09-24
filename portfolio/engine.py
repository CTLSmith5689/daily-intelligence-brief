"""Model portfolios: the ledger, valuation, style classification and the rules books.

Everything here is arithmetic over files already in the repository. It fetches
nothing and calls no model. The daily pipeline (lambda_function.py) and the
scripts in portfolio/bin/ both import it, so the site, the seed and the tests
run the same code.

Files
-----
portfolio/ledger/trades.csv     every cash movement and trade, append-only
portfolio/ledger/decisions.csv  every decision, with a short reason and its author
portfolio/ledger/mandates.csv   every mandate a book has had, append-only
portfolio/books/<id>/mandate.json  the current mandate (the last mandates.csv row)
data/portfolio/nav.csv          the value of each book at each close, recorded
                                by the daily run, append-only

All four CSVs are append-only and carry merge=union in .gitattributes. A writer
checks the header before appending and refuses a file whose header differs: a
new column needs a one-time migration (migrate_add_columns), never a silent
widening.

Prices
------
A holding is valued at the stored close for that exact date, from
docs/prices/TICKER.json. A missing close is never filled from another day: the
holding is left out, the day is marked partial and its NAV is left blank.
Returns are price-only (no dividends). Every trade pays COST_BPS of its value.
"""
import csv
import json
import math
import re
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
LEDGER_DIR = REPO / "portfolio" / "ledger"
BOOKS_DIR = REPO / "portfolio" / "books"
PANEL_DIR = REPO / "data" / "fundamentals"
NAV_CSV = REPO / "data" / "portfolio" / "nav.csv"
EVENTS_CSV = REPO / "theses" / "ledger" / "events.csv"
PRICES_DIR = REPO / "docs" / "prices"
BENCHMARKS_FILE = "_BENCHMARKS.json"
MARKET_FILE = "_MARKET.json"

COST_BPS = 5
COST_RATE = COST_BPS / 10000.0
INCEPTION_CAPITAL = 1_000_000.0

TRADE_COLUMNS = ["trade_id", "date", "book", "ticker", "side", "shares", "price", "cost",
                 "lot_id", "reason_code", "decision_id"]
DECISION_COLUMNS = ["date", "book", "decision_id", "action", "reason", "author"]
MANDATE_COLUMNS = ["date", "book", "decision_id", "mandate"]
NAV_COLUMNS = ["date", "book", "nav", "priced_nav", "cash", "long_value", "short_value",
               "capital", "holdings", "priced", "partial", "missing", "basis_break",
               "benchmark", "benchmark_close", "computed_at"]

# deposit and withdraw move cash in and out of a book (ticker CASH, price 1).
# buy/sell open and close long lots; short/cover open and close short lots.
SIDES = ("deposit", "withdraw", "buy", "sell", "short", "cover")
DECISION_ACTIONS = ("inception", "rebalance", "hold", "trade", "mandate_change")
AUTHORS = ("rules", "pm")

# A stored close that differs from the price a lot was bought at, on the same
# date, by more than this, means the stored series has been rebased since (Yahoo
# adjusts past closes for splits). Valuing the lot's shares at today's close would
# then show a loss or gain that never happened, so the holding is left out and the
# day marked partial until the ledger records the corporate action. Dividends move
# past closes by a few percent at most and stay under it.
BASIS_BREAK = 0.20

# ---------------------------------------------------------------------------
# The books

SIZES = (("large", "lg", "Large Cap"), ("mid", "mid", "Mid Cap"), ("small", "sm", "Small Cap"))
STYLES = (("growth", "Growth"), ("core", "Core"), ("value", "Value"))
STYLE_BENCHMARK = {
    ("large", "growth"): "IWY", ("large", "core"): "IWL", ("large", "value"): "IWX",
    ("mid", "growth"): "IWP", ("mid", "core"): "IWR", ("mid", "value"): "IWS",
    ("small", "growth"): "IWO", ("small", "core"): "IWM", ("small", "value"): "IWN",
}
BENCHMARK_NAMES = {
    "IWY": "Russell Top 200 Growth (iShares IWY)", "IWL": "Russell Top 200 (iShares IWL)",
    "IWX": "Russell Top 200 Value (iShares IWX)",
    "IWF": "Russell 1000 Growth (iShares IWF)",
    "IWD": "Russell 1000 Value (iShares IWD)", "IWP": "Russell Midcap Growth (iShares IWP)",
    "IWR": "Russell Midcap (iShares IWR)", "IWS": "Russell Midcap Value (iShares IWS)",
    "IWO": "Russell 2000 Growth (iShares IWO)", "IWM": "Russell 2000 (iShares IWM)",
    "IWN": "Russell 2000 Value (iShares IWN)", "IWV": "Russell 3000 (iShares IWV)",
    "^GSPC": "S&P 500",
}
# The series the benchmark fetch stores (the S&P 500 is already in _MARKET.json):
# the nine style books' funds, and the tax books' all-cap IWF, IWD and IWV.
BENCHMARK_SYMBOLS = ("IWY", "IWL", "IWX", "IWP", "IWR", "IWS", "IWO", "IWM", "IWN",
                     "IWF", "IWD", "IWV")


def _style_books():
    out = []
    for size, prefix, size_name in SIZES:
        for style, style_name in STYLES:
            out.append({"id": f"{prefix}-{style}", "name": f"{size_name} {style_name} Model",
                        "kind": "style", "size": size, "style": style,
                        "benchmark": STYLE_BENCHMARK[(size, style)],
                        "built_by": "Rules, reviewed by the PM", "status": "live"})
    return out


STYLE_BOOKS = _style_books()
# Later phases. Listed so the site can say what is coming; nothing trades them yet.
PLANNED_BOOKS = [
    {"id": "tax-growth", "name": "Tax-Managed Growth Model", "kind": "tax", "style": "growth",
     "benchmark": "IWF", "built_by": "Rules, reviewed by the PM, with tax rules", "status": "planned"},
    {"id": "tax-core", "name": "Tax-Managed Core Model", "kind": "tax", "style": "core",
     "benchmark": "IWV", "built_by": "Rules, reviewed by the PM, with tax rules", "status": "planned"},
    {"id": "tax-value", "name": "Tax-Managed Value Model", "kind": "tax", "style": "value",
     "benchmark": "IWD", "built_by": "Rules, reviewed by the PM, with tax rules", "status": "planned"},
    {"id": "hedge", "name": "Hedge Fund Strategy Model", "kind": "hedge",
     "benchmark": "^GSPC", "built_by": "The PM", "status": "planned"},
    {"id": "neural", "name": "Neural Model Portfolio", "kind": "neural",
     "benchmark": "^GSPC", "built_by": "The PM, with complete freedom", "status": "planned"},
]
BOOKS = {b["id"]: b for b in STYLE_BOOKS + PLANNED_BOOKS}

# The first mandate of a style book. The PM owns these and may change any of them;
# a change is a decision with a reason and a new mandates.csv row.
DEFAULT_STYLE_MANDATE = {
    "holdings_range": [25, 45],
    "max_position": 0.05,
    "sector_cap": 0.40,
    "cash_band": [0.0, 0.05],
    "turnover_budget": 1.0,
    "long_only": True,
    "weighting": "equal",
    "overweight_factor": 1.5,
}

# ---------------------------------------------------------------------------
# CSV plumbing


class SchemaError(ValueError):
    """A ledger file's header is not the one this code writes."""


def _num(v):
    if v is None or v == "":
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def read_rows(path):
    path = Path(path)
    if not path.exists():
        return []
    with path.open(encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def _header(path):
    with Path(path).open(encoding="utf-8", newline="") as fh:
        return next(csv.reader(fh), None)


def append_rows(path, columns, rows):
    """Append rows to an append-only CSV, writing the header for a new file.

    Never rewrites: an existing file is opened in append mode. A header that does
    not match `columns` exactly raises SchemaError instead of writing rows under
    the wrong names; widening a file is migrate_add_columns's job."""
    path = Path(path)
    rows = list(rows)
    if not rows:
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    exists = path.exists() and path.stat().st_size > 0
    if exists:
        head = _header(path)
        if head != list(columns):
            raise SchemaError(f"{path.name}: header {head} is not {list(columns)}; "
                              f"run a one-time migration first")
        with path.open("rb") as fh:
            fh.seek(-1, 2)
            needs_nl = fh.read(1) not in (b"\n",)
    with path.open("a", encoding="utf-8", newline="") as fh:
        if exists and needs_nl:
            fh.write("\n")
        w = csv.DictWriter(fh, fieldnames=list(columns), extrasaction="raise",
                           lineterminator="\n")
        if not exists:
            w.writeheader()
        for r in rows:
            w.writerow({k: _cell(r.get(k)) for k in columns})
    return len(rows)


def migrate_add_columns(path, columns):
    """The one-time migration for a new column: rewrite the file once with the new
    header, every existing row kept in order with the new cells blank. Refuses to
    drop or reorder a column. Run it by hand in a quiet window (no concurrent run
    appending), because a rewrite merged against another run's append under
    merge=union keeps both copies of every line."""
    path = Path(path)
    old = _header(path) or []
    if list(columns[:len(old)]) != old:
        raise SchemaError(f"{path.name}: new columns must extend {old}, not reorder it")
    if list(columns) == old:
        return 0
    rows = read_rows(path)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(columns), lineterminator="\n")
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in columns})
    tmp.replace(path)
    return len(rows)


def _cell(v):
    if v is None:
        return ""
    if isinstance(v, bool):
        return "1" if v else ""
    if isinstance(v, float):
        return repr(round(v, 6)) if v != int(v) or abs(v) >= 1e15 else str(int(v))
    return str(v)


# ---------------------------------------------------------------------------
# Stored prices

_WIN_RESERVED = {"CON", "PRN", "AUX", "NUL", "COM1", "COM2", "COM3", "COM4", "COM5",
                 "COM6", "COM7", "COM8", "COM9", "LPT1", "LPT2", "LPT3", "LPT4",
                 "LPT5", "LPT6", "LPT7", "LPT8", "LPT9"}


def price_filename(ticker):
    base = str(ticker).upper()
    return f"_{base}.json" if base in _WIN_RESERVED else f"{base}.json"


class PriceStore:
    """Stored daily closes by ticker and date. close(t, d) is the close stored for
    exactly d, or None. It never looks at another date."""

    def __init__(self, prices_dir=None):
        self.dir = Path(prices_dir or PRICES_DIR)
        self._cache = {}

    def series(self, ticker):
        if ticker not in self._cache:
            out = {}
            try:
                data = json.loads((self.dir / price_filename(ticker)).read_text(encoding="utf-8"))
                for row in data.get("closes") or []:
                    try:
                        px = float(row[1])
                    except (TypeError, ValueError, IndexError):
                        continue
                    if math.isfinite(px) and px > 0:
                        out[str(row[0])] = px
            except (OSError, ValueError):
                pass
            self._cache[ticker] = out
        return self._cache[ticker]

    def close(self, ticker, day):
        return self.series(ticker).get(day)


class BenchmarkStore:
    """Benchmark closes by symbol and date, from _BENCHMARKS.json (the Russell ETFs)
    and _MARKET.json (^GSPC). Exact dates only, like PriceStore."""

    def __init__(self, prices_dir=None):
        self.dir = Path(prices_dir or PRICES_DIR)
        self._series = None
        self.updated = ""

    def _load(self):
        if self._series is not None:
            return
        self._series = {}
        try:
            data = json.loads((self.dir / BENCHMARKS_FILE).read_text(encoding="utf-8"))
            self.updated = data.get("updated") or ""
            for sym, s in (data.get("series") or {}).items():
                self._series[sym] = {str(d): float(c) for d, c in (s.get("closes") or [])
                                     if _num(c) and float(c) > 0}
        except (OSError, ValueError, TypeError, AttributeError):
            pass
        try:
            data = json.loads((self.dir / MARKET_FILE).read_text(encoding="utf-8"))
            self._series["^GSPC"] = {str(d): float(c) for d, c in (data.get("benchmark") or [])
                                     if _num(c) and float(c) > 0}
        except (OSError, ValueError, TypeError, AttributeError):
            pass

    def close(self, symbol, day):
        self._load()
        return (self._series.get(symbol) or {}).get(day)

    def has(self, symbol):
        self._load()
        return bool(self._series.get(symbol))


def merge_benchmark_series(stored, fresh, tol=5e-4):
    """Fresh closes plus the stored ones the fresh download no longer covers.

    The download is a year long; the book's inception close must outlive that, so
    older stored points are kept. They are kept only when stored and fresh agree on
    the dates they share (within `tol`); after a split the old points sit on a
    different basis and are dropped rather than joined to the new ones."""
    fresh = sorted((str(d), float(c)) for d, c in (fresh or []) if _num(c) and float(c) > 0)
    stored = sorted((str(d), float(c)) for d, c in (stored or []) if _num(c) and float(c) > 0)
    if not fresh:
        return [[d, c] for d, c in stored]
    f = dict(fresh)
    shared = [d for d, _ in stored if d in f]
    same_basis = bool(shared) and all(
        abs(f[d] / c - 1) <= tol for d, c in stored if d in f)
    merged = dict(fresh)
    if same_basis:
        for d, c in stored:
            merged.setdefault(d, c)
    return [[d, merged[d]] for d in sorted(merged)]


# ---------------------------------------------------------------------------
# The panel


def panel_files(panel_dir=None):
    return sorted(Path(panel_dir or PANEL_DIR).glob("*.csv"))


def panel_dates(panel_dir=None):
    dates = set()
    for path in panel_files(panel_dir):
        with path.open(encoding="utf-8", newline="") as fh:
            r = csv.reader(fh)
            next(r, None)
            for row in r:
                if row:
                    dates.add(row[0])
    return sorted(dates)


def panel_rows(day=None, panel_dir=None):
    """(date, rows) for `day`, or for the latest date in the panel. The last row
    for a ticker wins."""
    files = panel_files(panel_dir)
    if not files:
        return "", []
    if day is None:
        day = max(panel_dates(panel_dir) or [""])
    by = {}
    for path in files:
        if path.stem != day[:7]:
            continue
        with path.open(encoding="utf-8", newline="") as fh:
            for r in csv.DictReader(fh):
                if r.get("date") == day and r.get("ticker"):
                    by[r["ticker"]] = r
    return day, list(by.values())


# ---------------------------------------------------------------------------
# Robust z, ported from web/zengine.js stat()


def _median(s):
    n = len(s)
    if not n:
        return float("nan")
    h = n >> 1
    return s[h] if n % 2 else (s[h - 1] + s[h]) / 2


def robust_stat(values):
    """zengine.js stat(): median and 1.4826 x MAD, falling back to mean and sample
    sd when the MAD is zero. Returns (center, scale) or None."""
    s = sorted(values)
    if not s:
        return None
    med = _median(s)
    mad = _median(sorted(abs(x - med) for x in s))
    center, scale = med, 1.4826 * mad
    if not scale > 0:
        mean = sum(s) / len(s)
        sd = math.sqrt(sum((x - mean) ** 2 for x in s) / (len(s) - 1)) if len(s) > 1 else float("nan")
        center, scale = mean, sd
    if not scale > 0:
        return None
    return center, scale


def robust_z(values_by_key, min_n=20, clip=5.0):
    """{key: value or None} -> {key: z or None}, clipped to +/-clip. A cohort with
    fewer than min_n values gets no z (MIN_COHORT_FOR_ZSCORE in the pipeline)."""
    vals = [v for v in values_by_key.values() if v is not None]
    if len(vals) < min_n:
        return {k: None for k in values_by_key}
    st = robust_stat(vals)
    if st is None:
        return {k: None for k in values_by_key}
    c, s = st
    return {k: (None if v is None else max(-clip, min(clip, (v - c) / s)))
            for k, v in values_by_key.items()}


# ---------------------------------------------------------------------------
# Style classification

# Russell-style size by rank of market cap: Top 200, Midcap (201 to 1000), 2000 (1001 to 3000).
SIZE_RANKS = (("large", 200), ("mid", 1000), ("small", 3000))
MIN_SECTOR_COHORT = 10
# A listing the repository can reliably tell is a foreign issuer's: a depositary share by
# name (the same pattern the pipeline's P/E code uses), or a filer whose EPS basis is
# "annual", which the pipeline sets only for a 20-F or 40-F filer with no quarterly XBRL.
# Foreign issuers filing IFRS statements carry no marker in the panel and stay in.
_DEPOSITARY_NAME = re.compile(r"depositar|depositor|\bADRs?\b|\bADS(?![-\w])", re.I)
SHARE_CLASS_CAP_TOL = 0.10
MIN_INPUTS = 2
MIN_COHORT = 20
QUALITY_WEIGHT = 0.5

VALUE_INPUTS = ("earnings_yield", "book_yield", "fcf_yield")
GROWTH_INPUTS = ("revenue_growth_yoy", "eps_growth_yoy", "revenue_acceleration")
# The panel's Quality group, lower-is-better fields negated so higher is better.
QUALITY_INPUTS = (("roe_ttm", 1), ("earnings_consistency", 1), ("net_debt_ebitda", -1),
                  ("op_margin_stability", -1), ("accruals_ratio", -1))

CLASSIFICATION_RULE = (
    "Operating companies only, leaving out depositary shares (ADRs) and foreign "
    "companies that file annual reports only (forms 20-F and 40-F). Share classes of one "
    "company that report the same market cap (within 10%) count once, as the most traded "
    "class. Ranked by market cap, as the Russell indexes are: the largest 200 are large (the "
    "Russell Top 200), ranks 201 to 1,000 are mid (the Russell Midcap), ranks 1,001 to 3,000 "
    "are small (the Russell 2000), and the rest (micro caps) are left out. Every input below "
    "is turned into a robust z-score against the companies of the same size and sector, or of "
    "the same size where the sector has fewer than 10, so a sector-wide boom in revenue does "
    "not read as growth. Value is the average z of earnings yield "
    "(1 / P/E, or diluted EPS / price where P/E is blank), book yield (1 / price-to-book) and "
    "FCF yield. Growth is the average z of revenue growth, EPS growth and revenue acceleration. "
    "Each needs at least 2 of its 3 inputs. Style is growth minus value: the top third is "
    "Growth, the bottom third Value, the middle third Core. No dividend yield is in the panel, "
    "so none is used.")
CANDIDATE_RULE = (
    "Within its box, each company is ranked by its box score plus half its quality score. The "
    "box score is the growth score for Growth, the value score for Value, and the average of "
    "the two for Core. Quality is the average z of return on equity, earnings consistency, "
    "net debt to EBITDA, operating margin volatility and the accruals ratio (the last three "
    "counted lower is better), within the same size; a company with fewer than 2 of them "
    "counts as average. The book holds the top N, where N is the middle of the mandate's "
    "holdings range, skipping a company once its sector is at the mandate's sector cap. "
    "Positions are equal weight. A company the analyst says to avoid, sell or bet against is "
    "left out; one the analyst says to initiate or add to is held at 1.5 times equal weight, "
    "up to the largest position the mandate allows.")


def _norm_name(name):
    n = re.sub(r"\(.*?\)", " ", str(name or ""))
    n = re.sub(r"\b(class|series)\s+[a-z0-9]\b", " ", n, flags=re.I)
    n = re.sub(r"\b(common stock|common shares|ordinary shares)\b", " ", n, flags=re.I)
    return re.sub(r"[^a-z0-9]+", " ", n.lower()).strip()


def _earnings_yield(r):
    pe = _num(r.get("pe"))
    if pe:
        return 1.0 / pe
    eps, px = _num(r.get("ttm_eps_diluted")), _num(r.get("price"))
    if eps is not None and px and px > 0:
        return eps / px
    return None


def _book_yield(r):
    pb = _num(r.get("price_book"))
    return 1.0 / pb if pb else None


def _inputs(r):
    return {
        "earnings_yield": _earnings_yield(r), "book_yield": _book_yield(r),
        "fcf_yield": _num(r.get("fcf_yield")),
        "revenue_growth_yoy": _num(r.get("revenue_growth_yoy")),
        "eps_growth_yoy": _num(r.get("eps_growth_yoy")),
        "revenue_acceleration": _num(r.get("revenue_acceleration")),
        **{k: _num(r.get(k)) for k, _ in QUALITY_INPUTS},
    }


def _mean_of(zs):
    got = [z for z in zs if z is not None]
    return sum(got) / len(got) if len(got) >= MIN_INPUTS else None


def _foreign_marker(r):
    if _DEPOSITARY_NAME.search(r.get("name") or ""):
        return "a depositary share (ADR)"
    if (r.get("eps_basis") or "") == "annual":
        return "a foreign issuer filing annual reports only (20-F or 40-F)"
    return None


def _sector_z(inputs, sectors, key):
    """Robust z of one input within sector, inside a size bucket. A sector with fewer
    than MIN_SECTOR_COHORT companies, or too few values for this input, falls back to
    the whole size bucket."""
    whole = robust_z({t: v[key] for t, v in inputs.items()}, MIN_COHORT)
    by = {}
    for t in inputs:
        by.setdefault(sectors[t] or "Unclassified", []).append(t)
    out = {}
    for sec, ts in by.items():
        zz = (robust_z({t: inputs[t][key] for t in ts}, MIN_SECTOR_COHORT)
              if len(ts) >= MIN_SECTOR_COHORT else {t: None for t in ts})
        for t in ts:
            if inputs[t][key] is None:
                out[t] = None
            else:
                out[t] = zz[t] if zz[t] is not None else whole[t]
    return out


def classify(rows):
    """Size bucket and style for every operating row of one panel date.

    Returns {ticker: info}. info always has size (large/mid/small/micro/None),
    style (growth/core/value/None), box ("large-growth" and so on, or None) and,
    when unclassified, a short `why`. Value and growth inputs are robust z within
    size and sector (size alone for a small sector); quality is within size."""
    out = {}
    op = []
    for r in rows:
        t = r.get("ticker")
        if not t:
            continue
        cap = _num(r.get("market_cap"))
        info = {"ticker": t, "name": r.get("name") or "", "sector": r.get("sector") or "",
                "market_cap": cap, "size": None, "style": None, "box": None,
                "value": None, "growth": None, "quality": None, "style_score": None}
        out[t] = info
        if (r.get("security_type") or "") != "operating":
            info["why"] = "not an operating company"
            continue
        if not cap or cap <= 0:
            info["why"] = "no market cap"
            continue
        foreign = _foreign_marker(r)
        if foreign:
            info["why"] = foreign
            info["foreign"] = True
            continue
        op.append(r)

    # One company, several share classes, one market cap: count it once.
    groups = {}
    for r in op:
        groups.setdefault(_norm_name(r.get("name")), []).append(r)
    counted = []
    for key, rs in groups.items():
        if len(rs) == 1 or not key:
            counted.extend(rs)
            continue
        rs = sorted(rs, key=lambda r: -((_num(r.get("price")) or 0) * (_num(r.get("volume")) or 0)))
        primaries = []
        for r in rs:
            cap = _num(r["market_cap"])
            twin = next((p for p in primaries
                         if abs(cap / _num(p["market_cap"]) - 1) <= SHARE_CLASS_CAP_TOL), None)
            if twin is None:
                primaries.append(r)
            else:
                out[r["ticker"]]["why"] = f"another share class of {twin['ticker']}"
                out[r["ticker"]]["share_class_of"] = twin["ticker"]
        counted.extend(primaries)

    counted.sort(key=lambda r: (-_num(r["market_cap"]), r["ticker"]))
    buckets = {"large": [], "mid": [], "small": []}
    for rank, r in enumerate(counted, 1):
        size = next((name for name, last in SIZE_RANKS if rank <= last), "micro")
        out[r["ticker"]]["size"] = size
        out[r["ticker"]]["cap_rank"] = rank
        if size == "micro":
            out[r["ticker"]]["why"] = "micro cap (ranked below 3,000 by market cap)"
        else:
            buckets[size].append(r)

    for size, rs in buckets.items():
        inputs = {r["ticker"]: _inputs(r) for r in rs}
        sectors = {r["ticker"]: r.get("sector") or "" for r in rs}
        z = {}
        for key in VALUE_INPUTS + GROWTH_INPUTS:
            z[key] = _sector_z(inputs, sectors, key)
        for key, sign in QUALITY_INPUTS:
            zz = robust_z({t: v[key] for t, v in inputs.items()}, MIN_COHORT)
            z[key] = {t: (None if x is None else sign * x) for t, x in zz.items()}
        scored = []
        for t in inputs:
            info = out[t]
            info["value"] = _mean_of([z[k][t] for k in VALUE_INPUTS])
            info["growth"] = _mean_of([z[k][t] for k in GROWTH_INPUTS])
            info["quality"] = _mean_of([z[k][t] for k, _ in QUALITY_INPUTS])
            if info["value"] is None or info["growth"] is None:
                info["why"] = ("fewer than 2 of the 3 value inputs" if info["value"] is None
                               else "fewer than 2 of the 3 growth inputs")
                continue
            info["style_score"] = info["growth"] - info["value"]
            scored.append(info)
        scored.sort(key=lambda i: (i["style_score"], i["ticker"]))
        n = len(scored)
        third = n // 3
        for k, info in enumerate(scored):
            info["style"] = "value" if k < third else "growth" if k >= n - third else "core"
            info["box"] = f"{size}-{info['style']}"
    return out


def box_counts(classes):
    counts = {}
    for info in classes.values():
        key = info["box"] or (info["size"] == "micro" and "micro") or "unclassified"
        counts[key] = counts.get(key, 0) + 1
    return counts


# ---------------------------------------------------------------------------
# Analyst views (theses/ledger/events.csv)

EXCLUDE_ACTIONS = {"avoid", "exit", "sell"}
OVERWEIGHT_ACTIONS = {"initiate", "add"}
EXCLUDE_DIRECTIONS = {"avoid", "short"}


def analyst_views(path=None):
    """{ticker: {"exclude": bool, "overweight": bool, "why": str}} from the newest
    event per ticker. The memo format's `action` column decides when present; the
    older notes carry only a direction, and a direction of avoid or short also
    keeps a company out of a long-only book."""
    rows = read_rows(path or EVENTS_CSV)
    latest = {}
    for e in rows:
        if e.get("ticker"):
            latest[e["ticker"]] = e
    out = {}
    for t, e in latest.items():
        if e.get("kind") == "close":
            continue
        action = (e.get("action") or "").strip().lower()
        direction = (e.get("direction") or "").strip().lower()
        ex = action in EXCLUDE_ACTIONS or direction in EXCLUDE_DIRECTIONS
        ow = (not ex) and action in OVERWEIGHT_ACTIONS
        if ex or ow:
            out[t] = {"exclude": ex, "overweight": ow,
                      "why": f"analyst: {action or direction}"}
    return out


# ---------------------------------------------------------------------------
# The rules candidate book


def target_count(mandate):
    lo, hi = mandate["holdings_range"]
    return int(round((lo + hi) / 2.0))


def rules_candidate(book, classes, mandate, views=None, eligible=None):
    """The rules book for one style box: [{ticker, weight, rank, score, ...}].

    `eligible(ticker)` can veto a name (the seed uses it for "has a stored close on
    the trade date"); vetoed names are skipped, not replaced by guesses."""
    views = views or {}
    members = [i for i in classes.values()
               if i["size"] == book["size"] and i["style"] == book["style"]]

    def box_score(i):
        if book["style"] == "growth":
            return i["growth"]
        if book["style"] == "value":
            return i["value"]
        return (i["growth"] + i["value"]) / 2.0

    ranked = []
    for i in members:
        q = i["quality"] if i["quality"] is not None else 0.0
        ranked.append((box_score(i) + QUALITY_WEIGHT * q, i))
    ranked.sort(key=lambda x: (-x[0], x[1]["ticker"]))
    n = target_count(mandate)
    per_sector = max(1, int(math.floor(mandate["sector_cap"] * n + 1e-9)))
    picks, skipped = [], []
    sectors = {}
    over = [x for x in ranked if views.get(x[1]["ticker"], {}).get("overweight")]
    rest = [x for x in ranked if x not in over]
    for rank_score, i in over + rest:
        t = i["ticker"]
        if len(picks) >= n:
            break
        if views.get(t, {}).get("exclude"):
            skipped.append({"ticker": t, "why": views[t]["why"]})
            continue
        if eligible is not None and not eligible(t):
            skipped.append({"ticker": t, "why": "no stored close on the trade date"})
            continue
        sec = i["sector"] or "Unclassified"
        if sectors.get(sec, 0) >= per_sector:
            continue
        sectors[sec] = sectors.get(sec, 0) + 1
        picks.append((rank_score, i))
    if not picks:
        return []
    eq = 1.0 / len(picks)
    heavy = {i["ticker"] for _, i in picks if views.get(i["ticker"], {}).get("overweight")}
    w_heavy = min(mandate["max_position"], mandate.get("overweight_factor", 1.5) * eq)
    if heavy and len(heavy) < len(picks):
        w_rest = (1.0 - w_heavy * len(heavy)) / (len(picks) - len(heavy))
    else:
        w_heavy = w_rest = eq
    out = []
    for k, (rank_score, i) in enumerate(picks):
        out.append({"ticker": i["ticker"], "name": i["name"], "sector": i["sector"],
                    "weight": w_heavy if i["ticker"] in heavy else w_rest,
                    "rank": k + 1, "score": rank_score, "value": i["value"],
                    "growth": i["growth"], "quality": i["quality"],
                    "market_cap": i["market_cap"], "overweight": i["ticker"] in heavy})
    return out


# ---------------------------------------------------------------------------
# The ledger: positions, lots and value


def trades_for(trades, book, upto=None):
    return [t for t in trades if t.get("book") == book and (upto is None or t["date"] <= upto)]


def derive_lots(trades):
    """Open lots and closed lots from one book's trades, in file order.

    A buy or short opens a lot whose id is its lot_id (the trade_id when blank). A
    sell or cover closes shares of the lot named in lot_id, or, when blank, of the
    oldest open lot on that side (FIFO). Tax books will name lots; the style books
    use FIFO. Raises ValueError on a sale of shares that are not held."""
    open_lots, closed = [], []
    for t in trades:
        side = t["side"]
        if side in ("deposit", "withdraw"):
            continue
        sh, px = float(t["shares"]), float(t["price"])
        if side in ("buy", "short"):
            open_lots.append({"lot_id": t.get("lot_id") or t["trade_id"], "ticker": t["ticker"],
                              "side": "long" if side == "buy" else "short", "date": t["date"],
                              "shares": sh, "price": px})
            continue
        want = "long" if side == "sell" else "short"
        remaining = sh
        named = t.get("lot_id") or ""
        pool = [l for l in open_lots if l["ticker"] == t["ticker"] and l["side"] == want
                and (not named or l["lot_id"] == named)]
        for lot in pool:
            if remaining <= 1e-9:
                break
            take = min(lot["shares"], remaining)
            lot["shares"] -= take
            remaining -= take
            sign = 1 if want == "long" else -1
            closed.append({"lot_id": lot["lot_id"], "ticker": lot["ticker"], "side": want,
                           "opened": lot["date"], "closed": t["date"], "shares": take,
                           "cost_price": lot["price"], "close_price": px,
                           "gain": sign * take * (px - lot["price"])})
        if remaining > 1e-6:
            raise ValueError(f"{t['trade_id']}: {side} of {sh} {t['ticker']} exceeds the "
                             f"open {want} lots")
        open_lots = [l for l in open_lots if l["shares"] > 1e-9]
    return open_lots, closed


def book_state(trades):
    """Cash, capital and signed shares from one book's trades."""
    cash = capital = 0.0
    shares = {}
    for t in trades:
        side = t["side"]
        sh, px = float(t["shares"]), float(t["price"])
        cost = float(t.get("cost") or 0)
        gross = sh * px
        if side == "deposit":
            cash += gross
            capital += gross
        elif side == "withdraw":
            cash -= gross
            capital -= gross
        elif side == "buy":
            cash -= gross + cost
            shares[t["ticker"]] = shares.get(t["ticker"], 0.0) + sh
        elif side == "sell":
            cash += gross - cost
            shares[t["ticker"]] = shares.get(t["ticker"], 0.0) - sh
        elif side == "short":
            cash += gross - cost
            shares[t["ticker"]] = shares.get(t["ticker"], 0.0) - sh
        elif side == "cover":
            cash -= gross + cost
            shares[t["ticker"]] = shares.get(t["ticker"], 0.0) + sh
        else:
            raise ValueError(f"unknown side {side!r}")
    return {"cash": cash, "capital": capital,
            "shares": {k: v for k, v in shares.items() if abs(v) > 1e-9}}


def inception(trades, book):
    dep = [t for t in trades if t.get("book") == book and t["side"] == "deposit"]
    return dep[0]["date"] if dep else None


def value_book(trades, book, day, prices):
    """The book at the close of `day`, from its trades and the stored closes for
    exactly that day. Nothing is carried from another day: a holding with no stored
    close (or whose stored series no longer matches its lot prices, see
    BASIS_BREAK) is left out and listed, the day is partial, and nav is None."""
    ts = trades_for(trades, book, day)
    st = book_state(ts)
    open_lots, _ = derive_lots(ts)
    marks, missing, broken = [], [], []
    long_v = short_v = 0.0
    for tk in sorted(st["shares"]):
        sh = st["shares"][tk]
        px = prices.close(tk, day)
        if px is None:
            missing.append(tk)
            continue
        lots = [l for l in open_lots if l["ticker"] == tk]
        if any(_basis_broken(prices, l) for l in lots):
            broken.append(tk)
            continue
        v = sh * px
        if v >= 0:
            long_v += v
        else:
            short_v += v
        cost = sum(l["shares"] * l["price"] for l in lots)
        held = sum(l["shares"] for l in lots)
        marks.append({"ticker": tk, "shares": sh, "close": px, "value": v,
                      "avg_cost": cost / held if held else None,
                      "since": min((l["date"] for l in lots), default=None)})
    priced_nav = st["cash"] + long_v + short_v
    partial = bool(missing or broken)
    return {"date": day, "book": book, "cash": st["cash"], "capital": st["capital"],
            "long_value": long_v, "short_value": short_v, "priced_nav": priced_nav,
            "nav": None if partial else priced_nav, "partial": partial,
            "holdings": len(st["shares"]), "priced": len(marks),
            "missing": missing, "basis_break": broken, "marks": marks}


def _basis_broken(prices, lot):
    then = prices.close(lot["ticker"], lot["date"])
    return then is not None and abs(then / lot["price"] - 1) > BASIS_BREAK


# ---------------------------------------------------------------------------
# Daily NAV record (data/portfolio/nav.csv)


def _nav_key(row):
    return (row.get("nav") or "", row.get("partial") or "", row.get("missing") or "",
            row.get("basis_break") or "", row.get("benchmark_close") or "",
            row.get("holdings") or "")


def nav_row(v, benchmark, bench_close, computed_at):
    return {"date": v["date"], "book": v["book"],
            "nav": None if v["nav"] is None else round(v["nav"], 2),
            "priced_nav": round(v["priced_nav"], 2), "cash": round(v["cash"], 2),
            "long_value": round(v["long_value"], 2), "short_value": round(v["short_value"], 2),
            "capital": round(v["capital"], 2), "holdings": v["holdings"], "priced": v["priced"],
            "partial": "1" if v["partial"] else "", "missing": ";".join(v["missing"]),
            "basis_break": ";".join(v["basis_break"]), "benchmark": benchmark,
            "benchmark_close": bench_close, "computed_at": computed_at}


def latest_nav_rows(nav_rows):
    """The last recorded row per (book, date): a later row supersedes an earlier
    partial one for the same close."""
    out = {}
    for r in nav_rows:
        out[(r["book"], r["date"])] = r
    return out


def record_nav(trades, dates, prices, benchmarks, nav_csv=None, now=None):
    """Append one row per book per session date since inception, unless the last
    row recorded for that book and date is already complete (not partial, with a
    benchmark close) or would be identical. Returns the number of rows appended.
    Never trades and never rewrites."""
    nav_csv = Path(nav_csv or NAV_CSV)
    now = now or datetime.now(timezone.utc).isoformat(timespec="seconds")
    have = latest_nav_rows(read_rows(nav_csv))
    new = []
    books = sorted({t["book"] for t in trades if t["side"] == "deposit"})
    for b in books:
        start = inception(trades, b)
        bench = (BOOKS.get(b) or {}).get("benchmark", "")
        for d in dates:
            if d < start:
                continue
            last = have.get((b, d))
            if last and not last.get("partial") and last.get("benchmark_close"):
                continue
            v = value_book(trades, b, d, prices)
            bc = benchmarks.close(bench, d) if bench else None
            row = nav_row(v, bench, bc, now)
            if last and _nav_key({k: _cell(x) for k, x in row.items()}) == _nav_key(last):
                continue
            new.append(row)
    append_rows(nav_csv, NAV_COLUMNS, new)
    return len(new)


# ---------------------------------------------------------------------------
# Mandates


def mandate_path(book, books_dir=None):
    return Path(books_dir or BOOKS_DIR) / book / "mandate.json"


def load_mandate(book, books_dir=None):
    try:
        return json.loads(mandate_path(book, books_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def mandate_history(book, ledger_dir=None):
    rows = [r for r in read_rows(Path(ledger_dir or LEDGER_DIR) / "mandates.csv")
            if r.get("book") == book]
    return [{"date": r["date"], "decision_id": r["decision_id"],
             "mandate": json.loads(r["mandate"])} for r in rows]


# ---------------------------------------------------------------------------
# Seeding


def _long_date(day):
    d = datetime.strptime(day, "%Y-%m-%d")
    return f"{d.day} {d:%B %Y}"


def _shares_for(value, price):
    return int(math.floor(value / (price * (1 + COST_RATE))))


def seed(ledger_dir=None, books_dir=None, panel_dir=None, prices=None, events=None,
         day=None, max_back=5, dry_run=False, log=print):
    """Incept every style book that has not been incepted.

    Idempotent: a book with a deposit row in trades.csv is never seeded again, so a
    second run appends nothing. The date is the latest panel date (or `day`) on
    which every holding of every book being seeded has a stored close; up to
    `max_back` earlier panel dates are tried. Each book gets INCEPTION_CAPITAL,
    its first mandate (if it has none) and the rules candidate book, bought in
    whole shares at the stored close for that date, each trade paying COST_BPS."""
    ledger_dir = Path(ledger_dir or LEDGER_DIR)
    books_dir = Path(books_dir or BOOKS_DIR)
    prices = prices or PriceStore()
    trades = read_rows(ledger_dir / "trades.csv")
    done = {t["book"] for t in trades if t.get("side") == "deposit"}
    todo = [b for b in STYLE_BOOKS if b["id"] not in done]
    if not todo:
        log("seed: every style book already has an inception; nothing to do.")
        return {"seeded": [], "date": None}
    views = analyst_views(events)
    mandates = {b["id"]: (load_mandate(b["id"], books_dir) or dict(DEFAULT_STYLE_MANDATE))
                for b in todo}
    dates = [d for d in panel_dates(panel_dir) if day is None or d <= day]
    chosen = None
    for d in sorted(dates, reverse=True)[:max_back + 1]:
        _, rows = panel_rows(d, panel_dir)
        classes = classify(rows)
        books = {b["id"]: rules_candidate(b, classes, mandates[b["id"]], views) for b in todo}
        missing = sorted({p["ticker"] for ps in books.values() for p in ps
                          if prices.close(p["ticker"], d) is None})
        if missing:
            log(f"seed: {d} lacks a stored close for {len(missing)} holding(s) "
                f"({', '.join(missing[:8])}); trying the panel date before.")
            continue
        chosen = (d, classes, books)
        break
    if chosen is None:
        raise RuntimeError("seed: no recent panel date has a stored close for every holding")
    d, classes, books = chosen
    stamp = d
    t_rows, d_rows, m_rows = [], [], []
    for b in todo:
        bid, mandate = b["id"], mandates[b["id"]]
        picks = books[bid]
        n_prior = len([r for r in read_rows(ledger_dir / "decisions.csv") if r.get("book") == bid])
        dec_m = f"{bid}-{stamp}-{n_prior + 1}"
        dec_i = f"{bid}-{stamp}-{n_prior + 2}"
        if not mandate_history(bid, ledger_dir):
            m_rows.append({"date": d, "book": bid, "decision_id": dec_m,
                           "mandate": json.dumps(mandate, sort_keys=True, separators=(",", ":"))})
            d_rows.append({"date": d, "book": bid, "decision_id": dec_m, "action": "mandate_change",
                           "reason": "First mandate: the default for a style book, until the PM "
                                     "reviews it.", "author": "rules"})
        d_rows.append({"date": d, "book": bid, "decision_id": dec_i, "action": "inception",
                       "reason": f"Start with ${INCEPTION_CAPITAL:,.0f} of paper cash and buy the "
                                 f"rules candidate book ({len(picks)} companies, equal weight) "
                                 f"at the close on {_long_date(d)}.", "author": "rules"})
        seq = 1
        tid = f"{bid}-{stamp}-{seq:03d}"
        t_rows.append({"trade_id": tid, "date": d, "book": bid, "ticker": "CASH", "side": "deposit",
                       "shares": INCEPTION_CAPITAL, "price": 1, "cost": 0, "lot_id": "",
                       "reason_code": "inception_capital", "decision_id": dec_i})
        for p in picks:
            px = prices.close(p["ticker"], d)
            sh = _shares_for(p["weight"] * INCEPTION_CAPITAL, px)
            if sh <= 0:
                log(f"seed: {bid} {p['ticker']} at ${px:,.2f} buys no whole share at "
                    f"{p['weight']:.2%}; left in cash.")
                continue
            seq += 1
            tid = f"{bid}-{stamp}-{seq:03d}"
            t_rows.append({"trade_id": tid, "date": d, "book": bid, "ticker": p["ticker"],
                           "side": "buy", "shares": sh, "price": px,
                           "cost": round(sh * px * COST_RATE, 2), "lot_id": tid,
                           "reason_code": "rules_inception", "decision_id": dec_i})
    if dry_run:
        return {"seeded": [b["id"] for b in todo], "date": d, "trades": t_rows,
                "decisions": d_rows, "mandates": m_rows, "books": books, "classes": classes}
    for b in todo:
        p = mandate_path(b["id"], books_dir)
        if not p.exists():
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(mandates[b["id"]], indent=2, sort_keys=True) + "\n",
                         encoding="utf-8")
    append_rows(ledger_dir / "mandates.csv", MANDATE_COLUMNS, m_rows)
    append_rows(ledger_dir / "decisions.csv", DECISION_COLUMNS, d_rows)
    # trades.csv last: its deposit row is what marks a book as incepted.
    append_rows(ledger_dir / "trades.csv", TRADE_COLUMNS, t_rows)
    log(f"seed: incepted {len(todo)} book(s) on {d}: {len(t_rows)} trades.")
    return {"seeded": [b["id"] for b in todo], "date": d, "trades": t_rows,
            "decisions": d_rows, "mandates": m_rows, "books": books, "classes": classes}


# ---------------------------------------------------------------------------
# What the site shows


def _r(v, n=6):
    return None if v is None else round(v, n)


def site_data(ledger_dir=None, books_dir=None, panel_dir=None, prices=None,
              benchmarks=None, nav_csv=None, events=None, repo_url=""):
    """Everything the Portfolios and book pages draw, as plain JSON.

    The rules candidate book is recomputed here from the latest panel date, so it
    is published fresh every run for the PM to compare with what is held."""
    ledger_dir = Path(ledger_dir or LEDGER_DIR)
    prices = prices or PriceStore()
    benchmarks = benchmarks or BenchmarkStore()
    trades = read_rows(ledger_dir / "trades.csv")
    decisions = read_rows(ledger_dir / "decisions.csv")
    navs = latest_nav_rows(read_rows(nav_csv or NAV_CSV))
    asof, rows = panel_rows(None, panel_dir)
    classes = classify(rows) if rows else {}
    views = analyst_views(events)
    counts = box_counts(classes)
    names = {r["ticker"]: (r.get("name") or "", r.get("sector") or "") for r in rows}
    books = []
    for b in STYLE_BOOKS:
        bid = b["id"]
        mandate = load_mandate(bid, books_dir) or dict(DEFAULT_STYLE_MANDATE)
        cand = rules_candidate(b, classes, mandate, views) if classes else []
        start = inception(trades, bid)
        item = dict(b, benchmarkName=BENCHMARK_NAMES.get(b["benchmark"], b["benchmark"]),
                    mandate=mandate, inception=start, boxCount=counts.get(f"{b['size']}-{b['style']}", 0),
                    candidate=[{k: (_r(v) if isinstance(v, float) else v) for k, v in p.items()}
                               for p in cand],
                    candidateAsof=asof, targetCount=target_count(mandate))
        if start:
            ts = trades_for(trades, bid)
            v = value_book(trades, bid, asof, prices) if asof >= start else None
            b0, b1 = benchmarks.close(b["benchmark"], start), benchmarks.close(b["benchmark"], asof)
            held = set(book_state(ts)["shares"])
            total = v["priced_nav"] if v else None
            marks = []
            for m in (v or {}).get("marks", []):
                nm, sec = names.get(m["ticker"], ("", ""))
                marks.append({"ticker": m["ticker"], "name": nm, "sector": sec,
                              "shares": m["shares"], "close": m["close"],
                              "value": round(m["value"], 2), "avgCost": _r(m["avg_cost"], 4),
                              "since": m["since"],
                              "weight": _r(m["value"] / total) if total else None,
                              "ret": _r(m["close"] / m["avg_cost"] - 1) if m["avg_cost"] else None})
            marks.sort(key=lambda m: -abs(m["value"]))
            unpriced = [{"ticker": t, "name": names.get(t, ("", ""))[0],
                         "shares": book_state(ts)["shares"].get(t), "why": why}
                        for why, lst in (("no stored close", (v or {}).get("missing", [])),
                                         ("stored prices rebased", (v or {}).get("basis_break", [])))
                        for t in lst]
            sectors = {}
            for m in marks:
                sectors[m["sector"] or "Unclassified"] = sectors.get(m["sector"] or "Unclassified", 0) + m["value"]
            series = sorted((r for (bk, _), r in navs.items() if bk == bid), key=lambda r: r["date"])
            item.update({
                "asof": asof,
                "nav": _r(v["nav"], 2) if v else None,
                "pricedNav": _r(v["priced_nav"], 2) if v else None,
                "partial": bool(v and v["partial"]),
                "cash": _r(v["cash"], 2) if v else None,
                "capital": _r(v["capital"], 2) if v else None,
                "ret": _r(v["nav"] / v["capital"] - 1) if v and v["nav"] is not None and v["capital"] else None,
                "benchStart": b0, "benchNow": b1,
                "benchRet": _r(b1 / b0 - 1) if b0 and b1 else None,
                "holdings": marks, "unpriced": unpriced,
                "holdingsCount": len(book_state(ts)["shares"]),
                "sectors": sorted(([s, _r(x / total)] for s, x in sectors.items()),
                                  key=lambda kv: -kv[1]) if total else [],
                "trades": [{k: t.get(k) for k in TRADE_COLUMNS} for t in ts],
                "decisions": [d for d in decisions if d.get("book") == bid],
                "mandateHistory": mandate_history(bid, ledger_dir),
                "series": [{"date": r["date"], "nav": _num(r.get("nav")),
                            "partial": bool(r.get("partial")), "missing": r.get("missing") or "",
                            "capital": _num(r.get("capital")),
                            "bench": _num(r.get("benchmark_close"))} for r in series],
                "candidateAdds": [p["ticker"] for p in cand if p["ticker"] not in held],
                "candidateDrops": sorted(t for t in held if t not in {p["ticker"] for p in cand}),
            })
            mine = [d for d in decisions if d.get("book") == bid]
            item["lastDecision"] = mine[-1] if mine else None
        books.append(item)
    return {
        "asof": asof, "costBps": COST_BPS, "capital": INCEPTION_CAPITAL,
        "classificationRule": CLASSIFICATION_RULE, "candidateRule": CANDIDATE_RULE,
        "boxCounts": counts, "books": books, "planned": PLANNED_BOOKS,
        "benchmarksUpdated": benchmarks.updated if hasattr(benchmarks, "updated") else "",
    }
