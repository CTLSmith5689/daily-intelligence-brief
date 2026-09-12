"""Shared plumbing for the thesis agent. Stdlib only, matching the pipeline."""
import csv, io, json, os, sys, urllib.request, gzip, zlib
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
THESES = REPO / "theses"
LEDGER = THESES / "ledger"
RAW = "https://raw.githubusercontent.com/CTLSmith5689/daily-intelligence-brief/main"
PAGES = "https://ctlsmith5689.github.io/daily-intelligence-brief"
UA = "daily-intelligence-brief thesis agent (ctlsmith@me.com)"

# GVMQ exactly as the pipeline defines it (SCORE_GROUPS_PY in lambda_function.py),
# so a score here means what the screener means. Fields where a LOWER raw value
# is better are listed in `invert`; everywhere downstream higher means better.
# Note op_margin_stability is a standard DEVIATION despite the name: lower is
# steadier, so it inverts.
SLEEVES = {
    "Growth":   {"fields": ["revenue_growth_yoy", "eps_growth_yoy", "revenue_acceleration",
                            "gross_margin_trend", "fcf_growth_yoy"], "invert": []},
    "Value":    {"fields": ["pe", "ev_ebitda", "ev_revenue", "price_book", "fcf_yield"],
                 "invert": ["pe", "ev_ebitda", "ev_revenue", "price_book"]},
    "Momentum": {"fields": ["return_12_2", "return_1m", "high52w_proximity",
                            "rel_strength_sp500", "volume_trend"], "invert": []},
    "Quality":  {"fields": ["roe_ttm", "earnings_consistency", "net_debt_ebitda",
                            "op_margin_stability", "accruals_ratio"],
                 "invert": ["net_debt_ebitda", "op_margin_stability", "accruals_ratio"]},
}

# Never read these. The first seven are dead columns, stripped from the panel by
# FUNDAMENTAL_SKIP_FIELDS and permanently empty. The last four are the news block,
# which is contaminated for every date before 2026-09-12 by a company-news fetch
# that applied no relevance filter, and whose source headlines were never archived
# so it cannot be audited or repaired.
DEAD = {"dims_present", "g", "m", "pct", "q", "scorable", "v"}
CONTAMINATED = {"news_count_7d", "news_lm_avg", "news_vader_avg", "neglect_score"}
NEWS_FIX_DATE = "2026-09-12"


def fetch(url, tries=3):
    req = urllib.request.Request(url, headers={"User-Agent": UA,
                                               "Accept-Encoding": "gzip, deflate"})
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=40) as r:
                data = r.read()
                enc = (r.headers.get("Content-Encoding") or "").lower()
            if enc == "gzip":
                data = gzip.decompress(data)
            elif enc == "deflate":
                data = zlib.decompress(data, -zlib.MAX_WBITS)
            return data
        except Exception:
            if attempt == tries - 1:
                return None
    return None


def num(v):
    if v in ("", None):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f != f or f in (float("inf"), float("-inf")) else f


def load_panel():
    """Latest panel slice. Handles month rollover: on the 1st the new file has
    one date in it and the newest data may still be in the prior month, so both
    are read and whichever carries the later max(date) wins."""
    today = datetime.now(tz=timezone.utc).date()
    months = [today.strftime("%Y-%m"),
              (today.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")]
    best, best_date = [], ""
    for m in months:
        raw = fetch(f"{RAW}/data/fundamentals/{m}.csv")
        if not raw:
            continue
        rows = list(csv.DictReader(io.StringIO(raw.decode("utf-8", "replace"))))
        rows = [r for r in rows if r.get("date")]
        if not rows:
            continue
        latest = max(r["date"] for r in rows)
        if latest > best_date:
            best_date, best = latest, [r for r in rows if r["date"] == latest]
    return best_date, best


def read_csv_rows(path):
    if not path.exists():
        return []
    with path.open(encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def csv_header(path):
    if not path.exists():
        return None
    with path.open(encoding="utf-8", newline="") as fh:
        return next(csv.reader(fh), None)


def append_csv(path, columns, rows):
    """Append to an append-only ledger, refusing to misalign it.

    A CSV row is positional. Writing today's column order into a file whose
    header was written with a different one shifts every field after the first
    difference, and the result still parses, which is the dangerous part. It
    happened here: score.py grew peers_used, max_favourable, max_adverse and
    note, the ledger's header predated them, and a run wrote peers_used into
    the peer_median_return column and target_hit into rel_peer. Nothing failed.

    So the file's own header wins, and a mismatch raises rather than degrading.
    An append-only archive is worth nothing if a schema change can silently
    corrupt the rows already in it."""
    if not rows:
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = csv_header(path)
    if existing and existing != list(columns):
        missing = [c for c in columns if c not in existing]
        extra = [c for c in existing if c not in columns]
        raise SystemExit(
            f"append_csv: refusing to write {path.name}.\n"
            f"  file header : {existing}\n"
            f"  writer wants: {list(columns)}\n"
            + (f"  new fields not in the file: {missing}\n" if missing else "")
            + (f"  file has fields the writer dropped: {extra}\n" if extra else "")
            + "  Migrate the file deliberately. Appending now would shift every\n"
              "  field after the first difference and still parse cleanly.")
    with path.open("a", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=existing or list(columns), extrasaction="ignore")
        if not existing:
            w.writeheader()
        for r in rows:
            w.writerow(r)
    return len(rows)
