#!/usr/bin/env python3
"""Universe gate, peer-relative GVMQ composite, and slot allocation.

Deterministic and model-free on purpose. The screen is the part of this system
that must give the same answer twice, and a run that improvises its own universe
each week cannot be compared against its own track record.

Emits one JSON object on stdout. Zero model tokens.
"""
import json, math, statistics as st, sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (SLEEVES, THESES, LEDGER, load_panel, num, read_csv_rows)

CFG = json.loads((THESES / "config.json").read_text())


def recompute_ev_ebitda(r):
    """The panel's own ev_ebitda does not reconcile to its balance sheet: median
    relative error 9.8%, p90 58%, because it uses some other EV definition. Use
    the panel's own inputs so the number is at least self-consistent."""
    px, sh = num(r.get("price")), num(r.get("shares_outstanding"))
    debt, cash = num(r.get("total_debt")), num(r.get("cash_and_investments"))
    ebitda = num(r.get("ttm_ebitda"))
    if None in (px, sh, ebitda) or ebitda <= 0:
        return None
    ev = px * sh + (debt or 0.0) - (cash or 0.0)
    return ev / ebitda if ev > 0 else None


def core_universe(rows):
    """Gate to names where peer-relative work is actually supportable.

    Sub-$1B, sector-less and revenue-less rows are where every fill-rate problem
    in this panel lives. Removing them takes sub_industry coverage from 62% to
    100% and costs nothing that could have been scored anyway."""
    out = []
    for r in rows:
        cap = num(r.get("market_cap"))
        if not cap or cap < CFG["min_market_cap"]:
            continue
        if not (r.get("sector") or "").strip():
            continue
        if not num(r.get("ttm_revenue")):
            continue
        r = dict(r)
        r["_ev_ebitda"] = recompute_ev_ebitda(r)
        out.append(r)
    return out


def value_of(r, field):
    return r["_ev_ebitda"] if field == "ev_ebitda" else num(r.get(field))


def peer_key(r, groups):
    """Sub-industry when the group is big enough to mean anything, else sector.
    The pipeline's own screener only ever cohorts on sector; sub-industry is
    tighter where it exists, and peer_n is reported so the reader can tell."""
    sub = (r.get("sub_industry") or "").strip()
    if sub and groups.get(sub, 0) >= CFG["min_peer_group"]:
        return sub
    return (r.get("sector") or "").strip()


def winsorize(vals, lo=0.02, hi=0.98):
    if len(vals) < 5:
        return min(vals), max(vals)
    s = sorted(vals)
    return s[int(lo * (len(s) - 1))], s[int(hi * (len(s) - 1))]


def _field_stats(members, field):
    """Winsorized mean/sd for one field over one cohort, or None."""
    vals = [v for v in (value_of(m, field) for m in members) if v is not None]
    if len(vals) < 20:          # the floor the pipeline uses before it will z-score
        return None
    lo, hi = winsorize(vals)
    clipped = [min(max(v, lo), hi) for v in vals]
    mean = sum(clipped) / len(clipped)
    var = sum((v - mean) ** 2 for v in clipped) / len(clipped)      # population sd
    if var <= 0:
        return None
    return (mean, math.sqrt(var), lo, hi, len(vals))


def score(rows):
    """Peer-relative GVMQ, cohorted per field rather than per name.

    Sub-industry is the sharper comparison and sector is the one with enough
    rows to support a z-score. Choosing one cohort for the whole name forces a
    bad trade: cohorting everything on sub-industry left 266 of 1,613 names
    scorable across 9 peer groups, because a 40-member sub-industry rarely has
    20 non-null values for any given field. So the cohort is chosen per FIELD,
    sub-industry where that field is well enough populated and sector otherwise.
    Each name reports how many of its z-scores came from the tighter cohort."""
    sub_members, sec_members = {}, {}
    for r in rows:
        sub = (r.get("sub_industry") or "").strip()
        sec = (r.get("sector") or "").strip()
        if sub:
            sub_members.setdefault(sub, []).append(r)
        if sec:
            sec_members.setdefault(sec, []).append(r)

    all_fields = sorted({f for s in SLEEVES.values() for f in s["fields"]})
    sub_stats, sec_stats = {}, {}
    for field in all_fields:
        for name, members in sub_members.items():
            if len(members) >= CFG["min_peer_group"]:
                st_ = _field_stats(members, field)
                if st_:
                    sub_stats[(name, field)] = st_
        for name, members in sec_members.items():
            st_ = _field_stats(members, field)
            if st_:
                sec_stats[(name, field)] = st_

    for r in rows:
        sub = (r.get("sub_industry") or "").strip()
        sec = (r.get("sector") or "").strip()
        sleeve_scores, sleeve_n, tight = {}, {}, 0
        for name, sleeve in SLEEVES.items():
            zs = []
            for f in sleeve["fields"]:
                v = value_of(r, f)
                if v is None:
                    continue
                stat = sub_stats.get((sub, f))
                if stat:
                    tight += 1
                else:
                    stat = sec_stats.get((sec, f))
                if not stat:
                    continue
                mean, sd, lo, hi, _ = stat
                z = (min(max(v, lo), hi) - mean) / sd
                if f in sleeve["invert"]:
                    z = -z
                zs.append(max(-3.0, min(3.0, z)))
            sleeve_n[name] = len(zs)
            if len(zs) >= CFG["min_fields_per_sleeve"]:
                sleeve_scores[name] = (sum(zs) / len(zs)) * math.sqrt(len(zs) / 5.0)
            else:
                sleeve_scores[name] = None
        present = [v for v in sleeve_scores.values() if v is not None]
        r["_sleeves"] = sleeve_scores
        r["_sleeve_n"] = sleeve_n
        r["_tight_z"] = tight
        r["_scorable"] = len(present) >= CFG["min_sleeves"]
        r["_composite"] = sum(present) / len(present) if r["_scorable"] else None
        r["_peer"] = sub if (sub and len(sub_members.get(sub, [])) >= CFG["min_peer_group"]) else sec
        r["_peer_n"] = len(sub_members.get(r["_peer"], sec_members.get(r["_peer"], [])))
    return rows


def coverage_from_events():
    """ticker -> {last_covered, times_covered, last_thesis_id}, folded from events.

    This used to read ledger/coverage.csv. Nothing in the repo ever wrote that
    file: screen.py was its only reference and it has sat as a header-only stub
    since it was created, so off_cooldown() returned True for every name and
    cooldown has never once applied. The screen would have re-surfaced the same
    tickers indefinitely.

    Deriving it from events.csv is the same treatment positions/{TICKER}.md
    already gets, and it cannot go stale: a note that produced an event is by
    definition a note that covered the name."""
    cov = {}
    for e in read_csv_rows(LEDGER / "events.csv"):
        t, d = e.get("ticker"), e.get("date")
        if not t or not d:
            continue
        row = cov.setdefault(t, {"ticker": t, "last_covered": "",
                                 "times_covered": 0, "last_thesis_id": ""})
        row["times_covered"] += 1
        if d >= row["last_covered"]:        # ISO dates sort lexically
            row["last_covered"] = d
            row["last_thesis_id"] = e.get("thesis_id") or ""
    return cov


def load_state():
    cov = coverage_from_events()
    preds = read_csv_rows(LEDGER / "predictions.csv")
    scored_ids = {r["prediction_id"] for r in read_csv_rows(LEDGER / "scores.csv")}
    open_preds = [p for p in preds if p.get("prediction_id") not in scored_ids]
    wl_path = THESES / "watchlist.txt"
    watchlist = []
    if wl_path.exists():
        for line in wl_path.read_text().splitlines():
            t = line.split("#", 1)[0].strip().upper()
            if t:
                watchlist.append(t)
    return cov, open_preds, watchlist


def off_cooldown(ticker, cov, kind, today):
    row = cov.get(ticker)
    if not row or not row.get("last_covered"):
        return True
    try:
        last = datetime.strptime(row["last_covered"], "%Y-%m-%d").date()
    except ValueError:
        return True
    return (today - last).days >= CFG["cooldown_days"][kind]


def allocate(scored, cov, open_preds, watchlist, today):
    """Four slots with four different jobs.

    The structure is the point. Handed one rule, a screen returns four variations
    of the same idea every week: whatever is cheapest and rising, all of them
    long. A forced bear case and a forced revisit are what stop the archive from
    becoming a pile of agreeable notes."""
    by_ticker = {r["ticker"]: r for r in scored}
    pool = [r for r in scored if r["_scorable"]]
    chosen, used_sectors = [], set()
    # One of each specialist slot; screen takes whatever is left. Counting the
    # running total instead let contrarian occupy the empty review slot, which
    # is every run until the ledger has open predictions in it.
    cap = {"watchlist": 1, "review": 1, "contrarian": 1}
    taken = {k: 0 for k in cap}

    def room(kind):
        return taken[kind] < cap[kind] and len(chosen) < CFG["slots_per_run"]

    def take(r, slot, reason):
        if not r or r["ticker"] in {c["ticker"] for c in chosen}:
            return False
        taken[slot] = taken.get(slot, 0) + 1
        chosen.append({"ticker": r["ticker"], "slot": slot, "reason": reason,
                       "composite_z": round(r["_composite"], 4) if r["_composite"] is not None else None,
                       "peer_group": r["_peer"], "peer_n": r["_peer_n"],
                       "sector": r.get("sector", ""),
                       "price": num(r.get("price")),
                       "sleeves": {k: (round(v, 3) if v is not None else None)
                                   for k, v in r["_sleeves"].items()},
                       "sleeve_fields": r["_sleeve_n"]})
        used_sectors.add(r.get("sector", ""))
        return True

    # WATCHLIST: hand-picked names bypass every gate except cooldown.
    for t in sorted(watchlist, key=lambda t: (cov.get(t, {}).get("last_covered") or "")):
        if not room("watchlist"):
            break
        if off_cooldown(t, cov, "watchlist", today) and t in by_ticker:
            take(by_ticker[t], "watchlist", "watchlist, longest since covered")

    # REVIEW: an open prediction that has moved, or is past its review date.
    def move(p):
        r = by_ticker.get(p.get("ticker"))
        entry = num(p.get("entry_price"))
        now = num(r.get("price")) if r else None
        return abs(now / entry - 1) if (entry and now) else 0.0
    movers = sorted(open_preds, key=move, reverse=True)
    for p in movers:
        if not room("review"):
            break
        if move(p) >= CFG["review_move_threshold"] and p.get("ticker") in by_ticker:
            take(by_ticker[p["ticker"]], "review",
                 f"open prediction moved {move(p)*100:.0f}% since {p.get('written_on')}")
    for p in sorted(open_preds, key=lambda x: x.get("review_by") or ""):
        if not room("review"):
            break
        if (p.get("review_by") or "9999") <= today.isoformat() and p.get("ticker") in by_ticker:
            take(by_ticker[p["ticker"]], "review", f"past review_by {p.get('review_by')}")

    # CONTRARIAN: the worst-ranked large name. Forces a bear case into every run.
    big = [r for r in pool
           if (num(r.get("market_cap")) or 0) >= CFG["contrarian_min_market_cap"]
           and off_cooldown(r["ticker"], cov, "contrarian", today)]
    for r in sorted(big, key=lambda x: x["_composite"]):
        if not room("contrarian"):
            break
        take(r, "contrarian", "lowest composite among large caps")

    # SCREEN: fill the rest from the top, at most one per sector.
    for r in sorted(pool, key=lambda x: -x["_composite"]):
        if len(chosen) >= CFG["slots_per_run"]:
            break
        if not off_cooldown(r["ticker"], cov, "screen", today):
            continue
        if r.get("sector", "") in used_sectors and CFG["max_per_sector_per_run"] <= 1:
            continue
        take(r, "screen", "highest composite off cooldown")

    # ROTATION: only if the slots above could not be filled. Deterministic by
    # date so a same-day re-run picks the same names.
    if len(chosen) < CFG["slots_per_run"]:
        never = [r for r in pool if r["ticker"] not in cov]
        never.sort(key=lambda r: (r["ticker"] + today.isoformat()))
        for r in never:
            if len(chosen) >= CFG["slots_per_run"]:
                break
            take(r, "rotation", "never covered")
    return chosen


def main():
    today = datetime.now(tz=timezone.utc).date()
    panel_date, rows = load_panel()
    if not rows:
        print(json.dumps({"error": "panel unavailable"}))
        return 1
    core = core_universe(rows)
    scored = score(core)
    pool = [r for r in scored if r["_scorable"]]
    cov, open_preds, watchlist = load_state()
    slots = allocate(scored, cov, open_preds, watchlist, today)
    print(json.dumps({
        "run_date": today.isoformat(),
        "panel_date": panel_date,
        "panel_rows": len(rows),
        "core_universe": len(core),
        "scorable": len(pool),
        "peer_groups": len({r["_peer"] for r in pool}),
        "watchlist": watchlist,
        "open_predictions": len(open_preds),
        "slots": slots,
        # Stated rather than implied: at four a run this covers a few hundred
        # names a year out of a thousand-odd eligible. Most of the pool is never
        # written about, and that is the correct behaviour for a screen.
        "coverage_note": f"{len(pool)} eligible, {len(slots)} covered this run",
    }, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
