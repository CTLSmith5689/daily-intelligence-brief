#!/usr/bin/env python3
"""Assemble everything known about one ticker into a markdown dossier.

The thesis writer sees this and nothing else, so anything it must not assume has
to be stated here: how old each number is, what the news filter threw away, and
which fields are missing outright. Silence reads as confidence.

Usage: dossier.py TICKER [--panel-cache FILE]
"""
import csv, io, json, math, re, statistics, sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (SLEEVES, THESES, LEDGER, RAW, PAGES, fetch, fetch_site, num, load_panel,
                    read_csv_rows, CONTAMINATED, NEWS_FIX_DATE, security_type,
                    NON_OPERATING)
import screen
import tensions
import desks

MAX_FILING_CHARS = 34000      # ~8.5K tokens; the largest single block here
MAX_NEWS = 10

STALENESS = [("prices_updated", 4, "price and every return derived from it"),
             ("last_updated", 3, "vendor fields: EV multiples, ownership, analyst count"),
             ("edgar_updated", 10, "TTM financials and the balance sheet"),
             ("insider_updated", 14, "insider buy/sell signal")]


# --- sector playbook and desk ------------------------------------------------
#
# A refiner, a bank and a SaaS company do not answer the same questions, and
# several fields in the panel mean different things or nothing at all depending
# on which of those you are looking at. ev_ebitda is not a concept for a bank.
# A low pe on a cyclical at the top of its cycle is the most expensive kind of
# cheap. Without this the agent applies one template to every name.
#
# Keyed on GICS sector, which is a column on every row, so this is a dictionary
# lookup and not a retrieval problem. Since 2026-09-24 the dossier carries the
# sector's playbook (theses/desks/sectors/) and the owning desk's file
# (theses/desks/), both in desks.py. The older sector lenses in theses/lenses/
# are no longer injected: only about a third of their claims survived a fact
# check. Their _fields.json, whose claims were each checked, is still read.
LENSES = THESES / "lenses"


# The full items are 26,000 and 42,000 characters. Pasting both put the dossier
# at 106,000 characters, roughly 26,000 tokens per ticker against a budget of
# about 12,000 for filing text. The agent has a checkout, so the whole item is
# a file read away; what the dossier owes it is enough to work from and the path
# to the rest.
_ITEM_DOSSIER_CAP = {"business": 12000, "risk_factors": 9000}


def item_excerpt(kind, text, path):
    """Capped item text, with a pointer to the whole thing."""
    cap = _ITEM_DOSSIER_CAP.get(kind, 10000)
    if len(text) <= cap:
        return text, ""
    cut = text.rfind(". ", 0, cap)
    body = text[:cut + 1] if cut > cap * 0.6 else text[:cap]
    return body, (f"Truncated at {len(body):,} of {len(text):,} characters. The whole "
                  f"item is in the checkout at `data/filings/{path}`. Read it if the "
                  f"thesis turns on something this excerpt cuts off.")


# Management's discussion runs from 20,000 characters (Apple) to well over 120,000
# (CF, which repeats every table per product). Two parts of it carry most of what
# a note needs: the opening, where management says what moved sales and profit,
# and the liquidity section, which is where cash, debt and share buybacks are.
# The rest is a file read away, like the 10-K items.
_MDNA_OPENING_CAP = 16000
_MDNA_LIQUIDITY_CAP = 7000
_RELEASE_CAP_WITH_MDNA = 14000
_MDNA_LIQUIDITY = re.compile(r"liquidity\s+and\s+capital\s+resources", re.I)


def mdna_excerpt(text, path):
    """[(label, excerpt)] for management's discussion, and a note on what was cut."""
    if len(text) <= _MDNA_OPENING_CAP + _MDNA_LIQUIDITY_CAP:
        return [("", text)], ""
    cut = text.rfind(". ", 0, _MDNA_OPENING_CAP)
    parts = [("The opening", text[:cut + 1] if cut > _MDNA_OPENING_CAP * 0.6
              else text[:_MDNA_OPENING_CAP])]
    # The first mention past the opening that is the heading itself. Earlier ones
    # are the list of contents, and CF's next three are pointers of the form
    # See "Liquidity and Capital Resources—Debt—Senior Notes," below: taking the
    # first of those printed 8,751 characters on interest income under this label.
    hits = list(_MDNA_LIQUIDITY.finditer(text, len(parts[0][1])))

    def pointer(h):
        before = text[max(0, h.start() - 3):h.start()].rstrip()
        after = text[h.end():h.end() + 2].lstrip()[:1]
        return before.endswith(("\u201c", '"', "\u2018", "'")) or after in ("\u2014", "\u2013", "-", ",", "\u201d", '"')

    m = next((h for h in hits if not pointer(h)), hits[0] if hits else None)
    if m:
        end = text.rfind(". ", m.start(), m.start() + _MDNA_LIQUIDITY_CAP)
        parts.append(("From \"Liquidity and Capital Resources\"",
                      text[m.start():end + 1] if end > m.start() else
                      text[m.start():m.start() + _MDNA_LIQUIDITY_CAP]))
    shown = sum(len(p[1]) for p in parts)
    return parts, (f"Showing {shown:,} of {len(text):,} characters. The whole discussion is "
                   f"in the checkout at `data/filings/{path}`. Read it if the thesis turns "
                   f"on a product line, a cost or a plan these excerpts leave out.")


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
    # A listed company cannot have no shares. The panel stores 0 where the vendor
    # returned nothing, on 229 of the 2,026 gated names on 2026-09-22, HIMS among
    # them, and a count of "0" in the table reads as a measurement rather than a
    # gap. Insider counts are left alone: zero buyers is a real observation.
    if field == "shares_outstanding" and not v:
        return "not reported"
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
    raw = fetch_site(f"news/{ticker}.json")
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


_SITE_JSON = {}


def site_json(path):
    """A published JSON file, fetched once per process. None when unreachable."""
    if path not in _SITE_JSON:
        raw = fetch_site(path)
        try:
            _SITE_JSON[path] = json.loads(raw) if raw else None
        except Exception:
            _SITE_JSON[path] = None
    return _SITE_JSON[path]


def price_block(ticker):
    data = site_json(f"prices/{ticker}.json")
    if not isinstance(data, dict):
        return None
    closes = data.get("closes", [])
    if len(closes) < 30:
        return None
    vals = [c[1] for c in closes if isinstance(c, list) and len(c) == 2]
    if not vals:
        return None
    hi, lo, last = max(vals), min(vals), vals[-1]
    return {"n": len(vals), "first_date": closes[0][0], "last_date": closes[-1][0],
            "high": hi, "low": lo, "last": last,
            "pos": (last - lo) / (hi - lo) if hi > lo else None}


def latest_per_period(rows):
    """One row per (period, period_end), later rows filling what earlier ones left
    blank. record_financials appends a second row for a period only to fill a
    blank (capex, when a collection fix starts reading a tag the first fetch
    missed), so the last row is the fullest one."""
    out = {}
    for r in rows:
        key = (r.get("period"), r.get("period_end"))
        prev = out.get(key, {})
        out[key] = {**prev, **{k: v for k, v in r.items() if v not in ("", None)}}
    return list(out.values())


def reported_history(ticker):
    """Annual and quarterly reported periods for this ticker."""
    raw = fetch(f"{RAW}/data/financials/reported.csv")
    if not raw:
        return [], []
    rows = latest_per_period([r for r in csv.DictReader(io.StringIO(raw.decode("utf-8", "replace")))
                              if r.get("ticker") == ticker])
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


FILINGS_START = (2026, 9)     # the first month data/filings/ has an index for


def _filing_index_months():
    """Every month that can hold a filing index, oldest first.

    The index is filed under the month a document was RECORDED, not the month it
    was filed with the SEC: a 10-K filed in February and collected in September
    sits in 2026-09.csv for good. Reading only this month and last, as this used
    to, would have dropped every 10-K description from every dossier on the first
    of November without anything failing."""
    today = datetime.now(tz=timezone.utc).date()
    y, m = FILINGS_START
    months = []
    while (y, m) <= (today.year, today.month):
        months.append(f"{y:04d}-{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return months


# The segment note is taken from the filing's XBRL viewer page, which follows the
# note with the tag's own definition and a list of accounting-standard references.
# That tail was 54% of all segment text collected by 2026-09-19 and says nothing
# about the company.
_XBRL_TAIL = re.compile(r"\n\s*X\s*\n\s*\n?\s*- (?:References|Definition)")


def filing_index_rows(ticker):
    """Every filing index row held for this ticker, across all months."""
    rows = []
    for m in _filing_index_months():
        raw = fetch(f"{RAW}/data/filings/{m}.csv")
        if raw:
            rows += [r for r in csv.DictReader(io.StringIO(raw.decode("utf-8", "replace")))
                     if r.get("ticker") == ticker]
    return rows


def filings_for(ticker):
    """Latest collected document of each kind for this ticker.

    Returns {doc_kind: (index_row, text)}. Kinds today are earnings_release
    (the 8-K EX-99.1), business and risk_factors (10-K Items 1 and 1A) and
    segment_note (the segment note from the last periodic filing). The segment
    note is here because three of the first four theses named the segment split
    as the thing they could not see.

    A kind with nothing collected is absent from the result. There is
    deliberately no "most recent document of any kind" fallback: there was one,
    and it printed CF's 10-Q segment note under the earnings release heading."""
    rows = filing_index_rows(ticker)
    out = {}
    for kind in {r.get("doc_kind") or "earnings_release" for r in rows}:
        of_kind = [r for r in rows if (r.get("doc_kind") or "earnings_release") == kind]
        row = max(of_kind, key=lambda r: r.get("filed", ""))
        raw = fetch(f"{RAW}/data/filings/{row.get('text_path', '')}")
        text = raw.decode("utf-8", "replace") if raw else None
        if text and kind == "segment_note":
            text = _XBRL_TAIL.split(text, 1)[0].rstrip()
        # Management's discussion is excerpted from two places, so it arrives whole.
        if text and kind != "mdna":
            text = text[:MAX_FILING_CHARS]
        out[kind] = (row, text or None)
    return out


# --- memo inputs --------------------------------------------------------------
#
# The buy-side memo asks for figures the sections below never printed: enterprise
# value, a named peer table, management's guidance, a split-adjusted history with
# free cash flow, the sizing rule's answer for this name. The first NVDA sample
# had to rebuild every one of them by hand from the files this script already
# reads. These blocks print them, each under a heading the analyst prompt names.
#
# Every figure is read or computed from something on disk. Where the input is not
# there, the block says so in one plain line; nothing is estimated to fill it.

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "portfolio" / "bin"))
import construct  # noqa: E402  the portfolio's own sizing rule, imported so it cannot drift

BEAR_LOSS_CAP = 0.02      # a draft: the owner has not approved this number
PEER_N = 8                # peers shown, largest by market cap
PEER_MIN = 6              # fewer than this in the sub-industry widens to the sector
HISTORY_YEARS = 10
QUARTERS_SHOWN = 8
REVENUE_GAP = 0.10        # trailing revenue against the four quarters it should equal
DAYS_PER_QUARTER = 91


def _day(s):
    try:
        return datetime.strptime((s or "")[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def _money(v):
    if v is None:
        return "n/a"
    a = abs(v)
    if a >= 1e9:
        return f"${v/1e9:,.1f}B"
    if a >= 1e6:
        return f"${v/1e6:,.1f}M"
    return f"${v:,.0f}"


def _mn(v):
    """Whole $ millions, the unit filings print, so a figure can be found in one."""
    return "n/a" if v is None else f"{v/1e6:,.0f}"


def _pct(v, d=1):
    if v is None:
        return "n/a"
    s = f"{v*100:.{d}f}"
    return (s.lstrip("-") if float(s) == 0 else s) + "%"


def _ratio(v, d=1):
    return "n/a" if v is None else f"{v:,.{d}f}"


# --- quarters, the trailing-revenue check, splits ------------------------------

_Q_FLOW = ("revenue", "gross_profit", "operating_income", "net_income", "eps_diluted")
_Q_KEEP = _Q_FLOW + ("ocf", "capex", "shares_diluted")


def quarter_series(fy, q):
    """Quarters oldest first, as {end, derived, field: float or None}.

    Q4 is never filed as a quarter: the 10-K reports the year. It is derived here
    as the fiscal year less the three quarters inside it, for income lines only
    and only when all three are held. Cash flow is not derived, because a 10-Q
    states it year to date and the nine-month figure is not stored."""
    rows = [{"end": r["period_end"], "derived": False, **{k: num(r.get(k)) for k in _Q_KEEP}}
            for r in q if _day(r.get("period_end"))]
    held = {r["end"] for r in rows}
    fy = sorted((r for r in fy if _day(r.get("period_end"))), key=lambda r: r["period_end"])
    for i, f in enumerate(fy):
        end = _day(f["period_end"])
        if f["period_end"] in held:
            continue
        prev = _day(fy[i - 1]["period_end"]) if i else None
        start = prev if prev and 350 <= (end - prev).days <= 380 else end - timedelta(days=372)
        inside = [r for r in rows if not r["derived"] and start < _day(r["end"]) < end]
        if len(inside) != 3:
            continue
        d = {"end": f["period_end"], "derived": True, "ocf": None, "capex": None,
             "shares_diluted": None}
        for k in _Q_FLOW:
            vals = [r[k] for r in inside]
            fv = num(f.get(k))
            d[k] = None if fv is None or None in vals else fv - sum(vals)
        rows.append(d)
    return sorted(rows, key=lambda r: r["end"])


def ttm_from_quarters(qs, field, end=None):
    """(sum, [ends]) over four consecutive quarters ending at `end` (default the
    latest held), or (None, reason)."""
    if end is None:
        idx = len(qs) - 1
    else:
        idx = next((i for i, r in enumerate(qs) if r["end"] == end), None)
        if idx is None:
            return None, f"no quarter ending {end} is held"
    if idx < 3:
        return None, "fewer than four quarters are held"
    four = qs[idx - 3: idx + 1]
    for a, b in zip(four, four[1:]):
        gap = (_day(b["end"]) - _day(a["end"])).days
        if not 75 <= gap <= 105:
            return None, f"the quarters held are not consecutive ({a['end']} to {b['end']})"
    vals = [r[field] for r in four]
    if None in vals:
        return None, f"{field} is missing for one of the four quarters"
    return sum(vals), [r["end"] for r in four]


def revenue_check(panel_rev, fiscal_period_end, qs, fy):
    """The panel's trailing revenue against the sum of the last four quarters.

    This used to compare it with the last fiscal year, a window up to three
    quarters older, and so flagged any company growing faster than about 25% a
    year: NVDA's correct $302,969M "did not match" the $215,938M of FY2026, and
    the note that followed told the reader its revenue fields were unusable.
    The four quarters to the panel's own fiscal period end are the same window,
    so they should agree to rounding. Only when they cannot be summed does this
    fall back to the fiscal year, with the tolerance widened by that year's growth."""
    if not panel_rev:
        return None
    ends = [r["end"] for r in qs]
    fpe = (fiscal_period_end or "")[:10]
    if fpe and ends and fpe > ends[-1]:
        ttm, why = None, (f"the panel's fiscal period ({fpe}) is newer than the last quarter "
                          f"held ({ends[-1]})")
    else:
        ttm, why = ttm_from_quarters(qs, "revenue", fpe if fpe in ends else None)
    if ttm:
        gap = abs(panel_rev - ttm) / ttm
        return {"basis": "quarters", "ttm": ttm, "ends": why, "gap": gap,
                "bad": gap >= REVENUE_GAP}
    fy_rev = num(fy[-1].get("revenue")) if fy else None
    if not fy_rev or fy_rev <= 0:
        return {"basis": "none", "why": why, "bad": False}
    prior = num(fy[-2].get("revenue")) if len(fy) > 1 else None
    growth = fy_rev / prior if prior and prior > 0 else 1.0
    lo, hi = 0.75 * min(1.0, growth), 1.25 * max(1.0, growth)
    ratio = panel_rev / fy_rev
    return {"basis": "fy", "why": why, "fy_rev": fy_rev, "fy_end": fy[-1]["period_end"],
            "ratio": ratio, "lo": lo, "hi": hi, "bad": not lo <= ratio <= hi}


_SPLIT_RATIOS = (1.5, 2, 3, 4, 5, 6, 7, 8, 10, 12, 15, 20, 25, 30, 40, 50, 100)


def split_divisors(fy):
    """(divisors, jumps). divisors[i] turns row i's EPS into today's share basis
    (EPS / divisor) and its share count likewise (shares x divisor).

    Filings state old years on the share basis of the filing they came from. A
    jump of more than 40% in the diluted count between adjacent years is read as
    a split when it sits within 8% of a usual ratio (2, 3, 4, 10 ...): NVDA's
    625M to 2,472M is 3.96, a 4-for-1, and 2,535M to 25,070M is 9.89, a
    10-for-1. A jump that fits no ratio returns divisors None, and nothing is
    restated."""
    counts = [num(r.get("shares_diluted")) for r in fy]
    step = [1.0] * len(fy)
    jumps = []
    for i in range(1, len(fy)):
        a, b = counts[i - 1], counts[i]
        if not a or not b or 0.7 <= b / a <= 1.4:
            continue
        r = b / a
        k = r if r > 1 else 1 / r
        best = min(_SPLIT_RATIOS, key=lambda s: abs(k / s - 1))
        pair = (fy[i - 1]["period_end"][:4], fy[i]["period_end"][:4], r)
        if abs(k / best - 1) > 0.08:
            return None, jumps + [(*pair, None)]
        step[i] = best if r > 1 else 1 / best
        jumps.append((*pair, step[i]))
    divs, cum = [], 1.0
    for i in reversed(range(len(fy))):
        divs.insert(0, cum)
        cum *= step[i]
    return divs, jumps


def history_rows(fy):
    """Per fiscal year: revenue, growth, margins, EPS as filed and restated, cash flow."""
    divs, jumps = split_divisors(fy)
    out = []
    for i, r in enumerate(fy):
        g = lambda k: num(r.get(k))
        rev, gp, oi, ni, eps = g("revenue"), g("gross_profit"), g("operating_income"), \
            g("net_income"), g("eps_diluted")
        ocf, capex, sh = g("ocf"), g("capex"), g("shares_diluted")
        ni_derived = False
        # Rows collected before 2026-09-19 have no net income where the filer
        # does not tag NetIncomeLoss. EPS times diluted shares is the same figure
        # by construction: CF FY2025 gives $1,455M against $1,455M filed.
        if ni is None and eps is not None and sh:
            ni, ni_derived = eps * sh, True
        prev = num(fy[i - 1].get("revenue")) if i else None
        div = divs[i] if divs else None
        out.append({"year": r["period_end"][:4], "end": r["period_end"], "rev": rev,
                    "growth": rev / prev - 1 if rev and prev else None,
                    "gm": gp / rev if gp is not None and rev else None,
                    "om": oi / rev if oi is not None and rev else None,
                    "ni": ni, "ni_derived": ni_derived, "eps": eps,
                    "eps_adj": eps / div if eps is not None and div else None,
                    "shares_adj": sh * div if sh and div else None,
                    "ocf": ocf, "capex": capex,
                    "fcf": ocf - capex if ocf is not None and capex is not None else None,
                    "conv": ocf / ni if ocf is not None and ni and ni > 0 else None})
    return out, jumps, divs is not None


# --- the earnings release: guidance, balance sheet, cash flow, dividend --------
#
# Releases arrive as flattened text: a table becomes one line of labels and
# numbers ("Accounts receivable, net 63,059 38,466"). A line is read only when
# its label is followed directly by the expected count of numbers, inside a
# statement whose unit ("In millions") is stated beside its heading. CF's
# release is a slide deck with no statements, and every parser here returns
# None for it rather than a guess.

_MONTHS = ("January|February|March|April|May|June|July|August|September|October|"
           "November|December")
_UNITS = re.compile(r"\(\s*(?:\$\s*)?in\s+(millions|thousands|billions)[^)]{0,60}\)(?:\s*\(unaudited\))?",
                    re.I)
_UNIT_MULT = {"thousands": 1e3, "millions": 1e6, "billions": 1e9}
_DASH = chr(0x2014)   # how a release prints a zero; by code point, so no dash sits in this file
_NUM_TOKEN = r"(?:\(\s*[\d,]*\d(?:\.\d+)?\s*\)|[\d,]*\d(?:\.\d+)?|" + _DASH + ")"
_NEXT_STATEMENT = re.compile(r"STATEMENTS? OF|Statements? of|RECONCILIATION|Reconciliation of")


def _to_num(tok):
    t = tok.replace(",", "").replace(" ", "")
    if t == _DASH:
        return 0.0
    neg = t.startswith("(")
    try:
        v = float(t.strip("()"))
    except ValueError:
        return None
    return -v if neg else v


def _statement(text, heading):
    """(multiplier, unit, body) of the first statement whose heading matches and
    states its unit within 150 characters, or None."""
    for m in re.finditer(heading, text, re.I):
        u = _UNITS.search(text, m.end(), m.end() + 150)
        if not u:
            continue
        body = text[u.end(): u.end() + 7000]
        nxt = _NEXT_STATEMENT.search(body, 60)
        return _UNIT_MULT[u.group(1).lower()], u.group(1).lower(), body[:nxt.start()] if nxt else body
    return None


def _line(body, label, count):
    """(label as printed, [numbers]) for the first line of `label` followed by
    between 2 and `count` numbers, or None."""
    m = re.search(r"(?<![A-Za-z])(" + label + r")(?:\s*\([a-z0-9]\))?((?:\s*\$?\s*" + _NUM_TOKEN
                  + r"){2," + str(count) + r"})", body)
    if not m:
        return None
    vals = [_to_num(t) for t in re.findall(_NUM_TOKEN, m.group(2))]
    return (m.group(1), vals) if None not in vals else None


def _column_dates(head):
    """ISO dates of a statement's columns from a flattened header such as
    'July 26, January 25, 2026 2026', or [] when they cannot be paired."""
    md = re.findall(r"(" + _MONTHS + r")\s+(\d{1,2})\b", head)
    years = re.findall(r"\b(20\d\d)\b", head)
    if not md or len(md) != len(years):
        return []
    try:
        return [datetime.strptime(f"{m} {d} {y}", "%B %d %Y").date().isoformat()
                for (m, d), y in zip(md, years)]
    except ValueError:
        return []


_BS_ITEMS = (
    ("cash", "Cash and cash equivalents", r"Cash and cash equivalents"),
    ("securities", "Marketable debt securities or short-term investments",
     r"Marketable (?:debt )?securities|Short-term investments|Available-for-sale (?:debt )?securities"),
    ("equity_securities", "Marketable equity securities", r"Marketable equity securities"),
    ("receivables", "Accounts receivable", r"(?:Accounts|Trade) receivables?(?:, net)?"),
    ("inventory", "Inventories", r"Inventor(?:y|ies)(?:, net)?"),
    ("short_debt", "Short-term debt",
     r"Short-term debt|Short-term borrowings|Current portion of long-term debt|"
     r"Current maturities of long-term debt"),
    ("long_debt", "Long-term debt",
     r"Long-term debt(?:, net)?(?:,? (?:less|net of|excluding) current (?:portion|maturities))?"),
)


def release_balance_sheet(text):
    """{key: (latest, prior)} in dollars, plus 'unit' and 'dates', or None."""
    # Dell and others title it "Statements of Financial Position".
    st = _statement(text or "", r"balance sheets?|statements? of financial position")
    if not st:
        return None
    mult, unit, body = st
    head_end = re.search(r"ASSETS|Assets|Current assets", body)
    out = {"unit": unit, "dates": _column_dates(body[:head_end.start() if head_end else 200])}
    for key, _, label in _BS_ITEMS:
        found = _line(body, label, 2)
        if found and len(found[1]) == 2:
            out[key] = (found[1][0] * mult, found[1][1] * mult)
    return out if "cash" in out else None


_CF_ITEMS = (
    ("net_income", r"Net (?:income|earnings)(?: \(loss\))?"),
    ("ocf", r"Net cash (?:provided by|from|provided by \(used in\)|\(used in\) provided by) "
            r"operating activities|Change in cash from operating activities"),
    ("capex", r"Purchases related to property and equipment(?: and intangible assets)?|"
              r"Purchases of property,? (?:plant )?and equipment|Capital expenditures|"
              r"Additions to property,? (?:plant )?and equipment|"
              r"Payments for property,? (?:plant )?and equipment"),
)


def release_cash_flow(text):
    """The cash flow statement's header and its net income, operating cash flow
    and capex lines as printed, with the year-to-date column found from the
    header, or None."""
    st = _statement(text or "", r"statements? of cash flows?")
    if not st:
        return None
    mult, unit, body = st
    first = re.search(r"Cash flows? from|Operating activities|OPERATING ACTIVITIES", body)
    header = re.sub(r"\s+", " ", body[:first.start() if first else 200]).strip()
    lines = {}
    for key, label in _CF_ITEMS:
        found = _line(body, label, 4)
        if found:
            lines[key] = found
    if "ocf" not in lines:
        return None
    n = len(lines["ocf"][1])
    ytd = re.search(r"(Six|Nine|Twelve) Months|Years? Ended|Fiscal Year", header, re.I)
    three = re.search(r"Three Months|Quarter Ended", header, re.I)
    months = {"six": 6, "nine": 9, "twelve": 12}.get((ytd.group(1) or "").lower(), 12) if ytd else None
    if ytd and three and n == 4:
        col = 2                      # three months now, a year ago; year to date now, a year ago
    elif ytd and not three and n == 2:
        col = 0
    elif three and not ytd and n == 2:
        col, months = 0, 3           # a first quarter: three months is the year to date
    else:
        col = None
    return {"unit": unit, "mult": mult, "header": header[:220], "lines": lines,
            "ytd_col": col, "ytd_months": months if col is not None else None}


def release_ytd(cf, key):
    """(this year to date, the same period a year earlier) in dollars, or None."""
    if not cf or cf["ytd_col"] is None or key not in cf["lines"]:
        return None
    vals, c = cf["lines"][key][1], cf["ytd_col"]
    if len(vals) < c + 2:
        return None
    return vals[c] * cf["mult"], vals[c + 1] * cf["mult"]


_GUIDE_CUE = re.compile(r"(?i)expected|expects|anticipate[sd]?|plus or minus|guidance (?:for|of|is)|"
                        r"outlook for|forecast|to be (?:approximately|about|between|in the range)|"
                        r"range of")
_GUIDE_STOP = re.compile(r"\b(?:Highlights|HIGHLIGHTS|CFO Commentary|Conference Call|Webcast|"
                         r"About [A-Z]|Non-GAAP Measures|Forward-Looking|FORWARD-LOOKING|"
                         r"Safe Harbor|Cautionary)")
_BOILERPLATE = re.compile(r"(?i)forward-looking statements|safe harbor|private securities litigation")
_MONTH_ABBR = re.compile(r"\b(?:Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)\.$")
GUIDANCE_CAP = 1500


def guidance_excerpt(text):
    """[(label, excerpt)] of management's forward guidance in a release, or []."""
    text = text or ""
    out = []
    # A heading ("Outlook", "GUIDANCE") first, where the numbers are laid out;
    # a mention in a sentence ("raises full-year guidance to ...") only if there
    # is no such heading, and then quoted from the start of its sentence.
    heads = list(re.finditer(r"\b(?:Outlook|OUTLOOK|Guidance|GUIDANCE)\b", text))
    words = [m for m in re.finditer(r"\b(?:outlook|guidance)\b", text)]
    # Headings with a cue verb first, then a heading over a table of dollar
    # figures with no cue verb ("Guidance Summary ... Revenue $ 49.0"), and only
    # then a mention in a sentence. Without the middle pass DELL's guidance
    # table was skipped for a sentence in the summary bullets.
    for tabular_pass, m in [(False, x) for x in heads] + [(True, x) for x in heads] + [(False, x) for x in words]:
        s = m.start()
        if m.group(0)[0].islower():
            back = text[max(0, s - 300): s]
            # A full stop after a month abbreviation ("Sept. 1, 2026") is not a
            # sentence end; without this the quote began "1, 2026 ..." for DELL.
            stops = [x.end() - 1 for x in re.finditer(r"\. ", back)
                     if not _MONTH_ABBR.search(back[:x.start() + 1])]
            cut = max(stops + [back.rfind(chr(0x2022)), back.rfind("\n")])
            s = s - len(back) + cut + 1 if cut >= 0 else s
        # The safe-harbour paragraph names the outlook too ("our outlook for the
        # third quarter ... are forward-looking statements"): skip a mention
        # with that language just before it or later in its own sentence.
        stop_at = text.find(". ", s)
        if _BOILERPLATE.search(text[max(0, s - 250): stop_at if stop_at > 0 else s + 300]):
            continue
        window = text[s: s + GUIDANCE_CAP]
        # A heading over a table ("Guidance Summary ... Revenue $ 49.0") has no
        # cue verb; two dollar figures right after it are cue enough.
        if tabular_pass:
            if len(re.findall(r"\$\s?\d", window[:400])) < 2:
                continue
        elif not (_GUIDE_CUE.search(window[:400]) and re.search(r"\d", window[:400])):
            continue
        stop = _GUIDE_STOP.search(window, 40)
        body = window[:stop.start()] if stop else window
        end = max(body.rfind(". "), body.rfind(".\n"))
        if not stop and end > len(body) * 0.5:
            body = body[:end + 1]
        out.append(("Outlook", re.sub(r"\s+", " ", body).strip()))
        break
    rec = re.search(r"(?i)reconciliation of [^.]{0,40}outlook", text)
    if rec:
        out.append(("Reconciliation of the outlook",
                    re.sub(r"\s+", " ", text[rec.start(): rec.start() + 600]).strip()))
    return out


def release_dividend(text):
    """{amount, quarterly, sentence, dates: [(kind, iso)]} or None."""
    m = re.search(r"(?i)dividend of \$\s*(\d+(?:\.\d+)?) per (?:share|common share)", text or "")
    if not m:
        return None
    a = max(text.rfind(". ", 0, m.start()), text.rfind(chr(0x2022), 0, m.start())) + 1
    b = text.find(". ", m.end(), m.end() + 300)
    sentence = re.sub(r"\s+", " ", text[a: b + 1 if b > 0 else m.end() + 200]).strip()
    # Dates only close after the amount and only where the words say which date
    # it is. A slide deck (CF's) runs one "sentence" across a page, and the first
    # version took a December date from an unrelated line as the payment date.
    near = re.sub(r"\s+", " ", text[max(0, m.start() - 80): m.end() + 200])
    dates = []
    for d in re.finditer(r"(" + _MONTHS + r")\s+(\d{1,2}),\s*(\d{4})", near):
        try:
            iso = datetime.strptime(f"{d.group(1)} {d.group(2)} {d.group(3)}", "%B %d %Y").date()
        except ValueError:
            continue
        before = near[max(0, d.start() - 80): d.start()].lower()
        if "record" in before[-40:]:
            dates.append(("record", iso.isoformat()))
        elif re.search(r"\bpa(?:y|yable|id)\b", before):
            dates.append(("payable", iso.isoformat()))
    return {"amount": float(m.group(1)), "quarterly": "quarterly" in sentence.lower(),
            "sentence": sentence[:300], "dates": dates}


# --- peers ----------------------------------------------------------------------

def peer_metrics(r):
    """Market cap, P/E, EV/EBITDA, EV/revenue, growth, margin, FCF yield from a
    panel row, each None with a reason in `why` when it cannot be stated."""
    mc, px, eps = num(r.get("market_cap")), num(r.get("price")), num(r.get("ttm_eps_diluted"))
    debt, cash = num(r.get("total_debt")), num(r.get("cash_and_investments"))
    rev, prev = num(r.get("ttm_revenue")), num(r.get("prior_ttm_revenue"))
    ebitda, oi, fcf = num(r.get("ttm_ebitda")), num(r.get("ttm_operating_income")), num(r.get("ttm_fcf"))
    # A missing debt or cash figure is not a zero, so no enterprise value then.
    ev = mc + debt - cash if mc and debt is not None and cash is not None else None
    m, why = {"mc": mc, "ev": ev}, {}
    if px and eps and eps > 0:
        m["pe"] = px / eps
        if num(r.get("pe")) is not None and screen.usable_pe(r) is None:
            m["pe"], why["pe"] = None, "n/m (EPS disputed)"
    else:
        m["pe"] = None
        why["pe"] = "n/m (loss)" if eps is not None and eps <= 0 else "n/a"
    # INTC's stored ttm_ebitda of $123M on $53B of revenue gave an EV/EBITDA
    # near 5,000. A figure under 2% of revenue is a tagging gap, not a margin.
    if ebitda is None or not ev:
        m["eve"], why["eve"] = None, "n/a"
    elif ebitda <= 0:
        m["eve"], why["eve"] = None, "n/m (negative)"
    elif rev and ebitda < 0.02 * rev:
        m["eve"], why["eve"] = None, "n/m (EBITDA under 2% of revenue)"
    else:
        m["eve"] = ev / ebitda
    m["evr"] = ev / rev if ev and rev else None
    m["gr"] = rev / prev - 1 if rev and prev and prev > 0 else None
    m["om"] = oi / rev if oi is not None and rev else None
    m["fy"] = fcf / mc if fcf is not None and mc else None
    return m, why


def select_peers(me, universe, n=PEER_N, min_n=PEER_MIN):
    """(peer rows, basis). Operating listings in the gated universe, same
    sub-industry, largest by market cap. When the sub-industry has fewer than
    `min_n` others, all of them are kept and the rest of the table is filled
    with the largest names in the sector, so the direct competitors stay in."""
    t = me.get("ticker")
    sub = (me.get("sub_industry") or "").strip()
    sector = (me.get("sector") or "").strip()
    has_cap = lambda r: r.get("ticker") != t and num(r.get("market_cap"))
    by_cap = lambda rs: sorted(rs, key=lambda r: -num(r.get("market_cap")))
    pool = by_cap(r for r in universe if has_cap(r) and sub
                  and (r.get("sub_industry") or "").strip() == sub)
    if len(pool) >= min_n or not sector:
        return pool[:n], (f"the {min(n, len(pool))} largest by market cap in sub-industry {sub} "
                          f"({len(pool)} other operating listings in the gated universe)")
    have = {r["ticker"] for r in pool}
    fill = by_cap(r for r in universe if has_cap(r) and r["ticker"] not in have
                  and (r.get("sector") or "").strip() == sector)[:max(0, n - len(pool))]
    return pool + fill, (f"all {len(pool)} other operating listings in sub-industry "
                         f"{sub or '(none)'}, then the {len(fill)} largest by market cap in "
                         f"sector {sector}, because the sub-industry has fewer than {min_n}")


def _median(vals):
    vals = sorted(v for v in vals if v is not None)
    if not vals:
        return None, 0
    k = len(vals)
    return (vals[k // 2] if k % 2 else (vals[k // 2 - 1] + vals[k // 2]) / 2), k


# --- sizing ------------------------------------------------------------------

def realised_vol(closes):
    """Standard deviation of daily returns x sqrt(252), as construct.build does."""
    vals = [c[1] for c in closes if isinstance(c, list) and len(c) == 2
            and isinstance(c[1], (int, float))]
    rets = [vals[i] / vals[i - 1] - 1 for i in range(1, len(vals)) if vals[i - 1]]
    return (statistics.pstdev(rets) * math.sqrt(252), len(rets)) if len(rets) > 60 else (None, len(rets))


def rule_weight(vol, bounds):
    """construct.size's weight for one name that clears the conviction gate."""
    w, _ = construct.size({"_": {"conviction": str(construct.CFG["min_conviction"])}},
                          {"_": vol}, bounds)
    return w.get("_")


def bear_loss_cap(bear_return, cap=BEAR_LOSS_CAP):
    """Largest position size at which the bear case costs `cap` of the portfolio."""
    return cap / abs(bear_return) if bear_return else None


def market_rate(market, key):
    """(symbol, date, percent) of the last point of a series in _MARKET.json."""
    if not isinstance(market, dict):
        return None
    pts = [p for p in (market.get(key) or []) if isinstance(p, list) and len(p) == 2
           and isinstance(p[1], (int, float))]
    if not pts:
        return None
    sym = market.get(f"{key}_symbol") or {"risk_free": "^IRX", "ten_year": "^TNX"}.get(key, key)
    return sym, pts[-1][0], pts[-1][1]


# --- the blocks ----------------------------------------------------------------

def memo_inputs(ticker, me, rows, universe, panel_date, fy, qh, docs, events, predictions):
    """(markdown lines, caveats) for the eight blocks the memo prompt reads."""
    out, caveats = [], []
    w = out.append
    prices = site_json(f"prices/{ticker}.json") or {}
    closes = [c for c in prices.get("closes", []) if isinstance(c, list) and len(c) == 2]
    volumes = prices.get("volumes") or []
    last_date, px = (closes[-1][0], closes[-1][1]) if closes else (None, None)
    px_src = f"the close on {last_date} (`prices/{ticker}.json`)"
    if px is None:
        px, px_src = num(me.get("price")), "the panel's `price`, as no close series is held"
    rel_row, rel_text = docs.get("earnings_release", (None, None))
    if rel_row:
        full = fetch(f"{RAW}/data/filings/{rel_row.get('text_path', '')}")
        rel_text = full.decode("utf-8", "replace") if full else rel_text
    rel_src = (f"the earnings release filed {rel_row.get('filed')} "
               f"(`data/filings/{rel_row.get('text_path', '')}`)") if rel_row else ""
    bs = release_balance_sheet(rel_text) if rel_text else None
    cf = release_cash_flow(rel_text) if rel_text else None
    div = release_dividend(rel_text) if rel_text else None
    qs = quarter_series(fy, qh)
    real_q = [r for r in qs if not r["derived"]]

    w("")
    w("## Memo inputs")
    w("")
    w("Figures the memo format asks for, each computed from files this dossier already "
      "reads. A block whose input is not on disk says so in one line. Nothing here is a "
      "forecast.")

    # ---- Key data
    w("")
    w("### Key data")
    w("")
    shares = num(me.get("shares_outstanding"))
    mcap = px * shares if px and shares else num(me.get("market_cap"))
    mcap_how = (f"close x `shares_outstanding` ({shares/1e6:,.1f}M)" if px and shares
                else "stored `market_cap`")
    p_debt, p_cash = num(me.get("total_debt")), num(me.get("cash_and_investments"))
    # Both or neither: a missing debt figure is not a zero.
    panel_net = p_cash - p_debt if p_debt is not None and p_cash is not None else None
    rel_net = None
    if bs and "cash" in bs and ("short_debt" in bs or "long_debt" in bs):
        cash_inv = bs["cash"][0] + bs.get("securities", (0.0, 0.0))[0]
        debt = bs.get("short_debt", (0.0, 0.0))[0] + bs.get("long_debt", (0.0, 0.0))[0]
        rel_net = cash_inv - debt
        bs_date = bs["dates"][0] if bs["dates"] else "the latest column"
        net_how = (f"release balance sheet at {bs_date}: cash and equivalents "
                   f"{_money(bs['cash'][0])}"
                   + (f" + marketable debt securities {_money(bs['securities'][0])}" if "securities" in bs else "")
                   + f" - debt {_money(debt)}")
    net = rel_net if rel_net is not None else panel_net
    if rel_net is None and panel_net is not None:
        net_how = (f"panel: `cash_and_investments` {_money(p_cash)} - `total_debt` {_money(p_debt)}; "
                   f"no balance sheet could be read from the release, so check both against "
                   f"management's discussion before quoting them")
    ev = mcap - net if mcap is not None and net is not None else None
    ebitda, rev = num(me.get("ttm_ebitda")), num(me.get("ttm_revenue"))
    eps, fcf = num(me.get("ttm_eps_diluted")), num(me.get("ttm_fcf"))
    dil = next((r for r in reversed(real_q) if r.get("shares_diluted")), None)
    if not px and mcap is None:
        w(f"Not available: no close series, panel price or market cap is held for {ticker}.")
    else:
        w(f"Price of record: ${px:,.2f}, {px_src}." if px else "No price is held for this name.")
        w("")
        w("| Measure | Value | How |")
        w("|---|---|---|")
        w(f"| Market cap | {_money(mcap)} | {mcap_how} |")
        if net is not None:
            w(f"| {'Net cash' if net >= 0 else 'Net debt'} | {_money(abs(net))} | {net_how} |")
            w(f"| Enterprise value | {_money(ev)} | market cap {'-' if net >= 0 else '+'} "
              f"{'net cash' if net >= 0 else 'net debt'} |")
        else:
            w(f"| Net cash or debt | not available | the release gives no debt line, and the panel "
              f"has `cash_and_investments` {_money(p_cash)} and `total_debt` {_money(p_debt)}, "
              f"so enterprise value cannot be stated |")
        w(f"| Diluted shares | "
          + (f"{dil['shares_diluted']/1e6:,.1f}M | weighted average, quarter to {dil['end']} "
             f"(`data/financials/reported.csv`) |" if dil else "not available | no quarterly row holds it |"))
        if closes:
            vals = [c[1] for c in closes]
            lo, hi = min(vals), max(vals)
            lo_d = next(d for d, v in closes if v == lo)
            hi_d = next(d for d, v in closes if v == hi)
            w(f"| 52-week range | ${lo:,.2f} ({lo_d}) to ${hi:,.2f} ({hi_d}) | "
              f"{len(closes)} closes from {closes[0][0]} |")
        else:
            w("| 52-week range | not available | no close series held |")
        vols = [v for v in volumes if isinstance(v, (int, float))]
        if len(vols) >= 63 and px:
            a63, a20 = statistics.mean(vols[-63:]), statistics.mean(vols[-20:])
            w(f"| Average daily volume | {a63/1e6:,.1f}M shares, {_money(a63*px)} (63 days); "
              f"{a20/1e6:,.1f}M shares (20 days) | mean of `volumes`; value at the last close |")
        else:
            w("| Average daily volume | not available | fewer than 63 daily volumes held |")
        pe_ok = eps and eps > 0 and px and not (num(me.get("pe")) is not None
                                                and screen.usable_pe(me) is None)
        w(f"| P/E, past 12 months | {_ratio(px/eps) if pe_ok else 'n/m'} | "
          + (f"close / `ttm_eps_diluted` (${eps:,.2f}) |" if pe_ok else
             "no positive, undisputed `ttm_eps_diluted` |"))
        good_ebitda = ebitda and ebitda > 0 and not (rev and ebitda < 0.02 * rev)
        w(f"| EV/EBITDA, past 12 months | {_ratio(ev/ebitda) if ev and good_ebitda else 'n/m'} | "
          + (f"EV / `ttm_ebitda` ({_money(ebitda)}) |" if ev and good_ebitda else
             "`ttm_ebitda` missing, negative or under 2% of revenue |"))
        w(f"| EV/revenue, past 12 months | {_ratio(ev/rev) if ev and rev else 'n/a'} | "
          f"EV / `ttm_revenue` ({_money(rev)}) |")
        w(f"| FCF yield, past 12 months | {_pct(fcf/mcap, 2) if fcf is not None and mcap else 'n/a'} | "
          f"`ttm_fcf` ({_money(fcf)}) / market cap |")
        if div:
            yearly = div["amount"] * 4 if div["quarterly"] else None
            w(f"| Dividend per share | ${div['amount']:,.2f}"
              + (f" a quarter; ${yearly:,.2f} a year if unchanged, {_pct(yearly/px, 2)} of the price"
                 if yearly and px else " (the release does not say how often)")
              + f" | {rel_src} |")
        else:
            w("| Dividend per share | none found | no dividend per share stated in the latest release |")
        w("| Consensus estimates | not available | no source in this pipeline |")
    if rel_net is not None and panel_net is not None and abs(rel_net - panel_net) > 0.05 * max(abs(rel_net), 1e8):
        w("")
        w(f"The panel's fields give {'net cash' if panel_net >= 0 else 'net debt'} of "
          f"{_money(abs(panel_net))} (`cash_and_investments` {_money(p_cash)}, `total_debt` "
          f"{_money(p_debt)}), which does not reconcile to the release's balance sheet. The "
          f"figures above use the release. The peer table uses the panel's fields for every "
          f"company, so its enterprise values compare like with like but differ from this one.")
        caveats.append("the panel's net debt (cash_and_investments less total_debt) does not "
                       "reconcile to the release's balance sheet; Key data uses the release")

    # ---- Guidance
    w("")
    w("### Guidance")
    w("")
    guide = guidance_excerpt(rel_text) if rel_text else []
    if not rel_row:
        w(f"None given: no earnings release is held for {ticker} in `data/filings/text/`.")
    elif not guide:
        w(f"None given in {rel_src}.")
    else:
        w(f"Quoted from {rel_src}. Management's words, not this pipeline's; check the whole "
          f"release before building a forecast on it.")
        for label, body in guide:
            w("")
            w(f"**{label}**")
            w("")
            w(f"> {body}")

    # ---- Peers
    w("")
    w("### Peers")
    w("")
    peers, basis = select_peers(me, universe)
    if not peers:
        w(f"Not available: no other operating listing in {me.get('sub_industry') or 'this sub-industry'} "
          f"or {me.get('sector') or 'this sector'} has a market cap in the panel.")
    else:
        w(f"Peers: {basis}; panel dated {panel_date}. Every "
          f"figure is from the panel's own fields: EV = `market_cap` + `total_debt` - "
          f"`cash_and_investments`, P/E = `price` / `ttm_eps_diluted`, revenue growth = "
          f"`ttm_revenue` / `prior_ttm_revenue` - 1, FCF yield = `ttm_fcf` / `market_cap`.")
        w("")
        w("| Company | Market cap ($B) | P/E | EV/EBITDA | EV/revenue | Revenue growth | "
          "Operating margin | FCF yield |")
        w("|---|---|---|---|---|---|---|---|")
        stats = []
        for r in [me] + peers:
            m, why = peer_metrics(r)
            if r is not me:
                stats.append(m)
            nm = (r.get("name") or "").strip()
            label = f"{nm[:30]} ({r['ticker']})" if nm else r["ticker"]
            w(f"| {label}{' (this name)' if r is me else ''} | "
              f"{_ratio(m['mc']/1e9, 0) if m['mc'] else 'n/a'} | "
              f"{_ratio(m['pe']) if m['pe'] is not None else why.get('pe', 'n/a')} | "
              f"{_ratio(m['eve']) if m['eve'] is not None else why.get('eve', 'n/a')} | "
              f"{_ratio(m['evr'])} | {_pct(m['gr'], 0)} | {_pct(m['om'], 0)} | {_pct(m['fy'])} |")
        meds = {k: _median(s[k] for s in stats) for k in ("pe", "eve", "evr", "gr", "om", "fy")}
        w(f"| Peer median (excluding {ticker}) | | {_ratio(meds['pe'][0])} | {_ratio(meds['eve'][0])} | "
          f"{_ratio(meds['evr'][0])} | {_pct(meds['gr'][0], 0)} | {_pct(meds['om'][0], 0)} | "
          f"{_pct(meds['fy'][0])} |")
        w("")
        w("Medians leave out n/m and n/a; they rest on " + ", ".join(
            f"{meds[k][1]} ({lab})" for k, lab in (("pe", "P/E"), ("eve", "EV/EBITDA"),
                                                  ("evr", "EV/revenue"), ("gr", "growth"),
                                                  ("om", "margin"), ("fy", "FCF yield")))
          + " companies. Foreign filers without stored trailing figures, and companies not "
            "in the panel, are absent.")

    # ---- Balance sheet and cash flow
    w("")
    w("### Balance sheet and cash flow")
    wrote = False
    if bs:
        wrote = True
        d0, d1 = (bs["dates"] + ["latest", "prior"])[:2] if len(bs["dates"]) >= 2 else ("latest", "prior")
        w("")
        w(f"From the balance sheet in {rel_src}, $ millions.")
        w("")
        w(f"| Line | {d0} | {d1} |")
        w("|---|---|---|")
        for key, label, _ in _BS_ITEMS:
            if key in bs:
                w(f"| {label} | {_mn(bs[key][0])} | {_mn(bs[key][1])} |")
        rel_ci = bs["cash"][0] + bs.get("securities", (0.0, 0.0))[0]
        w("")
        line = (f"Cash and investments (cash plus marketable debt securities or short-term "
                f"investments): {_money(rel_ci)}")
        if p_cash:
            gap = abs(p_cash - rel_ci) / rel_ci if rel_ci else None
            line += (f", against the panel's `cash_and_investments` of {_money(p_cash)}"
                     + (f", a gap of {gap:.0%}. Quote the release, not the panel." if gap and gap > 0.05
                        else ", which agrees."))
        else:
            line += "."
        w(line)
        if "short_debt" in bs or "long_debt" in bs:
            debt = bs.get("short_debt", (0.0, 0.0))[0] + bs.get("long_debt", (0.0, 0.0))[0]
            w("")
            w(f"Total debt (short-term plus long-term lines): {_money(debt)}"
              + (f", against the panel's `total_debt` of {_money(p_debt)}." if p_debt is not None else "."))
    if cf:
        wrote = True
        w("")
        w(f"Cash flow statement lines as printed in the release ({cf['unit']}; columns as headed: "
          f"\"{cf['header']}\"):")
        w("")
        for key in ("net_income", "ocf", "capex"):
            if key in cf["lines"]:
                lab, vals = cf["lines"][key]
                w(f"- {lab}: " + ", ".join(f"{v:,.0f}" for v in vals))
        ocf_ytd, ni_ytd = release_ytd(cf, "ocf"), release_ytd(cf, "net_income")
        if ocf_ytd and ni_ytd and ni_ytd[0] > 0:
            w("")
            w(f"Cash conversion, year to date ({cf['ytd_months']} months): operating cash flow "
              f"{_money(ocf_ytd[0])} / net income {_money(ni_ytd[0])} = {_pct(ocf_ytd[0]/ni_ytd[0], 0)}.")
        prior_fy = [r for r in fy if bs and bs["dates"] and r["period_end"] < bs["dates"][0]] or fy
        fy_ocf = num(prior_fy[-1].get("ocf")) if prior_fy else None
        if ocf_ytd and fy_ocf is not None and cf["ytd_months"] in (3, 6, 9):
            ttm_ocf = fy_ocf - ocf_ytd[1] + ocf_ytd[0]
            ni_ttm = num(me.get("ttm_net_income"))
            w("")
            w(f"Operating cash flow, past 12 months: FY{prior_fy[-1]['period_end'][:4]} "
              f"{_money(fy_ocf)} - {_money(ocf_ytd[1])} + {_money(ocf_ytd[0])} = {_money(ttm_ocf)}"
              + (f"; / `ttm_net_income` {_money(ni_ttm)} = {_pct(ttm_ocf/ni_ttm, 0)} cash conversion"
                 if ni_ttm and ni_ttm > 0 else "")
              + (f"; the stored `ttm_fcf` of {_money(fcf)} implies capex of {_money(ttm_ocf - fcf)}"
                 if fcf is not None else "") + ".")
    recent = qs[-QUARTERS_SHOWN:]
    if recent:
        wrote = True
        by_end = {r["end"]: r for r in qs}
        w("")
        w(f"Last {len(recent)} quarters, `data/financials/reported.csv`, $ millions. Q4 rows are "
          f"the fiscal year less its three quarters (income lines only). Operating cash flow "
          f"and capex appear only where a three-month figure is filed, which is the first "
          f"quarter: a 10-Q states cash flow year to date.")
        w("")
        w("| Quarter to | Revenue | Q/Q | Y/Y | Gross margin | Operating margin | Net income | "
          "OCF | Capex | FCF | OCF / net income |")
        w("|---|---|---|---|---|---|---|---|---|---|---|")
        for r in recent:
            i = qs.index(r)
            prev = qs[i - 1] if i else None
            yago = next((x for x in qs if x["end"] < r["end"]
                         and 350 <= (_day(r["end"]) - _day(x["end"])).days <= 380), None)
            g = lambda a, b: a / b - 1 if a is not None and b else None
            rv = r["revenue"]
            fcf_q = r["ocf"] - r["capex"] if r["ocf"] is not None and r["capex"] is not None else None
            w(f"| {r['end']}{' (Q4, derived)' if r['derived'] else ''} | {_mn(rv)} | "
              f"{_pct(g(rv, prev['revenue'] if prev else None), 0)} | "
              f"{_pct(g(rv, yago['revenue'] if yago else None), 0)} | "
              f"{_pct(r['gross_profit']/rv if r['gross_profit'] is not None and rv else None)} | "
              f"{_pct(r['operating_income']/rv if r['operating_income'] is not None and rv else None)} | "
              f"{_mn(r['net_income'])} | {_mn(r['ocf'])} | {_mn(r['capex'])} | {_mn(fcf_q)} | "
              f"{_pct(r['ocf']/r['net_income'] if r['ocf'] is not None and r['net_income'] else None, 0)} |")
        if bs and "receivables" in bs:
            dso = []
            ends = bs["dates"] if len(bs["dates"]) >= 2 else []
            if not ends and real_q:
                ends = [real_q[-1]["end"]]
                prior = [r["period_end"] for r in fy if r["period_end"] < ends[0]]
                ends += prior[-1:]
            for col, end in enumerate(ends[:2]):
                q = next((x for x in qs if _day(x["end"]) and _day(end)
                          and abs((_day(x["end"]) - _day(end)).days) <= 4), None)
                if q and q["revenue"]:
                    rcv = bs["receivables"][col]
                    dso.append(f"{rcv / q['revenue'] * DAYS_PER_QUARTER:.0f} days at {end} "
                               f"({_mn(rcv)} on quarter revenue of {_mn(q['revenue'])}"
                               f"{', the derived Q4' if q['derived'] else ''})")
            w("")
            w("Days sales outstanding (receivables / quarter revenue x 91): "
              + ("; ".join(dso) + "." if dso else
                 "not computable, the balance sheet dates do not match a quarter held."))
        else:
            w("")
            w("Days sales outstanding: not computable, no receivables line was read from the release.")
    if not wrote:
        w("")
        w(f"Not available: no balance sheet or cash flow statement could be read from the release, "
          f"and no quarterly rows are held for {ticker}.")

    # ---- History
    w("")
    w("### History")
    w("")
    if not fy:
        w("No reported history collected for this ticker yet, so current earnings cannot be "
          "placed against the company's own record.")
        caveats.append("no multi-year reported history; current earnings cannot be placed "
                       "against the company's own range")
    else:
        hist, jumps, restated = history_rows(fy)
        shown = hist[-HISTORY_YEARS:]
        w(f"{len(shown)} of {len(hist)} fiscal years held, `data/financials/reported.csv`, "
          f"$ millions except per share. FY is the calendar year the fiscal year ends in.")
        w("")
        w("| FY | Revenue | Growth | Gross margin | Operating margin | Net income | EPS as filed | "
          "EPS, today's shares | OCF | Capex | FCF | OCF / net income |")
        w("|---|---|---|---|---|---|---|---|---|---|---|---|")
        for h in shown:
            ea = h["eps_adj"]
            ea_s = "n/a" if ea is None else (f"{ea:,.3f}" if abs(ea) < 1 else f"{ea:,.2f}")
            eps_s = "n/a" if h["eps"] is None else f"{h['eps']:,.2f}"
            w(f"| FY{h['year']} | {_mn(h['rev'])} | {_pct(h['growth'], 0)} | {_pct(h['gm'])} | "
              f"{_pct(h['om'])} | {_mn(h['ni'])}{'*' if h['ni_derived'] else ''} | {eps_s} | "
              f"{ea_s} | {_mn(h['ocf'])} | {_mn(h['capex'])} | {_mn(h['fcf'])} | "
              f"{_pct(h['conv'], 0)} |")
        notes = []
        if any(h["ni_derived"] for h in shown):
            notes.append("\\* Net income not tagged in the filing data held; worked out as EPS "
                         "times diluted shares.")
        if jumps and restated:
            notes.append("EPS on today's shares divides each year by the splits since, read from "
                         "jumps in the diluted share count: "
                         + "; ".join(f"x{f:g} between FY{a} and FY{b} (count x{r:.2f})"
                                     for a, b, r, f in jumps)
                         + ". Inferred, not filed: check the 10-K's split note before relying "
                           "on a restated year.")
        elif jumps and not restated:
            a, b, r, _ = jumps[-1]
            notes.append(f"**The diluted share count moves x{r:.2f} between FY{a} and FY{b}**, which "
                         f"fits no usual split ratio, so nothing is restated. Do not compare EPS or "
                         f"the share count across those years, or call the change a buyback or an "
                         f"issue; compare revenue and net income, which a split does not touch.")
            caveats.append("the reported history crosses a share-count jump that fits no split "
                           "ratio, so profit per share is not comparable across years")
        blank_capex = sum(1 for h in shown if h["capex"] is None)
        if blank_capex:
            notes.append(f"Capex is blank for {blank_capex} of these {len(shown)} years, so free cash "
                         f"flow is shown only where it is held. Collection now also reads "
                         f"PaymentsToAcquireProductiveAssets and fills blank years when the "
                         f"company next reports.")
            if blank_capex == len(shown):
                caveats.append("capex is not held for any year shown, so there is no free cash "
                               "flow history")
        adj = [h["eps_adj"] for h in shown if h["eps_adj"] is not None]
        if len(adj) >= 5:
            mid, _ = _median(adj)
            notes.append(f"EPS on today's shares ranges {min(adj):,.3f} to {max(adj):,.3f} over these "
                         f"years; the median year earned {mid:,.3f} and the latest {adj[-1]:,.3f}. "
                         f"**Before describing current earnings as high or low, say where they sit "
                         f"in this range**, and tie each case to a year it resembles.")
        sh = [(h["year"], h["shares_adj"]) for h in shown if h["shares_adj"]]
        if len(sh) >= 4:
            notes.append(f"Diluted shares on today's basis went from {sh[0][1]/1e6:,.1f}M in "
                         f"FY{sh[0][0]} to {sh[-1][1]/1e6:,.1f}M in FY{sh[-1][0]}, "
                         f"{sh[-1][1]/sh[0][1]-1:+.0%}. A falling count is the company buying "
                         f"back shares, which lifts EPS without any rise in profit.")
        for n_ in notes:
            w("")
            w(n_)

        # The trailing-revenue check.
        chk = revenue_check(num(me.get("ttm_revenue")), me.get("fiscal_period_end"), qs, fy)
        if chk and chk["basis"] == "quarters":
            q4 = [e for e in chk["ends"] if by_quarter_derived(qs, e)]
            if not chk["bad"]:
                w("")
                w(f"Trailing revenue check: `ttm_revenue` {_mn(num(me.get('ttm_revenue')))} against "
                  f"{_mn(chk['ttm'])} summed from the four quarters to {chk['ends'][-1]}"
                  + (f" (Q4 to {q4[0]} derived)" if q4 else "")
                  + f", a gap of {chk['gap']:.1%}. The revenue-based panel fields "
                    f"(`revenue_growth_yoy`, `ev_revenue`, `operating_margin`, `gross_margin`) "
                    f"rest on the right figure.")
            else:
                w("")
                w(f"**The panel's revenue does not match the filings.** `ttm_revenue` is "
                  f"{_money(num(me.get('ttm_revenue')))} against {_money(chk['ttm'])} summed from "
                  f"the four quarters to {chk['ends'][-1]}, a gap of {chk['gap']:.0%}. The panel "
                  f"probably read an XBRL tag that captures part of revenue. Treat "
                  f"`revenue_growth_yoy`, `revenue_acceleration`, `ev_revenue`, "
                  f"`operating_margin` and `gross_margin` as unusable here, and use the "
                  f"reported series instead.")
                caveats.append(f"panel ttm_revenue is {chk['gap']:.0%} away from the sum of the "
                               f"last four reported quarters; every revenue-derived field is "
                               f"unreliable here")
        elif chk and chk["basis"] == "fy":
            w("")
            if chk["bad"]:
                w(f"**The panel's revenue may not match the filings.** Four quarters could not be "
                  f"summed ({chk['why']}), so `ttm_revenue` was compared with FY"
                  f"{chk['fy_end'][:4]} revenue of {_money(chk['fy_rev'])}: a ratio of "
                  f"{chk['ratio']:.2f}, outside the {chk['lo']:.2f} to {chk['hi']:.2f} that the "
                  f"year's own growth allows. Check `revenue_growth_yoy`, `ev_revenue`, "
                  f"`operating_margin` and `gross_margin` against the filings before quoting them.")
                caveats.append("panel ttm_revenue is outside the range the last fiscal year and its "
                               "growth allow; revenue-derived fields need checking")
            else:
                w(f"Trailing revenue check: four quarters could not be summed ({chk['why']}); "
                  f"`ttm_revenue` is {chk['ratio']:.2f} times FY{chk['fy_end'][:4]} revenue, "
                  f"within the {chk['lo']:.2f} to {chk['hi']:.2f} its growth allows.")
        elif chk:
            w("")
            w(f"Trailing revenue check: not possible ({chk['why']}).")

        # Profit per share against the company's own filings.
        panel_eps = num(me.get("ttm_eps_diluted"))
        ni_p, sh_p = num(me.get("ttm_net_income")), num(me.get("shares_outstanding"))
        filed_eps = num(fy[-1].get("eps_diluted"))
        implied = (ni_p / sh_p) if ni_p is not None and sh_p else None
        ref = implied if implied is not None else filed_eps
        if panel_eps is not None and ref is not None and abs(ref) >= 0.01:
            flipped = (panel_eps > 0) != (ref > 0)
            if flipped or abs(panel_eps - ref) / abs(ref) >= 0.5:
                where = ("its own reported net income divided by its shares"
                         if implied is not None else "the last year it filed")
                w("")
                w(f"**The panel's profit per share does not match the filings.** "
                  f"`ttm_eps_diluted` is {panel_eps:,.2f}, against {ref:,.2f} from {where}"
                  + (". The two disagree about whether the company made money at all."
                     if flipped else ".") +
                  f" A reverse split that the vendor's figure never took up does this. "
                  f"`pe` is the price divided by that number, so it is wrong by the same "
                  f"factor and its peer ranking with it. **Do not quote `pe` or "
                  f"`ttm_eps_diluted` for this company.** Use the profit per share in the "
                  f"reported history above, and the price against it.")
                caveats.append(
                    f"the panel's profit per share ({panel_eps:,.2f}) contradicts the "
                    f"company's own filings ({ref:,.2f}), so pe and every reading built on "
                    f"it, including the Value score, are unusable here")

    # ---- Calendar
    w("")
    w("### Calendar")
    w("")
    items = []
    pd_ = _day(panel_date)
    ed = (me.get("earnings_date") or "").strip()[:10]
    if _day(ed):
        items.append((ed, f"{ticker} reports (stored `earnings_date`)"
                          + (f", {(_day(ed) - pd_).days} days after the panel date" if pd_ else "")))
    for kind, iso in (div["dates"] if div else []):
        if pd_ and _day(iso) >= pd_:
            items.append((iso, f"{ticker} dividend of ${div['amount']:,.2f}: {kind} date, "
                               f"from the release"))
    for r in peers:
        d = (r.get("earnings_date") or "").strip()[:10]
        if _day(d) and pd_ and _day(d) >= pd_:
            items.append((d, f"{r['ticker']} reports (peer; stored `earnings_date`)"))
    if items:
        for d, what in sorted(items):
            w(f"- {d}: {what}")
    else:
        w(f"No dated event is stored for {ticker}: no `earnings_date`, and no dates in the release.")
    idx = filing_index_rows(ticker)
    latest_form = {}
    for r in idx:
        form = r.get("form") or "?"
        if r.get("filed", "") > latest_form.get(form, ""):
            latest_form[form] = r.get("filed", "")
    if latest_form:
        w("")
        w("Filings held, latest of each form: "
          + "; ".join(f"{f} filed {d}" for f, d in sorted(latest_form.items(), key=lambda kv: kv[1]))
          + ". The same filing a year later is an estimate of the next date, not an announced one.")

    # ---- Sizing inputs
    w("")
    w("### Sizing inputs")
    w("")
    vol, nret = realised_vol(closes)
    beta = num(me.get("beta_1y"))
    market = site_json("prices/_MARKET.json")
    rf, ten = market_rate(market, "risk_free"), market_rate(market, "ten_year")
    bounds = construct.universe_vol_bounds(rows)
    wt = rule_weight(vol, bounds) if vol else None
    w("| Input | Value | Source |")
    w("|---|---|---|")
    w(f"| 1-year volatility | {_pct(vol) if vol else 'not available'} | "
      + (f"{nret} daily returns in `prices/{ticker}.json`, standard deviation x sqrt(252), as "
         f"`portfolio/bin/construct.py` computes it; stored `volatility_1y` "
         f"{_pct(num(me.get('volatility_1y')))} |" if vol else "fewer than 61 daily returns held |"))
    w(f"| Beta, raw | {_ratio(beta, 2) if beta is not None else 'not available'} | stored `beta_1y`, "
      f"one year of daily returns against the S&P 500 |")
    w(f"| Beta, Blume-adjusted | {_ratio(0.67 * beta + 0.33, 2) if beta is not None else 'not available'} | "
      f"0.67 x raw + 0.33 x 1.0 |")
    for label, got, what in (("Risk-free rate (13-week bill)", rf, "risk_free"),
                             ("10-year Treasury yield", ten, "ten_year")):
        if got:
            sym, d, v = got
            age = (pd_ - _day(d)).days if pd_ and _day(d) else None
            stale = f", **{age} days before the panel date**" if age is not None and age > 5 else ""
            w(f"| {label} | {v:.3f}% on {d}{stale} | `prices/_MARKET.json` `{what}` ({sym}) |")
        else:
            w(f"| {label} | not stored yet | `prices/_MARKET.json` has no `{what}` series; "
              + ("collection began on 2026-09-23 and reaches this file on the next daily run |"
                 if what == "ten_year" else "the market series was not collected |"))
    if not ten:
        caveats.append("no 10-year Treasury yield is stored yet; say which rate the discount "
                       "rate uses")
    if wt:
        vlo, vhi = bounds
        cfg = construct.CFG
        w(f"| Rule weight | {_pct(wt)} | `construct.py`: {cfg['risk_budget']} / volatility "
          f"clamped to {_pct(vlo)} to {_pct(vhi)} (the gated universe's 25th and 90th "
          f"percentiles), limited to {_pct(cfg['min_position'], 0)} to "
          f"{_pct(cfg['max_position'], 0)}; needs conviction {cfg['min_conviction']} or more |")
        w(f"| Half weight | {_pct(wt / 2)} | rule weight / 2 |")
    else:
        w("| Rule weight | not available | no volatility, so the rule has no input |")
    w("")
    w(f"Bear-loss cap, **a draft the owner has not approved**: size x |bear_return| <= "
      f"{_pct(BEAR_LOSS_CAP, 0)} of the portfolio, so the largest size is "
      f"{_pct(BEAR_LOSS_CAP, 0)} / |bear_return|."
      + (f" At the rule weight a bear case worse than {_pct(-BEAR_LOSS_CAP / wt)} breaches it; "
         f"at half weight, worse than {_pct(-BEAR_LOSS_CAP / (wt / 2))}." if wt else ""))
    w("")
    w("| Bear return | " + " | ".join(f"{b:.0%}" for b in (-0.10, -0.20, -0.30, -0.40, -0.50)) + " |")
    w("|---|---|---|---|---|---|")
    w("| Largest size | " + " | ".join(_pct(bear_loss_cap(b)) for b in (-0.10, -0.20, -0.30, -0.40, -0.50))
      + " |")

    # ---- Current view
    w("")
    w("### Current view")
    w("")
    evs = [e for e in events if e.get("ticker") == ticker]
    if not evs:
        w(f"No view on record for {ticker} in `theses/ledger/events.csv`, so this is an initiation.")
    else:
        last = evs[-1]
        pred = next((p for p in reversed(predictions)
                     if p.get("thesis_id") == last.get("thesis_id")), None)
        w(f"{len(evs)} event{'s' if len(evs) > 1 else ''} on record in `theses/ledger/events.csv`. "
          f"**Write a revision, not an initiation**: the checker rejects a second initiation "
          f"for a ticker already in the ledger.")
        w("")
        w("| Field | Latest |")
        w("|---|---|")
        for k in ("date", "kind", "thesis_id", "note_path", "direction", "conviction",
                  "target_price", "horizon_days", "action", "size_now", "expected_return",
                  "bear_return", "rationale"):
            v = (last.get(k) or "").strip()
            if v:
                w(f"| {k} | {v.replace('|', '/')} |")
        if pred:
            for k in ("entry_price", "review_by", "falsifier"):
                v = (pred.get(k) or "").strip()
                if v:
                    w(f"| prediction {k} | {v.replace('|', '/')} |")
        if len(evs) > 1:
            w("")
            w("Earlier: " + "; ".join(f"{e.get('date')} {e.get('kind')} {e.get('direction')} "
                                      f"(conviction {e.get('conviction')})" for e in evs[:-1]) + ".")
    return out, caveats


def by_quarter_derived(qs, end):
    return any(r["end"] == end and r["derived"] for r in qs)


def main():
    if len(sys.argv) < 2:
        print("usage: dossier.py TICKER", file=sys.stderr)
        return 2
    ticker = sys.argv[1].upper()

    panel_date, rows = load_panel()
    # Refused before anything is assembled. A note ticker resolves to its
    # parent's CIK, so its panel row carries the parent's EPS and shares: with
    # the gate below fixed, AFGB (a baby bond) built a dossier at a P/E of 1.94
    # with no word that it was a bond, and CCD (a closed-end fund) read as the
    # cheapest name in Financials. Nonzero, so prepare.py records it as failed.
    raw_row = next((r for r in rows if r.get("ticker") == ticker), None)
    if raw_row is not None and security_type(raw_row) in NON_OPERATING:
        print(f"dossier: {ticker} is not an operating company (security_type "
              f"{security_type(raw_row)}: {raw_row.get('name', '')}). Its figures describe "
              f"a parent issuer, a fund portfolio or a blank-check trust, not a business "
              f"of its own, so no dossier is built.",
              file=sys.stderr)
        return 3
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
                   "_ev_ebitda": screen.recompute_ev_ebitda(raw),
                   # screen.value_of reads r["_pe"] for pe. core_universe sets it
                   # (commit c0e57ad) and this fallback did not, so every name
                   # outside the gate raised KeyError: '_pe'.
                   "_pe": screen.usable_pe(raw)})
        gate_note = ("**This name fails the screen's own gate** (needs market cap over $1B, "
                     "a sector, and revenue). It is here because it was asked for by name, "
                     "from the watchlist or by hand. "
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
    playbook = desks.playbook_text(me.get("sector"))
    desk = desks.desk_for(me.get("sector"))
    desk_body = desks.desk_text(desk)
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
    if playbook:
        w("")
        w("## How to read a company in this sector")
        w("")
        w(f"From the sector playbook, `theses/desks/sectors/{desks.sector_slug(me.get('sector'))}.md`. "
          "Every claim in it about our data was checked against the panel when it was written.")
        w("")
        # Two levels down, so the playbook's own headings sit under this one.
        w(re.sub(r"^(#{1,4})(\s)", r"##\1\2", playbook, flags=re.M))
    if desk_body:
        w("")
        w(f"## The desk that owns this name: {desks.desk_title(desk)}")
        w("")
        w(f"From `theses/desks/{desk}.md`. If this week's plan in `theses/director/` has a focus "
          "note for this desk, read it before writing.")
        w("")
        # The heading above already names the desk, so its own title line goes.
        desk_body = re.sub(r"\A#[ \t]+[^\n]*\n+", "", desk_body)
        w(re.sub(r"^(#{1,4})(\s)", r"##\1\2", desk_body, flags=re.M))

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

    # --- the blocks the memo prompt reads by heading -------------------------
    fy, qh = reported_history(ticker)
    docs = filings_for(ticker)
    memo_lines, memo_caveats = memo_inputs(
        ticker, me, rows, scored, panel_date, fy, qh, docs,
        read_csv_rows(LEDGER / "events.csv"), read_csv_rows(LEDGER / "predictions.csv"))
    out.extend(memo_lines)
    caveats.extend(memo_caveats)

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
    # What the company owes and holds. net_debt_ebitda alone gives a ratio, and
    # the first two-part note had to say it did not know how much CF owed.
    debt, cash = num(me.get("total_debt")), num(me.get("cash_and_investments"))
    if debt is not None or cash is not None:
        w("")
        line = (f"Total debt `total_debt` {fmt(debt, 'total_debt')}, cash and investments "
                f"`cash_and_investments` {fmt(cash, 'cash_and_investments')}")
        # The three debt fields come from different tags and need not agree. CF's
        # panel cash is $290M against the $2,480M its 10-Q states, which puts debt
        # after cash at 1.06 years of EBITDA beside a stored net_debt_ebitda of
        # 0.29. Printing the subtraction as a fact would hand the note a wrong
        # number with a field name on it.
        ebitda, stored = num(me.get("ttm_ebitda")), num(me.get("net_debt_ebitda"))
        if debt is not None and cash is not None:
            implied = (debt - cash) / ebitda if ebitda else None
            if implied is not None and stored is not None \
                    and abs(implied - stored) > max(0.25, 0.25 * abs(stored)):
                w(line + ".")
                w("")
                w(f"**These do not agree with each other.** Debt less cash is "
                  f"{fmt(debt - cash, 'total_debt')}, or {implied:.2f} years of `ttm_ebitda`, "
                  f"but `net_debt_ebitda` is stored as {stored:.2f}. That ratio comes from the "
                  f"data vendor and the other figures from the filing's own tags, so either a tag "
                  f"was misread or the two count debt differently, leases for example. Where "
                  f"management's discussion gives debt and cash, use those, and do not quote these.")
                caveats.append("the panel's debt and cash fields disagree with its own "
                               "net_debt_ebitda; use the figures in management's discussion")
            elif debt >= cash:
                w(line + f", so debt after using the cash is {fmt(debt - cash, 'total_debt')}.")
            else:
                w(line + f", so it holds {fmt(cash - debt, 'total_debt')} more cash than it owes.")
        else:
            w(line + ".")

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
        close_older = bool(panel_date and pb["last_date"] < panel_date)
        if panel_px and close_older and abs(pb["last"] / panel_px - 1) > 0.005:
            # The close series itself can lag (an offline copy, a failed fetch).
            # Then the panel is not shown to be stale, and neither price is
            # known to be current, so say so rather than calling the panel stale.
            drift = (pb["last"] / panel_px - 1) * 100
            w("")
            w(f"**The close series ends {pb['last_date']}, before the panel date {panel_date}.** "
              f"The panel's `price` of {fmt(panel_px, 'price')} differs by {drift:+.1f}%, and "
              f"neither can be shown to be current. The price of record is still the close; "
              f"say which date it is.")
            caveats.append(f"the close series ends {pb['last_date']}, before the panel date "
                           f"{panel_date}; the panel price differs by {drift:+.1f}%")
        elif panel_px and abs(pb["last"] / panel_px - 1) > 0.005:
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
        biz_body, biz_cut = item_excerpt("business", biz_text, biz_row.get("text_path", ""))
        w(f"10-K Item 1, filed {biz_row.get('filed')}. {len(biz_text):,} characters.")
        if biz_cut:
            w("")
            w(biz_cut)
        w("")
        w("```text")
        w(biz_body)
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
        rk_body, rk_cut = item_excerpt("risk_factors", rk_text, rk_row.get("text_path", ""))
        w(f"10-K Item 1A, filed {rk_row.get('filed')}. {len(rk_text):,} characters.")
        if rk_cut:
            w("")
            w(rk_cut)
        w("")
        w("Risk factors are largely boilerplate and are written by lawyers to be "
          "comprehensive rather than informative. Read them for what is specific to this "
          "company and ignore the rest. A risk that appears here is not a reason to avoid "
          "the stock; a risk that is **absent** from a peer's filing and present in this one "
          "is worth a sentence.")
        w("")
        w("```text")
        w(rk_body)
        w("```")
    else:
        w("No Item 1A collected yet.")
        caveats.append("no 10-K Item 1A; the company's own stated risks are not available")

    # --- management's discussion ----------------------------------------------
    md_row, md_text = docs.get("mdna", (None, None))
    w("")
    w("## What management says happened")
    w("")
    if md_row and md_text:
        md_parts, md_cut = mdna_excerpt(md_text, md_row.get("text_path", ""))
        w(f"Management's discussion, {md_row.get('form')} Item {md_row.get('items')}, filed "
          f"**{md_row.get('filed')}**. {len(md_text):,} characters.")
        if md_cut:
            w("")
            w(md_cut)
        w("")
        w("This is the company explaining its own results: what it sold, at what price, what "
          "it cost, and what it did with the cash. It is the first place to look for a figure "
          "the panel does not carry, such as a selling price per unit, an input cost, or the "
          "shares bought back in the quarter. It is management's account, so it says what "
          "happened more reliably than why.")
        for label, body in md_parts:
            w("")
            if label:
                w(f"**{label}**")
                w("")
            w("```text")
            w(body)
            w("```")
    else:
        w("No management's discussion collected for this ticker. Either the pipeline has not "
          "topped this name up yet, or the filing keeps the discussion under headings the "
          "extractor does not recognise, or includes it by reference to an exhibit. **You do "
          "not have the company's own account of what moved its sales, costs and cash.**")
        caveats.append("no management's discussion (10-Q Item 2 or 10-K Item 7); the company's "
                       "own explanation of its latest results is not in this dossier")

    # --- the filing ----------------------------------------------------------
    row, text = docs.get("earnings_release", (None, None))
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
        # Beside management's discussion the release mostly repeats it, and at full
        # length the pair put a dossier near 120,000 characters, four to a run.
        shown = text
        if md_text and len(text) > _RELEASE_CAP_WITH_MDNA:
            cut = text.rfind(". ", 0, _RELEASE_CAP_WITH_MDNA)
            shown = text[:cut + 1] if cut > _RELEASE_CAP_WITH_MDNA * 0.6 else text[:_RELEASE_CAP_WITH_MDNA]
            w(f"Showing {len(shown):,} of {len(text):,} characters. The "
              f"whole release is in the checkout at `data/filings/{row.get('text_path', '')}`.")
            w("")
        w("```text")
        w(shown)
        w("```")
    else:
        w("No earnings release collected for this ticker. Releases are collected as they are "
          "filed, and the latest one is fetched for any name the pipeline expects the analyst "
          "to be handed. Either this name was not among them yet, or its latest results 8-K "
          "carries no press release exhibit.")
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
