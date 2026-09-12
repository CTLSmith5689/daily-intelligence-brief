#!/usr/bin/env python3
"""Grade matured predictions. Arithmetic only, and never the analyst's own work.

The analyst writes the view. This writes what the price did. Nothing here reads
a note, and nothing the model produces can reach these numbers, which is the
only reason the track record is worth anything six months from now.

An open prediction is one present in predictions.csv and absent from scores.csv.
No row is ever mutated, so a call cannot be softened after the fact.

    python3 theses/bin/score.py [--dry-run] [--asof YYYY-MM-DD]
"""
import csv, io, json, math, statistics as st, sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (LEDGER, RAW, PAGES, fetch, num, read_csv_rows, append_csv, load_panel)

SCORE_COLUMNS = ["prediction_id", "scored_on", "horizon_end", "exit_price", "abs_return",
                 "spy_return", "rel_spy", "peer_median_return", "rel_peer", "peers_used",
                 "target_hit", "max_favourable", "max_adverse", "outcome", "note"]
FLAT_BAND = 0.03          # inside +/-3% against peers is a draw, not a win
MAX_PEERS = 12
_closes_cache = {}


def closes(ticker):
    if ticker in _closes_cache:
        return _closes_cache[ticker]
    raw = fetch(f"{PAGES}/prices/{ticker}.json")
    out = {}
    if raw:
        try:
            out = {d: p for d, p in json.loads(raw).get("closes", [])
                   if isinstance(p, (int, float))}
        except Exception:
            out = {}
    _closes_cache[ticker] = out
    return out


def close_on_or_after(series, date_iso):
    """First close on or after a date. Markets shut; a horizon can land on a
    Saturday or a holiday and the next session is the honest answer."""
    for d in sorted(series):
        if d >= date_iso:
            return d, series[d]
    return None, None


def close_on_or_before(series, date_iso):
    for d in sorted(series, reverse=True):
        if d <= date_iso:
            return d, series[d]
    return None, None


def spy_series():
    """SPY daily closes from data/quotes.csv.

    docs/prices holds equities only, so the benchmark cannot come from there.
    quotes.csv needs care: it carries a SECOND header at line 250 from a schema
    widening, and roughly 38% of its rows duplicate an earlier (observed_at,
    ticker). Reading it naively yields the literal string "observed_at" as data."""
    raw = fetch(f"{RAW}/data/quotes.csv")
    if not raw:
        return {}
    seen, by_day = set(), {}
    for row in csv.DictReader(io.StringIO(raw.decode("utf-8", "replace"))):
        if row.get("observed_at") == "observed_at" or row.get("ticker") != "SPY":
            continue
        key = (row.get("observed_at"), row.get("ticker"))
        if key in seen:
            continue
        seen.add(key)
        px = num(row.get("price"))
        obs = (row.get("observed_at") or "")[:10]
        if px and obs:
            by_day[obs] = px          # last observation of each day wins
    return by_day


def peer_return(ticker, panel, start, end):
    """Median return of up to 12 sub-industry peers over the same window.

    This is the benchmark that tests stock picking. Beating SPY on a refiner
    while every refiner rallied is a sector call that happened to be right, and
    only the peer comparison can tell those apart."""
    me = panel.get(ticker)
    if not me:
        return None, 0
    sub = (me.get("sub_industry") or "").strip()
    cohort = [t for t, r in panel.items()
              if t != ticker and (r.get("sub_industry") or "").strip() == sub]
    if len(cohort) < 3:
        sec = (me.get("sector") or "").strip()
        cohort = [t for t, r in panel.items()
                  if t != ticker and (r.get("sector") or "").strip() == sec]
    cohort = sorted(cohort, key=lambda t: -(num(panel[t].get("market_cap")) or 0))[:MAX_PEERS]
    rets = []
    for t in cohort:
        s = closes(t)
        if not s:
            continue
        _, a = close_on_or_before(s, start)
        _, b = close_on_or_after(s, end)
        if a and b and a > 0:
            rets.append(b / a - 1)
    return (st.median(rets) if rets else None), len(rets)


def main():
    dry = "--dry-run" in sys.argv
    today = (sys.argv[sys.argv.index("--asof") + 1] if "--asof" in sys.argv
             else datetime.now(tz=timezone.utc).date().isoformat())

    preds = read_csv_rows(LEDGER / "predictions.csv")
    done = {r["prediction_id"] for r in read_csv_rows(LEDGER / "scores.csv")}
    open_preds = [p for p in preds if p.get("prediction_id") not in done]
    if not open_preds:
        print(f"score: no open predictions ({len(preds)} total, {len(done)} already scored).")
        return 0

    matured = []
    for p in open_preds:
        try:
            end = (datetime.strptime(p["written_on"], "%Y-%m-%d").date()
                   + timedelta(days=int(p["horizon_days"]))).isoformat()
        except (KeyError, ValueError):
            continue
        if end <= today:
            matured.append((p, end))
    print(f"score: {len(open_preds)} open, {len(matured)} matured as of {today}.")
    if not matured:
        return 0

    _, rows = load_panel()
    panel = {r["ticker"]: r for r in rows}
    spy = spy_series()
    if not spy:
        print("score: SPY series unavailable; rel_spy will be blank but peer scoring "
              "continues. The peer benchmark is the one that tests stock picking.")

    out = []
    for p, end in matured:
        t = p["ticker"]
        s = closes(t)
        entry = num(p.get("entry_price"))
        if not s or not entry:
            out.append({"prediction_id": p["prediction_id"], "scored_on": today,
                        "horizon_end": end, "outcome": "voided",
                        "note": "no close series or no entry price"})
            continue
        xd, exit_px = close_on_or_after(s, end)
        if not exit_px:
            out.append({"prediction_id": p["prediction_id"], "scored_on": today,
                        "horizon_end": end, "outcome": "voided",
                        "note": f"no close on or after {end}; series ends {max(s)}"})
            continue

        sign = -1 if (p.get("direction") or "").strip() in ("short", "avoid") else 1
        abs_r = (exit_px / entry - 1) * sign

        sp = None
        if spy:
            _, a = close_on_or_before(spy, p["written_on"])
            _, b = close_on_or_after(spy, end)
            if a and b and a > 0:
                sp = (b / a - 1) * sign

        pm, npeers = peer_return(t, panel, p["written_on"], end)
        if pm is not None:
            pm *= sign

        window = {d: v for d, v in s.items() if p["written_on"] <= d <= xd}
        mfe = (max(window.values()) / entry - 1) * sign if window else None
        mae = (min(window.values()) / entry - 1) * sign if window else None

        tgt = num(p.get("target_price"))
        hit = ""
        if tgt and window:
            hit = "yes" if (max(window.values()) >= tgt if sign == 1
                            else min(window.values()) <= tgt) else "no"

        rel_peer = (abs_r - pm) if pm is not None else None
        if rel_peer is None:
            outcome, note = "unbenchmarked", "no peer series available"
        elif rel_peer > FLAT_BAND:
            outcome, note = "right", ""
        elif rel_peer < -FLAT_BAND:
            outcome, note = "wrong", ""
        else:
            outcome, note = "flat", f"inside the +/-{FLAT_BAND*100:.0f}% band against peers"

        r4 = lambda v: round(v, 4) if v is not None else ""
        out.append({
            "prediction_id": p["prediction_id"], "scored_on": today, "horizon_end": end,
            "exit_price": round(exit_px, 4), "abs_return": r4(abs_r),
            "spy_return": r4(sp), "rel_spy": r4(abs_r - sp if sp is not None else None),
            "peer_median_return": r4(pm), "rel_peer": r4(rel_peer), "peers_used": npeers,
            "target_hit": hit, "max_favourable": r4(mfe), "max_adverse": r4(mae),
            "outcome": outcome, "note": note,
        })

    for r in out:
        print(f"  {r['prediction_id']:22} {r['outcome']:14} "
              f"abs {r.get('abs_return','')!s:>8}  vs peers {r.get('rel_peer','')!s:>8}  "
              f"({r.get('peers_used','')} peers)")
    if dry:
        print("\nscore: --dry-run, nothing written.")
        return 0
    n = append_csv(LEDGER / "scores.csv", SCORE_COLUMNS, out)
    print(f"\nscore: appended {n} rows to theses/ledger/scores.csv")
    return 0


if __name__ == "__main__":
    sys.exit(main())
