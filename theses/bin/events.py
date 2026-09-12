#!/usr/bin/env python3
"""The spine of a thesis as it changes: initiate, reaffirm, revise, close.

Notes are never edited. A changed view is a NEW dated note plus an event row
saying what changed and why, and the superseded note stays exactly as written,
including the parts that turned out wrong.

That is not bureaucracy. Thesis drift is the commonest way a research archive
rots: a losing view quietly mutates to justify holding, and "margin expansion
drives a re-rating" becomes "the balance sheet is undervalued" without anyone
noticing the substitution, including the analyst. If the thesis is one mutable
document the old claim is simply gone and nothing can detect the swap.

The current view is a fold over these events, so positions/{TICKER}.md is
derived and never hand-maintained. It cannot drift from the notes because it is
not a separate thing.
"""
import re, sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import THESES, LEDGER, read_csv_rows, append_csv
import validate

EVENT_COLUMNS = ["event_id", "date", "ticker", "kind", "thesis_id", "note_path",
                 "direction", "conviction", "target_price", "horizon_days",
                 "prior_direction", "prior_conviction", "prior_target",
                 "claim_changed", "trigger", "rationale"]
KIND_ORDER = {"initiation": "initiate", "update": "reaffirm",
              "revision": "revise", "close": "close"}


def _repo_rel(path):
    """Repo-relative path, whether the caller passed a relative or absolute one."""
    p = Path(path).resolve()
    try:
        return str(p.relative_to(THESES.parent))
    except ValueError:
        return str(path)


def history(ticker):
    return [e for e in read_csv_rows(LEDGER / "events.csv") if e.get("ticker") == ticker]


def latest(ticker):
    h = history(ticker)
    return h[-1] if h else None


def event_from_note(path, trigger="", rationale=""):
    """Build the event row a note implies, diffed against the prior event."""
    fm, _ = validate.parse(Path(path).read_text(encoding="utf-8"))
    if not fm:
        raise SystemExit(f"events: {path} has no front-matter")
    t = str(fm.get("ticker", "")).strip().strip('"')
    prior = latest(t)
    kind = KIND_ORDER.get(str(fm.get("kind", "")).strip().strip('"'), "initiate")

    def s(k):
        v = fm.get(k)
        return "" if v is None else str(v).strip().strip('"')

    claim_changed = ""
    if prior:
        # A revise that keeps direction and conviction but swaps the claim is
        # exactly the drift pattern. Flag it so it is queryable, rather than
        # relying on anyone noticing in prose.
        same_shape = (prior.get("direction") == s("direction")
                      and prior.get("conviction") == s("conviction"))
        claim_changed = "yes" if (kind == "revise" and same_shape) else ""

    n = len(history(t)) + 1
    return {
        "event_id": f"{t}-{s('written_on')}-{n}",
        "date": s("written_on"),
        "ticker": t,
        "kind": kind,
        "thesis_id": s("thesis_id"),
        "note_path": _repo_rel(path),
        "direction": s("direction"),
        "conviction": s("conviction"),
        "target_price": s("target_price"),
        "horizon_days": s("horizon_days"),
        "prior_direction": (prior or {}).get("direction", ""),
        "prior_conviction": (prior or {}).get("conviction", ""),
        "prior_target": (prior or {}).get("target_price", ""),
        "claim_changed": claim_changed,
        "trigger": trigger,
        "rationale": rationale,
    }


def record(path, trigger="", rationale=""):
    ev = event_from_note(path, trigger, rationale)
    append_csv(LEDGER / "events.csv", EVENT_COLUMNS, [ev])
    return ev


def render_position(ticker):
    """Derive positions/{TICKER}.md from the event history. Never hand-edited."""
    h = history(ticker)
    if not h:
        return None
    cur = h[-1]
    out = [f"# {ticker}", ""]
    d, c = cur.get("direction", "?"), cur.get("conviction", "?")
    out += [f"**{d}**, conviction {c}/5, target {cur.get('target_price','?')} "
            f"({cur.get('horizon_days','?')}d) &mdash; as of {cur.get('date','?')}", ""]
    out += [f"Current note: [`{cur.get('note_path','')}`]({Path(cur.get('note_path','')).name})", ""]
    if any(e.get("claim_changed") == "yes" for e in h):
        out += ["> **This thesis has been revised with the direction and conviction unchanged "
                "at least once.** That is the shape thesis drift takes: the claim is replaced "
                "while the position stays. Read the history below before trusting the current "
                "view.", ""]
    out += ["## History", "",
            "| Date | Event | Direction | Conviction | Target | Trigger |",
            "|---|---|---|---|---|---|"]
    for e in reversed(h):
        conv = e.get("conviction", "")
        if e.get("prior_conviction") and e["prior_conviction"] != conv:
            conv = f"{e['prior_conviction']} &rarr; {conv}"
        tgt = e.get("target_price", "")
        if e.get("prior_target") and e["prior_target"] != tgt:
            tgt = f"{e['prior_target']} &rarr; {tgt}"
        out.append(f"| {e.get('date','')} | {e.get('kind','')} | {e.get('direction','')} "
                   f"| {conv} | {tgt} | {e.get('trigger','')} |")
    out += ["", "_Derived from theses/ledger/events.csv. Do not edit: it is regenerated._"]
    return "\n".join(out)


def write_position(ticker):
    md = render_position(ticker)
    if md is None:
        return None
    p = THESES / "positions" / f"{ticker}.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(md + "\n", encoding="utf-8")
    return p


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(2)
    if sys.argv[1] == "--render":
        for t in sys.argv[2:]:
            p = write_position(t)
            print(f"wrote {p}" if p else f"no events for {t}")
    else:
        trig = sys.argv[2] if len(sys.argv) > 2 else ""
        rat = sys.argv[3] if len(sys.argv) > 3 else ""
        ev = record(sys.argv[1], trig, rat)
        print(f"recorded {ev['event_id']} ({ev['kind']})")
        write_position(ev["ticker"])
