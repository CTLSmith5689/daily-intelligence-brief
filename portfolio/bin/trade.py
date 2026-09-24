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

Each batch is checked against the mandate in force on its date: the last row of
portfolio/ledger/mandates.csv dated on or before it.

The PM sets its own limits. To change a book's mandate:

    python3 portfolio/bin/trade.py --mandate BOOK MANDATE.json --date YYYY-MM-DD
        --reason "Why, in a sentence or two." [--write]

MANDATE.json names the limits to set, for example
{"holdings_range": [30, 40], "max_position": 0.06}; the rest stay as they are. The
PM may set, for a style book: holdings_range, max_position, sector_cap, cash_band,
max_active_share_vs_rules; for the hedge and neural books: gross_max, net_range,
max_long_position, max_short_position, holdings_range_per_side. What a book is
(long only, the size and style group a style book buys from, the neural book's
data, the benchmarks) is set by the owner and refused. The one list of both is
MANDATE_PM_FIELDS and MANDATE_OWNER_FIELDS in portfolio/engine.py, with the sanity
bounds in MANDATE_BOUNDS. The change applies from its date, which may not be
before the book's last mandate or last trade; it appends a mandates.csv row and a
mandate_change decision (author pm, with the reason) and updates mandate.json.
One change per book and date: running the same file again writes nothing.
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
        mandate = E.mandate_on(batch.get("book"), day, ledger_dir, books_dir) or {}
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


def run_mandate(book, day, change, reason, write=False, ledger_dir=None, books_dir=None,
                out=print):
    """Plan a mandate change and write it when `write` is true. Returns the plan.
    Raises E.OrderError, before anything is written, when it cannot be made."""
    plan = E.plan_mandate(book, day, change, reason, ledger_dir=ledger_dir, books_dir=books_dir)
    if plan["already"]:
        out(f"{book} {day} mandate: already written; nothing to do.")
        return plan
    out(f"{book} {day} mandate change ({plan['decision']['decision_id']}):")
    for c in plan["changes"]:
        out(f"  {c}")
    for w in plan["warnings"]:
        out(f"  warning: {w}")
    if write:
        E.write_mandate(plan, ledger_dir, books_dir)
        out("trade: wrote the mandate change.")
    else:
        out("trade: dry run, nothing written. Add --write to record it.")
    return plan


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("orders", nargs="?", help="an order batch file (not with --mandate)")
    ap.add_argument("--mandate", nargs=2, metavar=("BOOK", "FILE"),
                    help="change BOOK's mandate to the limits in FILE (JSON)")
    ap.add_argument("--date", help="with --mandate: the date it applies from, YYYY-MM-DD")
    ap.add_argument("--reason", help="with --mandate: why, in a sentence or two")
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--prices-dir", default=None)
    a = ap.parse_args(argv)
    if a.mandate:
        if a.orders or not a.date or not a.reason:
            ap.error("--mandate BOOK FILE needs --date and --reason, and no order file")
        book, path = a.mandate
        try:
            change = json.loads(Path(path).read_text(encoding="utf-8"))
            run_mandate(book, a.date, change, a.reason, write=a.write)
        except E.OrderError as exc:
            print("trade: mandate change refused, nothing written:")
            for e in exc.errors:
                print(f"  {e}")
            return 1
        return 0
    if not a.orders:
        ap.error("give an order file, or --mandate BOOK FILE")
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
