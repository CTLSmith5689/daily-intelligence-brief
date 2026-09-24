#!/usr/bin/env python3
"""What the PM reads before deciding: every book as it stands, and the rules books.

    python3 portfolio/bin/review.py [--prices-dir DIR] [--book ID] [--full]

Prints JSON: for each live book its value, return, benchmark return, cash, gross and
net exposure, holdings with weights, and for a style book the rules candidate with
the names it would buy and sell. The same numbers the Portfolios page shows,
computed from the ledger, the panel and the stored closes. It writes nothing.
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from portfolio import engine as E  # noqa: E402

BRIEF_KEYS = ("id", "name", "kind", "inception", "asof", "nav", "pricedNav", "partial", "cash",
              "ret", "benchmark", "benchRet", "cashRet", "gross", "net", "holdingsCount",
              "candidateAdds", "candidateDrops", "lastDecision", "mandate")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--prices-dir", default=None)
    ap.add_argument("--book", default=None)
    ap.add_argument("--full", action="store_true", help="include holdings, candidates and trades")
    a = ap.parse_args(argv)
    data = E.site_data(prices=E.PriceStore(a.prices_dir), benchmarks=E.BenchmarkStore(a.prices_dir))
    books = [b for b in data["books"] if not a.book or b["id"] == a.book]
    if not a.full:
        books = [dict({k: b.get(k) for k in BRIEF_KEYS},
                      holdings=[{k: h.get(k) for k in ("ticker", "side", "weight", "ret")}
                                for h in b.get("holdings") or []],
                      candidate=[{k: p.get(k) for k in ("rank", "ticker", "sector", "weight")}
                                 for p in b.get("candidate") or []])
                 for b in books]
    print(json.dumps({"asof": data["asof"], "coverage": data["coverage"],
                      "boxCounts": data["boxCounts"], "books": books}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
