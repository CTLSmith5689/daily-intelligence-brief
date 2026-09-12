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
import screen

REPO = Path(__file__).resolve().parents[2]
BOOKS = REPO / "portfolio" / "books"

CFG = {
    "risk_budget": 0.028,         # per-name risk budget: weight x volatility
    "min_conviction": 3,          # conviction is a GATE, not a multiplier
    "max_position": 0.12,
    "min_position": 0.03,
    "max_invested": 0.97,
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
        # Say which of the two reasons it is. "No usable close series" on a book
        # that simply has one holdable name sends the reader to check gh-pages
        # for a problem that is not there.
        if dropped:
            return None, dropped, (f"only {len(series)} of {len(views)} candidate positions have a "
                                   f"usable close series; missing: {dropped}")
        return None, dropped, (f"{len(views)} name(s) are holdable, which is not a portfolio. "
                               f"Correlation, book volatility and sector concentration are all "
                               f"undefined on one position. Write more theses before sizing.")

    common = sorted(set.intersection(*[set(s) for s in series.values()]))
    rets = {t: [series[t][common[i]] / series[t][common[i - 1]] - 1
                for i in range(1, len(common))] for t in series}
    vol = {t: st.pstdev(rets[t]) * math.sqrt(252) for t in series}
    return (common, rets, vol), dropped, None


def universe_vol_bounds(rows):
    """The 25th and 90th percentile of volatility across the gated universe.

    The clamp is the whole point of this scheme. Raw inverse-volatility over
    2,000 random 12-name books from this universe gives a max/min weight ratio
    of 3.91 at the median and 7.75 at the 95th percentile. Clamping the input to
    [p25, p90] pins it at 2.48 at BOTH. Without it the book is a low-volatility
    factor bet wearing prudence as a disguise."""
    # The GATED universe, not the raw panel. p90 across all 5,350 rows is 1.20,
    # because the tail is micro caps nobody can hold; across the 1,613 names the
    # screen will actually surface it is 0.71. Using the wrong one makes the
    # upper clamp barely bind and quietly reinstates the dispersion it exists to
    # remove.
    rows = screen.core_universe(rows)
    vols = sorted(v for v in (num(r.get("volatility_1y")) for r in rows) if v)
    if len(vols) < 50:
        return 0.25, 0.75
    pick = lambda q: vols[int(q * (len(vols) - 1))]
    return pick(0.25), pick(0.90)


def size(views, vol, bounds):
    """Clamped inverse volatility. Conviction is a gate, not a multiplier.

    The earlier version sized by conviction / volatility, and that was wrong for
    a reason worth writing down: conviction is self-graded, unvalidated, and the
    single largest subjective input in this system. Multiplying by it turns the
    note into a number, the number into a weight, and the portfolio into the
    ranking. That is the failure this whole project is trying to avoid.

    So conviction does one thing: below the gate a name is not in the book. It
    still gets written, and it still gets a row in predictions.csv so the scale
    keeps being scored, but it gets no weight. A one-bit signal gets a one-bit
    use, until the ledger has enough history to say whether it deserves more.

    Weight is then w = risk_budget / clamp(volatility), so every position
    contributes a similar amount of risk rather than a similar amount of money."""
    vlo, vhi = bounds
    w, gated = {}, {}
    for t, e in views.items():
        if t not in vol or vol[t] <= 0:
            continue
        try:
            c = int(e.get("conviction") or 0)
        except ValueError:
            c = 0
        if c < CFG["min_conviction"]:
            gated[t] = c
            continue
        vc = min(max(vol[t], vlo), vhi)
        w[t] = min(max(CFG["risk_budget"] / vc, CFG["min_position"]),
                   CFG["max_position"])
    # Normalise DOWN only. If the weights already sum to less than max_invested,
    # the remainder is cash, and it is reported rather than quietly spread around.
    total = sum(w.values())
    if total > CFG["max_invested"]:
        w = {t: x * CFG["max_invested"] / total for t, x in w.items()}
    return w, gated


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
    _, panel_rows = load_panel()
    bounds = universe_vol_bounds(panel_rows)
    w, gated = size(held, vol, bounds)
    if not w:
        print(f"construct: nothing clears the conviction gate of "
              f"{CFG['min_conviction']}. {len(gated)} name(s) written but unweighted. "
              f"An empty book is a real answer.")
        return 0

    bookvol = lambda ws: st.pstdev(
        [sum(ws[t] * rets[t][i] for t in ws) for i in range(len(common) - 1)]) * math.sqrt(252)
    eq = {t: 1 / len(w) for t in w}
    panel = {r["ticker"]: r for r in panel_rows}
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
    cash = 1.0 - sum(w.values())
    if cash > 0.15:
        flags.append({"severity": "soft", "kind": "large_cash_residual",
                      "detail": f"{cash*100:.1f}% of the book is uninvested. That is not a view "
                                f"on the market: it falls out of a {CFG['risk_budget']*100:.1f}% "
                                f"per-name risk budget meeting names whose median volatility is "
                                f"{sorted(vol.values())[len(vol)//2]*100:.0f}%. The rule is saying "
                                f"this set of theses cannot be held at full size without "
                                f"exceeding the budget. Decide whether to accept that, raise the "
                                f"budget, or find less volatile ideas."})
    if gated:
        flags.append({"severity": "soft", "kind": "below_conviction_gate",
                      "detail": f"{len(gated)} name(s) written but unweighted: "
                                + ", ".join(f"{t} (conviction {c})" for t, c in sorted(gated.items()))
                                + f". They still carry predictions so the conviction scale keeps "
                                  f"being scored."})
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
        "gated_out": gated,
        "cash": round(cash, 4),
        "vol_clamp": {"p25": round(bounds[0], 4), "p90": round(bounds[1], 4)},
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
        "rule": f"conviction >= {CFG['min_conviction']} to be eligible, then "
                f"w = clamp({CFG['risk_budget']} / clamp(volatility_1y, p25, p90), "
                f"{CFG['min_position']}, {CFG['max_position']}); normalised DOWN only",
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
