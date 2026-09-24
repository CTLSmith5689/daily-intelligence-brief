#!/usr/bin/env python3
"""Check a Research Director plan before it can steer a run.

A plan is theses/director/{SUNDAY}.md, written by the director routine on the
Sunday before the week it plans. prepare.py reads it on each weekday and takes
that day's assignments ahead of the screen, but only if this check passes. A
plan that fails is ignored and the run falls back to the screen exactly as it
ran before the director existed, so a bad plan costs a week of direction and
never a run.

    python3 theses/bin/director_check.py theses/director/2026-09-27.md
    python3 theses/bin/director_check.py --changes          # what is staged
    python3 theses/bin/director_check.py --changes REV      # REV..HEAD

Exit code 0 = pass. 1 = at least one FAIL. Warnings never fail.

--changes checks the director's commit rather than the plan: it may touch only
theses/director/, and may change a sector playbook only by appending dated lines
to its "## Lessons" section. Anything else is a proposal in the plan.

The plan's front-matter:

    ---
    week_of: 2026-09-28
    assignments:
      - {date: 2026-09-28, ticker: JPM, kind: initiation, desk: financials-realestate, reason: "..."}
    ---

week_of is the Monday the plan covers, and the file is named for the Sunday
before it. Each assignment is one line, a flow mapping; a reason that contains
a comma or a colon goes in double quotes.
"""
import json, re, sys
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import THESES, LEDGER, is_operating, load_panel, num, read_csv_rows
import desks as D

PLAN_DIR = THESES / "director"
KINDS = ("initiation", "revision")
FIELDS = ("date", "ticker", "kind", "desk", "reason")
SECTIONS = ("This week's focus", "Review of last week's memos", "Coverage gaps",
            "Playbook proposals")
# What the director's commit may touch. Anything under theses/director/, and
# the "## Lessons" section at the end of a sector playbook, by appending only.
# Every other change to a playbook or desk file is a proposal in the plan,
# which the owner applies by hand.
DIRECTOR_PREFIX = "theses/director/"
# Under theses/director/inputs/, only what director_inputs.py writes: the
# week's pack ({WEEK_OF}.json and .md). The news is the News Desk's, in
# theses/news/ (theses/NEWS_DESK.md), and the director never writes it.
INPUTS_PREFIX = "theses/director/inputs/"
INPUT_FILE = re.compile(r"^theses/director/inputs/\d{4}-\d{2}-\d{2}\.(?:json|md)$")
PLAYBOOK_GLOB = re.compile(r"^theses/desks/sectors/[a-z0-9-]+\.md$")
LESSONS = "## Lessons"
LESSON_LINE = re.compile(r"^- \d{4}-\d{2}-\d{2}: \S")
# A second name from one sector on one day is allowed only for a holding or an
# earnings date: the two reasons that cannot wait for next week.
SECTOR_EXEMPT = re.compile(r"\b(?:held|holds|holding|holdings|earnings)\b", re.I)
EM_DASH, EN_DASH = chr(0x2014), chr(0x2013)


def load_config():
    return json.loads((THESES / "config.json").read_text(encoding="utf-8"))


def monday_of(day):
    return day - timedelta(days=day.weekday())


def plan_path_for(day, plan_dir=None):
    """The plan that covers `day`: the file named for the Sunday before its week."""
    if isinstance(day, str):
        day = date.fromisoformat(day)
    return Path(plan_dir or PLAN_DIR) / f"{(monday_of(day) - timedelta(days=1)).isoformat()}.md"


def _split_flow(inner):
    """Split "a: 1, b: "x, y"" on commas outside double quotes."""
    parts, buf, quoted = [], [], False
    for ch in inner:
        if ch == '"':
            quoted = not quoted
            buf.append(ch)
        elif ch == "," and not quoted:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    if buf:
        parts.append("".join(buf))
    return parts


def _flow_map(s):
    """{"date": ..., ...} from "{date: 2026-09-28, reason: "..."}", or None."""
    m = re.fullmatch(r"\{(.*)\}", s.strip())
    if not m:
        return None
    out = {}
    for part in _split_flow(m.group(1)):
        if not part.strip():
            continue
        k, sep, v = part.partition(":")
        if not sep:
            return None
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        out[k.strip().lower()] = v.strip()
    return out


def parse_plan(text):
    """(front-matter dict, assignments or None, body). assignments is None when
    the list is missing, and a list of dicts (a bad line gives {"_raw": line})."""
    m = re.match(r"^---\n(.*?)\n---\n?(.*)$", text.replace("\r\n", "\n"), re.S)
    if not m:
        return None, None, text
    fm, assignments, key = {}, None, None
    for line in m.group(1).splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        item = re.match(r"^\s*-\s+(.*)$", line)
        if item and key == "assignments":
            got = _flow_map(item.group(1))
            assignments.append(got if got is not None else {"_raw": item.group(1).strip()})
            continue
        kv = re.match(r"^([A-Za-z_]\w*):\s*(.*)$", line)
        if kv:
            key, val = kv.group(1), kv.group(2).strip()
            if key == "assignments":
                assignments = []
                if val not in ("", "[]"):
                    assignments.append({"_raw": val})
            else:
                fm[key] = val.strip('"').strip("'")
    return fm, assignments, m.group(2)


def _sections(body):
    """{heading: text} for each "## " heading in the body."""
    out, name, buf = {}, None, []
    for line in body.splitlines():
        h = re.match(r"^##\s+(.+?)\s*#*\s*$", line)
        if h and not line.startswith("###"):
            if name is not None:
                out[name] = "\n".join(buf).strip()
            name, buf = h.group(1).replace("’", "'"), []
        elif name is not None:
            buf.append(line)
    if name is not None:
        out[name] = "\n".join(buf).strip()
    return out


def has_filing_text(ticker, root=None):
    """True when the checkout holds any filing text for the ticker."""
    d = Path(root or THESES.parent / "data" / "filings" / "text") / (ticker or "_")
    return d.is_dir() and any(d.iterdir())


def _coverage(events):
    cov = {}
    for e in events:
        t, d = e.get("ticker"), e.get("date")
        if t and d and d >= cov.get(t, ""):
            cov[t] = d
    return cov


def check_plan(path, rows=None, events=None, cfg=None, mapping=None):
    """(fails, warns, plan). plan is {"week_of", "assignments"} with each
    assignment's sector added, or None when the file cannot be read at all.

    rows is the latest panel slice (list of dicts); events the rows of
    theses/ledger/events.csv. Both are read when not given."""
    fails, warns = [], []
    F, W = fails.append, warns.append
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return [f"cannot read {path}: {exc}"], [], None
    cfg = cfg if cfg is not None else load_config()
    mapping = mapping if mapping is not None else D.desk_map(cfg)
    fm, assignments, body = parse_plan(text)
    if fm is None:
        return ["no front-matter: the plan starts with a --- line, then week_of and "
                "assignments, then another --- line"], [], None

    n = text.count(EM_DASH) + text.count(EN_DASH)
    if n:
        F(f"{n} em or en dash{'es' if n > 1 else ''}. Use a full stop, a comma or a colon.")

    # --- the week ---------------------------------------------------------
    week_of = None
    try:
        week_of = date.fromisoformat(fm.get("week_of", ""))
    except ValueError:
        F(f"week_of {fm.get('week_of')!r} is not a date (YYYY-MM-DD)")
    if week_of is not None:
        if week_of.weekday() != 0:
            F(f"week_of {week_of} is a {week_of.strftime('%A')}; it must be the Monday the plan covers")
        want = (monday_of(week_of) - timedelta(days=1)).isoformat() + ".md"
        if path.name != want:
            F(f"the file is named {path.name} but a plan for the week of {monday_of(week_of)} "
              f"must be named {want}, for the Sunday before it")
    days = [monday_of(week_of) + timedelta(days=i) for i in range(5)] if week_of else []

    # --- the body ---------------------------------------------------------
    secs = _sections(body)
    for s in SECTIONS:
        if s not in secs:
            F(f"missing the section '## {s}'")
        elif not secs[s].strip():
            F(f"the section '## {s}' is empty")
    extra = [s for s in secs if s not in SECTIONS]
    if extra:
        W(f"sections not in the plan format: {', '.join(extra)}")
    focus = secs.get(SECTIONS[0], "")
    for desk in D.desks(mapping):
        title = D.desk_title(desk)
        if focus and title.lower() not in focus.lower() and desk not in focus:
            F(f"'## {SECTIONS[0]}' has no paragraph for the {title} desk. Give each desk one "
              f"short paragraph, starting with its name, even if it is to say it has nothing assigned.")
    review = secs.get(SECTIONS[1], "")
    if review and not re.search(r"\bgrade\b", review, re.I) and "no memo" not in review.lower():
        W(f"'## {SECTIONS[1]}' gives no grade. Grade each memo A to D, or say there was no memo.")

    # --- the assignments --------------------------------------------------
    if assignments is None:
        F("no assignments list. Write 'assignments: []' for a week with none.")
        assignments = []
    if rows is None:
        _, rows = load_panel()
    if not rows:
        F("the panel could not be loaded, so no ticker can be checked")
    panel = {r.get("ticker"): r for r in rows or []}
    if events is None:
        events = read_csv_rows(LEDGER / "events.csv")
    covered = _coverage(events)
    slots = int(cfg.get("slots_per_run", 4))
    per_sector = int(cfg.get("max_per_sector_per_run", 1))
    cooldown = int((cfg.get("cooldown_days") or {}).get("screen", 0))

    out, seen = [], set()
    for i, a in enumerate(assignments, 1):
        where = f"assignment {i}"
        if "_raw" in a:
            F(f"{where} is {a['_raw'][:70]!r}. Write it on one line as {{date: YYYY-MM-DD, "
              f"ticker: T, kind: initiation|revision, desk: D, reason: \"...\"}}.")
            continue
        missing = [k for k in FIELDS if not (a.get(k) or "").strip()]
        if missing:
            F(f"{where} ({a.get('ticker', '?')}) has no {', '.join(missing)}")
        t = (a.get("ticker") or "").strip().upper()
        where = f"assignment {i} ({t or '?'})"
        try:
            d = date.fromisoformat(a.get("date", ""))
        except ValueError:
            d = None
            if a.get("date"):
                F(f"{where}: date {a.get('date')!r} is not YYYY-MM-DD")
        if d is not None and days and d not in days:
            F(f"{where}: {d} is not a weekday of the week of {days[0]} (Monday {days[0]} to Friday {days[-1]})")
        kind = (a.get("kind") or "").strip().lower()
        if kind and kind not in KINDS:
            F(f"{where}: kind {kind!r} is not initiation or revision")
        if t in seen:
            F(f"{where}: {t} is assigned twice this week")
        seen.add(t)
        r = panel.get(t)
        sector = ""
        if t and rows and r is None:
            F(f"{where}: {t} is not on the latest panel")
        elif r is not None:
            sector = (r.get("sector") or "").strip()
            if not is_operating(r):
                F(f"{where}: {t} is not an operating company ({r.get('security_type') or 'a note, fund or shell'})")
            if not sector:
                F(f"{where}: {t} has no sector on the panel, so no desk owns it")
            want = D.desk_for(sector, mapping)
            if sector and (a.get("desk") or "").strip() != want:
                F(f"{where}: desk is {a.get('desk')!r} but {sector} belongs to {want!r}")
        if r is not None and d is not None and d.weekday() == 0 and not has_filing_text(t):
            W(f"{where}: there is no filing text for {t} in data/filings/text/ yet. The daily "
              f"pipeline collects it for plan names on Monday evening, after Monday's run, so "
              f"a Monday dossier would have none. Move it to Tuesday or later.")
        if t and kind == "initiation" and t in covered:
            F(f"{where}: {t} already has a note ({covered[t]}), so this is a revision, not an initiation")
        if t and kind == "revision" and t not in covered:
            F(f"{where}: {t} has no note yet, so this is an initiation, not a revision")
        # Cooldown binds everything but a revision: a revision is the director's
        # way of asking for a name to be looked at again before the screen would.
        if t and d is not None and kind != "revision" and t in covered and cooldown:
            since = (d - date.fromisoformat(covered[t])).days
            if since < cooldown:
                F(f"{where}: {t} was covered {since} days before {d}; the cooldown is {cooldown} days")
        out.append({"date": d.isoformat() if d else a.get("date", ""), "ticker": t, "kind": kind,
                    "desk": (a.get("desk") or "").strip(), "reason": (a.get("reason") or "").strip(),
                    "sector": sector, "price": num((r or {}).get("price"))})

    by_day = {}
    for a in out:
        by_day.setdefault(a["date"], []).append(a)
    for d, items in sorted(by_day.items()):
        if len(items) > slots:
            F(f"{d}: {len(items)} assignments, more than the {slots} slots a run has")
        by_sector = {}
        for a in items:
            if a["sector"]:
                by_sector.setdefault(a["sector"], []).append(a)
        for sector, group in by_sector.items():
            plain = [a for a in group if not SECTOR_EXEMPT.search(a["reason"])]
            if len(group) > per_sector and len(plain) > per_sector:
                F(f"{d}: {len(group)} {sector} names ({', '.join(a['ticker'] for a in group)}), above "
                  f"the {per_sector} a run allows. A second name from one sector needs a reason that "
                  f"cites a holding or an earnings date.")
    if not out and not fails:
        W("no assignments: every run this week will come from the screen")
    return fails, warns, {"week_of": week_of.isoformat() if week_of else "", "assignments": out}


def assignments_for(plan, day):
    """The plan's assignments for one date, in the order written."""
    day = day.isoformat() if isinstance(day, date) else str(day)
    return [a for a in (plan or {}).get("assignments", []) if a["date"] == day]


def check_lessons_append(old, new, path="playbook"):
    """Fails for a playbook change that is anything but new dated lines at the
    end of its "## Lessons" section, which must be the file's last section."""
    fails = []
    o, n = old.rstrip("\n"), new.rstrip("\n")
    if o == n:
        return fails
    heads = re.findall(r"^## .*$", o, re.M)
    if not heads or heads[-1].strip() != LESSONS:
        return [f"{path}: '{LESSONS}' is not the last section, so nothing can be appended to it. "
                f"Propose the change in the plan instead."]
    if not n.startswith(o):
        return [f"{path}: the change edits or deletes existing text. The director may only append "
                f"lessons at the end; any other change is a proposal in '## Playbook proposals'."]
    for line in n[len(o):].splitlines():
        if not line.strip():
            continue
        if line.lstrip().startswith("#"):
            fails.append(f"{path}: adds a heading ({line.strip()[:40]!r}); lessons are list lines only")
        elif not LESSON_LINE.match(line):
            fails.append(f"{path}: {line.strip()[:60]!r} is not a lesson line. Write '- YYYY-MM-DD: "
                         f"one or two sentences, citing the memo, check or score behind it.'")
        if EM_DASH in line or EN_DASH in line:
            fails.append(f"{path}: a lesson contains an em or en dash")
    return fails


def check_changes(changes, read_old, read_new):
    """Fails for any change the director's commit may not make.

    changes is [(status, path)] as git prints them (A, M, D); read_old and
    read_new return a path's text before and after."""
    fails = []
    for status, p in changes:
        status = (status or "?")[0]
        if p.startswith(INPUTS_PREFIX) and not INPUT_FILE.match(p):
            fails.append(f"{p}: theses/director/inputs/ holds only the week's inputs "
                         f"({{WEEK_OF}}.json and .md); the news is the News Desk's, in theses/news/")
        elif p.startswith(DIRECTOR_PREFIX):
            if status == "D":
                fails.append(f"{p}: deletes a file under theses/director/; plans are never deleted")
        elif PLAYBOOK_GLOB.match(p):
            if status != "M":
                fails.append(f"{p}: {'adds' if status == 'A' else 'removes'} a playbook; only the owner does that")
            else:
                fails += check_lessons_append(read_old(p) or "", read_new(p) or "", p)
        else:
            fails.append(f"{p}: the director may change only theses/director/ and the '{LESSONS}' "
                         f"section of theses/desks/sectors/*.md")
    return fails


def git_changes(base=None, repo=None):
    """([(status, path)], read_old, read_new) for the staged changes (base None)
    or for base..HEAD."""
    import subprocess
    repo = str(repo or THESES.parent)

    def git(*a):
        r = subprocess.run(["git", *a], cwd=repo, capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(f"git {' '.join(a)}: {r.stderr.strip()}")
        return r.stdout

    diff = (["diff", "--cached", "--no-renames", "--name-status"] if base is None
            else ["diff", "--no-renames", "--name-status", f"{base}..HEAD"])
    changes = [tuple(l.split("\t", 1)) for l in git(*diff).splitlines() if "\t" in l]

    def show(ref, p):
        try:
            return git("show", f"{ref}:{p}")
        except RuntimeError:
            return ""
    old_ref = "HEAD" if base is None else base
    new_ref = "" if base is None else "HEAD"
    return changes, (lambda p: show(old_ref, p)), (lambda p: show(new_ref, p))


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        print(__doc__)
        return 2
    if args[0] == "--changes":
        base = args[1] if len(args) > 1 else None
        changes, old, new = git_changes(base)
        fails = check_changes(changes, old, new)
        what = "staged changes" if base is None else f"changes {base}..HEAD"
        print(f"[{'FAIL' if fails else 'pass'}] {what}: {len(changes)} file{'s' if len(changes) != 1 else ''}")
        for f in fails:
            print(f"  FAIL  {f}")
        return 1 if fails else 0
    bad = 0
    for p in args:
        fails, warns, plan = check_plan(p)
        status = "FAIL" if fails else ("warn" if warns else "pass")
        n = len((plan or {}).get("assignments") or [])
        print(f"[{status}] {p}: {n} assignment{'s' if n != 1 else ''}")
        for f in fails:
            print(f"  FAIL  {f}")
        for w in warns:
            print(f"  warn  {w}")
        bad += bool(fails)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
