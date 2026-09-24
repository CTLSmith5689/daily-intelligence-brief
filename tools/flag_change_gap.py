#!/usr/bin/env python3
"""Flag 2026-09-23 panel rows whose change_pct spans two sessions.

One-off repair for the missing 2026-09-22 bar. The 09-23 price pass downloaded
a year of closes per ticker and replaced each stored file with it. Yahoo's
response left out the 09-22 bar for most tickers, so the stored series went
from 09-21 straight to 09-23, and change_pct, computed from the last two closes,
was a two-day move recorded as one day's (CF: -2.13% against a real +0.04%).
The pipeline now keeps stored bars the download leaves out
(_merge_price_series) and leaves change_pct blank with change_gap=1 when the
last two closes are not consecutive sessions (derive_from_price_history). This
marks the rows written before that.

The panel is append-only, so this changes no existing value. It adds the
change_gap column if the file lacks it and fills change_gap=1 on the rows it can
prove were computed across the gap. Everything else round-trips as the same
strings, and change_pct keeps its recorded value.

A 2026-09-23 row is flagged when all of these hold:
  - it has a price and a change_pct, and its price_date is 2026-09-23, so the
    change came from the stored series rather than a live quote;
  - the ticker has a 2026-09-22 panel row with a price that is not flagged
    price_stale, so 09-22 was a session for it;
  - change_pct equals price / C_prev - 1 (in percent), where C_prev is the
    stored close on the last date before 2026-09-22 in the price history
    (docs/prices/ on gh-pages, which the 09-23 run wrote). That reproduces the
    computation across the gap exactly;
  - change_pct does NOT equal price / C_22 - 1, where C_22 is the stored
    09-22 close if the history has one, else the 09-22 panel price. When both
    formulas give the same number (a flat day, C_22 == C_prev), the recorded
    figure is right either way, and the row is counted as a tie, not flagged.
Rows that match neither formula are left alone and counted as undecidable.
Only 2026-09-23 is examined.

Usage:
    python tools/flag_change_gap.py PRICES_DIR [PANEL_CSV] [--write]

Dry run by default: prints the counts and writes nothing. --write applies the
flags. PRICES_DIR is a directory of <TICKER>.json price files from gh-pages as
the 09-23 run left them. PANEL_CSV defaults to data/fundamentals/2026-09.csv.
Safe to run again: rows already flagged are counted and left as they are.
"""
import collections
import csv
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import lambda_function as lf  # noqa: E402  (for the atomic writer the pipeline uses)

DATE = "2026-09-23"
MISSING = "2026-09-22"
COLUMN = "change_gap"
# change_pct is written to 6 decimal places and the closes to 4, so an exact
# reproduction agrees to well inside this many percentage points.
EXACT = 5e-4


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


def _num(s):
    try:
        v = float(s)
    except (TypeError, ValueError):
        return None
    return v if v == v else None


def classify(row, prev_row, closes):
    """'gap', 'current', 'tie', 'undecidable' or 'not_examined' for one 09-23 row."""
    price, chg = _num(row.get("price")), _num(row.get("change_pct"))
    if price is None or chg is None or row.get("price_date") != DATE:
        return "not_examined"
    if not prev_row or row.get("ticker") != prev_row.get("ticker"):
        return "undecidable"
    p22 = _num(prev_row.get("price"))
    if p22 is None or prev_row.get("price_stale") not in ("", None, "0"):
        return "undecidable"
    if not closes:
        return "undecidable"
    before = [d for d in closes if d < MISSING]
    if not before:
        return "undecidable"
    c_prev = closes[max(before)]
    c_22 = closes.get(MISSING, p22)
    if not (c_prev > 0 and c_22 > 0):
        return "undecidable"
    across = (price / c_prev - 1) * 100
    one_day = (price / c_22 - 1) * 100
    m_across, m_one = abs(chg - across) < EXACT, abs(chg - one_day) < EXACT
    if m_across and m_one:
        return "tie"
    if m_across:
        return "gap"
    if m_one:
        return "current"
    return "undecidable"


def flag_rows(rows, header, hist):
    """(columns, new rows, counts). Pure apart from reading `hist`."""
    columns = header + ([COLUMN] if COLUMN not in header else [])
    prev_by_ticker = {r.get("ticker"): r for r in rows if r.get("date") == MISSING}
    counts = collections.Counter()
    out = []
    for r in rows:
        new = {c: r.get(c, "") for c in columns}
        if r.get("date") == DATE:
            verdict = classify(r, prev_by_ticker.get(r.get("ticker")), hist.get(r.get("ticker")))
            counts[verdict] += 1
            if verdict == "gap":
                if new[COLUMN] == "1":
                    counts["already_flagged"] += 1
                elif new[COLUMN] == "":
                    new[COLUMN] = "1"
                    counts["flagged_now"] += 1
        out.append(new)

    # Nothing but the new column may differ from what was read.
    for before, after in zip(rows, out):
        for c in header:
            if c == COLUMN:
                continue
            assert before.get(c, "") == after[c], (before.get("date"), before.get("ticker"), c)
        if COLUMN in header and before.get(COLUMN, "") not in ("", after[COLUMN]):
            raise AssertionError("an existing change_gap value would change")
    return columns, out, counts


def main(argv):
    args = [a for a in argv if not a.startswith("--")]
    write = "--write" in argv
    if not args:
        print(__doc__)
        return 2
    prices_dir = Path(args[0])
    panel = Path(args[1]) if len(args) > 1 else REPO / "data" / "fundamentals" / "2026-09.csv"

    hist = load_histories(prices_dir)
    lacking = sum(1 for c in hist.values() if c and max(c) >= DATE and MISSING not in c)
    ending = sum(1 for c in hist.values() if c and max(c) >= DATE)
    print(f"flag: {len(hist)} price histories from {prices_dir}; {lacking} of the {ending} "
          f"that reach {DATE} have no {MISSING} bar.")

    with panel.open(encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        header = list(reader.fieldnames or [])
        rows = list(reader)
    columns, out, c = flag_rows(rows, header, hist)

    examined = sum(c[k] for k in ("gap", "current", "tie", "undecidable"))
    print(f"{DATE}: {examined} rows with a change from the stored series. "
          f"{c['gap']} computed across the missing {MISSING} (flagged {c['flagged_now']} now, "
          f"{c['already_flagged']} already), {c['current']} from consecutive sessions, "
          f"{c['tie']} ties where both give the same figure (not flagged), "
          f"{c['undecidable']} undecidable (not flagged); {c['not_examined']} rows had no "
          f"change from the stored series.")

    if not write:
        print("flag: dry run, nothing written. Pass --write to apply.")
        return 0
    lf._atomic_write_csv(panel, columns, out)
    print(f"flag: wrote {panel} ({len(out)} rows, columns {len(header)} -> {len(columns)}).")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
