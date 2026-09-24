#!/usr/bin/env python3
"""What the Research Director reads before writing a week's plan.

Deterministic and model-free, like prepare.py: the director clones the repo,
runs this, and plans from its output. Everything it reports is computed from
committed files and the latest panel, so any week's plan can be read against
exactly what the director was shown.

    python3 theses/bin/director_inputs.py [--week-of YYYY-MM-DD] [--today YYYY-MM-DD] [--out DIR]
                                          [--no-news]

--week-of is the Monday the plan covers (default: the next Monday after today,
US Eastern). Writes {DIR}/{week_of}.json and {DIR}/{week_of}.md (default DIR is
theses/director/inputs/) and prints the markdown summary. Zero model tokens.

The pack holds:
  holdings_without_memo    names a model book holds with no current memo
  candidates_without_memo  style books' rules candidates with no memo
  earnings_ahead           covered or held names reporting in the next 10 trading days
  stale_views              covered names past review_by or moved beyond the threshold
  screen                   what the screen would hand the analyst, and its top names
  last_week_memos          notes written last week, with validate.py results and word counts
  last_plan                last week's plan, and which assignments became notes
  desk_scorecard           per desk: names covered, calls open and scored, last week's memos
  coverage_by_desk         per desk: covered names against the names the screen can see
  pm_open_questions        questions in the PM's letters of the last 14 days, if any
  slots                    the week's weekdays and the slots each run has
  news_scope               the covered, held, candidate and screen names, for the record
  news                     whether the News Desk's pack in theses/news/latest/ is fresh

It does not build the news. The News Desk routine (theses/NEWS_DESK.md) builds
and labels it on its own schedule; this only reads theses/news/latest/news.json
and says whether its as-of time is within NEWS_FRESH_HOURS (common.py). A
missing or stale pack never stops the inputs: the director then plans without
headlines and says so.
"""
import io, json, re, sys
from contextlib import redirect_stdout
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (REPO, THESES, LEDGER, NEWS_LATEST, is_operating, load_panel, news_freshness, num,
                    read_csv_rows)
import desks as D
import director_check as DC

EASTERN = ZoneInfo("America/New_York")
NOTES = THESES / "notes"
LETTERS = REPO / "portfolio" / "letters"
OUT_DIR = THESES / "director" / "inputs"
EARNINGS_TRADING_DAYS = 10
SCREEN_TOP = 15
LETTER_DAYS = 14


def trading_days_after(day, n):
    """The next n weekdays after `day`. Exchange holidays are not known here."""
    out, d = [], day
    while len(out) < n:
        d += timedelta(days=1)
        if d.weekday() < 5:
            out.append(d)
    return out


def front_matter(path):
    """A note's front-matter, as validate.parse reads it, or {}."""
    import validate
    try:
        fm, _ = validate.parse(Path(path).read_text(encoding="utf-8"))
    except OSError:
        return {}
    return fm or {}


def notes_by_ticker(notes_dir=None):
    """{ticker: [path, ...]} oldest first."""
    out = {}
    d = Path(notes_dir or NOTES)
    if d.is_dir():
        for tdir in sorted(d.iterdir()):
            if tdir.is_dir():
                notes = sorted(tdir.glob("*.md"))
                if notes:
                    out[tdir.name] = notes
    return out


def current_views(events):
    """The newest event per ticker, leaving out closed views."""
    latest = {}
    for e in events:
        if e.get("ticker"):
            latest[e["ticker"]] = e
    return {t: e for t, e in latest.items() if e.get("kind") != "close"}


def memo_status(ticker, notes, today):
    """(has_current, why). Current means a note exists and its review_by, if
    any, has not passed."""
    paths = notes.get(ticker)
    if not paths:
        return False, "no note"
    fm = front_matter(paths[-1])
    rb = str(fm.get("review_by") or "").strip()
    if rb and rb < today.isoformat():
        return False, f"latest note {paths[-1].name} is past its review_by {rb}"
    return True, f"latest note {paths[-1].name}"


def rel(p):
    try:
        return Path(p).resolve().relative_to(REPO).as_posix()
    except ValueError:
        return str(p)


def prose_words(text):
    """Words outside front-matter, tables, SOURCES and GLOSSARY: the measure
    validate.py applies to a memo's length, near enough for a review."""
    body = re.sub(r"\A---\n.*?\n---\n", "", text, flags=re.S)
    body = re.split(r"^##\s+(?:SOURCES|GLOSSARY)\s*$", body, flags=re.M)[0]
    lines = [l for l in body.splitlines() if not l.strip().startswith("|")
             and not re.match(r"^#{1,6}\s", l)]
    return len(re.findall(r"[A-Za-z0-9$%][\w$%.,'-]*", "\n".join(lines)))


def validate_note(path):
    import validate
    try:
        with redirect_stdout(io.StringIO()):
            fails, warns = validate.check(path)
    except Exception as exc:
        fails, warns = [f"validate.py raised {type(exc).__name__}: {exc}"], []
    return fails, warns


def pm_holdings(portfolio_ledger=None):
    """{book: [ticker, ...]} from portfolio/ledger/trades.csv."""
    sys.path.insert(0, str(REPO))
    from portfolio import engine as E
    trades = E.read_rows(Path(portfolio_ledger or E.LEDGER_DIR) / "trades.csv")
    out = {}
    for b in E.LIVE_BOOKS:
        held = sorted(E.book_state(E.trades_for(trades, b["id"]))["shares"])
        if held:
            out[b["id"]] = held
    return out


def style_candidates():
    """{book: [ticker, ...]}: the rules candidate book of each style book, by the
    same code portfolio/bin/review.py prints (portfolio.engine)."""
    sys.path.insert(0, str(REPO))
    from portfolio import engine as E
    asof, rows = E.panel_rows()
    if not rows:
        return {}, asof, "no panel in data/fundamentals"
    classes = E.classify(rows, E.load_style_history(), asof)
    views = E.analyst_views()
    out = {}
    for b in E.STYLE_BOOKS:
        mandate = E.load_mandate(b["id"]) or E.default_mandate(b["id"])
        out[b["id"]] = [p["ticker"] for p in E.rules_candidate(b, classes, mandate, views)]
    note = ""
    if not any(out.values()):
        cov = E.growth_coverage(classes)
        note = (f"no style book has a candidate: {cov.get('classified', 0)} of {cov.get('sized', 0)} "
                f"sized companies could be classified as growth or value (the style history in "
                f"data/portfolio is missing or empty)")
    return out, asof, note


def screen_pack(rows, events, today, cfg):
    """What the screen would hand the analyst today, and its top names off cooldown."""
    import screen
    cov = screen.coverage_from_events()
    preds = read_csv_rows(LEDGER / "predictions.csv")
    scored_ids = {r["prediction_id"] for r in read_csv_rows(LEDGER / "scores.csv")}
    open_preds = [p for p in preds if p.get("prediction_id") not in scored_ids]
    _, _, watchlist = screen.load_state()
    core = screen.core_universe(rows)
    scored = screen.score(core)
    slots = screen.allocate(scored, cov, open_preds, watchlist, today)
    pool = [r for r in scored if r["_scorable"]]
    top = []
    for r in sorted(pool, key=lambda x: -x["_composite"]):
        if len(top) >= SCREEN_TOP:
            break
        if not screen.off_cooldown(r["ticker"], cov, "screen", today):
            continue
        top.append({"ticker": r["ticker"], "name": r.get("name", ""), "sector": r.get("sector", ""),
                    "desk": D.desk_for(r.get("sector")), "composite_z": round(r["_composite"], 3),
                    "peer_group": r["_peer"]})
    by_desk = {}
    for r in core:
        d = D.desk_for(r.get("sector"))
        s = by_desk.setdefault(d, {"gated": 0, "scorable": 0})
        s["gated"] += 1
        s["scorable"] += bool(r.get("_scorable"))
    return ({"would_assign": [{k: s.get(k) for k in ("ticker", "slot", "reason", "sector")}
                              | {"desk": D.desk_for(s.get("sector"))} for s in slots],
             "top_off_cooldown": top,
             "note": "The screen's picks for today, by screen.py's own rules. It re-runs on each "
                     "weekday, so a later day's picks can differ."},
            by_desk)


def letters_questions(today, letters_dir=None):
    """Questions in the PM's letters of the last LETTER_DAYS days.

    A letter's "## Open questions" (or any heading with "question" in it) is
    taken whole; failing that, any sentence ending in a question mark."""
    d = Path(letters_dir or LETTERS)
    out = []
    if not d.is_dir():
        return out, f"no letters yet ({rel(d)} does not exist)"
    since = (today - timedelta(days=LETTER_DAYS)).isoformat()
    for day_dir in sorted(p for p in d.iterdir() if p.is_dir() and p.name >= since):
        for f in sorted(day_dir.glob("*.md")):
            text = f.read_text(encoding="utf-8")
            m = re.search(r"^#{2,3}\s+[^\n]*question[^\n]*\n(.*?)(?=^#{1,3}\s|\Z)", text, re.M | re.S | re.I)
            if m:
                qs = [l.strip(" -*") for l in m.group(1).splitlines() if l.strip()]
            else:
                qs = [s.strip() for s in re.findall(r"[^.?!\n]*\?", text)]
            for q in qs[:10]:
                out.append({"letter": rel(f), "question": q})
    return out, ""


def build(today, week_of, rows, cfg, events=None, predictions=None, scores=None,
          notes_dir=None, holdings=None, candidates=None, letters=None, screen=None):
    """The pack as a dict. Every source can be passed in, for tests; anything
    left as None is read from the repository."""
    mapping = D.desk_map(cfg)
    events = read_csv_rows(LEDGER / "events.csv") if events is None else events
    predictions = read_csv_rows(LEDGER / "predictions.csv") if predictions is None else predictions
    scores = read_csv_rows(LEDGER / "scores.csv") if scores is None else scores
    notes = notes_by_ticker(notes_dir)
    panel = {r.get("ticker"): r for r in rows}
    views = current_views(events)
    covered = set(views)

    def info(t):
        r = panel.get(t) or {}
        sec = (r.get("sector") or "").strip()
        return {"ticker": t, "name": r.get("name", ""), "sector": sec,
                "desk": D.desk_for(sec, mapping), "on_panel": bool(r),
                "operating": is_operating(r) if r else None,
                "has_filings": DC.has_filing_text(t)}

    # --- the model books --------------------------------------------------
    if holdings is None:
        try:
            holdings = pm_holdings()
        except Exception as exc:
            holdings = {"_error": f"{type(exc).__name__}: {exc}"}
    held = {}
    for book, tickers in holdings.items():
        if book.startswith("_"):
            continue
        for t in tickers:
            held.setdefault(t, []).append(book)
    holdings_without = []
    for t, books in sorted(held.items()):
        ok, why = memo_status(t, notes, today)
        if not ok:
            holdings_without.append(dict(info(t), books=books, why=why))

    cand_note = ""
    if candidates is None:
        try:
            candidates, _, cand_note = style_candidates()
        except Exception as exc:
            candidates, cand_note = {}, f"could not compute: {type(exc).__name__}: {exc}"
    in_books = {}
    for book, tickers in candidates.items():
        for t in tickers:
            in_books.setdefault(t, []).append(book)
    candidates_without = [dict(info(t), books=b) for t, b in sorted(in_books.items())
                          if t not in notes]

    # --- earnings in the next 10 trading days ------------------------------
    window = trading_days_after(today, EARNINGS_TRADING_DAYS)
    lo, hi = window[0].isoformat(), window[-1].isoformat()
    earnings = []
    for t in sorted(covered | set(held)):
        ed = ((panel.get(t) or {}).get("earnings_date") or "").strip()
        if ed and lo <= ed <= hi:
            earnings.append(dict(info(t), earnings_date=ed, covered=t in covered,
                                 held_by=held.get(t, [])))
    earnings.sort(key=lambda x: (x["earnings_date"], x["ticker"]))

    # --- stale views -------------------------------------------------------
    threshold = float(cfg.get("review_move_threshold", 0.08))
    scored_ids = {s.get("prediction_id") for s in scores}
    open_preds = {}
    for p in predictions:
        if p.get("prediction_id") not in scored_ids:
            open_preds.setdefault(p.get("ticker"), []).append(p)
    stale = []
    for t in sorted(covered):
        reasons = []
        paths = notes.get(t) or []
        fm = front_matter(paths[-1]) if paths else {}
        rb = str(fm.get("review_by") or views[t].get("review_by") or "").strip()
        if rb and rb <= today.isoformat():
            reasons.append(f"review_by {rb} has passed")
        entry, px = num(fm.get("entry_price")), num((panel.get(t) or {}).get("price"))
        move = None
        if entry and px:
            move = px / entry - 1
            if abs(move) >= threshold:
                reasons.append(f"price moved {move * 100:+.1f}% since the note "
                               f"({entry:,.2f} to {px:,.2f} on the panel)")
        # The screen's review slot looks at every open prediction, not only the
        # latest note, so an older call still being scored counts here too.
        for p in open_preds.get(t, []):
            pe_ = num(p.get("entry_price"))
            if pe_ and px and abs(px / pe_ - 1) >= threshold and pe_ != entry:
                reasons.append(f"open prediction {p.get('prediction_id')} moved "
                               f"{(px / pe_ - 1) * 100:+.1f}% since {p.get('written_on')}")
        if reasons:
            stale.append(dict(info(t), latest_note=rel(paths[-1]) if paths else "",
                              direction=views[t].get("direction"), reasons=reasons,
                              move=round(move, 4) if move is not None else None))

    # --- the screen --------------------------------------------------------
    if screen is None:
        try:
            screen, gated = screen_pack(rows, events, today, cfg)
        except Exception as exc:
            screen, gated = {"error": f"{type(exc).__name__}: {exc}"}, {}
    else:
        screen, gated = screen

    # --- last week's memos ---------------------------------------------------
    last_mon = week_of - timedelta(days=7)
    last_sun = week_of - timedelta(days=1)
    last_week = []
    for t, paths in sorted(notes.items()):
        for p in paths:
            m = re.match(r"^(\d{4}-\d{2}-\d{2})-(\w+)\.md$", p.name)
            if not m or not (last_mon.isoformat() <= m.group(1) <= last_sun.isoformat()):
                continue
            fm = front_matter(p)
            fails, warns = validate_note(p)
            text = p.read_text(encoding="utf-8")
            last_week.append(dict(info(t), path=rel(p), written_on=m.group(1), kind=m.group(2),
                                  format=str(fm.get("format") or "note").strip(),
                                  action=str(fm.get("action") or "").strip(),
                                  direction=str(fm.get("direction") or "").strip(),
                                  conviction=str(fm.get("conviction") or "").strip(),
                                  prose_words=prose_words(text),
                                  validate={"status": "FAIL" if fails else ("warn" if warns else "pass"),
                                            "fails": fails, "warnings": warns}))

    # --- last week's plan --------------------------------------------------
    last_plan = None
    lp = DC.plan_path_for(last_mon)
    if lp.exists():
        _, assignments, _ = DC.parse_plan(lp.read_text(encoding="utf-8"))
        done = []
        for a in assignments or []:
            if "_raw" in a:
                continue
            t = (a.get("ticker") or "").upper()
            wrote = [rel(p) for p in notes.get(t, []) if p.name[:10] >= (a.get("date") or "9999")]
            done.append({"date": a.get("date"), "ticker": t, "kind": a.get("kind"),
                         "desk": a.get("desk"), "written": wrote})
        last_plan = {"path": rel(lp), "assignments": done}

    # --- per desk ----------------------------------------------------------
    pred_ids = {p.get("prediction_id") for p in predictions}
    scored = {s.get("prediction_id"): s for s in scores}
    card = {d: {"desk": d, "title": D.desk_title(d), "covered": [], "open_calls": 0,
                "scored": 0, "right": 0, "memos_last_week": 0, "validate_fails_last_week": 0}
            for d in D.desks(mapping)}
    for t in sorted(covered):
        d = info(t)["desk"]
        if d in card:
            card[d]["covered"].append(t)
    for p in predictions:
        d = info(p.get("ticker"))["desk"]
        if d not in card:
            continue
        s = scored.get(p.get("prediction_id"))
        if s:
            card[d]["scored"] += 1
            card[d]["right"] += s.get("outcome") == "right"
        else:
            card[d]["open_calls"] += 1
    for m in last_week:
        if m["desk"] in card:
            card[m["desk"]]["memos_last_week"] += 1
            card[m["desk"]]["validate_fails_last_week"] += m["validate"]["status"] == "FAIL"
    coverage = [{"desk": d, "title": c["title"], "covered": len(c["covered"]),
                 "gated": (gated.get(d) or {}).get("gated"),
                 "scorable": (gated.get(d) or {}).get("scorable")} for d, c in card.items()]

    if letters is None:
        letters, letters_note = letters_questions(today)
    else:
        letters_note = ""

    days = [week_of + timedelta(days=i) for i in range(5)]
    return {
        "generated_for": {"today": today.isoformat(), "week_of": week_of.isoformat(),
                          "plan_file": f"theses/director/{(week_of - timedelta(days=1)).isoformat()}.md",
                          "last_week": [last_mon.isoformat(), last_sun.isoformat()]},
        "slots": {"days": [d.isoformat() for d in days],
                  "per_day": int(cfg.get("slots_per_run", 4)),
                  "max_per_sector_per_day": int(cfg.get("max_per_sector_per_run", 1)),
                  "cooldown_days": cfg.get("cooldown_days", {})},
        "holdings_without_memo": holdings_without,
        "holdings_error": holdings.get("_error", ""),
        "candidates_without_memo": candidates_without,
        "candidates_note": cand_note,
        "earnings_ahead": earnings,
        "earnings_window": [lo, hi],
        "stale_views": stale,
        "screen": screen,
        "last_week_memos": last_week,
        "last_plan": last_plan,
        "desk_scorecard": list(card.values()),
        "coverage_by_desk": coverage,
        "pm_open_questions": letters,
        "pm_letters_note": letters_note,
        "news_scope": {"covered": sorted(covered), "held": {t: b for t, b in sorted(held.items())},
                       "candidates": {t: b for t, b in sorted(in_books.items())},
                       "screen_top": [s["ticker"] for s in (screen or {}).get("top_off_cooldown", [])]},
    }


def _names(items, extra=None):
    if not items:
        return "none"
    return ", ".join(f"{i['ticker']}" + (f" ({extra(i)})" if extra else "") for i in items)


def summary(pack):
    """The pack in a page of markdown, for the director to read first."""
    g, s = pack["generated_for"], pack["slots"]
    lines = [f"# Director inputs for the week of {g['week_of']}", "",
             f"Computed on {g['today']}. The plan goes in `{g['plan_file']}`. "
             f"{len(s['days'])} weekdays, {s['per_day']} slots each, at most "
             f"{s['max_per_sector_per_day']} name per sector per day unless the reason cites a "
             f"holding or earnings.", ""]
    lines += ["## Must consider", ""]
    lines.append("- Held by a model book with no current memo: "
                 + _names(pack["holdings_without_memo"], lambda i: ", ".join(i["books"]) + "; " + i["why"])
                 + (f" ({pack['holdings_error']})" if pack.get("holdings_error") else ""))
    lines.append("- Style books' rules candidates with no memo: "
                 + (f"{len(pack['candidates_without_memo'])} names" if pack["candidates_without_memo"] else "none")
                 + (f" ({pack['candidates_note']})" if pack.get("candidates_note") else ""))
    lines.append(f"- Reporting between {pack['earnings_window'][0]} and {pack['earnings_window'][1]}, "
                 f"covered or held: " + _names(pack["earnings_ahead"], lambda i: i["earnings_date"]))
    lines.append("- Stale views: " + _names(pack["stale_views"], lambda i: "; ".join(i["reasons"])))
    lines += ["", "## The screen", ""]
    sc = pack["screen"] or {}
    if sc.get("error"):
        lines.append(f"The screen could not run: {sc['error']}")
    else:
        lines.append("Today it would assign: " + _names(sc.get("would_assign", []),
                                                         lambda i: f"{i['slot']}, {i['desk']}"))
        lines.append("")
        lines.append("Top names off cooldown: " + _names(sc.get("top_off_cooldown", []),
                                                          lambda i: i["desk"]))
    lines += ["", "## Last week", ""]
    if pack["last_week_memos"]:
        for m in pack["last_week_memos"]:
            v = m["validate"]
            lines.append(f"- `{m['path']}`: {m['kind']}, {m['format']} format, {m['prose_words']:,} words "
                         f"of prose, validate {v['status']} ({len(v['fails'])} fails, "
                         f"{len(v['warnings'])} warnings), desk {m['desk']}")
    else:
        lines.append("- No notes were written last week.")
    if pack["last_plan"]:
        missed = [a["ticker"] for a in pack["last_plan"]["assignments"] if not a["written"]]
        lines.append(f"- Last plan `{pack['last_plan']['path']}`: "
                     f"{len(pack['last_plan']['assignments'])} assignments, not written: "
                     f"{', '.join(missed) or 'none'}")
    else:
        lines.append("- There was no plan last week.")
    lines += ["", "## Desks", "", "| Desk | Covered | Open calls | Scored | Right | Memos last week | "
              "Validate fails | Names the screen can see |", "|---|---|---|---|---|---|---|---|"]
    cov = {c["desk"]: c for c in pack["coverage_by_desk"]}
    for c in pack["desk_scorecard"]:
        lines.append(f"| {c['title']} | {len(c['covered'])} | {c['open_calls']} | {c['scored']} | "
                     f"{c['right']} | {c['memos_last_week']} | {c['validate_fails_last_week']} | "
                     f"{cov.get(c['desk'], {}).get('gated') or 0} |")
    news = pack.get("news") or {}
    lines += ["", "## News", ""]
    if news.get("fresh"):
        lines.append(f"- From the News Desk: `{news['path']}news.md` (read it), `news.json` and "
                     f"`news_labels.json`. {news['why']}. {news.get('line', '')}".rstrip())
    elif news:
        lines.append(f"- No news this week: {news['why']}. Plan without headlines, and say so in the "
                     f"plan and in your report.")
    else:
        lines.append("- Not read on this run.")
    lines += ["", "## The PM's open questions", ""]
    if pack["pm_open_questions"]:
        for q in pack["pm_open_questions"]:
            lines.append(f"- {q['question']} (`{q['letter']}`)")
    else:
        lines.append(f"- None{': ' + pack['pm_letters_note'] if pack.get('pm_letters_note') else '.'}")
    return "\n".join(lines) + "\n"


def news_step(now=None, latest=None):
    """Whether the News Desk's latest pack is fresh enough to plan from. It
    reads theses/news/latest/ and never builds or writes news."""
    pack, labels, why = news_freshness(latest, now)
    out = {"path": rel(Path(latest or NEWS_LATEST)) + "/", "fresh": pack is not None, "why": why}
    if pack is not None:
        t = pack.get("totals") or {}
        out["asof"] = pack["generated_for"].get("asof", "")
        out["labels"] = len(labels)
        out["line"] = (f"{t.get('names_in_scope', 0)} names, tier 1/2/3 headlines {t.get('tier1', 0)}/"
                       f"{t.get('tier2', 0)}/{t.get('tier3', 0)}, {t.get('price_claims_mismatched', 0)} "
                       f"price claims that do not match the stored closes.")
        out["names"] = pack_names(pack)
    return out


def pack_names(pack):
    return [n.get("ticker") for n in (pack or {}).get("names") or [] if n.get("ticker")]


def next_monday(day):
    return day + timedelta(days=(7 - day.weekday()) % 7 or 7)


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)

    def opt(name):
        return args[args.index(name) + 1] if name in args else None
    today = date.fromisoformat(opt("--today")) if opt("--today") else datetime.now(tz=EASTERN).date()
    week_of = date.fromisoformat(opt("--week-of")) if opt("--week-of") else next_monday(today)
    if week_of.weekday() != 0:
        print(f"director_inputs: --week-of {week_of} is not a Monday", file=sys.stderr)
        return 2
    out_dir = Path(opt("--out") or OUT_DIR)
    cfg = DC.load_config()
    panel_date, rows = load_panel()
    if not rows:
        print("director_inputs: FAILED. The panel could not be loaded.", file=sys.stderr)
        return 1
    pack = build(today, week_of, rows, cfg)
    pack["generated_for"]["panel_date"] = panel_date
    out_dir.mkdir(parents=True, exist_ok=True)
    if "--no-news" not in args:
        pack["news"] = news_step()
    (out_dir / f"{week_of.isoformat()}.json").write_text(json.dumps(pack, indent=1), encoding="utf-8")
    md = summary(pack)
    (out_dir / f"{week_of.isoformat()}.md").write_text(md, encoding="utf-8")
    print(md)
    print(f"director_inputs: wrote {rel(out_dir / (week_of.isoformat() + '.json'))} and .md",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
