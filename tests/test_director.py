"""The Research Director: desks and playbooks, the plan check, the input pack,
and how prepare.py takes a plan (theses/bin/desks.py, director_check.py,
director_inputs.py, prepare.py).

Nothing here touches the network or writes into the repository: plans, notes
and run directories go to temporary folders, and the panel is a fixture.
"""
import contextlib
import csv
import io
import json
import os
import re
import sys
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests import helpers as H  # noqa: E402

# theses/bin has a queue.py that would shadow the standard library's, so the
# path is restored once these are loaded (as in test_memo_validate).
import queue  # noqa: E402,F401
_saved_path = list(sys.path)
sys.path.insert(0, str(H.REPO / "theses" / "bin"))
import desks as D  # noqa: E402
import director_check as DC  # noqa: E402
import director_inputs as DI  # noqa: E402
import prepare  # noqa: E402
import validate  # noqa: E402,F401  (director_inputs imports it lazily)
sys.path[:] = _saved_path

EM, EN = chr(0x2014), chr(0x2013)
CFG = json.loads((H.REPO / "theses" / "config.json").read_text(encoding="utf-8"))


def row(ticker, sector, price="100", security_type="operating", earnings_date="", name=None):
    return {"ticker": ticker, "name": name or f"{ticker} Inc", "sector": sector,
            "security_type": security_type, "price": price, "earnings_date": earnings_date}


PANEL = [
    row("JPM", "Financials"), row("BAC", "Financials"), row("WFC", "Financials"),
    row("DELL", "Information Technology"), row("MU", "Information Technology"),
    row("NVDA", "Information Technology", price="200", earnings_date="2026-10-02"),
    row("CF", "Materials", price="120"), row("HIMS", "Health Care"),
    row("XOM", "Energy"), row("KO", "Consumer Staples"),
    row("QQQX", "Financials", security_type="cef"), row("NOSEC", ""),
]
EVENTS = [
    {"ticker": "NVDA", "date": "2026-09-22", "kind": "initiate", "direction": "watch"},
    {"ticker": "CF", "date": "2026-09-19", "kind": "revise", "direction": "avoid"},
    {"ticker": "HIMS", "date": "2026-09-25", "kind": "revise", "direction": "avoid"},
]

BODY = """
## This week's focus

**Technology and communications.** Look at memory prices in the filings.

**Energy, materials and utilities.** Nothing assigned; the screen may bring one.

**Financials and real estate.** Value banks on tangible book.

**Health care.** Nothing assigned this week.

**Consumer and industrials.** Nothing assigned this week.

## Review of last week's memos

### NVDA, 2026-09-22

Grade: B. The argument was clear. Rewrite ask: cut the peer figures from section 2.

## Coverage gaps

No bank is covered.

## Playbook proposals

None this week.
"""


def plan_text(assignments, week_of="2026-09-28", body=BODY):
    lines = ["---", f"week_of: {week_of}", "assignments:"]
    lines += [f"  - {a}" for a in assignments]
    if not assignments:
        lines[-1] = "assignments: []"
    return "\n".join(lines + ["---"]) + body


def write_plan(folder, text, name="2026-09-27.md"):
    p = Path(folder) / name
    p.write_text(text, encoding="utf-8")
    return p


GOOD = [
    '{date: 2026-09-28, ticker: JPM, kind: initiation, desk: financials-realestate, reason: "No bank is covered, and the screen cannot score one."}',
    '{date: 2026-09-28, ticker: DELL, kind: initiation, desk: tech-comms, reason: "Top of the screen."}',
    '{date: 2026-09-30, ticker: CF, kind: revision, desk: energy-materials-utilities, reason: "Its open call moved 11%."}',
]


def check(text, name="2026-09-27.md", events=EVENTS, rows=PANEL):
    with H.temp_dir() as d:
        return DC.check_plan(write_plan(d, text, name), rows=rows, events=events, cfg=CFG)


class DesksAndPlaybooks(unittest.TestCase):
    def test_every_panel_sector_has_a_desk_and_a_playbook(self):
        sectors = set()
        for p in sorted((H.REPO / "data" / "fundamentals").glob("*.csv"))[-1:]:
            with p.open(encoding="utf-8", newline="") as fh:
                sectors |= {(r.get("sector") or "").strip() for r in csv.DictReader(fh)}
        sectors.discard("")
        mapping = D.desk_map()
        self.assertEqual(len(mapping), 11)
        for s in sectors | set(mapping):
            self.assertIn(s, mapping, f"{s} has no desk")
            self.assertTrue((D.SECTORS_DIR / f"{D.sector_slug(s)}.md").exists(), f"no playbook for {s}")
        self.assertEqual(sorted(D.desks()), sorted(["tech-comms", "energy-materials-utilities",
                                                    "financials-realestate", "health-care",
                                                    "consumer-industrials"]))

    def test_desk_files_are_short_and_name_their_sectors(self):
        mapping = D.desk_map()
        for desk in D.desks():
            text = D.desk_text(desk)
            words = len(text.split())
            self.assertTrue(150 <= words <= 260, f"{desk} is {words} words")
            for sector in [s for s, d in mapping.items() if d == desk]:
                self.assertIn(sector, text, f"{desk} does not name {sector}")
                self.assertIn(f"sectors/{D.sector_slug(sector)}.md", text)

    def test_playbooks_have_the_sections_and_end_with_lessons(self):
        for p in sorted(D.SECTORS_DIR.glob("*.md")):
            text = p.read_text(encoding="utf-8")
            heads = re.findall(r"^## (.+)$", text, re.M)
            self.assertEqual(heads, ["The questions that decide a stock", "How to value it", "Traps",
                                     "Where our data misleads", "Lessons"], p.name)
            words = len(text.split())
            self.assertTrue(300 <= words <= 560, f"{p.name} is {words} words")

    def test_readme_table_agrees_with_the_config_map(self):
        text = (D.DESKS_DIR / "README.md").read_text(encoding="utf-8")
        mapping = D.desk_map()
        for line in text.splitlines():
            m = re.match(r"^\| ([^|]+) \| `([a-z-]+)\.md` \| ([^|]+) \|$", line)
            if not m:
                continue
            self.assertEqual(m.group(1).strip(), D.desk_title(m.group(2)))
            for sector in m.group(3).split(","):
                self.assertEqual(mapping.get(sector.strip()), m.group(2))

    def test_no_dashes_in_the_new_files(self):
        files = [*D.DESKS_DIR.rglob("*.md"), H.REPO / "theses" / "DIRECTOR.md",
                 H.REPO / "theses" / "routines" / "research-director.md",
                 *(H.REPO / "theses" / "bin" / n for n in ("desks.py", "director_check.py",
                                                           "director_inputs.py"))]
        for f in files:
            text = f.read_text(encoding="utf-8")
            self.assertNotIn(EM, text, f.name)
            self.assertNotIn(EN, text, f.name)

    def test_dossier_injects_the_playbook_and_the_desk_not_the_lens(self):
        src = (H.REPO / "theses" / "bin" / "dossier.py").read_text(encoding="utf-8")
        self.assertIn("desks.playbook_text(", src)
        self.assertIn("desks.desk_text(", src)
        self.assertNotIn("sector_lens(", src)
        self.assertIn("Where our data misleads", D.playbook_text("Financials"))
        self.assertEqual(D.desk_for("Real Estate"), "financials-realestate")
        self.assertEqual(D.desk_for(""), "")
        self.assertEqual(D.desk_title("health-care"), "Health care")

    def test_routine_file_has_the_same_header_format(self):
        text = (H.REPO / "theses" / "routines" / "research-director.md").read_text(encoding="utf-8")
        head, sep, body = text.partition("\n---\n")
        self.assertTrue(sep)
        self.assertRegex(head, r"(?m)^Schedule: Sundays 16:00 ET$")
        self.assertIn("theses/DIRECTOR.md", body)


class PlanCheck(unittest.TestCase):
    def test_a_good_plan_passes(self):
        fails, warns, plan = check(plan_text(GOOD))
        self.assertEqual(fails, [])
        self.assertEqual([a["ticker"] for a in plan["assignments"]], ["JPM", "DELL", "CF"])
        self.assertEqual(plan["assignments"][0]["reason"], "No bank is covered, and the screen cannot score one.")
        self.assertEqual(plan["assignments"][0]["sector"], "Financials")
        self.assertEqual([a["ticker"] for a in DC.assignments_for(plan, "2026-09-28")], ["JPM", "DELL"])

    def test_the_file_is_named_for_the_sunday_and_week_of_is_a_monday(self):
        fails, _, _ = check(plan_text(GOOD), name="2026-09-28.md")
        self.assertTrue(any("must be named 2026-09-27.md" in f for f in fails), fails)
        fails, _, _ = check(plan_text(GOOD, week_of="2026-09-29"), name="2026-09-28.md")
        self.assertTrue(any("must be the Monday" in f for f in fails), fails)
        self.assertEqual(DC.plan_path_for("2026-10-02").name, "2026-09-27.md")
        self.assertEqual(DC.plan_path_for("2026-09-28").name, "2026-09-27.md")

    def test_dates_must_be_weekdays_of_the_week(self):
        for bad in ("2026-10-03", "2026-10-05", "2026-09-25"):
            a = f'{{date: {bad}, ticker: DELL, kind: initiation, desk: tech-comms, reason: "x"}}'
            fails, _, _ = check(plan_text([a]))
            self.assertTrue(any("is not a weekday of the week" in f for f in fails), (bad, fails))

    def test_per_day_slot_limit(self):
        many = [f'{{date: 2026-09-28, ticker: {t}, kind: initiation, desk: {d}, reason: "earnings"}}'
                for t, d in (("JPM", "financials-realestate"), ("DELL", "tech-comms"),
                             ("XOM", "energy-materials-utilities"), ("KO", "consumer-industrials"),
                             ("BAC", "financials-realestate"))]
        fails, _, _ = check(plan_text(many))
        self.assertTrue(any("more than the 4 slots" in f for f in fails), fails)

    def test_one_sector_per_day_unless_a_holding_or_earnings(self):
        two = ['{date: 2026-09-28, ticker: JPM, kind: initiation, desk: financials-realestate, reason: "Gap."}',
               '{date: 2026-09-28, ticker: BAC, kind: initiation, desk: financials-realestate, reason: "Gap too."}']
        fails, _, _ = check(plan_text(two))
        self.assertTrue(any("Financials names" in f for f in fails), fails)
        two[1] = two[1].replace("Gap too.", "Reports earnings on Tuesday.")
        fails, _, _ = check(plan_text(two))
        self.assertEqual(fails, [])
        two[1] = two[1].replace("Reports earnings on Tuesday.", "The hedge book holds it.")
        self.assertEqual(check(plan_text(two))[0], [])
        two[1] = two[1].replace("2026-09-28", "2026-09-29")
        self.assertEqual(check(plan_text(two))[0], [])

    def test_ticker_must_exist_operate_and_have_a_desk(self):
        for t, desk, want in (("ZZZZ", "tech-comms", "not on the latest panel"),
                              ("QQQX", "financials-realestate", "not an operating company"),
                              ("NOSEC", "tech-comms", "no sector")):
            a = f'{{date: 2026-09-28, ticker: {t}, kind: initiation, desk: {desk}, reason: "x"}}'
            fails, _, _ = check(plan_text([a]))
            self.assertTrue(any(want in f for f in fails), (t, fails))

    def test_desk_must_match_the_sector_map(self):
        a = '{date: 2026-09-28, ticker: JPM, kind: initiation, desk: tech-comms, reason: "x"}'
        fails, _, _ = check(plan_text([a]))
        self.assertTrue(any("belongs to 'financials-realestate'" in f for f in fails), fails)

    def test_kind_follows_coverage_and_reason_is_required(self):
        a = '{date: 2026-09-28, ticker: NVDA, kind: initiation, desk: tech-comms, reason: "x"}'
        self.assertTrue(any("this is a revision" in f for f in check(plan_text([a]))[0]))
        a = '{date: 2026-09-28, ticker: DELL, kind: revision, desk: tech-comms, reason: "x"}'
        self.assertTrue(any("this is an initiation" in f for f in check(plan_text([a]))[0]))
        a = '{date: 2026-09-28, ticker: DELL, kind: initiation, desk: tech-comms, reason: ""}'
        self.assertTrue(any("has no reason" in f for f in check(plan_text([a]))[0]))
        a = '{date: 2026-09-28, ticker: DELL, kind: update, desk: tech-comms, reason: "x"}'
        self.assertTrue(any("not initiation or revision" in f for f in check(plan_text([a]))[0]))
        a = '{date: 2026-09-28, ticker: DELL}'
        self.assertTrue(any("has no kind, desk, reason" in f for f in check(plan_text([a]))[0]))

    def test_cooldown_binds_an_initiation_but_not_a_revision(self):
        events = EVENTS + [{"ticker": "DELL", "date": "2026-09-01", "kind": "close"}]
        # DELL has a (closed) note 27 days before: an initiation would also be refused
        # as covered, so check the cooldown message on its own.
        a = '{date: 2026-09-28, ticker: DELL, kind: initiation, desk: tech-comms, reason: "x"}'
        fails, _, _ = check(plan_text([a]), events=events)
        self.assertTrue(any("the cooldown is 45 days" in f for f in fails), fails)
        a = '{date: 2026-09-28, ticker: HIMS, kind: revision, desk: health-care, reason: "Moved 20%."}'
        self.assertEqual(check(plan_text([a]))[0], [])

    def test_twice_in_a_week_fails(self):
        a = ['{date: 2026-09-28, ticker: DELL, kind: initiation, desk: tech-comms, reason: "x"}',
             '{date: 2026-09-29, ticker: DELL, kind: initiation, desk: tech-comms, reason: "x"}']
        self.assertTrue(any("assigned twice" in f for f in check(plan_text(a))[0]))

    def test_body_sections_and_desk_paragraphs(self):
        fails, _, _ = check(plan_text(GOOD, body=BODY.replace("## Coverage gaps", "## Gaps")))
        self.assertTrue(any("missing the section '## Coverage gaps'" in f for f in fails), fails)
        fails, _, _ = check(plan_text(GOOD, body=BODY.replace("**Health care.** Nothing assigned this week.", "")))
        self.assertTrue(any("no paragraph for the Health care desk" in f for f in fails), fails)
        fails, _, _ = check(plan_text(GOOD, body=BODY.replace("## Playbook proposals\n\nNone this week.\n", "")))
        self.assertTrue(any("Playbook proposals" in f for f in fails), fails)

    def test_dashes_and_bad_lines_fail(self):
        fails, _, _ = check(plan_text(GOOD, body=BODY.replace("No bank is covered.", "No bank " + EM + " none.")))
        self.assertTrue(any("em or en dash" in f for f in fails), fails)
        fails, _, _ = check(plan_text(["date: 2026-09-28 ticker DELL"]))
        self.assertTrue(any("Write it on one line" in f for f in fails), fails)
        fails, _, _ = check("no front matter")
        self.assertTrue(fails)

    def test_an_empty_week_passes_with_a_warning(self):
        fails, warns, plan = check(plan_text([]))
        self.assertEqual(fails, [])
        self.assertEqual(plan["assignments"], [])
        self.assertTrue(any("every run this week will come from the screen" in w for w in warns))


class ChangeGuard(unittest.TestCase):
    OLD = "# Energy\n\n## Traps\n\n- one\n\n## Lessons\n"

    def test_appending_a_dated_lesson_passes(self):
        new = self.OLD + "\n- 2026-09-27: Refiners were valued on a peak year. Evidence: theses/notes/MPC/x.md.\n"
        self.assertEqual(DC.check_lessons_append(self.OLD, new), [])
        self.assertEqual(DC.check_lessons_append(self.OLD, self.OLD), [])

    def test_editing_earlier_text_or_adding_a_heading_fails(self):
        self.assertTrue(DC.check_lessons_append(self.OLD, self.OLD.replace("one", "two")))
        self.assertTrue(DC.check_lessons_append(self.OLD, self.OLD + "\n## New\n"))
        self.assertTrue(DC.check_lessons_append(self.OLD, self.OLD + "\nA loose sentence.\n"))
        self.assertTrue(DC.check_lessons_append(self.OLD, self.OLD + "\n- 2026-09-27: a " + EM + " b\n"))
        lessons_not_last = "# X\n\n## Lessons\n\n## Traps\n"
        self.assertTrue(DC.check_lessons_append(lessons_not_last, lessons_not_last + "- 2026-09-27: x\n"))

    def test_only_director_files_and_playbook_lessons_may_change(self):
        old = {"theses/desks/sectors/energy.md": self.OLD}
        new = {"theses/desks/sectors/energy.md": self.OLD + "- 2026-09-27: ok, see theses/notes/X.\n"}
        ok = [("A", "theses/director/2026-09-27.md"), ("A", "theses/director/inputs/2026-09-28.json"),
              ("M", "theses/desks/sectors/energy.md")]
        self.assertEqual(DC.check_changes(ok, old.get, new.get), [])
        for bad in (("M", "theses/desks/tech-comms.md"), ("M", "theses/PROMPTS.md"),
                    ("A", "theses/notes/DELL/2026-09-28-initiation.md"), ("M", "theses/ledger/events.csv"),
                    ("D", "theses/director/2026-09-20.md"), ("A", "theses/desks/sectors/new.md"),
                    ("M", "theses/bin/director_check.py")):
            self.assertTrue(DC.check_changes([bad], old.get, new.get), bad)
        edited = {"theses/desks/sectors/energy.md": self.OLD.replace("one", "two")}
        self.assertTrue(DC.check_changes([("M", "theses/desks/sectors/energy.md")], old.get, edited.get))


class PrepareTakesThePlan(unittest.TestCase):
    """The three paths, and that with no plan the run is the screen's, untouched."""

    SCREEN = {"run_date": "2026-09-28", "panel_date": "2026-09-25", "panel_rows": 10,
              "core_universe": 8, "scorable": 6, "peer_groups": 3, "open_predictions": 0,
              "slots": [{"ticker": "CF", "slot": "review", "reason": "moved", "sector": "Materials"},
                        {"ticker": "KO", "slot": "contrarian", "reason": "low", "sector": "Consumer Staples"},
                        {"ticker": "BAC", "slot": "screen", "reason": "top", "sector": "Financials"},
                        {"ticker": "MU", "slot": "screen", "reason": "top", "sector": "Information Technology"},
                        {"ticker": "XOM", "slot": "screen", "reason": "top", "sector": "Energy"},
                        {"ticker": "HIMS", "slot": "screen", "reason": "top", "sector": "Health Care"}]}

    def run_prepare(self, plan=None, date_="2026-09-28"):
        calls = []

        def fake_run(cmd, capture_output=True, text=True, env=None):
            calls.append((cmd, dict(env or {})))
            if cmd[1].endswith("screen.py"):
                n = int((env or {}).get("THESES_SLOTS") or CFG["slots_per_run"])
                return mock.Mock(returncode=0, stdout=json.dumps(dict(self.SCREEN, slots=self.SCREEN["slots"][:n])), stderr="")
            return mock.Mock(returncode=0, stdout="x" * 600, stderr="")

        with H.temp_dir() as d:
            plans = d / "director"
            plans.mkdir()
            if plan is not None:
                write_plan(plans, plan)
            env = {k: v for k, v in os.environ.items() if k != "THESES_SLOTS"}
            with mock.patch.object(prepare, "fetch_site", return_value=b"{}"), \
                    mock.patch.object(prepare.subprocess, "run", side_effect=fake_run), \
                    mock.patch.object(prepare, "THESES", d), \
                    mock.patch.object(prepare, "hit_rate", return_value={"scored": 0}), \
                    mock.patch.object(DC, "PLAN_DIR", plans), \
                    mock.patch.object(DC, "load_panel", return_value=("2026-09-25", PANEL)), \
                    mock.patch.object(DC, "read_csv_rows", return_value=EVENTS), \
                    mock.patch.dict(os.environ, env, clear=True), \
                    contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                with mock.patch.object(sys, "argv", ["prepare.py", "--date", date_]):
                    rc = prepare.main()
            manifest = json.loads((d / "runs" / date_ / "manifest.json").read_text(encoding="utf-8"))
        return rc, manifest, calls

    def screen_env(self, calls):
        return next(env for cmd, env in calls if cmd[1].endswith("screen.py"))

    def test_no_plan_is_exactly_the_screen(self):
        rc, m, calls = self.run_prepare(plan=None)
        self.assertEqual(rc, 0)
        self.assertEqual(m["director"]["path"], "no_plan")
        self.assertEqual([s["ticker"] for s in m["slots"]], ["CF", "KO", "BAC", "MU"])
        self.assertEqual([s["slot"] for s in m["slots"]], ["review", "contrarian", "screen", "screen"])
        self.assertNotIn("THESES_SLOTS", self.screen_env(calls))
        for s, want in zip(m["slots"], self.SCREEN["slots"]):
            self.assertEqual({k: v for k, v in s.items() if k not in ("dossier", "dossier_chars")}, want)

    def test_an_invalid_plan_is_ignored(self):
        bad = plan_text(['{date: 2026-09-28, ticker: ZZZZ, kind: initiation, desk: tech-comms, reason: "x"}'])
        rc, m, calls = self.run_prepare(plan=bad)
        self.assertEqual(m["director"]["path"], "invalid_plan")
        self.assertTrue(any("ZZZZ" in f for f in m["director"]["fails"]))
        self.assertEqual([s["ticker"] for s in m["slots"]], ["CF", "KO", "BAC", "MU"])
        self.assertNotIn("THESES_SLOTS", self.screen_env(calls))

    def test_a_plan_with_nothing_today_leaves_the_screen_alone(self):
        rc, m, calls = self.run_prepare(plan=plan_text(GOOD), date_="2026-09-29")
        self.assertEqual(m["director"]["path"], "no_assignments_today")
        self.assertEqual([s["ticker"] for s in m["slots"]], ["CF", "KO", "BAC", "MU"])
        self.assertNotIn("THESES_SLOTS", self.screen_env(calls))

    def test_todays_assignments_come_first_and_the_screen_fills_the_rest(self):
        rc, m, calls = self.run_prepare(plan=plan_text(GOOD), date_="2026-09-28")
        self.assertEqual(rc, 0)
        self.assertEqual(m["director"]["path"], "director")
        self.assertEqual(m["director"]["assigned"], ["JPM", "DELL"])
        # BAC (Financials) and MU (Information Technology) share a sector with a
        # director name, so the screen's next names in other sectors fill in.
        self.assertEqual([s["ticker"] for s in m["slots"]], ["JPM", "DELL", "CF", "KO"])
        self.assertEqual([s["slot"] for s in m["slots"][:2]], ["director", "director"])
        self.assertEqual(m["slots"][0]["desk"], "financials-realestate")
        self.assertEqual(self.screen_env(calls)["THESES_SLOTS"], "6")

    def test_a_plan_whose_check_raises_is_ignored(self):
        with H.temp_dir() as d, mock.patch.object(prepare, "THESES", d):
            (d / "director").mkdir()
            write_plan(d / "director", plan_text(GOOD))

            def boom(path):
                raise RuntimeError("panel unreachable")
            info, got = prepare.director_assignments("2026-09-28", check=boom)
        self.assertEqual(info["path"], "invalid_plan")
        self.assertEqual(got, [])
        info, got = prepare.director_assignments("not a date")
        self.assertEqual(info["path"], "no_plan")

    def test_prepare_finds_the_same_plan_file_as_director_check(self):
        for d in ("2026-09-28", "2026-10-02", "2026-10-03", "2026-10-04", "2026-10-05"):
            with H.temp_dir() as t, mock.patch.object(prepare, "THESES", t):
                info, _ = prepare.director_assignments(d)
            self.assertEqual(info["plan"], f"theses/director/{DC.plan_path_for(d).name}", d)

    def test_merge_relaxes_the_sector_rule_only_to_fill(self):
        assigned = [{"ticker": "JPM", "reason": "r", "kind": "initiation", "desk": "financials-realestate",
                     "sector": "Financials"}]
        screened = [{"ticker": "BAC", "sector": "Financials"}, {"ticker": "JPM", "sector": "Financials"},
                    {"ticker": "MU", "sector": "Information Technology"}]
        out = prepare.merge_slots(assigned, screened, 3)
        self.assertEqual([s["ticker"] for s in out], ["JPM", "MU", "BAC"])
        self.assertEqual([s["ticker"] for s in prepare.merge_slots(assigned * 1, screened, 1)], ["JPM"])


class InputPack(unittest.TestCase):
    def note(self, folder, ticker, name, fm):
        d = folder / ticker
        d.mkdir(parents=True, exist_ok=True)
        lines = ["---"] + [f"{k}: {v}" for k, v in fm.items()] + ["---", "", "Body text here. " * 20]
        (d / name).write_text("\n".join(lines), encoding="utf-8")

    def build(self, **kw):
        with H.temp_dir() as d:
            notes = d / "notes"
            self.note(notes, "NVDA", "2026-09-22-initiation.md",
                      {"ticker": "NVDA", "entry_price": "180", "review_by": "2026-12-01", "direction": "watch"})
            self.note(notes, "CF", "2026-09-19-revision.md",
                      {"ticker": "CF", "entry_price": "120", "review_by": "2026-09-20", "direction": "avoid"})
            self.note(notes, "HIMS", "2026-09-12-initiation.md",
                      {"ticker": "HIMS", "entry_price": "100", "review_by": "2027-01-01"})
            with mock.patch.object(DC, "PLAN_DIR", d / "director"):
                pack = DI.build(date(2026, 9, 27), date(2026, 9, 28), PANEL, CFG,
                                events=EVENTS, predictions=kw.get("predictions", []), scores=[],
                                notes_dir=notes,
                                holdings={"hedge": ["XOM", "NVDA"], "neural": ["XOM"]},
                                candidates={"lg-value": ["KO", "CF"]},
                                letters=[{"letter": "portfolio/letters/x/hedge.md", "question": "Is XOM cheap?"}],
                                screen=({"would_assign": [], "top_off_cooldown": []},
                                        {"tech-comms": {"gated": 5, "scorable": 4}}))
        return pack

    def test_the_pack(self):
        p = self.build(predictions=[{"prediction_id": "NVDA-1", "ticker": "NVDA", "entry_price": "150",
                                     "written_on": "2026-09-01"}])
        self.assertEqual([(h["ticker"], h["books"]) for h in p["holdings_without_memo"]],
                         [("XOM", ["hedge", "neural"])])
        self.assertEqual([c["ticker"] for c in p["candidates_without_memo"]], ["KO"])
        self.assertEqual(p["earnings_window"], ["2026-09-28", "2026-10-09"])
        self.assertEqual([(e["ticker"], e["earnings_date"]) for e in p["earnings_ahead"]],
                         [("NVDA", "2026-10-02")])
        stale = {s["ticker"]: s["reasons"] for s in p["stale_views"]}
        self.assertIn("review_by 2026-09-20 has passed", stale["CF"])
        self.assertTrue(any("price moved +11.1%" in r for r in stale["NVDA"]), stale)
        self.assertTrue(any("open prediction NVDA-1 moved +33.3%" in r for r in stale["NVDA"]), stale)
        self.assertNotIn("HIMS", stale)
        self.assertEqual([m["ticker"] for m in p["last_week_memos"]], ["NVDA"])
        m = p["last_week_memos"][0]
        self.assertEqual(m["desk"], "tech-comms")
        self.assertIn(m["validate"]["status"], ("FAIL", "warn", "pass"))
        self.assertGreater(m["prose_words"], 30)
        card = {c["desk"]: c for c in p["desk_scorecard"]}
        self.assertEqual(card["tech-comms"]["covered"], ["NVDA"])
        self.assertEqual(card["tech-comms"]["memos_last_week"], 1)
        self.assertEqual(card["tech-comms"]["open_calls"], 1)
        self.assertEqual(p["slots"]["days"][0], "2026-09-28")
        self.assertEqual(p["generated_for"]["plan_file"], "theses/director/2026-09-27.md")
        md = DI.summary(p)
        self.assertIn("XOM", md)
        self.assertIn("Is XOM cheap?", md)
        self.assertNotIn(EM, md)
        self.assertNotIn(EN, md)

    def test_trading_days_and_next_monday(self):
        self.assertEqual(DI.trading_days_after(date(2026, 9, 25), 2), [date(2026, 9, 28), date(2026, 9, 29)])
        self.assertEqual(DI.next_monday(date(2026, 9, 27)), date(2026, 9, 28))
        self.assertEqual(DI.next_monday(date(2026, 9, 28)), date(2026, 10, 5))

    def test_letters_questions(self):
        with H.temp_dir() as d:
            (d / "2026-09-25").mkdir()
            (d / "2026-09-25" / "hedge.md").write_text(
                "# Hedge\n\nSome text.\n\n## Open questions\n\n- Why is XOM cheap?\n- What about CF?\n\n## Trades\n\nNone?\n",
                encoding="utf-8")
            (d / "2026-08-01").mkdir()
            (d / "2026-08-01" / "hedge.md").write_text("Old question?", encoding="utf-8")
            qs, note = DI.letters_questions(date(2026, 9, 27), d)
        self.assertEqual([q["question"] for q in qs], ["Why is XOM cheap?", "What about CF?"])
        qs, note = DI.letters_questions(date(2026, 9, 27), Path("/nonexistent/letters"))
        self.assertEqual(qs, [])
        self.assertIn("no letters yet", note)


class ResearchPage(unittest.TestCase):
    def test_director_block_and_plan(self):
        LF = H.LF
        how = LF._director_instructions()
        self.assertEqual(how["routine"]["schedule"], "Sundays 16:00 ET")
        self.assertIn("director_inputs.py", how["prompt"])
        with H.temp_dir() as d:
            write_plan(d, plan_text(GOOD, body=BODY.replace("Look at memory", "<script>x</script> Look at memory")))
            write_plan(d, plan_text([], week_of="2026-10-05"), name="2026-10-04.md")
            with H.patched(LF, DIRECTOR_PLANS=d):
                plan = LF._director_plan(date(2026, 9, 30))
                later = LF._director_plan(date(2026, 10, 4))
                none = LF._director_plan(date(2026, 9, 26))
        self.assertEqual(plan["weekOf"], "2026-09-28")
        self.assertEqual([a["ticker"] for a in plan["assignments"]], ["JPM", "DELL", "CF"])
        self.assertEqual(plan["assignments"][0]["deskTitle"], "Financials and real estate")
        self.assertEqual(plan["assignments"][0]["reason"], "No bank is covered, and the screen cannot score one.")
        focus = next(s for s in plan["sections"] if s["title"] == "This week's focus")
        self.assertNotIn("<script>", focus["html"])
        self.assertIn("&lt;script&gt;", focus["html"])
        self.assertEqual(later["weekOf"], "2026-10-05")
        self.assertIsNone(none)
        titles = LF._desk_titles()
        self.assertEqual(titles["Utilities"], {"desk": "energy-materials-utilities",
                                               "title": "Energy, materials and utilities"})

    def test_plan_names_join_the_reading_pack(self):
        LF = H.LF
        with H.temp_dir() as d:
            (d / "director").mkdir()
            write_plan(d / "director", plan_text(GOOD))
            write_plan(d / "director", plan_text(GOOD[:1], week_of="2026-09-21"), name="2026-09-20.md")
            with H.patched(LF, THESES_DIR=d):
                self.assertEqual(LF._director_plan_tickers(date(2026, 9, 29)), ["JPM", "DELL", "CF"])
                self.assertEqual(LF._director_plan_tickers(date(2026, 9, 26)), ["JPM", "JPM", "DELL", "CF"])
            with H.patched(LF, THESES_DIR=d / "nothing"):
                self.assertEqual(LF._director_plan_tickers(date(2026, 9, 29)), [])

    def test_ledger_js_renders_the_director_and_the_desk(self):
        js = (H.REPO / "web" / "ledger.js").read_text(encoding="utf-8")
        self.assertIn("directorPlanHTML(CFG.directorPlan)", js)
        self.assertIn("directorInstructionsHTML(CFG.howDirector)", js)
        self.assertIn("<th>Desk</th>", js)


if __name__ == "__main__":
    unittest.main()
