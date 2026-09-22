#!/usr/bin/env python3
"""Flag panel rows already written with an older session's close.

One-off repair for the stale-close incident. On 2026-09-10, 09-11, 09-14 and
09-21 the panel recorded about 4,500 rows each whose price was the close of an
earlier session: the price pass either skipped the fetch (a 24h cache with no
session check, fixed in 72938c2d) or downloaded between about 20:00 and 20:40 ET,
when Yahoo had not yet published the day's bar, and the pipeline accepted the
series as fresh. The pipeline now withholds such prices and sets price_stale=1
going forward (_panel_rows_for_session in lambda_function.py). This marks the
rows written before that.

The panel is append-only, so this changes no existing value. It adds the
price_date and price_stale columns if the file lacks them (price_date stays
blank on old rows: it was not recorded then), and fills price_stale=1 on the
rows it can prove are lagged. Everything else round-trips as the same strings.

The rule is an exact match against the stored price histories (docs/prices/ on
gh-pages, which carry each session's close rounded to 4 places, exactly as the
panel copied them). A row is flagged when its price equals the close of one of
the three sessions before its date and does NOT equal the close of its own date.
Rows are left alone when:
  - the ticker has no history, or the history has no bar on the row's date,
  - the price matches its own session's close, even if it also matches an older
    one (a flat price: that tie is ambiguous, so it is not flagged),
  - the price matches no close exactly (for instance a dividend since then has
    auto-adjusted the history, so the comparison is no longer exact).
Only the four dates above are examined. On other dates the same rule finds a
handful of rows, which is indistinguishable from coincidence.

Usage:
    python tools/backfill_price_stale.py PRICES_DIR [PANEL_CSV] [--dry-run]

PRICES_DIR is a directory of <TICKER>.json price files from gh-pages. PANEL_CSV
defaults to data/fundamentals/2026-09.csv. Safe to run again: rows already
flagged are counted and left as they are.
"""
import collections
import csv
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import lambda_function as lf  # noqa: E402  (for the atomic writer the pipeline uses)

DATES = ("2026-09-10", "2026-09-11", "2026-09-14", "2026-09-21")
NEW_COLUMNS = ("price_date", "price_stale")
MAX_LAG = 3
# Closes are stored rounded to 4 places and the panel wrote the same float back
# out, so "exact" allows only float representation noise.
EXACT = 5e-5


def load_histories(prices_dir):
    hist = {}
    for f in Path(prices_dir).glob("*.json"):
        try:
            blob = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        ticker = blob.get("ticker")
        if not ticker:
            continue
        closes = {}
        for pair in blob.get("closes") or []:
            try:
                closes[str(pair[0])] = float(pair[1])
            except (TypeError, ValueError, IndexError):
                continue
        hist[ticker] = closes
    return hist


def classify(row, hist):
    """'lagged', 'current', 'tie' or 'unknown' for one panel row."""
    try:
        price = float(row.get("price") or "")
    except ValueError:
        return "unknown"
    closes = hist.get(row.get("ticker"))
    date = row.get("date")
    if not closes or date not in closes:
        return "unknown"
    sessions = sorted(d for d in closes if d <= date)
    matches = [k for k in range(MAX_LAG + 1)
               if len(sessions) > k and abs(price - closes[sessions[-1 - k]]) < EXACT]
    if not matches:
        return "unknown"
    if 0 in matches:
        return "tie" if len(matches) > 1 else "current"
    return "lagged"


def main(argv):
    args = [a for a in argv if not a.startswith("--")]
    dry = "--dry-run" in argv
    if not args:
        print(__doc__)
        return 2
    prices_dir = Path(args[0])
    panel = Path(args[1]) if len(args) > 1 else REPO / "data" / "fundamentals" / "2026-09.csv"

    hist = load_histories(prices_dir)
    print(f"backfill: {len(hist)} price histories from {prices_dir}.")

    with panel.open(encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        header = list(reader.fieldnames or [])
        rows = list(reader)
    columns = header + [c for c in NEW_COLUMNS if c not in header]

    counts = collections.defaultdict(collections.Counter)
    out = []
    for r in rows:
        new = {c: r.get(c, "") for c in columns}
        if r.get("date") in DATES and r.get("price"):
            verdict = classify(r, hist)
            counts[r["date"]][verdict] += 1
            if verdict == "lagged":
                if new["price_stale"] == "1":
                    counts[r["date"]]["already_flagged"] += 1
                elif new["price_stale"] == "":
                    new["price_stale"] = "1"
                    counts[r["date"]]["flagged_now"] += 1
        out.append(new)

    # Nothing but the new column may differ from what was read.
    for before, after in zip(rows, out):
        for c in header:
            if c == "price_stale":
                continue
            assert before.get(c, "") == after[c], (before.get("date"), before.get("ticker"), c)
        if "price_stale" in header and before.get("price_stale", "") not in ("", after["price_stale"]):
            raise AssertionError("an existing price_stale value would change")

    total = 0
    for d in DATES:
        c = counts[d]
        n = c["lagged"]
        total += n
        print(f"{d}: {n} lagged (flagged {c['flagged_now']} now, {c['already_flagged']} already), "
              f"{c['current']} current, {c['tie']} ambiguous ties not flagged, "
              f"{c['unknown']} not decidable.")
    print(f"backfill: {total} rows carry an older session's close.")

    if dry:
        print("backfill: dry run, nothing written.")
        return 0
    lf._atomic_write_csv(panel, columns, out)
    print(f"backfill: wrote {panel} ({len(out)} rows, columns {len(header)} -> {len(columns)}).")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
