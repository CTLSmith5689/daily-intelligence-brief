#!/usr/bin/env python3
"""Incept the nine style books. Safe to run again: a book already incepted is skipped.

    python3 portfolio/bin/seed.py [--prices-dir DIR] [--date YYYY-MM-DD] [--dry-run]

Reads the latest panel date in data/fundamentals/ (or the latest on or before
--date), the stored closes in docs/prices/ (restored from gh-pages by the
workflow; point --prices-dir at a copy when running locally), and the analyst's
views in theses/ledger/events.csv. Appends to portfolio/ledger/trades.csv,
decisions.csv and mandates.csv, and writes portfolio/books/<id>/mandate.json for
a book that has none. Trading after inception is the PM's job, not this script's.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from portfolio import engine as E  # noqa: E402


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--prices-dir", default=None)
    ap.add_argument("--date", default=None)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    out = E.seed(prices=E.PriceStore(a.prices_dir), day=a.date, dry_run=a.dry_run)
    for bid in out["seeded"]:
        picks = out["books"][bid]
        print(f"{bid}: {len(picks)} holdings; top 5 by rank: "
              + ", ".join(p["ticker"] for p in picks[:5]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
