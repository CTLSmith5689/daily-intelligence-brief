#!/usr/bin/env python3
"""Size a book from the current theses. Arithmetic and constraints only.

Agent 2's deterministic half. This decides nothing about whether a company is
good; it decides how much of the book is willing to be wrong about a view the
analyst already formed, and it reports the conflicts it cannot settle instead of
quietly picking whichever answer keeps the book intact.

    python3 portfolio/bin/construct.py [--date YYYY-MM-DD] [--dry-run]

Writes portfolio/books/{DATE}.json and {DATE}.md.
"""
import json, math, statistics as st, sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "theses" / "bin"))
from common import LEDGER, PAGES, fetch, num, read_csv_rows, load_panel

REPO = Path(__file__).resolve().parents[2]
BOOKS = REPO / "portfolio" / "books"

CFG = {
    "max_position": 0.15,
    "min_position": 0.02,
    "max_sector": 0.25,
    "correlation_warn": 0.50,     # a pair above this is effectively one bet
    "pair_warn_weight": 0.15,     # ... and matters once the pair is this big
    "excluded_directions": {"no view", "avoid", "watch"},
}


def current_views():
    """Latest event per ticker. The book follows the current view, not the
    first one: a thesis revised to 'no view' must drop out."""
    latest = {}
    for e in read_csv_rows(LEDGER / "events.csv"):
        if e.get("ticker"):
            latest[e["ticker"]] = e          # events.csv is append-only and ordered
    return {t: e for t, e in latest.items() if e.get("kind") != "close"}


def closes(t):
    raw = fetch(f"{PAGES}/prices/{t}.json")
    if not raw:
        return {}
    try:
        return {d: p for d, p in json.loads(raw).get("closes", [])
                if isinstance(p, (int, float))}
    except Exception:
        return {}


def build(views):
    series = {t: closes(t) for t in views}
    series = {t: s for t, s in series.items() if len(s) > 60}
    dropped = [t for t in views if t not in series]
    if len(series) < 2:
        return None, dropped, "fewer than two positions have a usable close series"

    common = sorted(set.intersection(*[set(s) for s in series.values()]))
    rets = {t: [series[t][common[i]] / series[t][common[i - 1]] - 1
                for i in range(1, len(common))] for t in series}
    vol = {t: st.pstdev(rets[t]) * math.sqrt(252) for t in series}
    return (common, rets, vol), dropped, None


def size(views, vol):
    """conviction / volatility, normalised, then capped until it settles.

    Conviction alone ignores risk. Inverse volatility alone ignores the analyst.
    This multiplies them and lets the cap bind. It is not optimal under any
    model and is not meant to be: it is legible, and a rule the PM can quietly
    abandon is worse than a blunt one it cannot."""
    raw = {}
    for t, e in views.items():
        if t not in vol:
            continue
        try:
            c = int(e.get("conviction") or 0)
        except ValueError:
            c = 0
        if c > 0 and vol[t] > 0:
            raw[t] = c / vol[t]
    if not raw:
        return {}
    norm = lambda d: {k: v / sum(d.values()) for k, v in d.items()}
    w = norm(raw)
    for _ in range(80):
        w = norm({t: min(max(x, CFG["min_position"]), CFG["max_position"])
                  for t, x in w.items()})
    return w


def corr(a, b):
    ma, mb = st.mean(a), st.mean(b)
    n = sum((x - ma) * (y - mb) for x, y in zip(a, b))
    da = math.sqrt(sum((x - ma) ** 2 for x in a))
    db = math.sqrt(sum((y - mb) ** 2 for y in b))
    return n / (da * db) if da and db else 0.0


def main():
    args = sys.argv[1:]
    date = (args[args.index("--date") + 1] if "--date" in args
            else datetime.now(tz=timezone.utc).date().isoformat())
    dry = "--dry-run" in args

    views = current_views()
    if not views:
        print("construct: no thesis events yet. Nothing to size.")
        return 0
    held = {t: e for t, e in views.items()
            if (e.get("direction") or "").strip() not in CFG["excluded_directions"]}
    excluded = {t: e.get("direction") for t, e in views.items() if t not in held}

    data, dropped, err = build(held)
    if err:
        print(f"construct: {err}")
        return 1
    common, rets, vol = data
    held = {t: e for t, e in held.items() if t in vol}
    w = size(held, vol)
    if not w:
        print("construct: no position carries a usable conviction.")
        return 1

    bookvol = lambda ws: st.pstdev(
        [sum(ws[t] * rets[t][i] for t in ws) for i in range(len(common) - 1)]) * math.sqrt(252)
    eq = {t: 1 / len(w) for t in w}
    _, rows = load_panel()
    panel = {r["ticker"]: r for r in rows}
    beta = lambda ws: sum(ws[t] * (num(panel.get(t, {}).get("beta_1y")) or 0) for t in ws)

    sectors = {}
    for t in w:
        sectors.setdefault((panel.get(t, {}).get("sector") or "unknown"), []).append(t)
    sector_w = {s: sum(w[t] for t in ts) for s, ts in sectors.items()}

    flags = []
    for s, x in sorted(sector_w.items(), key=lambda kv: -kv[1]):
        if x > CFG["max_sector"]:
            flags.append({"severity": "hard", "kind": "sector_cap",
                          "detail": f"{s} at {x*100:.1f}% against a {CFG['max_sector']*100:.0f}% "
                                    f"cap ({', '.join(sectors[s])}). No new position in this "
                                    f"sector until it is back under."})
    names = sorted(w)
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            c = corr(rets[a], rets[b])
            pair = w[a] + w[b]
            if c >= CFG["correlation_warn"] and pair >= CFG["pair_warn_weight"]:
                sa = panel.get(a, {}).get("sector", "?")
                sb = panel.get(b, {}).get("sector", "?")
                flags.append({"severity": "soft", "kind": "correlated_pair",
                              "detail": f"{a} and {b} correlate at {c:.2f} and together are "
                                        f"{pair*100:.1f}% of the book ({sa} / {sb}). No sector "
                                        f"rule catches this. Consider sizing them as one bet."})
    convs = [e.get("conviction") for e in held.values()]
    if len(set(convs)) < len(convs):
        dup = sorted({c for c in convs if convs.count(c) > 1})
        flags.append({"severity": "soft", "kind": "conviction_comparability",
                      "detail": f"Conviction {', '.join(dup)} appears on multiple names written "
                                f"in separate sessions with no memory of each other. Nothing "
                                f"guarantees those mean the same thing, and the sizing rule "
                                f"treats them as though they do. Bounded by the "
                                f"{CFG['max_position']*100:.0f}% cap, not solved."})
    if dropped:
        flags.append({"severity": "hard", "kind": "no_price_series",
                      "detail": f"No usable close series for {dropped}: excluded from the book "
                                f"rather than sized blind."})

    book = {
        "date": date,
        "positions": [{
            "ticker": t, "weight": round(w[t], 4), "equal_weight": round(eq[t], 4),
            "conviction": held[t].get("conviction"), "direction": held[t].get("direction"),
            "volatility_1y": round(vol[t], 4),
            "beta_1y": num(panel.get(t, {}).get("beta_1y")),
            "sector": panel.get(t, {}).get("sector", ""),
            "sub_industry": panel.get(t, {}).get("sub_industry", ""),
            "thesis_id": held[t].get("thesis_id"),
            "note_path": held[t].get("note_path"),
            "at_cap": abs(w[t] - CFG["max_position"]) < 1e-6,
        } for t in sorted(w, key=lambda x: -w[x])],
        "excluded": excluded,
        "risk": {
            "book_vol": round(bookvol(w), 4),
            "book_vol_equal_weight": round(bookvol(eq), 4),
            "book_beta": round(beta(w), 3),
            "book_beta_equal_weight": round(beta(eq), 3),
            "mean_single_name_vol": round(sum(vol[t] for t in w) / len(w), 4),
            "trading_days": len(common),
        },
        "sector_weights": {s: round(x, 4) for s, x in
                           sorted(sector_w.items(), key=lambda kv: -kv[1])},
        "flags": flags,
        "rule": "w = clamp(normalise(conviction / volatility_1y), "
                f"{CFG['min_position']}, {CFG['max_position']}), iterated to a fixed point",
    }

    print(json.dumps(book, indent=2))
    if dry:
        return 0
    BOOKS.mkdir(parents=True, exist_ok=True)
    (BOOKS / f"{date}.json").write_text(json.dumps(book, indent=2), encoding="utf-8")
    print(f"\nconstruct: wrote portfolio/books/{date}.json", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
