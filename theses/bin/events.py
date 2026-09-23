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

    python3 theses/bin/events.py NOTE.md "<trigger>" "<rationale>"            # record
    python3 theses/bin/events.py --dry-run NOTE.md "<trigger>" "<rationale>"  # show, write nothing
    python3 theses/bin/events.py --migrate                                    # add the memo columns
    python3 theses/bin/events.py --render TICKER ...
"""
import csv, io, os, re, sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import THESES, LEDGER, read_csv_rows, append_csv, csv_header
import validate

OLD_EVENT_COLUMNS = ["event_id", "date", "ticker", "kind", "thesis_id", "note_path",
                     "direction", "conviction", "target_price", "horizon_days",
                     "prior_direction", "prior_conviction", "prior_target",
                     "claim_changed", "trigger", "rationale"]
# Added for the buy-side memo (format: memo), at the end so every older column
# keeps its position. Blank for older notes and for every row written before.
MEMO_EVENT_COLUMNS = ["action", "size_now", "expected_return", "bear_return"]
EVENT_COLUMNS = OLD_EVENT_COLUMNS + MEMO_EVENT_COLUMNS
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

    # The memo's own call: what it asks the PM to do, at what size, and the two
    # returns page one sets side by side. Blank for the older plain note.
    memo = {c: "" for c in MEMO_EVENT_COLUMNS}
    if s("format").lower() == "memo":
        a = s("action")
        memo = {"action": next((x for x in validate.MEMO_ACTIONS if x.lower() == a.lower()), a),
                "size_now": s("size_now"), "expected_return": s("expected_return"),
                "bear_return": s("bear_return")}

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
        **memo,
    }


def migrate_events(path=None, write=True):
    """Give events.csv the memo columns, once, without disturbing a byte of it.

    append_csv refuses to write a row whose columns differ from the file's
    header, which is what keeps an append-only ledger from being shifted out of
    alignment. So the header has to change before the first memo is recorded.
    The four new columns go at the end and are blank on every existing row.

    Safe by construction: the file is parsed, and it is rewritten only if
    writing the parsed rows back reproduces the original bytes exactly, so the
    only change is the new header names and four empty fields on each row. The
    new file is written beside the old one and swapped in with one rename, then
    read back and compared. Running it again finds the new header and does
    nothing. Returns "absent", "current", "migrated", or with write=False
    "would migrate"."""
    path = Path(path) if path else LEDGER / "events.csv"
    header = csv_header(path)
    if header is None:
        return "absent"
    if header == EVENT_COLUMNS:
        return "current"
    if header != OLD_EVENT_COLUMNS:
        raise SystemExit(
            f"events: {path.name} has a header this migration does not know, so it was left alone.\n"
            f"  file header: {header}\n  expected   : {OLD_EVENT_COLUMNS}\n"
            f"  or already : {EVENT_COLUMNS}")
    text = path.read_bytes().decode("utf-8")
    rows = list(csv.reader(io.StringIO(text, newline="")))
    bad = [i for i, r in enumerate(rows[1:], 2) if len(r) != len(OLD_EVENT_COLUMNS)]
    if bad:
        raise SystemExit(f"events: {path.name} line(s) {bad[:5]} do not have "
                         f"{len(OLD_EVENT_COLUMNS)} fields. Nothing was changed.")

    def dump(row, term):
        buf = io.StringIO(newline="")
        csv.writer(buf, lineterminator=term).writerow(row)
        return buf.getvalue()

    # Each record keeps its own line ending. The live file has a header ending
    # in \n and rows ending in \r\n, because the header and the rows were
    # written by different tools.
    terms, pos = [], 0
    for r in rows:
        term = next((t for t in ("\r\n", "\n", "") if text.startswith(dump(r, t), pos)
                     and (t or pos + len(dump(r, t)) == len(text))), None)
        if term is None:
            raise SystemExit(f"events: {path.name} cannot be written back byte for byte, so migrating "
                             f"it could alter existing rows. Nothing was changed; migrate it by hand.")
        terms.append(term)
        pos += len(dump(r, term))
    if pos != len(text):
        raise SystemExit(f"events: {path.name} has trailing text after its last row. Nothing was changed.")
    if not write:
        return "would migrate"
    pad = [""] * len(MEMO_EVENT_COLUMNS)
    new = "".join(dump(r, t) for r, t in zip([EVENT_COLUMNS] + [r + pad for r in rows[1:]], terms))
    tmp = path.with_name(path.name + ".migrating")
    with tmp.open("w", encoding="utf-8", newline="") as fh:
        fh.write(new)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    back = list(csv.reader(io.StringIO(path.read_bytes().decode("utf-8"), newline="")))
    if back[0] != EVENT_COLUMNS or [r[:len(OLD_EVENT_COLUMNS)] for r in back[1:]] != rows[1:]:
        path.write_text(text, encoding="utf-8", newline="")
        raise SystemExit(f"events: {path.name} did not read back as written; the original was restored.")
    return "migrated"


PREDICTION_COLUMNS = ["prediction_id", "thesis_id", "ticker", "written_on", "panel_date",
                      "entry_price", "direction", "conviction", "evidence_base",
                      "falsifier_specific", "variant_perception", "disconfirmation",
                      "horizon_days", "target_price", "review_by", "key_claim", "falsifier"]


def append_prediction(path):
    """Append the gradeable claim this note makes, if it makes one.

    A note with direction "no view" or "watch" is research, not a prediction, and
    putting it in the ledger would dilute the hit rate with calls that were never
    made. prediction_id is deterministic, so re-running is a no-op rather than a
    duplicate."""
    fm, _ = validate.parse(Path(path).read_text(encoding="utf-8"))
    if not fm:
        return None
    g = lambda k: "" if fm.get(k) is None else str(fm.get(k)).strip().strip('"')
    if g("direction") in ("no view", "watch", ""):
        return None
    t, day = g("ticker"), g("written_on")
    existing = {r["prediction_id"] for r in read_csv_rows(LEDGER / "predictions.csv")}
    n = 1
    while f"{t}-{day}-{n}" in existing:
        n += 1
    pid = f"{t}-{day}-{n}"
    row = {"prediction_id": pid}
    for k in PREDICTION_COLUMNS[1:]:
        row[k] = g(k)
    append_csv(LEDGER / "predictions.csv", PREDICTION_COLUMNS, [row])
    return pid


def record(path, trigger="", rationale="", dry_run=False):
    # The ledger is append-only, so a bad row is permanent. Until now the only
    # thing that ran the note checks before a note was recorded was ingest.py,
    # on the Google Drive path. A session that pushes straight to the repository
    # never touches ingest.py, so the gate has to be here too: this is the one
    # function that writes a note into the ledger.
    fails, _warns = validate.check(path)
    if fails:
        first = "\n  - " + "\n  - ".join(str(f) for f in fails[:3])
        more = f"\n  ...and {len(fails) - 3} more" if len(fails) > 3 else ""
        raise SystemExit(
            f"events: refusing to record {Path(path).name}. It fails {len(fails)} check"
            f"{'' if len(fails) == 1 else 's'}:{first}{more}\n"
            f"Run python3 theses/bin/validate.py on it, fix what it reports, and record it "
            f"again. Nothing was written.")
    ev = event_from_note(path, trigger, rationale)
    if dry_run:
        # Everything a real record would decide, and no write anywhere.
        ev["migration"] = migrate_events(LEDGER / "events.csv", write=False)
        ev["prediction_id"] = "(would append)" if _gradeable(path) else ""
        return ev
    migrate_events(LEDGER / "events.csv")
    append_csv(LEDGER / "events.csv", EVENT_COLUMNS, [ev])
    pid = append_prediction(path)
    ev["prediction_id"] = pid or ""
    return ev


def _gradeable(path):
    fm, _ = validate.parse(Path(path).read_text(encoding="utf-8"))
    d = "" if not fm or fm.get("direction") is None else str(fm.get("direction")).strip().strip('"')
    return d not in ("no view", "watch", "")


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
    if cur.get("action"):
        out += [f"Memo action: **{cur['action']}**, size now {cur.get('size_now') or '0'}, expected "
                f"return {cur.get('expected_return','?')}, bear return {cur.get('bear_return','?')}.", ""]
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
    elif sys.argv[1] == "--migrate":
        print(f"events.csv: {migrate_events()}")
    elif sys.argv[1] == "--dry-run":
        if len(sys.argv) < 3:
            print(__doc__)
            sys.exit(2)
        trig = sys.argv[3] if len(sys.argv) > 3 else ""
        rat = sys.argv[4] if len(sys.argv) > 4 else ""
        ev = record(sys.argv[2], trig, rat, dry_run=True)
        print("dry run: nothing written. events.csv header: " + ev.pop("migration"))
        pid = ev.pop("prediction_id")
        for k in EVENT_COLUMNS:
            print(f"  {k}: {ev.get(k, '')}")
        print("a gradeable prediction would be appended" if pid else
              "no prediction would be appended: this note makes no gradeable claim")
    else:
        trig = sys.argv[2] if len(sys.argv) > 2 else ""
        rat = sys.argv[3] if len(sys.argv) > 3 else ""
        ev = record(sys.argv[1], trig, rat)
        print(f"recorded {ev['event_id']} ({ev['kind']})")
        if ev.get("prediction_id"):
            print(f"gradeable prediction {ev['prediction_id']} appended")
        else:
            print("no prediction appended: this note makes no gradeable claim")
        write_position(ev["ticker"])
