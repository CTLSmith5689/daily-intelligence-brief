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
STYLES = (("growth", "Growth"), ("value", "Value"))
STYLE_BENCHMARK = {
    ("large", "growth"): "IWY", ("large", "value"): "IWX",
    ("mid", "growth"): "IWP", ("mid", "value"): "IWS",
    ("small", "growth"): "IWO", ("small", "value"): "IWN",
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
# the six style books' funds, the core funds (IWL, IWR, IWM) for the core books
# that may come later, and the all-cap IWF, IWD and IWV for the tax books.
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
# Built by the weekly PM routine, not by rules. They start in cash.
PM_BOOKS = [
    {"id": "hedge", "name": "Hedge Fund Strategy Model", "kind": "hedge",
     "benchmark": "^GSPC", "cash_benchmark": True, "built_by": "The PM", "status": "live"},
    {"id": "neural", "name": "Neural Model Portfolio", "kind": "neural",
     "benchmark": "^GSPC", "built_by": "The PM, with complete freedom", "status": "live"},
]
# Reserved ids. Listed so the site can say what is coming; nothing trades them.
PLANNED_BOOKS = [
    {"id": "lg-core", "name": "Large Cap Core Model", "kind": "style", "benchmark": "IWL",
     "status": "planned"},
    {"id": "mid-core", "name": "Mid Cap Core Model", "kind": "style", "benchmark": "IWR",
     "status": "planned"},
    {"id": "sm-core", "name": "Small Cap Core Model", "kind": "style", "benchmark": "IWM",
     "status": "planned"},
    {"id": "tax-growth", "name": "Tax-Managed Growth Model", "kind": "tax", "benchmark": "IWF",
     "status": "planned"},
    {"id": "tax-core", "name": "Tax-Managed Core Model", "kind": "tax", "benchmark": "IWV",
     "status": "planned"},
    {"id": "tax-value", "name": "Tax-Managed Value Model", "kind": "tax", "benchmark": "IWD",
     "status": "planned"},
    {"id": "momentum", "name": "Momentum Model", "kind": "momentum", "benchmark": "",
     "status": "planned"},
]
LIVE_BOOKS = STYLE_BOOKS + PM_BOOKS
BOOKS = {b["id"]: b for b in LIVE_BOOKS + PLANNED_BOOKS}

# The first mandate of each book. The PM owns these and may change any of them;
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
    "universe": "style_box",
}
DEFAULT_HEDGE_MANDATE = {
    "long_only": False,
    "gross_max": 2.0,
    "net_range": [-0.20, 0.60],
    "max_long_position": 0.05,
    "max_short_position": 0.03,
    "holdings_range_per_side": [20, 60],
    "universe": "operating",
    "benchmarks": ["^GSPC", "cash"],
}
DEFAULT_NEURAL_MANDATE = {
    "long_only": False,
    "gross_max": 2.0,
    "universe": "stored_close",
    "data": "repository only, no internet",
    "benchmarks": ["^GSPC"],
}


def default_mandate(book_id):
    kind = (BOOKS.get(book_id) or {}).get("kind")
    base = {"style": DEFAULT_STYLE_MANDATE, "hedge": DEFAULT_HEDGE_MANDATE,
            "neural": DEFAULT_NEURAL_MANDATE}.get(kind)
    return json.loads(json.dumps(base)) if base else None


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
        self._series, self._rates = {}, {}
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
            self._rates = {str(d): float(c) for d, c in (data.get("risk_free") or [])
                           if _num(c) is not None}
        except (OSError, ValueError, TypeError, AttributeError):
            pass

    def close(self, symbol, day):
        self._load()
        return (self._series.get(symbol) or {}).get(day)

    def rate(self, day):
        """The 13-week Treasury bill rate stored for exactly `day`, in percent."""
        self._load()
        return self._rates.get(day)

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
# Multi-year history (data/financials/style_history.csv)

STYLE_HISTORY_CSV = REPO / "data" / "financials" / "style_history.csv"
STYLE_HISTORY_COLUMNS = ["ticker", "cik", "fy_end", "start_fy_end", "annual_form", "revenue",
                         "start_revenue", "net_income", "start_net_income", "shares_diluted",
                         "start_shares_diluted", "ocf", "sales_ps_growth_3y", "eps_growth_3y",
                         "note", "collected_at"]
# The start year is the annual period ending three fiscal years before the latest.
_THREE_YEARS_DAYS = (1060, 1130)
# A year-on-year change in the diluted share count this large is a split or a
# merger, not dilution; per-share growth across it is left blank.
SHARE_BASIS_JUMP = 1.8
# The latest fiscal year must have ended within this many days of the panel date
# (15 months, the pipeline's own limit for an annual figure).
HISTORY_MAX_AGE_DAYS = 457


def _days(a, b):
    return (datetime.strptime(b, "%Y-%m-%d") - datetime.strptime(a, "%Y-%m-%d")).days


def style_growth(periods):
    """Three-year growth from annual periods [{period_end, revenue, net_income,
    shares_diluted, ocf}].

    Sales per share and EPS are each built as a total divided by the weighted
    diluted share count of the same year, so no per-share figure is ever
    subtracted or summed across quarters. Growth is the compound annual rate over
    the three years. A figure that cannot be computed honestly is left blank and
    `note` says why: no start year, a start or end value that is not positive (a
    growth rate from a loss is not defined), or a share-count jump above
    SHARE_BASIS_JUMP between consecutive years (a split or a merger)."""
    ps = sorted((p for p in periods if p.get("period_end")), key=lambda p: p["period_end"])
    have = [p for p in ps if _num(p.get("revenue")) is not None
            and (_num(p.get("shares_diluted")) or 0) > 0]
    out = {k: None for k in ("fy_end", "start_fy_end", "revenue", "start_revenue", "net_income",
                             "start_net_income", "shares_diluted", "start_shares_diluted", "ocf",
                             "sales_ps_growth_3y", "eps_growth_3y")}
    out["note"] = ""
    if not have:
        out["note"] = "no annual period with revenue and diluted shares"
        return out
    end = have[-1]
    out.update(fy_end=end["period_end"], revenue=_num(end.get("revenue")),
               net_income=_num(end.get("net_income")),
               shares_diluted=_num(end.get("shares_diluted")), ocf=_num(end.get("ocf")))
    lo, hi = _THREE_YEARS_DAYS
    starts = [p for p in have if lo <= _days(p["period_end"], end["period_end"]) <= hi]
    if not starts:
        out["note"] = "no annual period three years before the latest"
        return out
    start = starts[-1]
    out.update(start_fy_end=start["period_end"], start_revenue=_num(start.get("revenue")),
               start_net_income=_num(start.get("net_income")),
               start_shares_diluted=_num(start.get("shares_diluted")))
    span = [p for p in have if start["period_end"] <= p["period_end"] <= end["period_end"]]
    for a, b in zip(span, span[1:]):
        r = _num(b["shares_diluted"]) / _num(a["shares_diluted"])
        if r >= SHARE_BASIS_JUMP or r <= 1 / SHARE_BASIS_JUMP:
            out["note"] = f"diluted share count jumps {r:.2f}x in {b['period_end'][:4]}"
            return out
    notes = []
    r0, r1 = out["start_revenue"], out["revenue"]
    s0, s1 = out["start_shares_diluted"], out["shares_diluted"]
    if r0 and r0 > 0 and r1 and r1 > 0:
        out["sales_ps_growth_3y"] = ((r1 / s1) / (r0 / s0)) ** (1 / 3.0) - 1
    else:
        notes.append("revenue not positive")
    n0, n1 = out["start_net_income"], out["net_income"]
    if n0 is None or n1 is None:
        notes.append("net income missing")
    elif n0 <= 0:
        notes.append("start EPS not positive")
    elif n1 <= 0:
        notes.append("latest EPS not positive")
    else:
        out["eps_growth_3y"] = ((n1 / s1) / (n0 / s0)) ** (1 / 3.0) - 1
    out["note"] = "; ".join(notes)
    return out


def load_style_history(path=None):
    """{ticker: row}, the last row written for each ticker."""
    out = {}
    for r in read_rows(path or STYLE_HISTORY_CSV):
        if r.get("ticker"):
            out[r["ticker"]] = r
    return out


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
SHARE_CLASS_CAP_TOL = 0.10
MIN_VALUE_INPUTS = 2
MIN_GROWTH_INPUTS = 1
MIN_QUALITY_INPUTS = 2
MIN_COHORT = 20
QUALITY_WEIGHT = 0.5
# A listing the repository can reliably tell is a foreign issuer's: a depositary share
# by name (the pattern the pipeline's P/E code uses), a filer whose EPS basis is
# "annual" (set only for a 20-F or 40-F filer with no quarterly XBRL), or one whose
# latest annual revenue fact comes from a 20-F or 40-F. Foreign issuers filing IFRS
# statements have no US GAAP history, so they drop out of the style books for want of
# the three-year history instead.
_DEPOSITARY_NAME = re.compile(r"depositar|depositor|\bADRs?\b|\bADS(?![-\w])", re.I)
_FOREIGN_FORMS = ("20-F", "40-F")

VALUE_INPUTS = ("book_yield", "earnings_yield", "sales_yield", "ocf_yield")
GROWTH_INPUTS = ("sales_ps_growth_3y", "eps_growth_3y")
# The panel's Quality group, lower-is-better fields negated so higher is better.
QUALITY_INPUTS = (("roe_ttm", 1), ("earnings_consistency", 1), ("net_debt_ebitda", -1),
                  ("op_margin_stability", -1), ("accruals_ratio", -1))

CLASSIFICATION_RULE = [
    "Only operating companies are sorted. Depositary shares (ADRs) and foreign companies "
    "that file annual reports on forms 20-F or 40-F are left out, and share classes of one "
    "company that report the same market cap (within 10%) count once, as the most traded "
    "class.",
    "Size follows the Russell indexes: companies are ranked by market cap, the largest 200 "
    "are large (the Russell Top 200), ranks 201 to 1,000 are mid (the Russell Midcap), ranks "
    "1,001 to 3,000 are small (the Russell 2000), and smaller companies are left out.",
    "Value measures how little the market pays for what a company has and earns. It is the "
    "average of four yields: book value, earnings, sales and operating cash flow, each "
    "divided by the market value of the shares. A company needs at least two of the four.",
    "Growth measures how fast the business has grown per share over its last three fiscal "
    "years: the yearly growth rate of sales per share and of earnings per share, each built "
    "from the annual reports (form 10-K) as a total divided by the diluted share count of "
    "the same year. A company needs at least one of the two. Earnings growth is left out "
    "when either year was a loss, and both are left out across a stock split or a merger. "
    "Recent share price moves are not part of growth.",
    "Every input is turned into a robust z-score against companies of the same size and "
    "sector, or of the same size where the sector has fewer than 10 companies. So a company "
    "is growth or value compared with its own industry, and a boom across a whole sector "
    "does not make all of it growth.",
    "The style score is the growth score minus the value score. Within each size, companies "
    "above the median are Growth and the rest are Value, so every company sorted is in "
    "exactly one box. A company without three years of annual reports in US GAAP (mostly "
    "foreign companies filing international accounts) cannot be scored and is left out.",
]
CANDIDATE_RULE = (
    "Within its box, each company is ranked by its box score plus half its quality score. The "
    "box score is the growth score in a Growth book and the value score in a Value book. "
    "Quality is the average z of return on equity, earnings consistency, net debt to EBITDA, "
    "operating margin volatility and the accruals ratio (the last three counted lower is "
    "better), within the same size; a company with fewer than 2 of them counts as average. "
    "The book holds the top N, where N is the middle of the mandate's holdings range, skipping "
    "a company once its sector is at the mandate's sector cap. Positions are equal weight. A "
    "company the analyst says to avoid, sell or bet against is left out; one the analyst says "
    "to initiate or add to is held at 1.5 times equal weight, up to the largest position the "
    "mandate allows.")


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


def _inputs(r, hist):
    cap = _num(r.get("market_cap"))
    pb = _num(r.get("price_book"))
    rev = _num(r.get("ttm_revenue"))
    if rev is None:
        rev = _num(hist.get("revenue"))
    ocf = _num(hist.get("ocf"))
    return {
        "book_yield": 1.0 / pb if pb else None,
        "earnings_yield": _earnings_yield(r),
        "sales_yield": rev / cap if rev is not None and cap else None,
        "ocf_yield": ocf / cap if ocf is not None and cap else None,
        "sales_ps_growth_3y": _num(hist.get("sales_ps_growth_3y")),
        "eps_growth_3y": _num(hist.get("eps_growth_3y")),
        **{k: _num(r.get(k)) for k, _ in QUALITY_INPUTS},
    }


def _mean_of(zs, need):
    got = [z for z in zs if z is not None]
    return sum(got) / len(got) if len(got) >= need else None


def _foreign_marker(r, hist):
    if _DEPOSITARY_NAME.search(r.get("name") or ""):
        return "a depositary share (ADR)"
    if (r.get("eps_basis") or "") == "annual" or \
            str(hist.get("annual_form") or "").startswith(_FOREIGN_FORMS):
        return "a foreign issuer filing annual reports on form 20-F or 40-F"
    return None


def _sector_z(inputs, sectors, key):
    """Robust z of one input within sector, inside a size group. A sector with fewer
    than MIN_SECTOR_COHORT companies, or too few values for this input, falls back to
    the whole size group."""
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


def _history_fresh(hist, day):
    fy = hist.get("fy_end") or ""
    if not fy or not day:
        return False
    try:
        return 0 <= _days(fy, day) <= HISTORY_MAX_AGE_DAYS
    except ValueError:
        return False


def classify(rows, history=None, day=None):
    """Size group and style for every row of one panel date.

    `history` is load_style_history(); `day` the panel date (the latest fiscal year
    must be fresh against it). Returns {ticker: info}. info has size (large, mid,
    small, micro or None), style (growth, value or None), box ("large-growth" and so
    on, or None) and, when unclassified, a short `why`. Value and growth inputs are
    robust z within size and sector (size alone for a small sector); quality is
    within size."""
    history = history or {}
    if day is None:
        day = max((r.get("date") or "" for r in rows), default="")
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
        foreign = _foreign_marker(r, history.get(t) or {})
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
        hist = {r["ticker"]: (history.get(r["ticker"]) or {}) for r in rs}
        hist = {t: (h if _history_fresh(h, day) else {}) for t, h in hist.items()}
        inputs = {r["ticker"]: _inputs(r, hist[r["ticker"]]) for r in rs}
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
            info["value"] = _mean_of([z[k][t] for k in VALUE_INPUTS], MIN_VALUE_INPUTS)
            info["growth"] = _mean_of([z[k][t] for k in GROWTH_INPUTS], MIN_GROWTH_INPUTS)
            info["quality"] = _mean_of([z[k][t] for k, _ in QUALITY_INPUTS], MIN_QUALITY_INPUTS)
            info["sales_ps_growth_3y"] = inputs[t]["sales_ps_growth_3y"]
            info["eps_growth_3y"] = inputs[t]["eps_growth_3y"]
            if not hist[t]:
                info["why"] = "no three-year annual history"
                continue
            if info["growth"] is None:
                info["why"] = "no three-year growth figure"
                continue
            if info["value"] is None:
                info["why"] = "fewer than 2 of the 4 value inputs"
                continue
            info["style_score"] = info["growth"] - info["value"]
            scored.append(info)
        scored.sort(key=lambda i: (i["style_score"], i["ticker"]))
        n = len(scored)
        n_value = n - n // 2        # ties at the median go to Value
        for k, info in enumerate(scored):
            info["style"] = "value" if k < n_value else "growth"
            info["box"] = f"{size}-{info['style']}"
    return out


def box_counts(classes):
    counts = {}
    for info in classes.values():
        key = info["box"] or (info["size"] == "micro" and "micro") or "unclassified"
        counts[key] = counts.get(key, 0) + 1
    return counts


def growth_coverage(classes):
    """How many sized, non-foreign operating companies have a growth input."""
    sized = [i for i in classes.values() if i["size"] in ("large", "mid", "small")]
    return {"sized": len(sized),
            "with_growth": sum(1 for i in sized if i.get("growth") is not None),
            "classified": sum(1 for i in sized if i["box"])}


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
        avg = cost / held if held else None
        marks.append({"ticker": tk, "shares": sh, "close": px, "value": v,
                      "side": "long" if sh > 0 else "short", "avg_cost": avg,
                      # A short gains when the price falls below the price it was sold at.
                      "ret": None if not avg else (px / avg - 1) * (1 if sh > 0 else -1),
                      "since": min((l["date"] for l in lots), default=None)})
    priced_nav = st["cash"] + long_v + short_v
    partial = bool(missing or broken)
    ok = not partial and priced_nav > 0
    return {"date": day, "book": book, "cash": st["cash"], "capital": st["capital"],
            "long_value": long_v, "short_value": short_v, "priced_nav": priced_nav,
            # Gross is long plus the size of the short side, net is long minus it,
            # both as a share of NAV. Blank on a partial day.
            "gross": (long_v - short_v) / priced_nav if ok else None,
            "net": (long_v + short_v) / priced_nav if ok else None,
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
         history=None, day=None, max_back=5, dry_run=False, log=print):
    """Incept every live book that has not been incepted.

    Idempotent: a book with a deposit row in trades.csv is never seeded again, so a
    second run appends nothing.

    A style book gets INCEPTION_CAPITAL, its first mandate (if it has none) and the
    rules candidate book, bought in whole shares at the stored close, each trade
    paying COST_BPS. The date is the latest panel date (or the latest on or before
    `day`) on which every holding of every style book being seeded has a stored
    close; up to `max_back` earlier dates are tried. A style book whose box holds
    fewer classified companies than its mandate's minimum is not seeded, and the
    reason is returned: seeding it would buy a book the method cannot fill.

    The hedge and neural books get their mandate and INCEPTION_CAPITAL in cash on
    the latest panel date; the PM builds them."""
    ledger_dir = Path(ledger_dir or LEDGER_DIR)
    books_dir = Path(books_dir or BOOKS_DIR)
    prices = prices or PriceStore()
    history = load_style_history(history) if not isinstance(history, dict) else history
    trades = read_rows(ledger_dir / "trades.csv")
    done = {t["book"] for t in trades if t.get("side") == "deposit"}
    todo = [b for b in LIVE_BOOKS if b["id"] not in done]
    result = {"seeded": [], "skipped": {}, "date": None, "books": {}, "trades": [],
              "decisions": [], "mandates": [], "classes": {}}
    if not todo:
        log("seed: every book already has an inception; nothing to do.")
        return result
    views = analyst_views(events)
    mandates = {b["id"]: (load_mandate(b["id"], books_dir) or default_mandate(b["id"]))
                for b in todo}
    dates = sorted((d for d in panel_dates(panel_dir) if day is None or d <= day), reverse=True)
    if not dates:
        raise RuntimeError("seed: the panel has no dates")
    style = [b for b in todo if b["kind"] == "style"]
    chosen = None
    for d in dates[:max_back + 1]:
        _, rows = panel_rows(d, panel_dir)
        classes = classify(rows, history, d)
        books = {b["id"]: rules_candidate(b, classes, mandates[b["id"]], views) for b in style}
        ready = {bid: ps for bid, ps in books.items()
                 if len(ps) >= mandates[bid]["holdings_range"][0]}
        missing = sorted({p["ticker"] for ps in ready.values() for p in ps
                          if prices.close(p["ticker"], d) is None})
        if missing:
            log(f"seed: {d} lacks a stored close for {len(missing)} holding(s) "
                f"({', '.join(missing[:8])}); trying the panel date before.")
            continue
        chosen = (d, classes, books, ready)
        break
    if chosen is None:
        raise RuntimeError("seed: no recent panel date has a stored close for every holding")
    d, classes, books, ready = chosen
    result.update(date=d, books=books, classes=classes)
    for bid, ps in books.items():
        if bid not in ready:
            result["skipped"][bid] = (f"only {len(ps)} classified companies in the box, fewer "
                                      f"than the mandate's minimum of "
                                      f"{mandates[bid]['holdings_range'][0]}")
            log(f"seed: {bid} not seeded: {result['skipped'][bid]}.")
    pm_day = d
    t_rows, d_rows, m_rows = [], [], []
    held_decisions = read_rows(ledger_dir / "decisions.csv")
    for b in todo:
        bid, mandate = b["id"], mandates[b["id"]]
        if b["kind"] == "style" and bid not in ready:
            continue
        on = d if b["kind"] == "style" else pm_day
        n_prior = len([r for r in held_decisions if r.get("book") == bid])
        dec_m = f"{bid}-{on}-{n_prior + 1}"
        dec_i = f"{bid}-{on}-{n_prior + 2}"
        if not mandate_history(bid, ledger_dir):
            m_rows.append({"date": on, "book": bid, "decision_id": dec_m,
                           "mandate": json.dumps(mandate, sort_keys=True, separators=(",", ":"))})
            d_rows.append({"date": on, "book": bid, "decision_id": dec_m, "action": "mandate_change",
                           "reason": f"First mandate: the default for a {b['kind']} book, until "
                                     f"the PM reviews it.", "author": "rules"})
        seq = 1
        t_rows.append({"trade_id": f"{bid}-{on}-{seq:03d}", "date": on, "book": bid,
                       "ticker": "CASH", "side": "deposit", "shares": INCEPTION_CAPITAL,
                       "price": 1, "cost": 0, "lot_id": "", "reason_code": "inception_capital",
                       "decision_id": dec_i})
        if b["kind"] != "style":
            d_rows.append({"date": on, "book": bid, "decision_id": dec_i, "action": "inception",
                           "reason": f"Start with ${INCEPTION_CAPITAL:,.0f} of paper cash. The PM "
                                     f"builds the book at its next weekly run.", "author": "rules"})
            result["seeded"].append(bid)
            continue
        picks = ready[bid]
        d_rows.append({"date": on, "book": bid, "decision_id": dec_i, "action": "inception",
                       "reason": f"Start with ${INCEPTION_CAPITAL:,.0f} of paper cash and buy the "
                                 f"rules candidate book ({len(picks)} companies, equal weight) "
                                 f"at the close on {_long_date(on)}.", "author": "rules"})
        for p in picks:
            px = prices.close(p["ticker"], on)
            sh = _shares_for(p["weight"] * INCEPTION_CAPITAL, px)
            if sh <= 0:
                log(f"seed: {bid} {p['ticker']} at ${px:,.2f} buys no whole share at "
                    f"{p['weight']:.2%}; left in cash.")
                continue
            seq += 1
            tid = f"{bid}-{on}-{seq:03d}"
            t_rows.append({"trade_id": tid, "date": on, "book": bid, "ticker": p["ticker"],
                           "side": "buy", "shares": sh, "price": px,
                           "cost": round(sh * px * COST_RATE, 2), "lot_id": tid,
                           "reason_code": "rules_inception", "decision_id": dec_i})
        result["seeded"].append(bid)
    result.update(trades=t_rows, decisions=d_rows, mandates=m_rows)
    if dry_run:
        return result
    for bid in result["seeded"]:
        p = mandate_path(bid, books_dir)
        if not p.exists():
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(mandates[bid], indent=2, sort_keys=True) + "\n",
                         encoding="utf-8")
    append_rows(ledger_dir / "mandates.csv", MANDATE_COLUMNS, m_rows)
    append_rows(ledger_dir / "decisions.csv", DECISION_COLUMNS, d_rows)
    # trades.csv last: its deposit row is what marks a book as incepted.
    append_rows(ledger_dir / "trades.csv", TRADE_COLUMNS, t_rows)
    log(f"seed: incepted {len(result['seeded'])} book(s) on {d}: {len(t_rows)} trades.")
    return result


# ---------------------------------------------------------------------------
# PM orders (portfolio/bin/trade.py)

ORDER_SIDES = ("buy", "sell", "short", "cover")


class OrderError(ValueError):
    """An order batch that cannot be written, with every reason."""

    def __init__(self, errors):
        super().__init__("; ".join(errors))
        self.errors = errors


def _exposure(trades, book, day, prices, names):
    """Weights, sector weights, gross and net at the close of `day`."""
    v = value_book(trades, book, day, prices)
    nav = v["priced_nav"]
    w = {m["ticker"]: m["value"] / nav for m in v["marks"]} if nav > 0 else {}
    sectors = {}
    for t, x in w.items():
        if x > 0:
            sec = names.get(t, "") or "Unclassified"
            sectors[sec] = sectors.get(sec, 0.0) + x
    return v, w, sectors


def plan_orders(batch, trades, prices, panel_day_rows, classes, mandate, decisions=()):
    """Validate and price one PM order batch. Returns a plan with the trade rows and
    the decision row to append; raises OrderError listing every reason it cannot be
    written. Pure: it writes nothing.

    batch = {"book", "date", "batch_id", "reason", "action" (default "trade"),
             "orders": [{"id", "ticker", "side", one of "shares" / "weight" / "value",
                         optional "lot_id"}]}

    Every fill is at the stored close for `date`; an order without one is refused.
    A limit (position size, sector, gross, net, cash) is refused when the batch
    leaves the book past it and further past it than before; a batch that reduces a
    breach it inherited is allowed. Idempotent: an order whose trade id is already
    in the ledger is skipped, and the decision is written once."""
    errs, warns = [], []
    bid, day = batch.get("book"), batch.get("date") or ""
    book = BOOKS.get(bid)
    batch_id = str(batch.get("batch_id") or "").strip()
    reason = str(batch.get("reason") or "").strip()
    action = batch.get("action") or "trade"
    if not book or book.get("status") != "live":
        raise OrderError([f"unknown or not live book {bid!r}"])
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
        errs.append(f"date {day!r} is not YYYY-MM-DD")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", batch_id):
        errs.append("batch_id is required (letters, digits, dot, dash, underscore)")
    if not reason:
        errs.append("a reason is required; it goes into decisions.csv")
    if action not in ("trade", "rebalance", "hold"):
        errs.append(f"action {action!r} must be trade, rebalance or hold")
    start = inception(trades, bid)
    if not start:
        errs.append(f"{bid} has not been incepted")
    elif day and day < start:
        errs.append(f"{day} is before {bid}'s inception on {start}")
    if errs:
        raise OrderError(errs)
    orders = batch.get("orders") or []
    if action == "hold" and orders:
        raise OrderError(["a hold decision carries no orders"])
    ids = [str(o.get("id") or "") for o in orders]
    if any(not re.fullmatch(r"[A-Za-z0-9_.-]+", i) for i in ids) or len(set(ids)) != len(ids):
        raise OrderError(["every order needs a unique id (letters, digits, dot, dash, underscore)"])
    have_ids = {t["trade_id"] for t in trades}
    decision_id = f"{bid}-{day}-{batch_id}"
    long_only = bool(mandate.get("long_only"))
    universe = mandate.get("universe", "operating")
    rows_by = {r["ticker"]: r for r in panel_day_rows}
    names = {t: (r.get("sector") or "") for t, r in rows_by.items()}
    pre_v, pre_w, pre_sec = _exposure(trades, bid, day, prices, names)
    fills, skipped = [], []
    for o in orders:
        oid, tk, side = str(o["id"]), str(o.get("ticker") or "").upper(), o.get("side")
        tid = f"{bid}-{day}-o{oid}"
        if tid in have_ids:
            skipped.append(oid)
            continue
        where = f"order {oid} ({side} {tk})"
        if side not in ORDER_SIDES:
            errs.append(f"{where}: side must be one of {', '.join(ORDER_SIDES)}")
            continue
        if long_only and side in ("short", "cover"):
            errs.append(f"{where}: this book is long only")
            continue
        if not tk or tk == "CASH":
            errs.append(f"{where}: a ticker is required")
            continue
        if side in ("buy", "short"):
            if universe == "style_box":
                box = f"{book['size']}-{book['style']}"
                if (classes.get(tk) or {}).get("box") != box:
                    errs.append(f"{where}: not in the {box} box on this panel date")
                    continue
            elif universe == "operating":
                if (rows_by.get(tk) or {}).get("security_type") != "operating":
                    errs.append(f"{where}: not an operating company in the panel")
                    continue
        px = prices.close(tk, day)
        if px is None:
            errs.append(f"{where}: no stored close for {day}; nothing is priced from another day")
            continue
        sizes = [k for k in ("shares", "weight", "value") if o.get(k) not in (None, "")]
        if len(sizes) != 1:
            errs.append(f"{where}: give exactly one of shares, weight or value")
            continue
        if sizes[0] == "shares":
            sh = float(o["shares"])
            if sh != int(sh):
                errs.append(f"{where}: shares must be a whole number")
                continue
            sh = int(sh)
        else:
            if pre_v["partial"]:
                errs.append(f"{where}: the book's value on {day} is partial, so a weight or value "
                            f"cannot be turned into shares; give shares")
                continue
            amount = float(o["value"]) if sizes[0] == "value" else float(o["weight"]) * pre_v["nav"]
            sh = int(math.floor(abs(amount) / (px * (1 + COST_RATE))))
        if sh <= 0:
            errs.append(f"{where}: comes to no whole share at ${px:,.2f}")
            continue
        fills.append({"trade_id": tid, "date": day, "book": bid, "ticker": tk, "side": side,
                      "shares": sh, "price": px, "cost": round(sh * px * COST_RATE, 2),
                      "lot_id": tid if side in ("buy", "short") else (o.get("lot_id") or ""),
                      "reason_code": "pm_order", "decision_id": decision_id})
    if errs:
        raise OrderError(errs)
    post_trades = list(trades) + fills
    try:
        derive_lots(trades_for(post_trades, bid, day))
    except ValueError as exc:
        raise OrderError([str(exc)])
    # A sell or cover must not flip a position through zero.
    st = book_state(trades_for(post_trades, bid, day))
    pre_sh = book_state(trades_for(trades, bid, day))["shares"]
    for f in fills:
        before, after = pre_sh.get(f["ticker"], 0.0), st["shares"].get(f["ticker"], 0.0)
        if before * after < 0:
            errs.append(f"{f['ticker']}: the batch flips the position from long to short or back; "
                        f"close it with one order and open the other side with another")
    post_v, post_w, post_sec = _exposure(post_trades, bid, day, prices, names)

    def check(label, pre, post, limit, above=True):
        bad = (post > limit + 1e-9) if above else (post < limit - 1e-9)
        worse = (post > pre + 1e-9) if above else (post < pre - 1e-9)
        if bad and worse:
            errs.append(f"{label}: {post:.2%} against a limit of {limit:.2%}")

    if book["kind"] == "style":
        for t, x in post_w.items():
            check(f"{t} position", pre_w.get(t, 0.0), x, mandate["max_position"])
        for s, x in post_sec.items():
            check(f"{s} sector", pre_sec.get(s, 0.0), x, mandate["sector_cap"])
        if post_v["cash"] < -1e-6:
            errs.append(f"cash would be ${post_v['cash']:,.2f}: a style book may not borrow")
        lo, hi = mandate["cash_band"]
        cw = post_v["cash"] / post_v["priced_nav"] if post_v["priced_nav"] > 0 else 0.0
        if not (lo - 1e-9 <= cw <= hi + 1e-9):
            warns.append(f"cash is {cw:.2%}, outside the mandate's {lo:.0%} to {hi:.0%} band")
        n = len(st["shares"])
        rlo, rhi = mandate["holdings_range"]
        if not (rlo <= n <= rhi):
            warns.append(f"{n} holdings, outside the mandate's range of {rlo} to {rhi}")
    else:
        pre_g = pre_v["gross"] or 0.0
        if post_v["gross"] is None:
            errs.append("the book cannot be valued after the batch (a holding has no stored close)")
        else:
            check("gross exposure", pre_g, post_v["gross"], mandate["gross_max"])
        if book["kind"] == "hedge" and post_v["net"] is not None:
            nlo, nhi = mandate["net_range"]
            pre_n = pre_v["net"] or 0.0
            check("net exposure", pre_n, post_v["net"], nhi)
            check("net exposure", pre_n, post_v["net"], nlo, above=False)
            for t, x in post_w.items():
                if x > 0:
                    check(f"{t} long position", max(pre_w.get(t, 0.0), 0.0), x,
                          mandate["max_long_position"])
                else:
                    check(f"{t} short position", abs(min(pre_w.get(t, 0.0), 0.0)), -x,
                          mandate["max_short_position"])
    if errs:
        raise OrderError(errs)
    have_decisions = {d_["decision_id"] for d_ in decisions}
    decision = None if decision_id in have_decisions else {
        "date": day, "book": bid, "decision_id": decision_id, "action": action,
        "reason": reason, "author": "pm"}
    return {"fills": fills, "decision": decision, "skipped": skipped, "warnings": warns,
            "pre": {k: pre_v[k] for k in ("priced_nav", "cash", "gross", "net")},
            "post": {k: post_v[k] for k in ("priced_nav", "cash", "gross", "net")}}


def write_orders(plan, ledger_dir=None):
    ledger_dir = Path(ledger_dir or LEDGER_DIR)
    if plan["decision"]:
        append_rows(ledger_dir / "decisions.csv", DECISION_COLUMNS, [plan["decision"]])
    return append_rows(ledger_dir / "trades.csv", TRADE_COLUMNS, plan["fills"])


# ---------------------------------------------------------------------------
# What the site shows


def _r(v, n=6):
    return None if v is None else round(v, n)


def cash_return(start, end, dates, benchmarks):
    """What cash would have earned from the close of `start` to the close of `end`:
    each session after `start` accrues the 13-week Treasury bill rate stored for
    that session, divided by 252. None when any session's rate is not stored."""
    growth = 1.0
    for d in dates:
        if start < d <= end:
            rate = benchmarks.rate(d)
            if rate is None:
                return None
            growth *= 1 + rate / 100.0 / 252
    return growth - 1


def site_data(ledger_dir=None, books_dir=None, panel_dir=None, prices=None,
              benchmarks=None, nav_csv=None, events=None, history=None, repo_url=""):
    """Everything the Portfolios and book pages draw, as plain JSON.

    The rules candidate books are recomputed here from the latest panel date, so
    they are published fresh every run for the PM to compare with what is held."""
    ledger_dir = Path(ledger_dir or LEDGER_DIR)
    prices = prices or PriceStore()
    benchmarks = benchmarks or BenchmarkStore()
    history = load_style_history(history) if not isinstance(history, dict) else history
    trades = read_rows(ledger_dir / "trades.csv")
    decisions = read_rows(ledger_dir / "decisions.csv")
    navs = latest_nav_rows(read_rows(nav_csv or NAV_CSV))
    asof, rows = panel_rows(None, panel_dir)
    dates = panel_dates(panel_dir)
    classes = classify(rows, history, asof) if rows else {}
    views = analyst_views(events)
    counts = box_counts(classes)
    names = {r["ticker"]: (r.get("name") or "", r.get("sector") or "") for r in rows}
    books = []
    for b in LIVE_BOOKS:
        bid = b["id"]
        mandate = load_mandate(bid, books_dir) or default_mandate(bid)
        start = inception(trades, bid)
        item = dict(b, benchmarkName=BENCHMARK_NAMES.get(b["benchmark"], b["benchmark"]),
                    mandate=mandate, inception=start)
        cand = []
        if b["kind"] == "style":
            cand = rules_candidate(b, classes, mandate, views) if classes else []
            item.update(boxCount=counts.get(f"{b['size']}-{b['style']}", 0),
                        candidate=[{k: (_r(v) if isinstance(v, float) else v) for k, v in p.items()}
                                   for p in cand],
                        candidateAsof=asof, targetCount=target_count(mandate))
        if start:
            ts = trades_for(trades, bid)
            v = value_book(trades, bid, asof, prices) if asof >= start else None
            b0, b1 = benchmarks.close(b["benchmark"], start), benchmarks.close(b["benchmark"], asof)
            state = book_state(ts)
            held = set(state["shares"])
            total = v["priced_nav"] if v else None
            marks = []
            for m in (v or {}).get("marks", []):
                nm, sec = names.get(m["ticker"], ("", ""))
                marks.append({"ticker": m["ticker"], "name": nm, "sector": sec,
                              "side": m["side"], "shares": m["shares"], "close": m["close"],
                              "value": round(m["value"], 2), "avgCost": _r(m["avg_cost"], 4),
                              "since": m["since"],
                              "weight": _r(m["value"] / total) if total else None,
                              "ret": _r(m["ret"])})
            marks.sort(key=lambda m: -abs(m["value"]))
            unpriced = [{"ticker": t, "name": names.get(t, ("", ""))[0],
                         "shares": state["shares"].get(t), "why": why}
                        for why, lst in (("no stored close", (v or {}).get("missing", [])),
                                         ("stored prices rebased", (v or {}).get("basis_break", [])))
                        for t in lst]
            sectors = {}
            for m in marks:
                if m["value"] > 0:
                    key = m["sector"] or "Unclassified"
                    sectors[key] = sectors.get(key, 0) + m["value"]
            series = sorted((r for (bk, _), r in navs.items() if bk == bid), key=lambda r: r["date"])
            item.update({
                "asof": asof,
                "nav": _r(v["nav"], 2) if v else None,
                "pricedNav": _r(v["priced_nav"], 2) if v else None,
                "partial": bool(v and v["partial"]),
                "cash": _r(v["cash"], 2) if v else None,
                "capital": _r(v["capital"], 2) if v else None,
                "gross": _r(v["gross"]) if v else None, "net": _r(v["net"]) if v else None,
                "longValue": _r(v["long_value"], 2) if v else None,
                "shortValue": _r(v["short_value"], 2) if v else None,
                "ret": _r(v["nav"] / v["capital"] - 1) if v and v["nav"] is not None and v["capital"] else None,
                "benchStart": b0, "benchNow": b1,
                "benchRet": _r(b1 / b0 - 1) if b0 and b1 else None,
                "cashRet": _r(cash_return(start, asof, dates, benchmarks)) if b.get("cash_benchmark") else None,
                "holdings": marks, "unpriced": unpriced,
                "holdingsCount": len(state["shares"]),
                "sectors": sorted(([s, _r(x / total)] for s, x in sectors.items()),
                                  key=lambda kv: -kv[1]) if total else [],
                "trades": [{k: t.get(k) for k in TRADE_COLUMNS} for t in ts],
                "decisions": [d for d in decisions if d.get("book") == bid],
                "mandateHistory": mandate_history(bid, ledger_dir),
                "series": [{"date": r["date"], "nav": _num(r.get("nav")),
                            "partial": bool(r.get("partial")), "missing": r.get("missing") or "",
                            "capital": _num(r.get("capital")),
                            "bench": _num(r.get("benchmark_close"))} for r in series],
            })
            if b["kind"] == "style":
                item["candidateAdds"] = [p["ticker"] for p in cand if p["ticker"] not in held]
                item["candidateDrops"] = sorted(t for t in held if t not in {p["ticker"] for p in cand})
            mine = [d for d in decisions if d.get("book") == bid]
            item["lastDecision"] = mine[-1] if mine else None
        books.append(item)
    return {
        "asof": asof, "costBps": COST_BPS, "capital": INCEPTION_CAPITAL,
        "classificationRule": CLASSIFICATION_RULE, "candidateRule": CANDIDATE_RULE,
        "boxCounts": counts, "coverage": growth_coverage(classes) if classes else {},
        "books": books, "planned": PLANNED_BOOKS,
        "benchmarksUpdated": benchmarks.updated if hasattr(benchmarks, "updated") else "",
    }
