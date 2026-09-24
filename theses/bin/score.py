#!/usr/bin/env python3
"""Grade the analyst's calls. Arithmetic only, and never the analyst's own work.

The analyst writes the view. This writes what the price did. Nothing here reads
a note's argument, and nothing the model produces can reach these numbers, which
is the only reason the track record is worth anything six months from now.

What a call is
--------------
Every row of theses/ledger/events.csv is a call, dated, with the note it came
from. What it is scored as comes from the memo's action when there is one, and
from the older notes' direction when there is not:

    Initiate, Add, Hold, Trim   long    scored as owning the stock
    Short                       short   scored as a short: the P&L is minus the
                                        stock's return
    Avoid                       avoid   "do not own". Right when the stock lags its
                                        sector fund over the horizon. Never scored
                                        as if it were a short: an Avoid makes no
                                        money when the stock falls, so it has no
                                        P&L, only a comparison
    Exit                        avoid   the same, measured from the exit memo's date
    direction watch / no view   none    not scored for direction (an abstention)

Until 2026-09-24 an "avoid" was scored as a short, which credited a stay-away
call with a profit it never made. No row had been scored by then.

Measures
--------
Returns are price-only (dividends excluded), from stored closes, each for the
exact date named. A missing close is never filled from another day: the measure
that needed it is left blank and the gap is flagged.

    r        = close(exit) / close(start) - 1                 the stock
    r_sector = the same for the company's SPDR sector fund     primary benchmark
    r_spx    = the same for the S&P 500 (^GSPC, _MARKET.json)
    r_peer   = median r over up to 12 sub-industry peers
    sign     = +1 long, -1 short, avoid or exit
    rel_sector = sign * (r - r_sector)   "excess return", the primary measure
    rel_spy    = sign * (r - r_spx);  rel_peer = sign * (r - r_peer)
    hit        = rel_sector > 0
    outcome    = right / wrong beyond +/-3% of rel_sector, flat inside it

start is the date of the close the note's entry price was taken from (its
entry_source), exit is the first session on or after written_on + horizon_days
with a stored close for the stock. Final scores are written only at the horizon.
Every day before that, the pipeline writes a mark (data/scoring/marks.csv).

Where things are written
------------------------
    theses/ledger/scores.csv   this script's CLI, run by the analyst (scores only)
    data/scoring/scores.csv    the daily pipeline (lambda_function.run_scoring)
    data/scoring/marks.csv     the daily pipeline: one row per open call per session

The pipeline commits data/, docs/ and state/ only; theses/ is committed by the
analyst's own delivery. So the daily record lives in data/scoring/, and the two
scores files are written by the same function from the same closes.

All three are append-only. No row is ever mutated, so a call cannot be softened
after the fact. A mark for a day that could not be completed (a close missing)
is superseded by a later row for the same call and day, never edited.

    python3 theses/bin/score.py [--dry-run] [--asof YYYY-MM-DD] [--prices-dir DIR]
    python3 theses/bin/score.py --migrate
"""
import csv, io, json, math, os, re, statistics as st, sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Appended, not prepended: the pipeline imports this module, and theses/bin holds
# a queue.py that must never shadow the standard library's. Run as a script, this
# directory is already first on the path.
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.append(str(Path(__file__).resolve().parent))
from common import (REPO, LEDGER, fetch_site, num, read_csv_rows, append_csv, csv_header,
                    is_operating)
import validate  # noqa: E402  (front-matter parser only)
import desks as desks_mod  # noqa: E402
from portfolio import engine as E  # noqa: E402

# The scores ledger. The first fifteen columns are the original ones, in their
# original order; everything after them was added on 2026-09-24 by
# migrate_scores(). Column meanings for rows with score_method "2" (every row
# since the migration): abs_return is the call's own P&L (long r, short -r,
# blank for avoid and exit), spy_return and peer_median_return are the raw
# benchmark returns, and every rel_ column carries the call's sign.
OLD_SCORE_COLUMNS = ["prediction_id", "scored_on", "horizon_end", "exit_price", "abs_return",
                     "spy_return", "rel_spy", "peer_median_return", "rel_peer", "peers_used",
                     "target_hit", "max_favourable", "max_adverse", "outcome", "note"]
NEW_SCORE_COLUMNS = ["call_id", "ticker", "written_on", "kind", "action", "direction", "stance",
                     "conviction", "sector", "desk", "sector_etf", "start_date", "start_close",
                     "exit_date", "stock_return", "sector_return", "rel_sector", "hit",
                     "spy_symbol", "scenario_landed", "brier", "note_path", "score_method"]
SCORE_COLUMNS = OLD_SCORE_COLUMNS + NEW_SCORE_COLUMNS
SCORE_METHOD = "2"

MARK_COLUMNS = ["date", "call_id", "ticker", "stance", "status", "days_elapsed", "start_date",
                "start_close", "close", "stock_return", "call_return", "sector_etf",
                "sector_start", "sector_close", "sector_return", "excess_sector", "spy_start",
                "spy_close", "spy_return", "excess_spy", "target_price", "progress_to_target",
                "thresholds_crossed", "flags", "computed_at"]

FLAT_BAND = 0.03          # inside +/-3% against the sector fund is a draw
MAX_PEERS = 12
SPX = "^GSPC"
MIN_GROUP = 10            # below this many scored calls a group is "too few to read"
DATA_SCORING = REPO / "data" / "scoring"

# ---------------------------------------------------------------------------
# Desks. The sector-to-desk map is the "desks" key of theses/config.json, read
# through theses/bin/desks.py, the same map the director and the dossiers use.
# A call is counted under the desk that owns its company's sector, by the desk's
# name (the "# " heading of theses/desks/{slug}.md).


def desk_for_sector(sector):
    """The name of the desk that covers a GICS sector, or "" when not known."""
    slug = desks_mod.desk_for(sector)
    return desks_mod.desk_title(slug) if slug else ""


def desk_list():
    """[(desk name, [sectors])], in the map's order."""
    m = desks_mod.desk_map()
    out = []
    for slug in desks_mod.desks(m):
        out.append((desks_mod.desk_title(slug), [s for s, d in m.items() if d == slug]))
    return out


def sector_etf(sector):
    return E.SECTOR_ETFS.get((sector or "").strip(), "")


# ---------------------------------------------------------------------------
# Calls

STANCE_BY_ACTION = {"initiate": "long", "add": "long", "hold": "long", "trim": "long",
                    "short": "short", "avoid": "avoid", "exit": "avoid"}
STANCE_BY_DIRECTION = {"long": "long", "short": "short", "avoid": "avoid"}
SIGN = {"long": 1, "short": -1, "avoid": -1}


def stance_of(action, direction):
    """(stance, source). stance is long, short, avoid, or "" (not scored)."""
    a = (action or "").strip().lower()
    if a in STANCE_BY_ACTION:
        return STANCE_BY_ACTION[a], "action"
    d = (direction or "").strip().lower()
    return STANCE_BY_DIRECTION.get(d, ""), "direction"


def horizon_end(written_on, horizon_days):
    try:
        return (datetime.strptime(written_on, "%Y-%m-%d").date()
                + timedelta(days=int(float(horizon_days)))).isoformat()
    except (TypeError, ValueError):
        return ""


_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def _s(v):
    return "" if v is None else str(v).strip().strip('"')


def parse_scenarios(items):
    """[(case, value, probability)] from the memo's scenarios list, or []."""
    out = []
    for it in items or []:
        txt = str(it)
        case = re.search(r"case:\s*([a-z]+)", txt)
        val = re.search(r"value:\s*([-\d.]+)", txt)
        prob = re.search(r"probability:\s*([-\d.]+)", txt)
        if case and val and prob:
            v, p = num(val.group(1)), num(prob.group(1))
            if v is not None and p is not None:
                out.append((case.group(1).lower(), v, p))
    return out


_PRICE_CHECK = re.compile(r"^\s*(?:the\s+)?(?:share|stock|closing)\s+price\s*$|^\s*(?:price|close)\s*$", re.I)
_DOLLAR = re.compile(r"\$\s?(\d[\d,]*(?:\.\d+)?)(?!\d)(\s*(?:[BbMmKkTt]\b|billion|million|thousand|trillion))?")
_UP = re.compile(r"\b(?:above|over|or more|or higher|at least|exceeds?|rises?|higher)\b", re.I)
_DOWN = re.compile(r"\b(?:below|under|or less|or lower|at most|falls?|lower)\b", re.I)


def price_thresholds(body, entry_price=None):
    """The price rows of a memo's monitoring table: [{"level", "op", "action", "label"}].

    Only a row whose "What I check" cell names the share price itself, and whose
    Threshold holds a plain dollar amount (not $108.0B), can be checked by a
    machine against a close. Every other row (guidance, margins, days of sales
    owed) needs a filing and a reader, and is not checked here. The direction is
    read from the threshold's words; with none, a level above the entry price is
    crossed going up and one below it going down."""
    if not body:
        return []
    sec = re.search(r"^##\s+4\.[^\n]*\n(.*?)(?=^##\s|\Z)", body, re.M | re.S)
    if not sec:
        return []
    out = []
    for line in sec.group(1).splitlines():
        if not line.strip().startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 4 or set(cells[0]) <= set("-: "):
            continue
        if not _PRICE_CHECK.match(cells[0]):
            continue
        m = _DOLLAR.search(cells[2])
        if not m or m.group(2):
            continue
        level = num(m.group(1).replace(",", ""))
        if level is None:
            continue
        if _UP.search(cells[2]):
            op = ">="
        elif _DOWN.search(cells[2]):
            op = "<="
        elif entry_price:
            op = ">=" if level >= entry_price else "<="
        else:
            continue
        out.append({"level": level, "op": op, "action": cells[3], "label": cells[2]})
    return out


def _read_note(note_path, root=None):
    p = (Path(root) if root else REPO) / note_path if note_path else None
    if not p or not p.is_file():
        return {}, ""
    fm, body = validate.parse(p.read_text(encoding="utf-8"))
    return fm or {}, body or ""


def load_calls(events=None, predictions=None, root=None):
    """Every call in the ledger, oldest first, as plain dicts.

    One call per event row. A prediction is attached by thesis_id; a prediction
    with no event (none exist) becomes a call of its own so nothing is lost."""
    events = read_csv_rows(LEDGER / "events.csv") if events is None else events
    predictions = read_csv_rows(LEDGER / "predictions.csv") if predictions is None else predictions
    by_thesis = {}
    for p in predictions:
        by_thesis.setdefault((p.get("thesis_id"), p.get("ticker")), p)
    calls, used = [], set()
    for e in events:
        t = (e.get("ticker") or "").strip()
        if not t:
            continue
        fm, body = _read_note(e.get("note_path"), root)
        pred = by_thesis.get((e.get("thesis_id"), t))
        if pred:
            used.add(pred.get("prediction_id"))
        calls.append(_call(e, fm, body, pred))
    for p in predictions:
        if p.get("prediction_id") not in used:
            e = {"event_id": p["prediction_id"], "date": p.get("written_on"), "ticker": p.get("ticker"),
                 "kind": "", "thesis_id": p.get("thesis_id"), "note_path": "",
                 "direction": p.get("direction"), "conviction": p.get("conviction"),
                 "target_price": p.get("target_price"), "horizon_days": p.get("horizon_days")}
            calls.append(_call(e, {}, "", p))
    return calls


def _call(e, fm, body, pred):
    written = _s(e.get("date")) or _s(fm.get("written_on"))
    action = _s(e.get("action")) or _s(fm.get("action"))
    direction = _s(e.get("direction")) or _s(fm.get("direction"))
    stance, source = stance_of(action, direction)
    m = _DATE.search(_s(fm.get("entry_source")))
    start = (m.group(0) if m else "") or _s(fm.get("panel_date")) or _s((pred or {}).get("panel_date")) or written
    entry = num(fm.get("entry_price")) or num((pred or {}).get("entry_price"))
    hz = _s(e.get("horizon_days")) or _s(fm.get("horizon_days"))
    kind = _s(e.get("kind")) or _s(fm.get("kind"))
    return {
        "call_id": _s(e.get("event_id")), "ticker": _s(e.get("ticker")), "written_on": written,
        "kind": kind, "stage": "initiation" if kind in ("initiate", "initiation") else "revision",
        "thesis_id": _s(e.get("thesis_id")), "note_path": _s(e.get("note_path")),
        "action": action, "direction": direction, "stance": stance, "stance_from": source,
        "conviction": _s(e.get("conviction")) or _s(fm.get("conviction")),
        "target_price": num(e.get("target_price")) or num(fm.get("target_price")),
        "horizon_days": hz, "horizon_end": horizon_end(written, hz),
        "start_date": start, "entry_price": entry,
        "scenarios": parse_scenarios(fm.get("scenarios")),
        "thresholds": price_thresholds(body, entry),
        "prediction_id": _s((pred or {}).get("prediction_id")),
    }


def action_label(call):
    """The action group a call is counted under on the scorecard."""
    if call.get("action"):
        return call["action"][:1].upper() + call["action"][1:].lower()
    d = (call.get("direction") or "").lower()
    return f"{d.capitalize()} (older note)" if d else "No view"


def superseded(calls):
    """{call_id: the call_id of the next call on the same ticker}."""
    out, last = {}, {}
    for c in calls:
        prev = last.get(c["ticker"])
        if prev:
            out[prev] = c["call_id"]
        last[c["ticker"]] = c["call_id"]
    return out


# ---------------------------------------------------------------------------
# Prices


class LocalPrices:
    """Stored closes from a docs/prices directory: per-ticker files, the benchmark
    funds (_BENCHMARKS.json) and the S&P 500 (_MARKET.json)."""

    def __init__(self, prices_dir=None):
        self.stocks = E.PriceStore(prices_dir)
        self.benches = E.BenchmarkStore(prices_dir)

    def stock(self, ticker):
        return self.stocks.series(ticker)

    def bench(self, symbol):
        return self.benches.series(symbol)


class SitePrices:
    """The same files read from the published site, for a checkout without docs/prices."""

    def __init__(self):
        self._stock, self._bench = {}, None

    def _json(self, path):
        raw = fetch_site(path)
        try:
            return json.loads(raw) if raw else {}
        except ValueError:
            return {}

    def stock(self, ticker):
        if ticker not in self._stock:
            d = self._json(f"prices/{E.price_filename(ticker)}")
            self._stock[ticker] = {str(a): float(b) for a, b in (d.get("closes") or [])
                                   if num(b) and float(b) > 0}
        return self._stock[ticker]

    def bench(self, symbol):
        if self._bench is None:
            self._bench = {}
            d = self._json(f"prices/{E.BENCHMARKS_FILE}")
            for sym, s in (d.get("series") or {}).items():
                self._bench[sym] = {str(a): float(b) for a, b in (s.get("closes") or [])
                                    if num(b) and float(b) > 0}
            m = self._json(f"prices/{E.MARKET_FILE}")
            self._bench[SPX] = {str(a): float(b) for a, b in (m.get("benchmark") or [])
                                if num(b) and float(b) > 0}
        return dict(self._bench.get(symbol) or {})


def default_prices(prices_dir=None):
    d = Path(prices_dir) if prices_dir else REPO / "docs" / "prices"
    if d.is_dir() and any(d.glob("*.json")):
        return LocalPrices(d)
    return SitePrices()


class Panel:
    """Panel rows by date, loaded from data/fundamentals on demand."""

    def __init__(self, panel_dir=None):
        self.dir = panel_dir
        self._dates = None
        self._rows = {}

    def dates(self):
        if self._dates is None:
            self._dates = E.panel_dates(self.dir)
        return self._dates

    def asof(self, day):
        """{ticker: row} for the latest panel date on or before `day` (else the first)."""
        ds = self.dates()
        if not ds:
            return {}
        pick = max((d for d in ds if d <= day), default=ds[0])
        if pick not in self._rows:
            _, rows = E.panel_rows(pick, self.dir)
            self._rows[pick] = {r["ticker"]: r for r in rows}
        return self._rows[pick]


def _ret(series, a, b):
    """series[b] / series[a] - 1, or None when either exact close is missing."""
    x, y = series.get(a), series.get(b)
    return (y / x - 1) if x and y else None


def peer_return(ticker, rows, prices, start, end):
    """Median return of up to 12 sub-industry peers, each from its stored closes on
    exactly `start` and `end`. A peer missing either close is left out, not
    estimated. Peers are operating companies only (a fund's return is its
    portfolio's, not a competitor's)."""
    me = rows.get(ticker)
    if not me:
        return None, 0
    peers = {t: r for t, r in rows.items() if t != ticker and is_operating(r)}
    sub = (me.get("sub_industry") or "").strip()
    cohort = [t for t, r in peers.items() if sub and (r.get("sub_industry") or "").strip() == sub]
    if len(cohort) < 3:
        sec = (me.get("sector") or "").strip()
        cohort = [t for t, r in peers.items() if sec and (r.get("sector") or "").strip() == sec]
    cohort = sorted(cohort, key=lambda t: (-(num(rows[t].get("market_cap")) or 0), t))[:MAX_PEERS]
    rets = [r for r in (_ret(prices.stock(t), start, end) for t in cohort) if r is not None]
    return (st.median(rets) if rets else None), len(rets)


# ---------------------------------------------------------------------------
# Marks and scores


def _r(v, n=6):
    return "" if v is None else round(v, n)


def archived_starts(marks_rows):
    """{call_id: {"start_close", "sector_start", "spy_start"}} from the first mark
    that recorded each. These were stored closes for the call's start date when
    written, so they stay usable after the price files have rolled past it."""
    out = {}
    for r in marks_rows:
        cur = out.setdefault(r.get("call_id"), {})
        for k in ("start_close", "sector_start", "spy_start"):
            if k not in cur and num(r.get(k)):
                cur[k] = num(r.get(k))
    return out


def _starts(call, prices, sec_etf, archive):
    arc = archive.get(call["call_id"], {})
    d = call["start_date"]
    s0 = prices.stock(call["ticker"]).get(d) or arc.get("start_close")
    b0 = (prices.bench(sec_etf).get(d) if sec_etf else None) or (arc.get("sector_start") if sec_etf else None)
    m0 = prices.bench(SPX).get(d) or arc.get("spy_start")
    return s0, b0, m0


def call_sector(call, panel):
    row = panel.asof(call["start_date"] or call["written_on"]).get(call["ticker"]) if panel else None
    return (row or {}).get("sector") or ""


def mark_call(call, day, prices, sector, archive=None, now=""):
    """One day's mark-to-market for an open call. Never a final score."""
    archive = archive or {}
    etf = sector_etf(sector)
    s0, b0, m0 = _starts(call, prices, etf, archive)
    t, d0 = call["ticker"], call["start_date"]
    close = prices.stock(t).get(day)
    bc = prices.bench(etf).get(day) if etf else None
    mc = prices.bench(SPX).get(day)
    flags = []
    if not s0:
        flags.append(f"no stored close for {t} on the start date {d0}")
    if close is None:
        flags.append(f"no stored close for {t} on {day}")
    if not etf:
        flags.append(f"no sector fund for sector {sector!r}")
    elif not b0:
        flags.append(f"no stored {etf} close on {d0}")
    elif bc is None:
        flags.append(f"no stored {etf} close on {day}")
    if not m0:
        flags.append(f"no stored {SPX} close on {d0}")
    elif mc is None:
        flags.append(f"no stored {SPX} close on {day}")
    sign = SIGN.get(call["stance"], 1)
    r = (close / s0 - 1) if (close and s0) else None
    rs = (bc / b0 - 1) if (bc and b0) else None
    rm = (mc / m0 - 1) if (mc and m0) else None
    status = "skipped" if r is None else ("ok" if not flags else "partial")
    tgt = call.get("target_price")
    prog = ((close - s0) / (tgt - s0)) if (r is not None and tgt and abs(tgt - s0) > 1e-9) else None
    crossed = []
    if close is not None and s0:
        # Thresholds are in the note's price basis; the stored series may have
        # been rebased since (a split), so the close is taken back to that basis.
        basis = (call.get("entry_price") or s0) / s0
        px = close * basis
        for th in call.get("thresholds") or []:
            if (px >= th["level"]) if th["op"] == ">=" else (px <= th["level"]):
                crossed.append(f"{'at or above' if th['op'] == '>=' else 'at or below'} "
                               f"${th['level']:,.2f} ({th['action']})")
    return {
        "date": day, "call_id": call["call_id"], "ticker": t, "stance": call["stance"],
        "status": status, "days_elapsed": _days(d0, day), "start_date": d0,
        "start_close": _r(s0), "close": _r(close), "stock_return": _r(r),
        "call_return": _r(None if r is None or call["stance"] == "avoid" else sign * r),
        "sector_etf": etf, "sector_start": _r(b0), "sector_close": _r(bc), "sector_return": _r(rs),
        "excess_sector": _r(sign * (r - rs) if r is not None and rs is not None else None),
        "spy_start": _r(m0), "spy_close": _r(mc), "spy_return": _r(rm),
        "excess_spy": _r(sign * (r - rm) if r is not None and rm is not None else None),
        "target_price": _r(tgt, 4), "progress_to_target": _r(prog),
        "thresholds_crossed": "; ".join(crossed), "flags": "; ".join(flags), "computed_at": now,
    }


def _days(a, b):
    try:
        return (datetime.strptime(b, "%Y-%m-%d") - datetime.strptime(a, "%Y-%m-%d")).days
    except (TypeError, ValueError):
        return ""


def _mark_key(r):
    return tuple(str(r.get(k, "")) for k in MARK_COLUMNS if k not in ("computed_at",))


def latest_rows(rows, key):
    out = {}
    for r in rows:
        out[key(r)] = r
    return out


def record_marks(calls, sessions, prices, panel, marks_csv, today, now=None):
    """Append a mark for every open call on every session from its start date to
    today (not past its horizon). A day already marked "ok" is left alone; a
    skipped or partial one gets a new row only if something has changed, which
    supersedes it. Returns the number of rows appended."""
    now = now or datetime.now(timezone.utc).isoformat(timespec="seconds")
    have_rows = E.read_rows(marks_csv)
    have = latest_rows(have_rows, lambda r: (r["call_id"], r["date"]))
    archive = archived_starts(have_rows)
    new = []
    for c in calls:
        if not c["stance"] or not c["start_date"]:
            continue
        end = min(today, c["horizon_end"] or today)
        sector = call_sector(c, panel)
        for d in sessions:
            if d < c["start_date"] or d > end:
                continue
            last = have.get((c["call_id"], d))
            if last and last.get("status") == "ok":
                continue
            row = mark_call(c, d, prices, sector, archive, now)
            if last and _mark_key({k: E._cell(v) for k, v in row.items()}) == _mark_key(last):
                continue
            new.append(row)
    E.append_rows(marks_csv, MARK_COLUMNS, new)
    return len(new)


def brier(scenarios, entry_price, r):
    """(landed case, Brier score) for a scored call with three scenarios.

    Each case's value becomes a return from the note's own entry price, and the
    outcome is the case whose return is nearest the stock's actual return. The
    score is the multi-category Brier: sum over cases of (stated probability -
    1 if landed else 0) squared. 0 is perfect, 2 the worst; always saying one
    third each scores 0.667 on any outcome."""
    if len(scenarios) != 3 or not entry_price or r is None:
        return "", None
    rets = [(c, v / entry_price - 1, p) for c, v, p in scenarios]
    landed = min(rets, key=lambda x: (abs(x[1] - r), x[0]))[0]
    return landed, sum((p - (1.0 if c == landed else 0.0)) ** 2 for c, _, p in rets)


def score_call(call, prices, panel, today, archive=None):
    """The final score of a call whose horizon has passed, or None while it has
    not (or while no stored close on or after the horizon exists yet)."""
    archive = archive or {}
    if not call["stance"] or not call["horizon_end"] or call["horizon_end"] > today:
        return None
    t = call["ticker"]
    s = prices.stock(t)
    after = sorted(d for d in s if d >= call["horizon_end"])
    base = {"prediction_id": call.get("prediction_id", ""), "scored_on": today,
            "horizon_end": call["horizon_end"], "call_id": call["call_id"], "ticker": t,
            "written_on": call["written_on"], "kind": call["kind"], "action": call["action"],
            "direction": call["direction"], "stance": call["stance"],
            "conviction": call["conviction"], "start_date": call["start_date"],
            "note_path": call["note_path"], "spy_symbol": SPX, "score_method": SCORE_METHOD}
    sector = call_sector(call, panel)
    etf = sector_etf(sector)
    base.update(sector=sector, desk=desk_for_sector(sector), sector_etf=etf)
    s0, b0, m0 = _starts(call, prices, etf, archive)
    if not s0:
        return dict(base, outcome="voided", note=f"no stored close for {t} on the start date "
                                                  f"{call['start_date']}")
    if not after:
        if s and max(s) < call["horizon_end"] and today > call["horizon_end"]:
            return None     # the series has not reached the horizon yet; wait
        return None
    xd = after[0]
    x = s[xd]
    sign = SIGN[call["stance"]]
    r = x / s0 - 1
    notes = []
    bc = prices.bench(etf).get(xd) if etf else None
    rs = (bc / b0 - 1) if (bc and b0) else None
    if etf and rs is None:
        notes.append(f"no stored {etf} close on {call['start_date'] if not b0 else xd}")
    mc = prices.bench(SPX).get(xd)
    rm = (mc / m0 - 1) if (mc and m0) else None
    if rm is None:
        notes.append(f"no stored {SPX} close on {call['start_date'] if not m0 else xd}")
    pm, npeers = peer_return(t, panel.asof(call["start_date"]) if panel else {}, prices,
                             call["start_date"], xd)
    window = [v for d, v in s.items() if call["start_date"] <= d <= xd]
    tgt = call.get("target_price")
    hit_t = ""
    if tgt and window:
        basis = (call.get("entry_price") or s0) / s0
        hi, lo = max(window) * basis, min(window) * basis
        hit_t = "yes" if ((hi >= tgt) if tgt >= (call.get("entry_price") or s0) else (lo <= tgt)) else "no"
    mfe = (max(window) / s0 - 1) * sign if sign > 0 else (min(window) / s0 - 1) * sign
    mae = (min(window) / s0 - 1) * sign if sign > 0 else (max(window) / s0 - 1) * sign
    rel_sector = sign * (r - rs) if rs is not None else None
    if rel_sector is None:
        outcome = "unbenchmarked"
        notes.append("no sector fund return, so the call is not graded")
    elif rel_sector > FLAT_BAND:
        outcome = "right"
    elif rel_sector < -FLAT_BAND:
        outcome = "wrong"
    else:
        outcome = "flat"
        notes.append(f"inside the +/-{FLAT_BAND * 100:.0f}% band against the sector fund")
    landed, b = brier(call.get("scenarios") or [], call.get("entry_price"), r)
    return dict(base, exit_price=round(x, 4), exit_date=xd, start_close=round(s0, 4),
                abs_return=_r(None if call["stance"] == "avoid" else sign * r, 4),
                stock_return=_r(r, 4), sector_return=_r(rs, 4), rel_sector=_r(rel_sector, 4),
                hit="" if rel_sector is None else ("yes" if rel_sector > 0 else "no"),
                spy_return=_r(rm, 4), rel_spy=_r(sign * (r - rm) if rm is not None else None, 4),
                peer_median_return=_r(pm, 4),
                rel_peer=_r(sign * (r - pm) if pm is not None else None, 4), peers_used=npeers,
                target_hit=hit_t, max_favourable=_r(mfe, 4), max_adverse=_r(mae, 4),
                outcome=outcome, note="; ".join(notes), scenario_landed=landed,
                brier=_r(b, 4))


def score_calls(calls, prices, panel, today, done, archive=None):
    """Score rows for every matured call not in `done` (a set of call ids)."""
    out = []
    for c in calls:
        if c["call_id"] in done:
            continue
        row = score_call(c, prices, panel, today, archive)
        if row:
            out.append(row)
    return out


def scored_ids(rows):
    return {r.get("call_id") or r.get("prediction_id") for r in rows}


# ---------------------------------------------------------------------------
# The one-time migration of theses/ledger/scores.csv


def migrate_scores(path=None, write=True):
    """Give scores.csv the columns added on 2026-09-24, once, at the end, blank on
    every existing row, without disturbing a byte of what is there.

    The file is rewritten only if serialising its parsed rows back reproduces its
    bytes exactly, so the only change is the longer header and empty cells. The
    new file is written beside the old one, swapped in with one rename, and read
    back; on any difference the original is restored. Running it again finds the
    new header and does nothing. Returns "absent", "current", "migrated" or, with
    write=False, "would migrate"."""
    path = Path(path) if path else LEDGER / "scores.csv"
    header = csv_header(path)
    if header is None:
        return "absent"
    if header == SCORE_COLUMNS:
        return "current"
    if header != OLD_SCORE_COLUMNS:
        raise SystemExit(f"score: {path.name} has a header this migration does not know; left alone.\n"
                         f"  file header: {header}\n  expected   : {OLD_SCORE_COLUMNS}")
    text = path.read_bytes().decode("utf-8")
    rows = list(csv.reader(io.StringIO(text, newline="")))
    if any(len(r) != len(OLD_SCORE_COLUMNS) for r in rows[1:]):
        raise SystemExit(f"score: {path.name} has a row of the wrong width. Nothing was changed.")

    def dump(row, term):
        buf = io.StringIO(newline="")
        csv.writer(buf, lineterminator=term).writerow(row)
        return buf.getvalue()

    terms, pos = [], 0
    for r in rows:
        term = next((t for t in ("\r\n", "\n", "") if text.startswith(dump(r, t), pos)
                     and (t or pos + len(dump(r, t)) == len(text))), None)
        if term is None:
            raise SystemExit(f"score: {path.name} cannot be written back byte for byte. Nothing was changed.")
        terms.append(term)
        pos += len(dump(r, term))
    if pos != len(text):
        raise SystemExit(f"score: {path.name} has trailing text after its last row. Nothing was changed.")
    if not write:
        return "would migrate"
    pad = [""] * len(NEW_SCORE_COLUMNS)
    new = "".join(dump(r, t) for r, t in zip([SCORE_COLUMNS] + [r + pad for r in rows[1:]], terms))
    tmp = path.with_name(path.name + ".migrating")
    with tmp.open("w", encoding="utf-8", newline="") as fh:
        fh.write(new)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    back = list(csv.reader(io.StringIO(path.read_bytes().decode("utf-8"), newline="")))
    if back[0] != SCORE_COLUMNS or [r[:len(OLD_SCORE_COLUMNS)] for r in back[1:]] != rows[1:]:
        path.write_text(text, encoding="utf-8", newline="")
        raise SystemExit(f"score: {path.name} did not read back as written; the original was restored.")
    return "migrated"


# ---------------------------------------------------------------------------
# The scorecard: aggregates and calibration


def _group(rows):
    ex = [num(r.get("rel_sector")) for r in rows]
    ex = [x for x in ex if x is not None]
    hits = sum(1 for r in rows if r.get("hit") == "yes")
    n = len(ex)
    return {"n": n, "hits": hits,
            "hitRate": round(hits / n, 4) if n else None,
            "meanExcess": round(sum(ex) / n, 4) if n else None,
            "medianExcess": round(st.median(ex), 4) if n else None,
            "tooFew": n < MIN_GROUP}


def graded(scores):
    """Rows scored for direction against a sector fund (voided and unbenchmarked
    rows are counted separately, never in a hit rate)."""
    return [r for r in scores if r.get("hit") in ("yes", "no")]


def aggregates(scores, calls):
    """Hit rate and excess return by desk, conviction, action and stage, with the
    number of scored and open calls in every group, always."""
    g = graded(scores)
    done = scored_ids(scores)
    open_calls = [c for c in calls if c["stance"] and c["call_id"] not in done]
    by_call = {c["call_id"]: c for c in calls}

    def key_row(r, dim):
        c = by_call.get(r.get("call_id")) or {}
        if dim == "desk":
            return r.get("desk") or "Sector not known"
        if dim == "conviction":
            return str(r.get("conviction") or c.get("conviction") or "?")
        if dim == "action":
            return action_label(c or {"action": r.get("action"), "direction": r.get("direction")})
        return "Initiation" if (c.get("stage") or ("initiation" if r.get("kind") == "initiate" else "revision")) == "initiation" else "Revision"

    def key_call(c, dim, sector_of):
        if dim == "desk":
            return desk_for_sector(sector_of.get(c["call_id"], "")) or "Sector not known"
        if dim == "conviction":
            return str(c.get("conviction") or "?")
        if dim == "action":
            return action_label(c)
        return "Initiation" if c["stage"] == "initiation" else "Revision"

    return {"overall": _group(g), "open": len(open_calls),
            "unscored": len([s for s in scores if s.get("hit") not in ("yes", "no")]),
            "_key_row": key_row, "_key_call": key_call, "_open": open_calls, "_graded": g}


def group_table(agg, dim, sector_of, order=None):
    rows = {}
    for r in agg["_graded"]:
        rows.setdefault(agg["_key_row"](r, dim), []).append(r)
    opens = {}
    for c in agg["_open"]:
        k = agg["_key_call"](c, dim, sector_of)
        opens[k] = opens.get(k, 0) + 1
    keys = list(order or [])
    for k in sorted(set(rows) | set(opens)):
        if k not in keys:
            keys.append(k)
    return [dict(_group(rows.get(k, [])), name=k, open=opens.get(k, 0)) for k in keys]


def calibration(scores):
    """For scored calls with three scenarios: where the outcome landed against the
    stated probabilities, and the mean Brier score."""
    rows = [r for r in scores if r.get("scenario_landed") and num(r.get("brier")) is not None]
    n = len(rows)
    return {"n": n, "tooFew": n < MIN_GROUP,
            "meanBrier": round(sum(num(r["brier"]) for r in rows) / n, 4) if n else None,
            "uniformBrier": round(2.0 / 3.0, 4),
            "landed": {c: sum(1 for r in rows if r["scenario_landed"] == c) for c in ("bull", "base", "bear")}}


def calibration_with_probs(scores, calls):
    """calibration() plus, per case, the mean probability the notes gave it."""
    out = calibration(scores)
    by = {c["call_id"]: c for c in calls}
    stated = {"bull": [], "base": [], "bear": []}
    for r in scores:
        if not r.get("scenario_landed"):
            continue
        for case, _v, p in (by.get(r.get("call_id")) or {}).get("scenarios") or []:
            if case in stated:
                stated[case].append(p)
    out["cases"] = [{"case": c, "stated": round(sum(v) / len(v), 4) if v else None,
                     "landed": out["landed"][c],
                     "share": round(out["landed"][c] / out["n"], 4) if out["n"] else None}
                    for c, v in stated.items()]
    return out


def scorecard_data(calls, scores, marks, sector_of, repo_url=""):
    """Everything scorecard.html draws, as plain JSON."""
    agg = aggregates(scores, calls)
    sup = superseded(calls)
    last_mark = latest_rows(marks, lambda r: r["call_id"])
    # the newest complete-or-partial mark per call, skipping days that were skipped
    usable = {}
    for r in marks:
        if r.get("status") in ("ok", "partial"):
            usable[r["call_id"]] = r
    done = {r.get("call_id"): r for r in scores}
    open_rows = []
    for c in agg["_open"]:
        m = usable.get(c["call_id"]) or {}
        lm = last_mark.get(c["call_id"]) or {}
        open_rows.append({
            "callId": c["call_id"], "ticker": c["ticker"], "written": c["written_on"],
            "action": action_label(c), "stance": c["stance"], "conviction": c["conviction"],
            "stage": c["stage"], "desk": desk_for_sector(sector_of.get(c["call_id"], "")),
            "sector": sector_of.get(c["call_id"], ""), "etf": sector_etf(sector_of.get(c["call_id"], "")),
            "horizonEnd": c["horizon_end"], "note": c["note_path"],
            "supersededBy": sup.get(c["call_id"], ""),
            "markDate": m.get("date") or "", "days": num(m.get("days_elapsed")),
            "start": num(m.get("start_close")), "close": num(m.get("close")),
            "ret": num(m.get("stock_return")), "callRet": num(m.get("call_return")),
            "excessSector": num(m.get("excess_sector")), "excessSpx": num(m.get("excess_spy")),
            "target": c.get("target_price"), "progress": num(m.get("progress_to_target")),
            "crossed": m.get("thresholds_crossed") or "",
            "thresholds": len(c.get("thresholds") or []),
            "flags": lm.get("flags") if lm.get("date") != m.get("date") or lm.get("flags") else "",
            "lastFlagDate": lm.get("date") or "",
        })
    scored_rows = []
    for r in scores:
        scored_rows.append({k: r.get(k) for k in (
            "call_id", "ticker", "written_on", "action", "stance", "conviction", "desk", "sector_etf",
            "start_date", "exit_date", "stock_return", "sector_return", "rel_sector", "hit",
            "abs_return", "rel_spy", "rel_peer", "peers_used", "outcome", "note", "note_path",
            "scenario_landed", "brier", "target_hit")})
    not_scored = [{"callId": c["call_id"], "ticker": c["ticker"], "written": c["written_on"],
                   "action": action_label(c), "note": c["note_path"]}
                  for c in calls if not c["stance"]]
    conv_order = ["5", "4", "3", "2", "1", "0"]
    return {
        "overall": agg["overall"], "openCount": agg["open"], "unscored": agg["unscored"],
        "minGroup": MIN_GROUP, "flatBand": FLAT_BAND,
        "open": open_rows, "scored": scored_rows, "notScored": not_scored,
        "byDesk": group_table(agg, "desk", sector_of, [d for d, _ in desk_list()]),
        "byConviction": [g for g in group_table(agg, "conviction", sector_of, conv_order)
                         if g["n"] or g["open"]],
        "byAction": group_table(agg, "action", sector_of),
        "byStage": group_table(agg, "stage", sector_of, ["Initiation", "Revision"]),
        "calibration": calibration_with_probs(scores, calls),
        "desks": [{"name": d, "sectors": ss} for d, ss in desk_list()],
        "sectorEtfs": E.SECTOR_ETFS, "repoUrl": repo_url,
        "done": sorted(k for k in done if k),
    }


# ---------------------------------------------------------------------------
# The pipeline's daily step


def run_daily(prices_dir, panel_dir, scores_csv=None, marks_csv=None, today=None,
              events=None, predictions=None, root=None, now=None, log=print):
    """Marks for every open call, and final scores for every call that matured.
    Returns (calls, marks appended, scores appended)."""
    scores_csv = Path(scores_csv or DATA_SCORING / "scores.csv")
    marks_csv = Path(marks_csv or DATA_SCORING / "marks.csv")
    today = today or datetime.now(timezone.utc).date().isoformat()
    calls = load_calls(events, predictions, root)
    prices = LocalPrices(prices_dir)
    panel = Panel(panel_dir)
    sessions = [d for d in panel.dates() if d <= today]
    n_marks = record_marks(calls, sessions, prices, panel, marks_csv, today, now)
    archive = archived_starts(E.read_rows(marks_csv))
    have = E.read_rows(scores_csv)
    rows = score_calls(calls, prices, panel, today, scored_ids(have), archive)
    E.append_rows(scores_csv, SCORE_COLUMNS, rows)
    log(f"scoring: {len(calls)} calls, {sum(1 for c in calls if c['stance'])} scored for "
        f"direction; appended {n_marks} mark(s) and {len(rows)} final score(s).")
    return calls, n_marks, len(rows)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--migrate" in argv:
        print(f"scores.csv: {migrate_scores()}")
        return 0
    dry = "--dry-run" in argv
    today = (argv[argv.index("--asof") + 1] if "--asof" in argv
             else datetime.now(tz=timezone.utc).date().isoformat())
    prices_dir = argv[argv.index("--prices-dir") + 1] if "--prices-dir" in argv else None
    calls = load_calls()
    have = read_csv_rows(LEDGER / "scores.csv")
    done = scored_ids(have)
    live = [c for c in calls if c["stance"] and c["call_id"] not in done]
    print(f"score: {len(calls)} calls, {len(live)} open and scored for direction, "
          f"{len(done)} already scored, as of {today}.")
    matured = [c for c in live if c["horizon_end"] and c["horizon_end"] <= today]
    if not matured:
        print("score: nothing has reached its horizon.")
        return 0
    rows = score_calls(matured, default_prices(prices_dir), Panel(), today, done)
    for r in rows:
        print(f"  {r['call_id']:22} {r['stance']:6} {r.get('outcome', ''):14} "
              f"vs sector {r.get('rel_sector', '')!s:>8}  vs peers {r.get('rel_peer', '')!s:>8}")
    if dry:
        print("\nscore: --dry-run, nothing written.")
        return 0
    migrate_scores()
    n = append_csv(LEDGER / "scores.csv", SCORE_COLUMNS, rows)
    print(f"\nscore: appended {n} rows to theses/ledger/scores.csv")
    return 0


if __name__ == "__main__":
    sys.exit(main())
