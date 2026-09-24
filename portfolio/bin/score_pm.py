#!/usr/bin/env python3
"""Score the PM and explain each book's return. Arithmetic over stored files only.

    python3 portfolio/bin/score_pm.py [--prices-dir DIR] [--dry-run]

Five things, each written to its own append-only file under data/portfolio/ by
the daily pipeline (lambda_function.run_scoring), after the NAV is recorded:

shadow_trades.csv, shadow_nav.csv
    For each style book, a rules-only shadow: the same starting cash, bought from
    the rules on the book's first day exactly as the book was, and rebalanced to
    the rules candidate book (engine.rules_candidate, the book review.py shows the
    PM) on every date the real book trades, at the stored close for that date,
    paying the same 5 basis points. A rebalance is written once and never
    recomputed, so a later change to the panel or the filed history cannot move a
    past shadow. PM value added = book return minus shadow return, since inception
    and day by day. The hedge and free-hand books have no rules to shadow; they
    are compared with the S&P 500 and with cash.
    Active share against the shadow, each session, cash counted as a position:
        active_share = 0.5 * (sum over names |w_book - w_shadow| + |cash_book - cash_shadow|)

disagreements.csv
    Every PM trade against the analyst's rating on its date (a buy of a name rated
    Avoid, Exit or Short; a sell or short of a name rated Initiate or Add; trade.py
    tags these override_analyst), marked 1, 3 and 6 months later and at the
    analyst's own horizon, against the company's SPDR sector fund:
        pm_excess = pm_sign * ((close(mark) / close(d) - 1) - (fund(mark) / fund(d) - 1))
    with pm_sign +1 for a buy and -1 for a sell or short. The PM was right when
    pm_excess > 0, and the analyst, who said the opposite, when it is below zero.

decision_marks.csv
    Each PM decision that departed from the rules, marked 1, 3 and 6 months
    (30, 91 and 182 calendar days) later, for the companies it traded only:
        did       = sum over those names of w_book_after(name) * r(name)
        rules     = sum over those names of w_rules(name) * r(name)
        value_added = did - rules, as a share of the book's value on the day
    w are weights at the decision date's close; r is the price return from that
    close to the mark date's close. w_rules is the shadow's weight after its own
    rebalance that day; for the hedge and free-hand books it is the book's weight
    before the decision (the alternative is not trading). A decision departed
    when sum |w_book_after - w_rules| over its names exceeds 0.5% of the book.

attribution.csv
    One row per book per session, for the holdings held over the day (the close of
    the session before to this close). Brinson-Fachler by sector against a proxy
    benchmark whose sector weights we can see (we cannot download fund holdings):
        style books: every company in the book's own box, weighted by market value
        hedge, free hand: every operating company in the panel, weighted by market
                          value, standing in for the S&P 500
    With w_s, r_s the book's sector weight and return and W_s, R_s the proxy's,
    R_b the proxy's total and w_c the cash weight (cash earns nothing in the
    ledger):
        allocation_s  = (w_s - W_s) * (R_s - R_b)
        selection_s   = W_s * (r_s - R_s)
        interaction_s = (w_s - W_s) * (r_s - R_s)
        cash drag     = w_c * (0 - R_b)
        costs         = -(trading costs paid at this close) / value at the last close
    and allocation + selection + interaction + cash drag + costs = r_book - R_b,
    exactly. A book that is short some names has sector weights near zero where
    long and short cancel, so r_s is not meaningful; there interaction is folded
    into selection (selection_s = w_s*r_s - w_s*R_s summed per name) and says so.
    proxy error = R_b - r_fund, the gap between the proxy and the fund itself.
    Also per day: the long leg's and short leg's contributions, gross and net
    exposure, the S&P 500's return, and the sizing effect
        sizing = sum_i (w_i - sign_i * gross / N) * r_i
    (actual weights against equal weights of the same holdings, same sides).

Linking over periods (site_summary): Carino's logarithmic method. With daily book
return r_t and proxy return b_t, and R, B their compounded totals,
    k_t = (ln(1+r_t) - ln(1+b_t)) / (r_t - b_t)   (1/(1+r_t) when equal)
    K   = (ln(1+R) - ln(1+B)) / (R - B)           (1/(1+R) when equal)
    linked effect = sum_t effect_t * k_t / K
so the linked effects add up exactly to R - B. The legs are linked the same way
with b_t = 0, so they add up to R. Proxy error is compounded separately over the
days both the proxy and the fund have closes.

Every price is a stored close for that exact date. A day with a missing close is
skipped and flagged, never filled. Returns are price-only: dividends excluded.
"""
import argparse
import json
import math
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from portfolio import engine as E  # noqa: E402

DATA = E.REPO / "data" / "portfolio"
SHADOW_TRADES_CSV = DATA / "shadow_trades.csv"
SHADOW_NAV_CSV = DATA / "shadow_nav.csv"
DECISION_MARKS_CSV = DATA / "decision_marks.csv"
ATTRIBUTION_CSV = DATA / "attribution.csv"

SHADOW_NAV_COLUMNS = ["date", "book", "status", "shadow_nav", "shadow_priced_nav", "shadow_cash",
                      "shadow_capital", "shadow_holdings", "missing", "book_nav", "book_capital",
                      "book_return_since", "shadow_return_since", "value_added_since",
                      "book_return_day", "shadow_return_day", "value_added_day", "active_share",
                      "flags", "computed_at"]
DISAGREEMENT_COLUMNS = ["decision_id", "book", "date", "ticker", "pm_side", "pm_sign",
                        "analyst_call", "analyst_rating", "horizon", "target_date", "mark_date",
                        "sector_etf", "stock_return", "fund_return", "pm_excess", "pm_right",
                        "flags", "computed_at"]
MIN_GROUP = 10
DECISION_MARK_COLUMNS = ["decision_id", "book", "date", "horizon", "target_date", "mark_date",
                         "basis", "names", "weights_book", "weights_rules", "did_return",
                         "rules_return", "value_added", "flags", "computed_at"]
ATTRIBUTION_COLUMNS = ["date", "book", "prev_date", "status", "book_return", "proxy_return",
                       "fund", "fund_return", "proxy_error", "allocation", "selection",
                       "interaction", "cash_drag", "costs", "interaction_folded", "cash_weight",
                       "long_contrib", "short_contrib", "gross", "net", "spx_return", "sizing",
                       "holdings", "proxy_members", "proxy_missing_weight", "proxy", "by_sector",
                       "flags", "computed_at"]

DEPART_TOL = 0.005
HORIZONS = (("1m", 30), ("3m", 91), ("6m", 182))
MIN_BETA_OBS = 60
PROXY_TEXT = {
    "style": "every company in the book's own box, weighted by market value",
    "market": "every operating company in the panel, weighted by market value, standing in "
              "for the S&P 500",
}


def _r(v, n=10):
    return None if v is None else round(v, n)


def _fmt(rows):
    """Rows with floats written at ten decimal places. engine.append_rows would
    round them to six, which is too coarse for daily returns that are linked."""
    return [{k: (repr(v) if isinstance(v, float) else v) for k, v in r.items()} for r in rows]


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _latest(rows, key):
    out = {}
    for r in rows:
        out[key(r)] = r
    return out


def _same(row, last, skip=("computed_at",)):
    """True when `row`, as it would be written, equals the stored `last`."""
    def cell(v):
        return repr(v) if isinstance(v, float) else E._cell(v)
    return all(cell(row.get(k)) == (last.get(k) or "") for k in row if k not in skip)


class Ctx:
    """What every step reads: the ledger, stored closes, the panel, the filed history."""

    def __init__(self, ledger_dir=None, prices=None, benchmarks=None, panel_dir=None,
                 history=None, events=None, books_dir=None):
        self.ledger_dir = Path(ledger_dir or E.LEDGER_DIR)
        self.books_dir = books_dir
        self.trades = E.read_rows(self.ledger_dir / "trades.csv")
        self.decisions = E.read_rows(self.ledger_dir / "decisions.csv")
        self.prices = prices or E.PriceStore()
        self.bench = benchmarks or E.BenchmarkStore()
        self.panel_dir = panel_dir
        self.dates = E.panel_dates(panel_dir)
        self.history = E.load_style_history(history) if not isinstance(history, dict) else history
        self.events = events
        self._panel, self._classes = {}, {}

    def panel_day(self, day):
        return max((d for d in self.dates if d <= day), default="")

    def rows(self, day):
        pd = self.panel_day(day)
        if pd not in self._panel:
            self._panel[pd] = E.panel_rows(pd, self.panel_dir)[1] if pd else []
        return self._panel[pd]

    def classes(self, day):
        pd = self.panel_day(day)
        if pd not in self._classes:
            rows = self.rows(pd)
            self._classes[pd] = E.classify(rows, self.history, pd) if rows else {}
        return self._classes[pd]

    def mandate(self, book, day):
        hist = [m for m in E.mandate_history(book, self.ledger_dir) if m["date"] <= day]
        if hist:
            return hist[-1]["mandate"]
        return E.load_mandate(book, self.books_dir) or E.default_mandate(book)

    def sessions(self, start, end=None):
        return [d for d in self.dates if d >= start and (end is None or d <= end)]


# ---------------------------------------------------------------------------
# Shadow books


def _is_marker(t):
    return t.get("reason_code") == "shadow_rebalance"


def shadow_trades_for(ctx, book, shadow_rows, today, log=print):
    """New shadow rows for `book`: the inception copy, then one rebalance per date
    the real book traded that the shadow has not processed. Pure: writes nothing."""
    start = E.inception(ctx.trades, book["id"])
    if not start:
        return []
    bid = book["id"]
    mine = [t for t in shadow_rows if t.get("book") == bid]
    done = {t["date"] for t in mine if _is_marker(t)}
    real = [t for t in ctx.trades if t.get("book") == bid]
    new = []
    if start not in done:
        seq = 0
        for t in real:
            if t["date"] != start or not (t["side"] == "deposit" or t.get("reason_code") == "rules_inception"):
                continue
            seq += 1
            new.append(dict(t, trade_id=f"shadow-{bid}-{start}-{seq:03d}",
                            lot_id=f"shadow-{bid}-{start}-{seq:03d}" if t["side"] == "buy" else "",
                            decision_id=f"shadow-{bid}-{start}"))
        new.append(_marker(bid, start, seq + 1))
    dates = sorted({t["date"] for t in real if t["side"] not in ("deposit", "withdraw")
                    and t["date"] > start and t["date"] <= today})
    for d in dates:
        if d in done:
            continue
        new.extend(_rebalance(ctx, book, mine + new, d, log))
    return new


def _marker(bid, day, seq):
    return {"trade_id": f"shadow-{bid}-{day}-{seq:03d}", "date": day, "book": bid, "ticker": "CASH",
            "side": "deposit", "shares": 0, "price": 1, "cost": 0, "lot_id": "",
            "reason_code": "shadow_rebalance", "decision_id": f"shadow-{bid}-{day}"}


def _rebalance(ctx, book, shadow, d, log):
    """Trade the shadow to the rules candidate book at the close of `d`."""
    bid = book["id"]
    mandate = ctx.mandate(bid, d)
    views = E.analyst_views(ctx.events, asof=d)
    classes = ctx.classes(d)
    cand = E.rules_candidate(book, classes, mandate, views,
                             eligible=lambda t: ctx.prices.close(t, d) is not None) if classes else []
    v = E.value_book(shadow, bid, d, ctx.prices)
    nav = v["priced_nav"]
    have = {m["ticker"]: m["shares"] for m in v["marks"]}
    target = {}
    for p in cand:
        px = ctx.prices.close(p["ticker"], d)
        target[p["ticker"]] = E._shares_for(p["weight"] * nav, px)
    rows, seq = [], 0

    def add(tk, side, sh):
        nonlocal seq
        seq += 1
        px = ctx.prices.close(tk, d)
        tid = f"shadow-{bid}-{d}-{seq:03d}"
        rows.append({"trade_id": tid, "date": d, "book": bid, "ticker": tk, "side": side,
                     "shares": sh, "price": px, "cost": round(sh * px * E.COST_RATE, 2),
                     "lot_id": tid if side == "buy" else "", "reason_code": "rules_shadow",
                     "decision_id": f"shadow-{bid}-{d}"})

    for tk in sorted(have):
        want = target.get(tk, 0)
        if have[tk] > want:
            add(tk, "sell", have[tk] - want)
    for tk in sorted(target):
        extra = target[tk] - have.get(tk, 0)
        if extra > 0 and tk not in v["missing"] and tk not in v["basis_break"]:
            add(tk, "buy", extra)
    rows.append(_marker(bid, d, seq + 1))
    if v["missing"] or v["basis_break"]:
        log(f"shadow: {bid} {d}: {', '.join(v['missing'] + v['basis_break'])} had no usable "
            f"close and were left as they were.")
    return rows


def _active_share(bv, sv):
    """0.5 * sum |w_book - w_shadow| over names and cash, both valued at one close."""
    def weights(v):
        nav = v["priced_nav"]
        return {m["ticker"]: m["value"] / nav for m in v["marks"]}, v["cash"] / nav
    wb, cb = weights(bv)
    ws, cs = weights(sv)
    return 0.5 * (sum(abs(wb.get(t, 0.0) - ws.get(t, 0.0)) for t in set(wb) | set(ws)) + abs(cb - cs))


def shadow_nav_rows(ctx, book, shadow_rows, have, today, now):
    """shadow_nav.csv rows to append for one style book."""
    bid = book["id"]
    start = E.inception(ctx.trades, bid)
    if not start:
        return []
    out = []
    prev = None
    for d in ctx.sessions(start, today):
        sv = E.value_book(shadow_rows, bid, d, ctx.prices)
        bv = E.value_book(ctx.trades, bid, d, ctx.prices)
        flags = []
        if sv["partial"]:
            flags.append("shadow partial: no stored close for " + ", ".join(sv["missing"] + sv["basis_break"]))
        if bv["partial"]:
            flags.append("book partial: no stored close for " + ", ".join(bv["missing"] + bv["basis_break"]))
        ok = sv["nav"] is not None and bv["nav"] is not None
        rb = bv["nav"] / bv["capital"] - 1 if ok and bv["capital"] else None
        rs = sv["nav"] / sv["capital"] - 1 if ok and sv["capital"] else None
        rbd = rsd = None
        if ok and prev and prev[0] is not None and prev[1] is not None:
            rbd = (bv["nav"] - (bv["capital"] - prev[2])) / prev[0] - 1
            rsd = (sv["nav"] - (sv["capital"] - prev[3])) / prev[1] - 1
        elif ok and prev:
            flags.append("no complete value on the session before, so no daily figure")
        row = {"date": d, "book": bid, "status": "ok" if ok else "skipped",
               "shadow_nav": _r(sv["nav"], 2), "shadow_priced_nav": _r(sv["priced_nav"], 2),
               "shadow_cash": _r(sv["cash"], 2), "shadow_capital": _r(sv["capital"], 2),
               "shadow_holdings": sv["holdings"], "missing": ";".join(sv["missing"] + bv["missing"]),
               "book_nav": _r(bv["nav"], 2), "book_capital": _r(bv["capital"], 2),
               "book_return_since": _r(rb), "shadow_return_since": _r(rs),
               "value_added_since": _r(rb - rs if rb is not None and rs is not None else None),
               "book_return_day": _r(rbd), "shadow_return_day": _r(rsd),
               "value_added_day": _r(rbd - rsd if rbd is not None and rsd is not None else None),
               "flags": "; ".join(flags), "computed_at": now}
        row["active_share"] = _r(_active_share(bv, sv)) if ok else None
        prev = (bv["nav"], sv["nav"], bv["capital"], sv["capital"])
        last = have.get((bid, d))
        if last and last.get("status") == "ok" and last.get("book_return_day") != "":
            continue
        if last and _same(row, last):
            continue
        out.append(row)
    return out


# ---------------------------------------------------------------------------
# Per-decision scoring


def _weights(v):
    nav = v["priced_nav"]
    return {m["ticker"]: m["value"] / nav for m in v["marks"]} if nav and nav > 0 else {}


def decision_marks(ctx, shadow_rows, have, today, now):
    """decision_marks.csv rows to append: one per departing PM decision per horizon
    that has come due and can be priced."""
    out = []
    kinds = {b["id"]: b["kind"] for b in E.LIVE_BOOKS}
    for dec in ctx.decisions:
        if dec.get("author") != "pm" or dec.get("action") not in ("trade", "rebalance"):
            continue
        bid, d, did = dec.get("book"), dec.get("date"), dec.get("decision_id")
        mine = [t for t in ctx.trades if t.get("decision_id") == did and t["side"] in E.ORDER_SIDES]
        names = sorted({t["ticker"] for t in mine})
        if not names:
            continue
        before = [t for t in ctx.trades if t.get("book") == bid and
                  (t["date"] < d or (t["date"] == d and t.get("decision_id") != did
                                     and ctx.trades.index(t) < ctx.trades.index(mine[0])))]
        after = before + mine
        w_pre = _weights(E.value_book(before, bid, d, ctx.prices))
        w_post = _weights(E.value_book(after, bid, d, ctx.prices))
        if kinds.get(bid) == "style":
            w_rules = _weights(E.value_book([t for t in shadow_rows if t["date"] <= d], bid, d, ctx.prices))
            basis = "rules"
        else:
            w_rules, basis = w_pre, "no trade"
        gap = sum(abs(w_post.get(n, 0.0) - w_rules.get(n, 0.0)) for n in names)
        if gap <= DEPART_TOL:
            continue
        for label, days in HORIZONS:
            if (did, label) in have:
                continue
            target = (datetime.strptime(d, "%Y-%m-%d").date() + timedelta(days=days)).isoformat()
            if target > today:
                continue
            flags = []
            mark = None
            for s in ctx.sessions(target, today):
                gaps = [n for n in names if ctx.prices.close(n, s) is None or ctx.prices.close(n, d) is None]
                if not gaps:
                    mark = s
                    break
                flags.append(f"{s} skipped: no stored close for {', '.join(gaps)}")
            if not mark:
                continue
            r = {n: ctx.prices.close(n, mark) / ctx.prices.close(n, d) - 1 for n in names}
            did_r = sum(w_post.get(n, 0.0) * r[n] for n in names)
            rules_r = sum(w_rules.get(n, 0.0) * r[n] for n in names)
            out.append({"decision_id": did, "book": bid, "date": d, "horizon": label,
                        "target_date": target, "mark_date": mark, "basis": basis,
                        "names": ";".join(names),
                        "weights_book": json.dumps({n: round(w_post.get(n, 0.0), 6) for n in names}, sort_keys=True),
                        "weights_rules": json.dumps({n: round(w_rules.get(n, 0.0), 6) for n in names}, sort_keys=True),
                        "did_return": _r(did_r), "rules_return": _r(rules_r),
                        "value_added": _r(did_r - rules_r), "flags": "; ".join(flags),
                        "computed_at": now})
    return out


# ---------------------------------------------------------------------------
# The PM against the analyst


def _latest_call(events, ticker, day):
    """The newest analyst event for `ticker` dated on or before `day`, or None."""
    out = None
    for e in events:
        if e.get("ticker") == ticker and (e.get("date") or "") <= day:
            out = e
    return out


def _rating(e):
    a = (e.get("action") or "").strip()
    return a or (e.get("direction") or "").strip()


def disagreements(ctx):
    """[(trade, analyst event)] for every PM trade against the analyst's rating."""
    events = E.read_rows(ctx.events) if ctx.events else []
    out = []
    for t in ctx.trades:
        if t["side"] not in ("buy", "sell", "short") or t.get("reason_code") not in ("pm_order", E.OVERRIDE_CODE):
            continue
        view = E.analyst_views(ctx.events, asof=t["date"]).get(t["ticker"]) or {}
        against = ((t["side"] == "buy" and view.get("exclude"))
                   or (t["side"] in ("sell", "short") and view.get("overweight")))
        if against:
            out.append((t, _latest_call(events, t["ticker"], t["date"])))
    return out


def disagreement_marks(ctx, have, today, now):
    """disagreements.csv rows to append: each disagreement at each horizon that has
    come due and can be priced (stock and sector fund closes on both dates)."""
    out = []
    for t, e in disagreements(ctx):
        d, tk = t["date"], t["ticker"]
        sign = 1 if t["side"] == "buy" else -1
        e = e or {}
        horizons = list(HORIZONS)
        try:
            hz = int(float(e.get("horizon_days") or 0))
            if hz:
                end = (datetime.strptime(e["date"], "%Y-%m-%d").date() + timedelta(days=hz))
                horizons.append(("analyst", (end - datetime.strptime(d, "%Y-%m-%d").date()).days))
        except (KeyError, ValueError):
            pass
        sector = next((r.get("sector") or "" for r in ctx.rows(d) if r.get("ticker") == tk), "")
        etf = E.SECTOR_ETFS.get(sector, "")
        for label, days in horizons:
            key = (t["decision_id"], tk, label)
            if key in have or days <= 0:
                continue
            target = (datetime.strptime(d, "%Y-%m-%d").date() + timedelta(days=days)).isoformat()
            if target > today:
                continue
            base = {"decision_id": t["decision_id"], "book": t["book"], "date": d, "ticker": tk,
                    "pm_side": t["side"], "pm_sign": sign, "analyst_call": e.get("event_id", ""),
                    "analyst_rating": _rating(e), "horizon": label, "target_date": target,
                    "sector_etf": etf, "computed_at": now}
            s0, f0 = ctx.prices.close(tk, d), ctx.bench.close(etf, d) if etf else None
            if not s0 or not f0:
                continue       # retried every run; nothing is written for an unpriceable start
            flags, mark = [], None
            for sd in ctx.sessions(target, today):
                s1, f1 = ctx.prices.close(tk, sd), ctx.bench.close(etf, sd)
                if s1 and f1:
                    mark = (sd, s1, f1)
                    break
                flags.append(f"{sd} skipped: no stored close for {tk if not s1 else etf}")
            if not mark:
                continue
            r, rf = mark[1] / s0 - 1, mark[2] / f0 - 1
            ex = sign * (r - rf)
            out.append(dict(base, mark_date=mark[0], stock_return=_r(r), fund_return=_r(rf),
                            pm_excess=_r(ex), pm_right="yes" if ex > 0 else "no",
                            flags="; ".join(flags)))
    return out


def disagreement_summary(rows):
    """Per horizon: count, PM win rate and average PM excess, with the small-sample
    label below MIN_GROUP."""
    out = []
    for label, name in (("1m", "1 month"), ("3m", "3 months"), ("6m", "6 months"),
                        ("analyst", "The analyst's horizon")):
        rs = [r for r in rows if r.get("horizon") == label and E._num(r.get("pm_excess")) is not None]
        n = len(rs)
        wins = sum(1 for r in rs if r.get("pm_right") == "yes")
        out.append({"name": name, "n": n, "wins": wins,
                    "winRate": round(wins / n, 4) if n else None,
                    "meanExcess": round(sum(E._num(r["pm_excess"]) for r in rs) / n, 6) if n else None,
                    "tooFew": n < MIN_GROUP})
    return out


# ---------------------------------------------------------------------------
# Brinson-Fachler


def brinson_fachler(w, r, W, R, cash_weight=0.0, cash_return=0.0, costs=0.0, contrib=None):
    """Sector attribution for one period.

    w, r: the book's sector weights and returns; W, R: the benchmark's, with the
    W adding up to 1. Weights are shares of the book's value, and with the cash
    weight they add up to 1. Cash earns `cash_return`; `costs` are trading costs
    as a share of the book's value. Returns {"sectors": {s: (alloc, sel, inter)},
    "allocation", "selection", "interaction", "cash", "costs", "book", "bench",
    "active"}; the parts add up to book - bench exactly.

    `contrib` ({sector: sum of weight * return}) is for a book with shorts, where a
    sector's net weight can be near zero and r_s = contrib / w_s means nothing.
    Then selection_s = contrib_s - w_s * R_s and interaction is reported inside
    it (it is zero)."""
    Rb = sum(W[s] * R[s] for s in W)
    sectors = {}
    for s in set(w) | set(W) | set(contrib or {}):
        ws, Ws = w.get(s, 0.0), W.get(s, 0.0)
        Rs = R.get(s, Rb) if Ws else Rb
        alloc = (ws - Ws) * (Rs - Rb)
        if contrib is not None:
            sel, inter = contrib.get(s, 0.0) - ws * Rs, 0.0
        else:
            rs = r.get(s, Rs) if ws else Rs
            sel, inter = Ws * (rs - Rs), (ws - Ws) * (rs - Rs)
        sectors[s] = (alloc, sel, inter)
    held = (sum(contrib.values()) if contrib is not None
            else sum(w[s] * r.get(s, 0.0) for s in w))
    book = held + cash_weight * cash_return - costs
    return {"sectors": sectors, "allocation": sum(v[0] for v in sectors.values()),
            "selection": sum(v[1] for v in sectors.values()),
            "interaction": sum(v[2] for v in sectors.values()),
            "cash": cash_weight * (cash_return - Rb), "costs": -costs,
            "book": book, "bench": Rb, "active": book - Rb}


def _proxy(ctx, book, day):
    """{ticker: (weight, sector)} of the proxy benchmark on `day`."""
    classes = ctx.classes(day)
    if book["kind"] == "style":
        box = f"{book['size']}-{book['style']}"
        members = {t: i for t, i in classes.items() if i.get("box") == box}
    else:
        members = {t: i for t, i in classes.items() if i.get("size")}
    tot = sum(i["market_cap"] for i in members.values() if i.get("market_cap"))
    return {t: (i["market_cap"] / tot, i.get("sector") or "Unclassified")
            for t, i in members.items() if i.get("market_cap")} if tot else {}


def attribution_row(ctx, book, p, d, now):
    """The attribution of one book's return from the close of `p` to the close of `d`."""
    bid = book["id"]
    base = {"date": d, "book": bid, "prev_date": p, "computed_at": now,
            "proxy": PROXY_TEXT["style" if book["kind"] == "style" else "market"],
            "fund": book.get("benchmark", "")}
    v = E.value_book(E.trades_for(ctx.trades, bid, p), bid, p, ctx.prices)
    flags = []
    if v["partial"]:
        flags.append(f"no usable close on {p} for " + ", ".join(v["missing"] + v["basis_break"]))
    nav = v["priced_nav"]
    sectors_of = {r["ticker"]: (r.get("sector") or "Unclassified") for r in ctx.rows(p)}
    pos = []
    for m in v["marks"]:
        c1 = ctx.prices.close(m["ticker"], d)
        if c1 is None:
            flags.append(f"no stored close for {m['ticker']} on {d}")
            continue
        pos.append((m["ticker"], m["value"] / nav, c1 / m["close"] - 1, sectors_of.get(m["ticker"], "Unclassified")))
    if flags or not nav or nav <= 0:
        return dict(base, status="skipped", flags="; ".join(flags) or "no value on the session before")
    proxy = _proxy(ctx, book, p)
    W, R, miss, members = {}, {}, 0.0, 0
    for t, (wt, sec) in proxy.items():
        a, b = ctx.prices.close(t, p), ctx.prices.close(t, d)
        if not a or not b:
            miss += wt
            continue
        members += 1
        W[sec] = W.get(sec, 0.0) + wt
        R[sec] = R.get(sec, 0.0) + wt * (b / a - 1)
    kept = sum(W.values())
    if not kept:
        return dict(base, status="skipped", flags=f"the proxy has no member with stored closes on {p} and {d}")
    for s in W:
        R[s] /= W[s]
        W[s] /= kept
    w, contrib = {}, {}
    for t, wt, ret, sec in pos:
        w[sec] = w.get(sec, 0.0) + wt
        contrib[sec] = contrib.get(sec, 0.0) + wt * ret
    shorts = any(wt < 0 for _, wt, _, _ in pos)
    r = {s: contrib[s] / w[s] for s in w if abs(w[s]) > 1e-12}
    costs = sum(float(t.get("cost") or 0) for t in ctx.trades
                if t.get("book") == bid and t["date"] == d) / nav
    cw = v["cash"] / nav
    bf = brinson_fachler(w, r, W, R, cash_weight=cw, costs=costs,
                         contrib=contrib if shorts else None)
    fr = None
    a, b = ctx.bench.close(book.get("benchmark", ""), p), ctx.bench.close(book.get("benchmark", ""), d)
    if a and b:
        fr = b / a - 1
    else:
        flags.append(f"no stored {book.get('benchmark')} close on {p if not a else d}, so no proxy error")
    ma, mb = ctx.bench.close("^GSPC", p), ctx.bench.close("^GSPC", d)
    spx = mb / ma - 1 if ma and mb else None
    n = len(pos)
    gross = sum(abs(wt) for _, wt, _, _ in pos)
    sizing = sum((wt - (1 if wt > 0 else -1) * gross / n) * ret for _, wt, ret, _ in pos) if n else 0.0
    by = {s: [round(w.get(s, 0.0), 6), round(W.get(s, 0.0), 6), _r(r.get(s), 6), _r(R.get(s), 6)]
          + [round(x, 8) for x in bf["sectors"][s]] for s in sorted(bf["sectors"])}
    return dict(base, status="ok",
                book_return=_r(bf["book"]), proxy_return=_r(bf["bench"]), fund_return=_r(fr),
                proxy_error=_r(bf["bench"] - fr if fr is not None else None),
                allocation=_r(bf["allocation"]), selection=_r(bf["selection"]),
                interaction=_r(bf["interaction"]), cash_drag=_r(bf["cash"]), costs=_r(bf["costs"]),
                interaction_folded="1" if shorts else "", cash_weight=_r(cw),
                long_contrib=_r(sum(wt * ret for _, wt, ret, _ in pos if wt > 0)),
                short_contrib=_r(sum(wt * ret for _, wt, ret, _ in pos if wt < 0)),
                gross=_r(v["gross"]), net=_r(v["net"]), spx_return=_r(spx), sizing=_r(sizing),
                holdings=n, proxy_members=members, proxy_missing_weight=_r(miss),
                by_sector=json.dumps(by, sort_keys=True, separators=(",", ":")),
                flags="; ".join(flags))


def attribution_rows(ctx, book, have, today, now):
    bid = book["id"]
    start = E.inception(ctx.trades, bid)
    if not start:
        return []
    ss = ctx.sessions(start, today)
    out = []
    for p, d in zip(ss, ss[1:]):
        last = have.get((bid, d))
        if last and last.get("status") == "ok" and last.get("fund_return") not in ("", None):
            continue
        row = attribution_row(ctx, book, p, d, now)
        if last and _same(row, last):
            continue
        out.append(row)
    return out


# ---------------------------------------------------------------------------
# Linking and the site's summary


def _k(r, b):
    if abs(r - b) < 1e-15:
        return 1.0 / (1.0 + r)
    return (math.log1p(r) - math.log1p(b)) / (r - b)


def carino(rows, parts, rkey="book_return", bkey="proxy_return"):
    """Carino-linked totals of `parts` over `rows` (each with numeric r, b and parts).
    Returns (R, B, {part: linked}); the linked parts add up to R - B."""
    R = math.prod(1 + x[rkey] for x in rows) - 1 if rows else 0.0
    B = math.prod(1 + x[bkey] for x in rows) - 1 if rows else 0.0
    K = _k(R, B)
    out = {p: sum(x[p] * _k(x[rkey], x[bkey]) / K for x in rows) for p in parts}
    return R, B, out


def _floats(r, keys):
    out = dict(r)
    for k in keys:
        out[k] = E._num(r.get(k))
    return out


def book_summary(book, attr_rows, shadow_rows, dmarks, sessions_since=None):
    """What the book page and the board show about one book."""
    bid = book["id"]
    keys = ["book_return", "proxy_return", "fund_return", "proxy_error", "allocation", "selection",
            "interaction", "cash_drag", "costs", "long_contrib", "short_contrib", "gross", "net",
            "spx_return", "sizing", "cash_weight"]
    rows = [_floats(r, keys) for r in _latest(attr_rows, lambda r: (r["book"], r["date"])).values()
            if r.get("book") == bid]
    rows.sort(key=lambda r: r["date"])
    ok = [r for r in rows if r.get("status") == "ok" and r["book_return"] is not None]
    skipped = [{"date": r["date"], "why": r.get("flags") or ""} for r in rows if r.get("status") != "ok"]
    out = {"proxy": PROXY_TEXT["style" if book["kind"] == "style" else "market"],
           "days": len(ok), "skipped": skipped, "sessions": len(rows)}
    if ok:
        parts = ["allocation", "selection", "interaction", "cash_drag", "costs"]
        R, B, linked = carino(ok, parts)
        out.update({"bookReturn": R, "proxyReturn": B, "active": R - B,
                    "from": ok[0]["prev_date"], "to": ok[-1]["date"],
                    "folded": any(r.get("interaction_folded") for r in ok)})
        out.update(linked)
        both = [r for r in ok if r["fund_return"] is not None]
        if both:
            pb = math.prod(1 + r["proxy_return"] for r in both) - 1
            fb = math.prod(1 + r["fund_return"] for r in both) - 1
            out.update(proxyError=pb - fb, proxyErrorDays=len(both), fundReturn=fb, proxyOnFundDays=pb)
        secs = {}
        for r in ok:
            k = _k(r["book_return"], r["proxy_return"]) / _k(R, B)
            for s, v in json.loads(r.get("by_sector") or "{}").items():
                a = secs.setdefault(s, [0.0, 0.0, 0.0, 0.0, 0.0])
                a[0] += v[4] * k
                a[1] += v[5] * k
                a[2] += v[6] * k
                a[3], a[4] = v[0], v[1]     # latest weights
        out["bySector"] = sorted(([s] + [round(x, 6) for x in v] for s, v in secs.items()),
                                 key=lambda x: -abs(x[1] + x[2] + x[3]))
        eq = math.prod(1 + r["book_return"] - r["sizing"] for r in ok) - 1
        out["sizing"] = R - eq
        out["equalWeightReturn"] = eq
        if book["kind"] != "style":
            legs = [dict(r, zero=0.0) for r in ok]
            _, _, lk = carino(legs, ["long_contrib", "short_contrib", "costs"], "book_return", "zero")
            out.update(longLeg=lk["long_contrib"], shortLeg=lk["short_contrib"], legCosts=lk["costs"])
            out["exposure"] = [[r["date"], r["gross"], r["net"]] for r in ok]
            pairs = [(r["book_return"], r["spx_return"]) for r in ok if r["spx_return"] is not None]
            out["betaObs"] = len(pairs)
            if len(pairs) >= MIN_BETA_OBS:
                mx = sum(m for _, m in pairs) / len(pairs)
                my = sum(x for x, _ in pairs) / len(pairs)
                var = sum((m - mx) ** 2 for _, m in pairs) / len(pairs)
                cov = sum((x - my) * (m - mx) for x, m in pairs) / len(pairs)
                if var > 0:
                    beta = cov / var
                    Rp = math.prod(1 + x for x, _ in pairs) - 1
                    Rm = math.prod(1 + m for _, m in pairs) - 1
                    out.update(beta=beta, betaAdjusted=Rp - beta * Rm, spxReturn=Rm)
    if book["kind"] == "style":
        srows = [r for r in _latest(shadow_rows, lambda r: (r["book"], r["date"])).values()
                 if r.get("book") == bid and r.get("status") == "ok"]
        srows.sort(key=lambda r: r["date"])
        if srows:
            last = srows[-1]
            out["shadow"] = {"date": last["date"], "valueAdded": E._num(last.get("value_added_since")),
                             "shadowReturn": E._num(last.get("shadow_return_since")),
                             "bookReturn": E._num(last.get("book_return_since")),
                             "valueAddedDay": E._num(last.get("value_added_day")),
                             "activeShare": E._num(last.get("active_share")),
                             "series": [[r["date"], E._num(r.get("value_added_since"))] for r in srows]}
    out["decisions"] = [{k: d.get(k) for k in ("decision_id", "date", "horizon", "mark_date", "basis",
                                                 "names", "did_return", "rules_return", "value_added",
                                                 "flags")}
                        for d in dmarks if d.get("book") == bid]
    return out


# ---------------------------------------------------------------------------
# The daily step


def run_daily(ctx, today=None, data_dir=None, now=None, log=print):
    """Append what is new to the four files. Returns {file: rows appended}."""
    today = today or datetime.now(timezone.utc).date().isoformat()
    now = now or _now()
    data_dir = Path(data_dir or DATA)
    paths = {"shadow_trades": data_dir / "shadow_trades.csv", "shadow_nav": data_dir / "shadow_nav.csv",
             "decision_marks": data_dir / "decision_marks.csv", "attribution": data_dir / "attribution.csv",
             "disagreements": data_dir / "disagreements.csv"}
    counts = {}
    shadow = E.read_rows(paths["shadow_trades"])
    new_shadow = []
    for b in E.STYLE_BOOKS:
        new_shadow.extend(shadow_trades_for(ctx, b, shadow + new_shadow, today, log))
    counts["shadow_trades"] = E.append_rows(paths["shadow_trades"], E.TRADE_COLUMNS, new_shadow)
    shadow = shadow + new_shadow
    have = _latest(E.read_rows(paths["shadow_nav"]), lambda r: (r["book"], r["date"]))
    rows = []
    for b in E.STYLE_BOOKS:
        rows.extend(shadow_nav_rows(ctx, b, shadow, have, today, now))
    counts["shadow_nav"] = E.append_rows(paths["shadow_nav"], SHADOW_NAV_COLUMNS, _fmt(rows))
    have = {(r["decision_id"], r["horizon"]) for r in E.read_rows(paths["decision_marks"])}
    counts["decision_marks"] = E.append_rows(paths["decision_marks"], DECISION_MARK_COLUMNS,
                                             _fmt(decision_marks(ctx, shadow, have, today, now)))
    have = {(r["decision_id"], r["ticker"], r["horizon"]) for r in E.read_rows(paths["disagreements"])}
    counts["disagreements"] = E.append_rows(paths["disagreements"], DISAGREEMENT_COLUMNS,
                                            _fmt(disagreement_marks(ctx, have, today, now)))
    have = _latest(E.read_rows(paths["attribution"]), lambda r: (r["book"], r["date"]))
    rows = []
    for b in E.LIVE_BOOKS:
        rows.extend(attribution_rows(ctx, b, have, today, now))
    counts["attribution"] = E.append_rows(paths["attribution"], ATTRIBUTION_COLUMNS, _fmt(rows))
    log("pm scoring: appended " + ", ".join(f"{v} {k.replace('_', ' ')} row(s)" for k, v in counts.items()) + ".")
    return counts


def site_summary(data_dir=None):
    """{book id: book_summary} from the stored files."""
    data_dir = Path(data_dir or DATA)
    attr = E.read_rows(data_dir / "attribution.csv")
    shadow = E.read_rows(data_dir / "shadow_nav.csv")
    dm = E.read_rows(data_dir / "decision_marks.csv")
    return {b["id"]: book_summary(b, attr, shadow, dm) for b in E.LIVE_BOOKS}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--prices-dir", default=None)
    ap.add_argument("--dry-run", action="store_true", help="print the summaries, write nothing")
    a = ap.parse_args(argv)
    if not a.dry_run:
        ctx = Ctx(prices=E.PriceStore(a.prices_dir), benchmarks=E.BenchmarkStore(a.prices_dir))
        run_daily(ctx)
    print(json.dumps(site_summary(), indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
