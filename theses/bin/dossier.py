#!/usr/bin/env python3
"""Assemble everything known about one ticker into a markdown dossier.

The thesis writer sees this and nothing else, so anything it must not assume has
to be stated here: how old each number is, what the news filter threw away, and
which fields are missing outright. Silence reads as confidence.

Usage: dossier.py TICKER [--panel-cache FILE]
"""
import csv, io, json, math, re, sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (SLEEVES, THESES, RAW, PAGES, fetch, num, load_panel,
                    read_csv_rows, CONTAMINATED, NEWS_FIX_DATE)
import screen
import tensions

MAX_FILING_CHARS = 34000      # ~8.5K tokens; the largest single block here
MAX_NEWS = 10

STALENESS = [("prices_updated", 4, "price and every return derived from it"),
             ("last_updated", 3, "vendor fields: EV multiples, ownership, analyst count"),
             ("edgar_updated", 10, "TTM financials and the balance sheet"),
             ("insider_updated", 14, "insider buy/sell signal")]


# --- sector lens ------------------------------------------------------------
#
# A refiner, a bank and a SaaS company do not answer the same questions, and
# several fields in the panel mean different things or nothing at all depending
# on which of those you are looking at. ev_ebitda is not a concept for a bank.
# A low pe on a cyclical at the top of its cycle is the most expensive kind of
# cheap. Without this the agent applies one template to every name.
#
# Keyed on GICS sector, which is a column on every row, so this is a dictionary
# lookup and not a retrieval problem. There is nothing to embed and nothing that
# can come back wrong: either the file exists for that sector or it does not.
LENSES = THESES / "lenses"


def sector_lens(sector):
    """The lens body for a sector, or "" when none is written."""
    if not sector:
        return ""
    slug = re.sub(r"[^a-z0-9]+", "-", sector.lower()).strip("-")
    path = LENSES / f"{slug}.md"
    if not path.exists():
        return ""
    try:
        return path.read_text(encoding="utf-8").strip()
    except Exception:
        return ""


def sector_field_warnings(sector):
    """[(field, severity, why, instead)] for fields that mislead in this sector."""
    path = LENSES / "_fields.json"
    if not path.exists() or not sector:
        return []
    try:
        blob = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    return blob.get(sector) or []


def age_days(stamp, panel_date):
    if not stamp:
        return None
    try:
        s = datetime.strptime(stamp[:10], "%Y-%m-%d").date()
        p = datetime.strptime(panel_date, "%Y-%m-%d").date()
        return (p - s).days
    except ValueError:
        return None


def pct_rank(value, cohort_values):
    if value is None or len(cohort_values) < 8:
        return None
    below = sum(1 for v in cohort_values if v < value)
    return round(100.0 * below / len(cohort_values))


# Counts, not ratios. Without this list a value of 2 sellers renders as +200.0%,
# which is not a rounding nit: it is a number the thesis writer would reason from.
_COUNT_FIELDS = {"analyst_count", "insider_buyer_count_90d", "insider_seller_count_90d",
                 "insider_tx_count_90d", "insider_cluster_max_30d", "volume",
                 "shares_outstanding"}
# Per-share dollars. Without this an EPS of 2.08 falls into the ratio branch and
# renders as "+208.0%", which is not a cosmetic problem: it is a number the
# thesis writer would compute a P/E from.
_PERSHARE_FIELDS = {"ttm_eps_diluted", "prior_ttm_eps_diluted"}
_MONEY_FIELDS = {"market_cap", "ttm_revenue", "ttm_ebitda", "ttm_net_income", "ttm_fcf",
                 "ttm_gross_profit", "ttm_operating_income", "total_debt", "equity",
                 "cash_and_investments", "insider_net_buy_90d"}


def fmt(v, field):
    if v is None:
        return "—"
    if field in _MONEY_FIELDS:
        a = abs(v)
        if a >= 1e12:
            return f"${v/1e12:,.2f}T"
        if a >= 1e9:
            return f"${v/1e9:,.1f}B"
        if a >= 1e6:
            return f"${v/1e6:,.1f}M"
        return f"${v:,.0f}"
    if field in _PERSHARE_FIELDS:
        return f"${v:,.2f}"
    if field in _COUNT_FIELDS:
        return f"{v:,.0f}"
    if field in ("price", "pe", "ev_ebitda", "ev_revenue", "price_book",
                 "net_debt_ebitda", "beta_1y", "sharpe_1y"):
        return f"{v:,.2f}"
    if abs(v) < 3:
        return f"{v*100:+.1f}%"
    return f"{v:,.2f}"


def news_for(ticker, name):
    """Re-derive company news with a relevance filter applied here.

    The panel's own news columns cannot be used: before 2026-09-12 the fetcher
    applied no relevance check at all, and the headlines that produced those
    numbers were never archived, so they can be neither audited nor repaired.
    Filtering at read time is the only way to know what was kept."""
    raw = fetch(f"{PAGES}/news/{ticker}.json")
    if not raw:
        return [], 0
    try:
        data = json.loads(raw)
    except Exception:
        return [], 0
    items = data if isinstance(data, list) else data.get("items", [])
    kept = []
    for it in items:
        title = (it.get("title") or "")
        if _relevant(title, ticker, name):
            kept.append(it)
    return kept[:MAX_NEWS], len(items)


def _relevant(title, ticker, name):
    """Same shape as the pipeline's filter, kept local so the dossier does not
    depend on importing an 11,000-line module."""
    if not title:
        return False
    esc = re.escape(ticker)
    if re.search(r"(?:\$" + esc + r"|\(" + esc + r"\)|:\s*" + esc + r")(?![A-Za-z0-9])", title):
        return True
    if len(ticker) >= 3 and re.search(r"(?<![A-Za-z0-9])" + esc + r"(?![A-Za-z0-9])", title):
        return True
    clean = re.sub(r"\b(Inc|Corp|Corporation|Ltd|Plc|Holdings|Group|Co)\b\.?", "", name or "").strip(" ,.")
    if len(clean) >= 2 and re.search(r"(?<![A-Za-z0-9])" + re.escape(clean) + r"(?![A-Za-z0-9])", title):
        return True
    toks = [t for t in re.split(r"[^A-Za-z]+", clean.lower()) if len(t) > 2]
    if not toks:
        return False
    low = title.lower()
    hits = sum(1 for t in set(toks) if re.search(r"\b" + re.escape(t) + r"\b", low))
    return hits >= 2 if len(set(toks)) >= 2 else (hits >= 1 and len(toks[0]) >= 5)


def price_block(ticker):
    raw = fetch(f"{PAGES}/prices/{ticker}.json")
    if not raw:
        return None
    try:
        closes = json.loads(raw).get("closes", [])
    except Exception:
        return None
    if len(closes) < 30:
        return None
    vals = [c[1] for c in closes if isinstance(c, list) and len(c) == 2]
    if not vals:
        return None
    hi, lo, last = max(vals), min(vals), vals[-1]
    return {"n": len(vals), "first_date": closes[0][0], "last_date": closes[-1][0],
            "high": hi, "low": lo, "last": last,
            "pos": (last - lo) / (hi - lo) if hi > lo else None}


def reported_history(ticker):
    """Annual and quarterly reported periods for this ticker."""
    raw = fetch(f"{RAW}/data/financials/reported.csv")
    if not raw:
        return [], []
    rows = [r for r in csv.DictReader(io.StringIO(raw.decode("utf-8", "replace")))
            if r.get("ticker") == ticker]
    fy = sorted([r for r in rows if r.get("period") == "FY"], key=lambda r: r["period_end"])
    q = sorted([r for r in rows if r.get("period") == "Q"], key=lambda r: r["period_end"])
    return fy, q


def peer_share(row, scored):
    """Revenue share within the sub-industry, across public filers in the panel.

    This is not market share. It excludes private companies, foreign issuers that
    do not file here, and any competitor sitting in a different sub-industry. It
    is a floor on concentration among listed peers and should be read as nothing
    more. The real number needs industry data this project does not have."""
    sub = (row.get("sub_industry") or "").strip()
    if not sub:
        return None
    peers = [r for r in scored if (r.get("sub_industry") or "").strip() == sub
             and num(r.get("ttm_revenue"))]
    total = sum(num(r.get("ttm_revenue")) for r in peers)
    mine = num(row.get("ttm_revenue"))
    if not total or not mine or len(peers) < 3:
        return None
    ranked = sorted(peers, key=lambda r: -(num(r.get("ttm_revenue")) or 0))
    rank = next((i + 1 for i, r in enumerate(ranked) if r["ticker"] == row["ticker"]), None)
    return {"share": mine / total, "rank": rank, "n": len(peers),
            "leader": ranked[0]["ticker"], "leader_share": (num(ranked[0].get("ttm_revenue")) or 0) / total}


def filings_for(ticker):
    """Latest collected document of each kind for this ticker.

    Returns {doc_kind: (index_row, text)}. Kinds today are earnings_release
    (the 8-K EX-99.1) and segment_note (the segment note from the last periodic
    filing). The segment note is here because three of the first four theses
    named the segment split as the thing they could not see."""
    today = datetime.now(tz=timezone.utc).date()
    rows = []
    for m in (today.strftime("%Y-%m"),
              (today.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")):
        raw = fetch(f"{RAW}/data/filings/{m}.csv")
        if raw:
            rows += [r for r in csv.DictReader(io.StringIO(raw.decode("utf-8", "replace")))
                     if r.get("ticker") == ticker]
    out = {}
    for kind in {r.get("doc_kind") or "earnings_release" for r in rows}:
        of_kind = [r for r in rows if (r.get("doc_kind") or "earnings_release") == kind]
        row = max(of_kind, key=lambda r: r.get("filed", ""))
        text = fetch(f"{RAW}/data/filings/{row.get('text_path', '')}")
        out[kind] = (row, text.decode("utf-8", "replace")[:MAX_FILING_CHARS] if text else None)
    return out


def latest_filing(ticker):
    """Most recent earnings release text collected by the pipeline."""
    today = datetime.now(tz=timezone.utc).date()
    for m in (today.strftime("%Y-%m"),
              (today.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")):
        raw = fetch(f"{RAW}/data/filings/{m}.csv")
        if not raw:
            continue
        rows = [r for r in csv.DictReader(io.StringIO(raw.decode("utf-8", "replace")))
                if r.get("ticker") == ticker]
        if not rows:
            continue
        row = max(rows, key=lambda r: r.get("filed", ""))
        text = fetch(f"{RAW}/data/filings/{row.get('text_path', '')}")
        if text:
            return row, text.decode("utf-8", "replace")[:MAX_FILING_CHARS]
        return row, None
    return None, None


def main():
    if len(sys.argv) < 2:
        print("usage: dossier.py TICKER", file=sys.stderr)
        return 2
    ticker = sys.argv[1].upper()

    panel_date, rows = load_panel()
    core = screen.core_universe(rows)
    scored = screen.score(core)
    me = next((r for r in scored if r["ticker"] == ticker), None)
    if me is None:
        raw = next((r for r in rows if r.get("ticker") == ticker), None)
        if raw is None:
            print(f"# {ticker}\n\nNot in the panel for {panel_date}.")
            return 1
        me = dict(raw)
        me.update({"_peer": (raw.get("sector") or "").strip(), "_peer_n": 0,
                   "_composite": None, "_scorable": False,
                   "_sleeves": {k: None for k in SLEEVES},
                   "_sleeve_n": {k: 0 for k in SLEEVES},
                   "_ev_ebitda": screen.recompute_ev_ebitda(raw)})
        gate_note = ("**This name fails the screen's own gate** (needs market cap over $1B, "
                     "a sector, and revenue). It is here because it is on the watchlist. "
                     "Peer comparisons below are unavailable or thin.")
    else:
        gate_note = ""

    peers = [r for r in scored if r["_peer"] == me["_peer"]]
    name = me.get("name", ticker)
    out = []
    w = out.append

    w(f"# {ticker} — {name}")
    w("")
    w(f"{me.get('sector','—')} / {me.get('sub_industry','—')} · {me.get('index','—')}")
    w(f"Panel date **{panel_date}** · peer group **{me['_peer']}** (n={me['_peer_n']})")
    if me.get("_composite") is not None:
        sl = " ".join(f"{k[0]}{v:+.2f}" if v is not None else f"{k[0]}—"
                      for k, v in me["_sleeves"].items())
        w(f"Composite z **{me['_composite']:+.2f}** · {sl}")
    if gate_note:
        w("")
        w(gate_note)

    # --- the question, before any of the numbers ------------------------------
    found = tensions.detect(me, [r for r in scored if r["_scorable"]]) if me.get("_scorable") else []
    w("")
    w("## What is worth asking about this name")
    w("")
    if found:
        w("Places where this company's own data disagrees with itself. These are questions, "
          "not signals, and none of them is a reason to buy or sell anything on its own. "
          "A tension that turns out to have a dull explanation is a finished piece of work.")
        for t in found:
            w("")
            w(f"**{t['headline']}**  ")
            w(f"`{t['evidence']}`")
            w("")
            w(f"> {t['question']}")
    else:
        w("No tension detected: this name's factors broadly agree with each other. That is "
          "common and is not a criticism of the company. It does mean there is no obvious "
          "question to start from, and a thesis will have to come from the filings or from "
          "somewhere outside this dossier.")

    # --- how to read this kind of business ------------------------------------
    lens = sector_lens(me.get("sector"))
    warns = sector_field_warnings(me.get("sector"))
    if warns:
        w("")
        w("## Fields that mislead in this sector")
        w("")
        w("Read these before the factor table, not after. A field listed here is not "
          "merely noisy for this kind of business: it is measuring something other than "
          "what its name suggests, and quoting it as evidence would be a defect in the "
          "note rather than a difference of opinion.")
        w("")
        w("| Field | | Why | Look at instead |")
        w("|---|---|---|---|")
        for x in warns:
            w(f"| `{x.get('field','')}` | {x.get('severity','')} | {x.get('why','')} "
              f"| {x.get('instead','') or '—'} |")
    if lens:
        w("")
        w("## How to read a company in this sector")
        w("")
        w(lens)

    # --- freshness, because every number below inherits it --------------------
    w("")
    w("## How current is this")
    w("")
    w("| Block | Stamp | Age | Covers |")
    w("|---|---|---|---|")
    caveats = []
    for field, limit, covers in STALENESS:
        stamp = (me.get(field) or "").strip()
        a = age_days(stamp, panel_date)
        if not stamp:
            flag = "**not collected**"
            if field == "insider_updated":
                caveats.append("insider data is not collected for this name "
                               "(Form 4 is fetched only for the largest ~600 by market cap); "
                               "absence is not a signal")
            else:
                caveats.append(f"{field} is absent, so {covers} cannot be dated")
        else:
            flag = f"{a}d" if a is not None else "?"
            if a is not None and a > limit:
                flag = f"**{a}d, stale**"
                caveats.append(f"{covers} is {a} days old ({field} = {stamp})")
        w(f"| `{field}` | {stamp or '—'} | {flag} | {covers} |")

    # --- the factor panel ----------------------------------------------------
    w("")
    w("## Factors, against peers")
    w("")
    w("Percentile is within the peer group above; higher is better after inversion.")
    for sleeve_name, sleeve in SLEEVES.items():
        score_v = me["_sleeves"].get(sleeve_name)
        n = me["_sleeve_n"].get(sleeve_name, 0)
        head = f"{score_v:+.2f}" if score_v is not None else f"not scored, only {n}/5 fields"
        w("")
        w(f"### {sleeve_name} — {head}")
        w("")
        w("| Field | Value | Peer pct | Note |")
        w("|---|---|---|---|")
        for f in sleeve["fields"]:
            v = screen.value_of(me, f)
            cohort = [x for x in (screen.value_of(p, f) for p in peers) if x is not None]
            pr = pct_rank(v, cohort)
            if f in sleeve["invert"] and pr is not None:
                pr = 100 - pr
            note = ""
            if f == "ev_ebitda":
                note = "recomputed; the panel's own column does not reconcile"
            elif f == "op_margin_stability":
                note = "std dev of quarterly op margin; lower is steadier"
            elif f == "accruals_ratio":
                note = "Sloan accruals; high means earnings are not cash-backed"
            elif f == "return_12_2":
                note = "12-month return skipping the last month"
            w(f"| `{f}` | {fmt(v, f)} | {pr if pr is not None else '—'} | {note} |")

    # --- size, risk ----------------------------------------------------------
    w("")
    w("## Size and risk")
    w("")
    w("| Field | Value | | Field | Value |")
    w("|---|---|---|---|---|")
    pairs = [("market_cap", "beta_1y"), ("price", "volatility_1y"),
             ("shares_outstanding", "sharpe_1y"), ("ttm_revenue", "max_drawdown_1y"),
             ("ttm_eps_diluted", "high52w_proximity"), ("analyst_count", "return_52w")]
    for a, b in pairs:
        w(f"| `{a}` | {fmt(num(me.get(a)), a)} | | `{b}` | {fmt(num(me.get(b)), b)} |")

    # --- insider, only when it is real ---------------------------------------
    ins_age = age_days((me.get("insider_updated") or "").strip(), panel_date)
    w("")
    w("## Insider activity")
    w("")
    if ins_age is None or ins_age > 14:
        w("Not collected for this name. Form 4 is fetched only for roughly the largest 600 "
          "by market cap, so **absence here is not evidence of anything**.")
    else:
        for f in ("insider_net_buy_90d", "insider_buyer_count_90d",
                  "insider_seller_count_90d", "insider_cluster_max_30d"):
            w(f"- `{f}` = {fmt(num(me.get(f)), f)}")
        w("")
        w("A cluster of three or more distinct buyers in 30 days is the Seyhun signal; "
          "one insider buying is noise.")

    # --- price history -------------------------------------------------------
    pb = price_block(ticker)
    w("")
    w("## Price")
    w("")
    panel_px = num(me.get("price"))
    if pb:
        pos = f"{pb['pos']*100:.0f}% of range" if pb["pos"] is not None else "—"
        w(f"**Latest close ${pb['last']:,.2f}** ({pb['last_date']}). "
          f"52-week range ${pb['low']:,.2f} to ${pb['high']:,.2f}, {pos}. "
          f"{pb['n']} daily closes from {pb['first_date']}.")
        # The panel's price column is frozen for most of the universe: on
        # 2026-09-11, 4,703 of 5,340 tickers carried an identical price across
        # three consecutive panel dates, because enrich_with_prices caches each
        # ticker's file for 24h and the panel reads that file's last close. The
        # prices_updated stamp advances anyway, so the row looks fresh. The
        # close series is therefore the price of record here.
        if panel_px and abs(pb["last"] / panel_px - 1) > 0.005:
            drift = (pb["last"] / panel_px - 1) * 100
            w("")
            w(f"**The panel's `price` is stale: {fmt(panel_px, 'price')} against a "
              f"{drift:+.1f}% move to ${pb['last']:,.2f}.** The panel caches prices for 24 "
              f"hours and stamps `prices_updated` on the attempt rather than on a new value, "
              f"so the row reads fresh when it is not. **Use ${pb['last']:,.2f}.**")
            eps = num(me.get("ttm_eps_diluted"))
            sh = num(me.get("shares_outstanding"))
            parts = []
            if eps and eps > 0:
                parts.append(f"P/E {pb['last']/eps:,.1f} (panel says {fmt(num(me.get('pe')), 'pe')})")
            if sh:
                parts.append(f"market cap {fmt(pb['last']*sh, 'market_cap')} "
                             f"(panel says {fmt(num(me.get('market_cap')), 'market_cap')})")
            if parts:
                w("")
                w("Recomputed at the live close: " + "; ".join(parts) + ".")
            caveats.append(f"the panel's price was {drift:+.1f}% stale; every panel field "
                           f"derived from price (market_cap, pe, high52w_proximity) inherits that")
    else:
        w("No close series available. The panel's `price` of "
          f"{fmt(panel_px, 'price')} is all there is, and it may be stale.")
        caveats.append("no close series, so the panel price could not be checked for staleness")

    # --- news ----------------------------------------------------------------
    items, total = news_for(ticker, name)
    w("")
    w("## Recent headlines")
    w("")
    w(f"Kept **{len(items)} of {total}** after filtering for relevance. "
      f"The panel's own news columns are not used here: before {NEWS_FIX_DATE} the fetcher "
      f"applied no relevance check, and the headlines behind those numbers were never "
      f"archived, so they cannot be audited.")
    w("")
    if items:
        for it in items:
            src = (it.get("source") or "").strip()
            w(f"- {it.get('title','').strip()}" + (f" — *{src}*" if src else ""))
    else:
        w("No reliable company news. Treat this as absence of evidence, not evidence of "
          "absence: the filter may simply have rejected everything the query returned.")
        caveats.append("no relevance-passing headlines were available")

    # --- reported history -----------------------------------------------------
    fy, qh = reported_history(ticker)
    w("")
    w("## Reported history")
    w("")
    if fy:
        w(f"{len(fy)} fiscal years from filings, {fy[0]['period_end'][:4]} to "
          f"{fy[-1]['period_end'][:4]}. The panel itself holds five dates and cannot "
          f"describe a cycle; this can.")
        w("")
        w("| FY | Revenue | Op income | Net income | EPS | OCF | Capex |")
        w("|---|---|---|---|---|---|---|")
        for r in fy[-10:]:
            g = lambda k: num(r.get(k))
            w(f"| {r['period_end'][:4]} | {fmt(g('revenue'),'ttm_revenue')} | "
              f"{fmt(g('operating_income'),'ttm_operating_income')} | "
              f"{fmt(g('net_income'),'ttm_net_income')} | "
              f"{fmt(g('eps_diluted'),'ttm_eps_diluted')} | "
              f"{fmt(g('ocf'),'ttm_fcf')} | {fmt(g('capex'),'ttm_fcf')} |")
        # The panel's ttm_revenue and the filer's own reported revenue should
        # agree within a quarter's growth. When they do not, the panel picked the
        # wrong XBRL tag and caught a fragment of revenue rather than the whole
        # of it. Measured across the gated universe, 38 of 216 Financials and 9
        # of 97 Real Estate names imply a net margin above 50 percent, which is
        # the same bug seen from the other side. JPM's panel revenue is 95.1B
        # against 182.4B reported.
        #
        # Every field built on revenue inherits this: revenue_growth_yoy,
        # revenue_acceleration, ev_revenue, operating_margin, gross_margin. The
        # screen ranks on them, so the note has to know.
        panel_rev, filed_rev = num(me.get("ttm_revenue")), num(fy[-1].get("revenue"))
        if panel_rev and filed_rev and filed_rev > 0:
            gap = abs(panel_rev - filed_rev) / filed_rev
            if gap >= 0.25:
                w("")
                w(f"**The panel's revenue does not match the filing.** `ttm_revenue` is "
                  f"{fmt(panel_rev,'ttm_revenue')} against {fmt(filed_rev,'ttm_revenue')} "
                  f"reported for FY{fy[-1]['period_end'][:4]}, a gap of {gap:.0%}. The panel "
                  f"selected an XBRL tag that captures part of revenue rather than all of it. "
                  f"Treat `revenue_growth_yoy`, `revenue_acceleration`, `ev_revenue`, "
                  f"`operating_margin` and `gross_margin` as unusable for this name, and use "
                  f"the reported series above instead. Do not quote the panel's revenue.")
                caveats.append(
                    f"panel ttm_revenue is {gap:.0%} away from the revenue this company "
                    f"reported for FY{fy[-1]['period_end'][:4]}; every revenue-derived field "
                    f"is unreliable here")

        eps = [num(r.get("eps_diluted")) for r in fy if num(r.get("eps_diluted")) is not None]
        if len(eps) >= 5:
            w("")
            w(f"EPS over that span ranges {min(eps):,.2f} to {max(eps):,.2f}, against "
              f"{eps[-1]:,.2f} most recently. **Before describing current earnings as high or "
              f"low, say where they sit in this range.**")
        if qh:
            w("")
            w(f"{len(qh)} quarters are also held, {qh[0]['period_end']} to "
              f"{qh[-1]['period_end']}. Note the gaps: Q4 is never filed as a quarter, "
              f"because the 10-K reports the full year instead.")
    else:
        w("No reported history collected for this ticker yet. Collection is tied to earnings "
          "events, so it arrives when the company next reports. **Without it there is no way "
          "to say whether current earnings are high or low for this business**, and a claim "
          "that they are either should not be made.")
        caveats.append("no multi-year reported history; current earnings cannot be placed "
                       "against the company's own range")

    # --- competitive position --------------------------------------------------
    # The full gated universe, not the scorable subset. A competitor's revenue
    # counts whether or not it has enough populated fields to be factor-scored.
    # Filtering on _scorable dropped 7 of MPC's 9 listed refining peers.
    ps = peer_share(me, scored)
    w("")
    w("## Position among listed peers")
    w("")
    if ps:
        sub = (me.get("sub_industry") or "").strip() or me["_peer"]
        if ps["rank"] == 1:
            w(f"**{ps['share']*100:.1f}%** of TTM revenue across the {ps['n']} filers in "
              f"{sub}, which is the **largest** of them.")
        else:
            w(f"**{ps['share']*100:.1f}%** of TTM revenue across the {ps['n']} filers in "
              f"{sub}, ranked **{ps['rank']} of {ps['n']}**. Largest is {ps['leader']} at "
              f"{ps['leader_share']*100:.1f}%.")
        w("")
        w("This is not market share. It excludes private companies, foreign issuers that do "
          "not file here, and any competitor classified into a different sub-industry. Treat "
          "it as concentration among listed peers and nothing more.")
    else:
        w("Not computable: fewer than three peers with revenue in this sub-industry.")

    # --- segment structure ---------------------------------------------------
    docs = filings_for(ticker)
    seg_row, seg_text = docs.get("segment_note", (None, None))
    w("")
    w("## Segment structure")
    w("")
    if seg_row and seg_text:
        w(f"From the {seg_row.get('form')} filed {seg_row.get('filed')}, "
          f"note titled \"{seg_row.get('items')}\". {len(seg_text):,} characters.")
        w("")
        w("```text")
        w(seg_text)
        w("```")
    else:
        w("No segment note collected for this ticker yet. Collection is tied to earnings "
          "events, so a company that has not reported since 2026-09-12 will have none. "
          "**Where a company's earnings mix across segments is the thesis, say so and stop, "
          "rather than reasoning about a consolidated number as though it described one "
          "business.**")
        caveats.append("no segment note collected; the revenue and margin mix across "
                       "business lines is not visible")

    # --- what the company says it does and fears -------------------------------
    biz_row, biz_text = docs.get("business", (None, None))
    rk_row, rk_text = docs.get("risk_factors", (None, None))
    w("")
    w("## The business, in its own words")
    w("")
    if biz_row and biz_text:
        w(f"10-K Item 1, filed {biz_row.get('filed')}. {len(biz_text):,} characters.")
        w("")
        w("```text")
        w(biz_text)
        w("```")
    else:
        w("No Item 1 collected yet. **You do not have a description of what this company "
          "sells, to whom, or why anyone buys it.** Do not write one from the ticker and "
          "the sector label.")
        caveats.append("no 10-K Item 1; there is no business description in this dossier")

    w("")
    w("## What the company says could go wrong")
    w("")
    if rk_row and rk_text:
        w(f"10-K Item 1A, filed {rk_row.get('filed')}. {len(rk_text):,} characters.")
        w("")
        w("Risk factors are largely boilerplate and are written by lawyers to be "
          "comprehensive rather than informative. Read them for what is specific to this "
          "company and ignore the rest. A risk that appears here is not a reason to avoid "
          "the stock; a risk that is **absent** from a peer's filing and present in this one "
          "is worth a sentence.")
        w("")
        w("```text")
        w(rk_text)
        w("```")
    else:
        w("No Item 1A collected yet.")
        caveats.append("no 10-K Item 1A; the company's own stated risks are not available")

    # --- the filing ----------------------------------------------------------
    row, text = docs.get("earnings_release", (None, None))
    if row is None:
        row, text = latest_filing(ticker)
    w("")
    w("## Latest earnings release (8-K item 2.02, EX-99.1)")
    w("")
    if row and text:
        has_guidance = bool(re.search(r"(?i)\b(outlook|guidance)\b", text))
        w(f"Filed **{row.get('filed')}**, items `{row.get('items')}`, "
          f"{len(text):,} characters shown.")
        w("")
        if not has_guidance:
            w("**No outlook or guidance section detected.** Roughly half of these releases "
              "carry forward guidance and half do not; Apple, for one, gives it only on the "
              "call. Do not infer guidance that is not here.")
            w("")
        w("```text")
        w(text)
        w("```")
    else:
        w("No earnings release collected for this ticker yet. Collection began 2026-09-12 and "
          "runs forward only, so a company that last reported before then will have nothing "
          "here until its next quarter.")
        caveats.append("no earnings release text available; there is no management commentary "
                       "in this dossier")

    # --- what is missing -----------------------------------------------------
    w("")
    w("## Data caveats")
    w("")
    caveats.append("no consensus estimates and no forward analyst figures are available "
                   "anywhere in this pipeline, so nothing here is calibrated against what "
                   "the market expects")
    for c in caveats:
        w(f"- {c}")
    print("\n".join(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
