#!/usr/bin/env python3
"""What changed since a thesis was written. The daily run's whole job.

A weekly deep run writes new theses. A daily run should almost always do
nothing, and should wake the model only when something happened to a view that
already exists. This computes that, deterministically and at zero model cost, so
that most days the answer is an empty list and the agent stops.

    python3 theses/bin/queue.py            # JSON on stdout
    python3 theses/bin/queue.py --brief    # one line per item, for a human
"""
import csv, io, json, sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (LEDGER, RAW, PAGES, fetch, num, read_csv_rows, load_panel)

PRICE_MOVE = 0.08          # since the note was written
EARNINGS_WINDOW = 10       # days ahead
_price_cache = {}


def last_close(ticker):
    if ticker in _price_cache:
        return _price_cache[ticker]
    raw = fetch(f"{PAGES}/prices/{ticker}.json")
    out = (None, None)
    if raw:
        try:
            c = json.loads(raw).get("closes", [])
            if c:
                out = (c[-1][0], c[-1][1])
        except Exception:
            pass
    _price_cache[ticker] = out
    return out


def current_views():
    latest = {}
    for e in read_csv_rows(LEDGER / "events.csv"):
        if e.get("ticker"):
            latest[e["ticker"]] = e
    return {t: e for t, e in latest.items() if e.get("kind") != "close"}


def filings_since(tickers, since_by_ticker):
    """Any earnings release filed after the note was written."""
    today = datetime.now(tz=timezone.utc).date()
    hits = {}
    for m in {today.strftime("%Y-%m"),
              (today.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")}:
        raw = fetch(f"{RAW}/data/filings/{m}.csv")
        if not raw:
            continue
        for r in csv.DictReader(io.StringIO(raw.decode("utf-8", "replace"))):
            t = r.get("ticker")
            if t in tickers and r.get("filed", "") > since_by_ticker.get(t, "9999"):
                prev = hits.get(t)
                if not prev or r["filed"] > prev["filed"]:
                    hits[t] = r
    return hits


def build():
    views = current_views()
    if not views:
        return []
    today = datetime.now(tz=timezone.utc).date()
    today_s = today.isoformat()
    _, rows = load_panel()
    panel = {r["ticker"]: r for r in rows}
    preds = {p["ticker"]: p for p in read_csv_rows(LEDGER / "predictions.csv")}
    written = {t: e.get("date", "") for t, e in views.items()}
    filings = filings_since(set(views), written)

    queue = []
    for t, e in views.items():
        reasons = []

        d, px = last_close(t)
        entry = num((preds.get(t) or {}).get("entry_price"))
        if px and entry:
            move = px / entry - 1
            if abs(move) >= PRICE_MOVE:
                reasons.append({"trigger": "price_move", "urgency": 2,
                                "detail": f"{move*100:+.1f}% since the note "
                                          f"({entry:,.2f} to {px:,.2f} on {d})"})

        rb = e.get("review_by") or (preds.get(t) or {}).get("review_by") or ""
        if rb and rb <= today_s:
            reasons.append({"trigger": "review_due", "urgency": 2,
                            "detail": f"review_by was {rb}"})

        # Only usable since the earnings_date key priority was fixed; before that
        # the column held the last reported date for 88% of the universe.
        ed = (panel.get(t, {}) or {}).get("earnings_date", "")
        if ed:
            try:
                days = (datetime.strptime(ed, "%Y-%m-%d").date() - today).days
                if 0 <= days <= EARNINGS_WINDOW:
                    reasons.append({"trigger": "earnings_near", "urgency": 3,
                                    "detail": f"reports in {days} day"
                                              f"{'s' if days != 1 else ''} ({ed})"})
            except ValueError:
                pass

        f = filings.get(t)
        if f:
            reasons.append({"trigger": "new_filing", "urgency": 3,
                            "detail": f"8-K items {f.get('items')} filed {f.get('filed')}, "
                                      f"{f.get('text_chars')} chars of release text"})

        if reasons:
            reasons.sort(key=lambda r: -r["urgency"])
            queue.append({
                "ticker": t, "thesis_id": e.get("thesis_id"),
                "note_path": e.get("note_path"), "written_on": e.get("date"),
                "direction": e.get("direction"), "conviction": e.get("conviction"),
                "urgency": max(r["urgency"] for r in reasons),
                "reasons": reasons,
            })
    queue.sort(key=lambda q: (-q["urgency"], q["ticker"]))
    return queue


def main():
    q = build()
    if "--brief" in sys.argv:
        if not q:
            print("queue: empty. Nothing has happened to an open thesis. "
                  "A daily run should stop here and commit nothing.")
            return 0
        print(f"queue: {len(q)} name{'s' if len(q) != 1 else ''} need attention\n")
        for item in q:
            print(f"  {item['ticker']:6} {item['direction'] or '?':8} "
                  f"conv {item['conviction'] or '-'}")
            for r in item["reasons"]:
                print(f"           {r['trigger']:14} {r['detail']}")
        return 0
    print(json.dumps({"date": datetime.now(tz=timezone.utc).date().isoformat(),
                      "count": len(q), "queue": q}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
