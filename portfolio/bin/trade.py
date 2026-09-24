#!/usr/bin/env python3
"""The only way the PM writes trades. Dry run unless --write is given.

    python3 portfolio/bin/trade.py ORDERS.json [--write] [--prices-dir DIR]

ORDERS.json is one batch for one book, or a list of batches:

    {"book": "hedge", "date": "2026-09-28", "batch_id": "w1",
     "action": "trade",
     "reason": "Why, in a sentence or two. It goes into decisions.csv.",
     "orders": [
       {"id": "1", "ticker": "AAPL", "side": "buy",   "weight": 0.03},
       {"id": "2", "ticker": "XYZ",  "side": "short", "shares": 400},
       {"id": "3", "ticker": "MSFT", "side": "sell",  "value": 25000, "lot_id": "..."}
     ]}

side is buy, sell, short or cover. Size each order by exactly one of shares (whole),
weight (of the book's value at the date's close) or value (dollars). A decision with
no trades is {"action": "hold", "orders": []}.

A buy of a name the analyst rates Avoid, Exit or Short (theses/ledger/events.csv,
the newest memo on or before the date) needs "override_reason" on the order; it is
added to the decision's reason. Every trade against the analyst's rating (such a
buy, or a sell or short of a name rated Initiate or Add) is written with
reason_code override_analyst. A style book whose mandate sets
max_active_share_vs_rules is refused a batch that leaves it further from the rules
book than that and further than it was.

Each fill is priced at the stored close for the batch's date (docs/prices, restored
from gh-pages; point --prices-dir at a copy when running locally). An order with no
stored close for that date is refused; nothing is priced from another day. Every
trade pays 5 bps. The batch is checked against the book's mandate
(portfolio/books/<id>/mandate.json): long-only, the universe, position and sector
limits for a style book, gross and net exposure and position limits for the hedge
book, gross exposure for the neural book. A batch with any error writes nothing.

Idempotent: the trade id is <book>-<date>-o<order id> and the decision id is
<book>-<date>-<batch_id>. Running the same file twice writes nothing the second time.
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from portfolio import engine as E  # noqa: E402


def run(batches, write=False, prices=None, ledger_dir=None, books_dir=None, panel_dir=None,
        history=None, events=None, out=print):
    """Plan every batch, and write them when `write` is true. Returns the plans.
    Raises E.OrderError on the first batch that fails, before anything is written."""
    ledger_dir = Path(ledger_dir or E.LEDGER_DIR)
    prices = prices or E.PriceStore()
    history = E.load_style_history(history)
    trades = E.read_rows(ledger_dir / "trades.csv")
    decisions = E.read_rows(ledger_dir / "decisions.csv")
    dates = E.panel_dates(panel_dir)
    plans = []
    for batch in batches:
        day = batch.get("date") or ""
        panel_day = max((d for d in dates if d <= day), default="")
        _, rows = E.panel_rows(panel_day, panel_dir) if panel_day else ("", [])
        classes = E.classify(rows, history, panel_day) if rows else {}
        mandate = E.load_mandate(batch.get("book"), books_dir) or E.default_mandate(batch.get("book")) or {}
        views = E.analyst_views(events, asof=day)
        rules = None
        book = E.BOOKS.get(batch.get("book")) or {}
        if book.get("kind") == "style" and mandate.get("max_active_share_vs_rules") is not None:
            rules = {p["ticker"]: p["weight"] for p in E.rules_candidate(
                book, classes, mandate, views, eligible=lambda t: prices.close(t, day) is not None)}
        plan = E.plan_orders(batch, trades, prices, rows, classes, mandate, decisions,
                             views=views, rules=rules)
        plans.append(plan)
        trades = trades + plan["fills"]
        if plan["decision"]:
            decisions = decisions + [plan["decision"]]
        out(f"{batch['book']} {day} batch {batch.get('batch_id')}: {len(plan['fills'])} fill(s)"
            + (f", {len(plan['skipped'])} already written" if plan["skipped"] else "")
            + (", decision already written" if not plan["decision"] else ""))
        for f in plan["fills"]:
            out(f"  {f['side']:5} {f['shares']:>8} {f['ticker']:<8} at {f['price']:.4f}  "
                f"cost {f['cost']:.2f}  ({f['trade_id']})")
        post = plan["post"]
        out(f"  after: value ${post['priced_nav']:,.2f}, cash ${post['cash']:,.2f}"
            + (f", gross {post['gross']:.1%}, net {post['net']:.1%}" if post["gross"] is not None else ""))
        for w in plan["warnings"]:
            out(f"  warning: {w}")
    if write:
        for plan in plans:
            E.write_orders(plan, ledger_dir)
        out(f"trade: wrote {sum(len(p['fills']) for p in plans)} trade(s).")
    else:
        out("trade: dry run, nothing written. Add --write to record these.")
    return plans


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("orders")
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--prices-dir", default=None)
    a = ap.parse_args(argv)
    data = json.loads(Path(a.orders).read_text(encoding="utf-8"))
    batches = data if isinstance(data, list) else [data]
    try:
        run(batches, write=a.write, prices=E.PriceStore(a.prices_dir))
    except E.OrderError as exc:
        print("trade: refused, nothing written:")
        for e in exc.errors:
            print(f"  {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
