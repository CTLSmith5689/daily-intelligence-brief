"""The buy-side memo format (format: memo) in theses/bin/validate.py and events.py.

The fixtures in tests/fixtures/memo/ are the reference NVIDIA memo, drafted on
2026-09-23 and rewritten on 2026-09-24 as an argument-first memo from the same
figures: once as the full initiation, and once as the revision the analyst would
write now that NVIDIA already has a note. Every test that needs
the ledger gets a temporary one; nothing here reads or writes theses/ledger/.
"""
import contextlib
import csv
import io
import os
import re
import shutil
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests import helpers as H  # noqa: E402

# theses/bin has a queue.py. Left at the front of sys.path it would shadow the
# standard library's queue for every test that runs after this module is
# imported, so the path is restored as soon as the two modules are loaded.
import queue  # noqa: E402,F401  (the standard library's, pinned before the path changes)
_saved_path = list(sys.path)
sys.path.insert(0, str(H.REPO / "theses" / "bin"))
import validate  # noqa: E402
import events  # noqa: E402
sys.path[:] = _saved_path

FIX = H.REPO / "tests" / "fixtures" / "memo"
REVISION = FIX / "NVDA-2026-09-23-revision.md"
INITIATION = FIX / "NVDA-2026-09-23-initiation.md"
NOTES = sorted((H.REPO / "theses" / "notes").glob("*/*.md"))

# The ledger as it stood when the memo format was switched on, in miniature: the
# old header, a quoted field with a comma, and the live file's mixed line
# endings (a header ending in \n, rows ending in \r\n).
OLD_HEADER = ",".join(events.OLD_EVENT_COLUMNS)
PRIOR_NVDA = ('NVDA-2026-09-22-1,2026-09-22,NVDA,initiate,NVDA-2026-09-22,'
              'theses/notes/NVDA/2026-09-22-initiation.md,watch,4,240.00,252,,,,,'
              '"weekly run, watchlist name longest since covered",first note on NVIDIA')
PRIOR_CF = ('CF-2026-09-12-1,2026-09-12,CF,initiate,CF-2026-09-12,theses/notes/CF/2026-09-12-initiation.md,'
            'long,2,152.00,252,,,,,"screen slot, highest composite off cooldown",balance sheet absorbs the cycle')
OLD_LEDGER = OLD_HEADER + "\n" + PRIOR_CF + "\r\n" + PRIOR_NVDA + "\r\n"


def fails_matching(fails, pattern):
    return [f for f in fails if re.search(pattern, f)]


class MemoCase(unittest.TestCase):
    """A temporary ledger holding NVIDIA's earlier note, and a place to write
    altered copies of the fixtures."""

    def setUp(self):
        self._stack = contextlib.ExitStack()
        self.tmp = self._stack.enter_context(H.temp_dir())
        self.ledger = self.tmp / "ledger"
        self.ledger.mkdir()
        (self.ledger / "events.csv").write_bytes(OLD_LEDGER.encode("utf-8"))
        self._stack.enter_context(H.patched(validate, LEDGER=self.ledger))
        self._stack.enter_context(H.patched(events, LEDGER=self.ledger))
        self.revision = REVISION.read_text(encoding="utf-8")
        self.initiation = INITIATION.read_text(encoding="utf-8")

    def tearDown(self):
        self._stack.close()

    def check(self, text, name="NVDA-memo.md"):
        p = self.tmp / name
        p.write_text(text, encoding="utf-8")
        return validate.check(p)

    def swap(self, text, old, new, count=1):
        self.assertEqual(text.count(old), count, f"fixture no longer contains {old!r}")
        return text.replace(old, new)


class PassingMemo(MemoCase):

    def test_revision_fixture_passes_with_no_warnings(self):
        fails, warns = validate.check(REVISION)
        self.assertEqual(fails, [])
        self.assertEqual(warns, [])

    def test_revision_front_matter_matches_the_contract(self):
        fm, _ = validate.parse(self.revision)
        self.assertEqual(fm["format"], "memo")
        self.assertEqual(fm["kind"], "revision")
        self.assertEqual(fm["thesis_id"], "NVDA-2026-09-23")
        self.assertEqual(fm["action"], "Avoid")
        self.assertEqual(fm["direction"], "watch")
        self.assertEqual(fm["horizon_days"], "365")
        for k in ("size_now", "size_plan", "expected_return", "bear_return", "required_return"):
            self.assertIn(k, fm)
        self.assertEqual(len(fm["scenarios"]), 3)

    def test_full_initiation_passes_when_the_ticker_is_not_covered(self):
        (self.ledger / "events.csv").write_text(OLD_HEADER + "\n", encoding="utf-8")
        fails, warns = validate.check(INITIATION)
        self.assertEqual(fails, [])
        self.assertEqual(warns, [])

    def test_old_format_word_checks_would_have_refused_it(self):
        # The switch is what lets the memo through: with format removed it is
        # judged as an old note and fails on headings and length.
        fails, _ = self.check(self.swap(self.revision, "format: memo\n", ""))
        self.assertTrue(fails_matching(fails, r"missing the heading: WHAT THE COMPANY DOES"))


class InitiationWhenCovered(MemoCase):

    def test_initiation_is_rejected_when_the_ledger_has_the_ticker(self):
        fails, _ = validate.check(INITIATION)
        self.assertEqual(len(fails), 1, fails)
        self.assertRegex(fails[0], r"kind is initiation, but the ledger already has 1 event\(s\) for NVDA")

    def test_a_revision_is_not_rejected(self):
        fails, _ = validate.check(REVISION)
        self.assertEqual(fails_matching(fails, r"kind is initiation"), [])

    def test_the_note_s_own_event_does_not_count(self):
        (self.ledger / "events.csv").write_text(OLD_HEADER + "\n", encoding="utf-8")
        with H.quiet():
            events.record(INITIATION, "test", "test")
        fails, _ = validate.check(INITIATION)
        self.assertEqual(fails, [])

    def test_old_format_initiations_are_not_checked_against_the_ledger(self):
        fails, _ = validate.check(H.REPO / "theses" / "notes" / "NVDA" / "2026-09-22-initiation.md")
        self.assertEqual(fails, [])


class ScenarioArithmetic(MemoCase):
    """Section 3's scenario table and page one's returns against the front-matter."""

    CASES = [
        ("probabilities do not sum to 1",
         [("{case: base, value: 260.00, probability: 0.50}", "{case: base, value: 260.00, probability: 0.52}")],
         r"probabilities sum to 1\.020, not 1"),
        ("stated weighted value is off by more than $0.50",
         [("| Probability-weighted | | $253.75", "| Probability-weighted | | $254.50")],
         r"weighted value of \$254\.50, but the three cases give \$253\.75"),
        ("a case value in section 3 disagrees with the front-matter",
         [("| Bull | 25% | $370 |", "| Bull | 25% | $380 |")],
         r"gives the bull case \$380\.00; scenarios in the front-matter say \$370\.00"),
        ("a case probability in section 3 disagrees with the front-matter",
         [("| Bear | 25% | $125 |", "| Bear | 30% | $125 |")],
         r"bear case a probability of 30%"),
        ("expected_return does not follow from the weighted value",
         [("expected_return: 0.113", "expected_return: 0.125")],
         r"expected_return 0\.125 does not follow from the weighted value \$253\.75"),
        ("bear_return does not follow from the bear value",
         [("bear_return: -0.454", "bear_return: -0.440")],
         r"bear_return -0\.44 does not follow from the bear value \$125\.00"),
        ("target_price more than $5 from the weighted value",
         [("target_price: 255.00", "target_price: 259.00")],
         r"target_price 259 is more than \$5 from the probability-weighted value \$253\.75"),
        ("no probability-weighted row in section 3",
         [("| Probability-weighted | | $253.75 | +10.9%; +11.3% with $1.00 of dividends | |\n", "")],
         r"3\. WHAT IT IS WORTH does not show the probability-weighted value"),
        ("page one leaves out the expected return",
         [("I expect a return of 11.3% over 12 months", "I expect a modest return over 12 months")],
         r"page one does not show the expected return as a percentage \(11\.3%\)"),
        ("page one leaves out the bear-case loss",
         [("loses 45.4%", "loses heavily")],
         r"page one does not show the bear-case loss as a percentage \(45\.4%\)"),
    ]

    def test_each_arithmetic_failure(self):
        for label, swaps, expect in self.CASES:
            with self.subTest(label):
                text = self.revision
                for old, new in swaps:
                    text = self.swap(text, old, new)
                fails, _ = self.check(text)
                self.assertTrue(fails_matching(fails, expect), fails)

    def test_within_tolerance_passes(self):
        text = self.swap(self.revision, "target_price: 255.00", "target_price: 258.00")
        text = self.swap(text, "expected_return: 0.113", "expected_return: 0.117")
        text = self.swap(text, "bear_return: -0.454", "bear_return: -0.450")
        fails, _ = self.check(text)
        self.assertEqual(fails, [])

    def test_expected_return_may_leave_out_the_dividend(self):
        # (253.75 + 1.00) / 228.87 - 1 = 0.113 with the stated dividend; 0.109 without.
        fails, _ = self.check(self.swap(self.revision, "expected_return: 0.113", "expected_return: 0.109"))
        self.assertEqual(fails_matching(fails, r"expected_return"), [])

    def test_scenarios_must_be_three_named_cases(self):
        text = self.swap(self.revision, "  - {case: bear, value: 125.00, probability: 0.25}\n", "")
        fails, _ = self.check(text)
        self.assertTrue(fails_matching(fails, r"exactly three cases"), fails)
        text = self.swap(self.revision, "{case: bear, value: 125.00", "{case: worst, value: 125.00")
        fails, _ = self.check(text)
        self.assertTrue(fails_matching(fails, r"scenarios item 3"), fails)


class ActionAndDirection(MemoCase):

    MAPPING = {"Initiate": {"long"}, "Add": {"long"}, "Hold": {"long"}, "Trim": {"long"},
               "Exit": {"watch"}, "Avoid": {"watch", "avoid"}, "Short": {"short"}}

    def test_every_action_against_every_direction(self):
        self.assertEqual(validate.MEMO_ACTIONS, self.MAPPING)
        for action, allowed in self.MAPPING.items():
            for direction in ("long", "short", "avoid", "watch", "no view"):
                with self.subTest(action=action, direction=direction):
                    text = self.swap(self.revision, "action: Avoid", f"action: {action}")
                    text = self.swap(text, "direction: watch", f"direction: {direction}")
                    fails, _ = self.check(text)
                    mismatch = fails_matching(fails, r"is scored as direction")
                    if direction in allowed:
                        self.assertEqual(mismatch, [])
                    else:
                        self.assertEqual(len(mismatch), 1, fails)

    def test_action_is_one_of_the_seven_words(self):
        fails, _ = self.check(self.swap(self.revision, "action: Avoid", "action: Buy"))
        self.assertTrue(fails_matching(fails, r"action 'Buy' is not one of Initiate, Add"), fails)

    def test_lower_case_action_is_accepted(self):
        fails, _ = self.check(self.swap(self.revision, "action: Avoid", "action: avoid"))
        self.assertEqual(fails, [])

    def test_size_now_is_zero_unless_the_portfolio_holds_it(self):
        fails, _ = self.check(self.swap(self.revision, "size_now: 0.000", "size_now: 0.037"))
        self.assertTrue(fails_matching(fails, r"size_now is 0\.037 but the action is Avoid"), fails)
        text = self.swap(self.revision, "action: Avoid", "action: Hold")
        text = self.swap(text, "direction: watch", "direction: long")
        text = self.swap(text, "size_now: 0.000", "size_now: 0.037")
        text = self.swap(text, "**Avoid for now:", "**Hold for now:")
        fails, _ = self.check(text)
        self.assertEqual(fails_matching(fails, r"size_now"), [])

    def test_size_now_is_a_fraction(self):
        text = self.swap(self.revision, "action: Avoid", "action: Hold")
        text = self.swap(text, "direction: watch", "direction: long")
        fails, _ = self.check(self.swap(text, "size_now: 0.000", "size_now: 3.7"))
        self.assertTrue(fails_matching(fails, r"size_now 3\.7 is not a fraction"), fails)

    def test_memo_fields_are_required(self):
        for field in ("action", "expected_return", "bear_return", "required_return", "size_plan"):
            with self.subTest(field):
                text = re.sub(rf"^{field}:.*\n", "", self.revision, count=1, flags=re.M)
                fails, _ = self.check(text)
                self.assertTrue(fails_matching(fails, rf"missing memo field: {field}"), fails)

    def test_size_plan_may_be_blank(self):
        text = re.sub(r"^size_plan:.*$", "size_plan:", self.revision, count=1, flags=re.M)
        fails, _ = self.check(text)
        self.assertEqual(fails, [])

    def test_horizon_is_365_days(self):
        fails, _ = self.check(self.swap(self.revision, "horizon_days: 365", "horizon_days: 252"))
        self.assertTrue(fails_matching(fails, r"horizon_days is 252"), fails)

    def test_unknown_format_is_rejected(self):
        fails, _ = self.check(self.swap(self.revision, "format: memo", "format: memorandum"))
        self.assertTrue(fails_matching(fails, r"format 'memorandum' is not 'memo'"), fails)


class RevisionShape(MemoCase):

    def test_revision_length_band(self):
        anchor = "The earlier note was wrong about one figure"
        filler = ("I checked the same figure again in the quarterly report and it had not changed. " * 30)
        text = self.swap(self.revision, anchor, filler + "\n\n" + anchor)
        fails, warns = self.check(text)
        self.assertEqual(fails_matching(fails, r"words of prose"), [])
        self.assertTrue(fails_matching(warns, r"the revision is [\d,]+ words of prose.*Aim for 300 to 800"), warns)
        text = self.swap(self.revision, anchor, filler * 2 + "\n\n" + anchor)
        fails, _ = self.check(text)
        self.assertTrue(fails_matching(fails, r"the revision is [\d,]+ words of prose.*outside 200 to 1,000"), fails)

        # Page one's headline, the tables and the quote alone: far too short.
        short = re.sub(r"\n(?![|#>\n])(?!\*\*Avoid)[^\n]+", "", self.revision.split("---\n", 2)[2])
        parts = self.revision.split("---\n", 2)
        fails, _ = self.check(parts[0] + "---\n" + parts[1] + "---\n" + short)
        self.assertTrue(fails_matching(fails, r"the revision is [\d,]+ words of prose.*outside 200 to 1,000"), fails)

    def test_the_same_text_as_an_initiation_is_held_to_the_initiation_band(self):
        (self.ledger / "events.csv").write_text(OLD_HEADER + "\n", encoding="utf-8")
        fails, _ = self.check(self.swap(self.revision, "kind: revision", "kind: initiation"))
        self.assertTrue(fails_matching(fails, r"the initiation is [\d,]+ words of prose.*1,200 to 2,000"), fails)
        self.assertTrue(fails_matching(fails, r"missing the heading: ## 1\. THE DEBATE"), fails)
        self.assertTrue(fails_matching(fails, r"heading\(s\) not in the memo format: WHAT CHANGED"), fails)

    def test_revision_needs_what_changed_and_sections_3_and_4(self):
        for old in ("## WHAT CHANGED", "## 3. WHAT IT IS WORTH", "## 4. WHAT WOULD PROVE ME WRONG"):
            with self.subTest(old):
                fails, _ = self.check(self.swap(self.revision, old, "## SOMETHING ELSE"))
                self.assertTrue(fails_matching(fails, "missing the heading: " + re.escape(old)), fails)

    def test_revision_section_order(self):
        text = self.revision
        i2, i3, i4, isrc = (text.index(h) for h in ("## 2. MY VIEW", "## 3. WHAT IT IS WORTH",
                                                    "## 4. WHAT WOULD PROVE", "## SOURCES"))
        sec2, sec3, sec4 = text[i2:i3], text[i3:i4], text[i4:isrc]
        def order(middle):
            return text[:i2] + middle + text[isrc:]
        # Section 4 straight after WHAT CHANGED, then 2 and 3: allowed.
        fails, _ = self.check(order(sec4 + sec2 + sec3))
        self.assertEqual(fails_matching(fails, r"out of order"), [])
        # 3 before 2: not allowed.
        fails, _ = self.check(order(sec3 + sec2 + sec4))
        self.assertTrue(fails_matching(fails, r"numbered sections are out of order"), fails)

    def test_page_one_has_no_heading(self):
        fails, _ = self.check(self.swap(self.revision, "\n**Avoid for now:", "\n## Page one\n\n**Avoid for now:"))
        self.assertTrue(fails_matching(fails, r"heading\(s\) not in the memo format: PAGE ONE"), fails)

    def test_page_one_needs_the_bold_headline_naming_the_action(self):
        fails, _ = self.check(self.swap(self.revision, "**Avoid for now: $228.87 already pays for my base case.**",
                                        "Avoid for now."))
        self.assertTrue(fails_matching(fails, r"page one must open with a bold one-line headline"), fails)
        fails, _ = self.check(self.swap(self.revision, "**Avoid for now: $228.87 already pays for my base case.**",
                                        "**Not yet: $228.87 already pays for my base case.**"))
        self.assertTrue(fails_matching(fails, r"headline does not name the action, Avoid"), fails)

    def test_page_one_is_short(self):
        extra = " ".join(["Each report this year has shown the same pattern of rising sales and slower payments."] * 25)
        fails, _ = self.check(self.swap(self.revision, "**Why now.** ", "**Why now.** " + extra + " "))
        self.assertTrue(fails_matching(fails, r"page one is \d+ words\. Keep it under 250"), fails)


class MonitoringTable(MemoCase):

    def test_needs_an_exit_or_cut_row_with_threshold_and_date(self):
        fails, _ = self.check(self.swap(self.revision, "Stay out. If owned, Exit", "Stay out", count=3))
        self.assertTrue(fails_matching(fails, r"WHAT WOULD PROVE ME WRONG needs a table with Threshold and Action"), fails)

    def test_cut_counts_and_a_month_and_year_is_a_date(self):
        text = self.swap(self.revision, "Stay out. If owned, Exit", "Stay out", count=3)
        text = self.swap(text, "| Above 70 days at the FY2027 year end | Stay out |",
                         "| Above 70 days at the FY2027 year end | Cut to half |")
        fails, _ = self.check(text)
        self.assertEqual(fails, [])

    def test_a_row_without_a_number_does_not_count(self):
        text = self.swap(self.revision, "Stay out. If owned, Exit", "Stay out", count=3)
        text = self.swap(text, "| Above 70 days at the FY2027 year end | Stay out |",
                         "| Much higher | Exit |")
        fails, _ = self.check(text)
        self.assertTrue(fails_matching(fails, r"WHAT WOULD PROVE ME WRONG needs"), fails)


class ArgumentNotData(MemoCase):
    """The rules that keep a memo an argument: where tables may stand, how many
    figures a paragraph carries, how a paragraph opens, and the section limits."""

    PARA = "My view is wrong if the company grows faster than I assume while its customers pay on time."

    def test_tables_only_in_sections_3_and_4_sources_and_glossary(self):
        table = "\n\n| Measure | Value |\n|---|---|\n| P/E | 28.9 |\n\n"
        for where, anchor in (("page one", "**Thesis.**"), ("WHAT CHANGED", "The earlier note was wrong"),
                              ("2. MY VIEW", "My forecast and the price agree")):
            with self.subTest(where):
                fails, _ = self.check(self.swap(self.revision, anchor, table + anchor))
                self.assertTrue(fails_matching(fails, r"table\(s\) in " + re.escape(where)), fails)

    def test_history_and_peer_tables_fail_even_where_tables_are_allowed(self):
        years = "\n\n| Measure | FY2024 | FY2025 | FY2026 |\n|---|---|---|---|\n| Revenue | 60.9 | 130.5 | 215.9 |\n"
        fails, _ = self.check(self.swap(self.revision, "\n\nP/E, price divided", years + "\nP/E, price divided"))
        self.assertTrue(fails_matching(fails, r"table of years"), fails)
        peers = "\n\n| Company | P/E |\n|---|---|\n| Micron | 24.8 |\n"
        fails, _ = self.check(self.swap(self.revision, "\n\n" + self.PARA, peers + "\n" + self.PARA))
        self.assertTrue(fails_matching(fails, r"peer table"), fails)

    def test_figures_in_a_paragraph(self):
        five = " Sales were $96.2 billion, $108.0 billion, $118.8 billion, $125 billion and $105.8 billion."
        _, warns = self.check(self.swap(self.revision, self.PARA, self.PARA + five))
        self.assertTrue(fails_matching(warns, r"1 paragraph\(s\) with more than 4 figures, e\.g\. 5 figures"), warns)
        seven = five + " Margins were 75.0% and 72.4%."
        fails, _ = self.check(self.swap(self.revision, self.PARA, self.PARA + seven))
        self.assertTrue(fails_matching(fails, r"1 paragraph\(s\) with more than 6 figures, e\.g\. 7 figures"), fails)

    def test_what_the_figure_count_leaves_out(self):
        text = ("In section 3, the report of 2026-11-17 and the report of 17 November 2026 cover FY2028 "
                "and the 12 months to January 2028, when revenue grew 30% to $527.8 billion (10-K).")
        self.assertEqual(validate._figure_count(text), 2)

    def test_a_list_item_counts_as_a_paragraph(self):
        item = "- Revenue was $1, $2, $3, $4, $5, $6 and $7 billion in the seven quarters I hold.\n"
        fails, _ = self.check(self.swap(self.revision, "## SOURCES", item + "\n## SOURCES"))
        self.assertTrue(fails_matching(fails, r"more than 6 figures"), fails)

    def test_paragraph_openers(self):
        fails, _ = self.check(self.swap(self.revision, self.PARA, "30.2% is the growth the price requires. " + self.PARA))
        self.assertTrue(fails_matching(fails, r"1 paragraph\(s\) open with a number"), fails)
        _, warns = self.check(self.swap(self.revision, self.PARA, "As noted above, my view could be wrong."))
        self.assertTrue(fails_matching(warns, r"open by pointing back"), warns)
        _, warns = self.check(self.swap(self.revision, self.PARA + " ", ""))
        self.assertTrue(fails_matching(warns, r"open with a definition, e\.g\. \"Guidance is"), warns)

    def test_page_one_return_paragraph_may_open_with_figures(self):
        fails, warns = validate.check(REVISION)
        self.assertEqual(fails_matching(fails + warns, r"open with"), [])

    def test_my_view_needs_arguments(self):
        text = re.sub(r"^### .*\n\n", "", self.revision, flags=re.M)
        fails, _ = self.check(text)
        self.assertTrue(fails_matching(fails, r"2\. MY VIEW has no argument"), fails)
        text = self.swap(self.revision, "### The January-quarter forecast is the one number that could change that.\n\n", "")
        _, warns = self.check(text)
        self.assertTrue(fails_matching(warns, r"2\. MY VIEW has 1 argument"), warns)

    def test_a_sub_heading_may_not_copy_an_input_block(self):
        fails, _ = self.check(self.swap(self.revision, "### The next year is well supported, and the price already needs it.",
                                        "### Key data"))
        self.assertTrue(fails_matching(fails, r"the sub-heading 'Key data' copies a block"), fails)

    def test_at_most_three_risks(self):
        (self.ledger / "events.csv").write_text(OLD_HEADER + "\n", encoding="utf-8")
        fourth = ("4. **Competition.** Customers with their own chip designs could buy fewer of NVIDIA's. "
                  "The first sign would be a fall in Data Center revenue from one quarter to the next.\n")
        fails, _ = self.check(self.swap(self.initiation, "\n## 6. WHAT I DO NOT KNOW", fourth + "\n## 6. WHAT I DO NOT KNOW"))
        self.assertTrue(fails_matching(fails, r"5\. RISKS lists 4 risks"), fails)

    def test_initiation_length_bands(self):
        (self.ledger / "events.csv").write_text(OLD_HEADER + "\n", encoding="utf-8")
        filler = "\n\nI read the quarterly report again and found nothing that changes this view of the company.\n"
        anchor = "\n## 5. RISKS"
        _, warns = self.check(self.swap(self.initiation, anchor, filler * 50 + anchor))
        self.assertTrue(fails_matching(warns, r"the initiation is [\d,]+ words of prose.*Aim for 1,200 to 2,000"), warns)
        fails, _ = self.check(self.swap(self.initiation, anchor, filler * 80 + anchor))
        self.assertTrue(fails_matching(fails, r"the initiation is [\d,]+ words of prose.*outside 900 to 2,400"), fails)

    def test_glossary_lists_only_terms_the_memo_uses(self):
        row = "| Beta | How much a stock tends to move for each 1% move in the market. |\n"
        _, warns = self.check(self.swap(self.revision, "| Bull case |", row + "| Bull case |"))
        self.assertTrue(fails_matching(warns, r"GLOSSARY lists term\(s\) the memo does not use: Beta"), warns)

    def test_term_used_matches_plurals_abbreviations_and_split_terms(self):
        self.assertTrue(validate._term_used("Bull case", "the bull, base and bear cases"))
        self.assertTrue(validate._term_used("Days sales outstanding (DSO)", "DSO rose to 60"))
        self.assertTrue(validate._term_used("Segment", "It reports two segments."))
        self.assertFalse(validate._term_used("Beta", "the alphabet"))


class VocabularyAndSources(MemoCase):

    def test_a_term_used_but_not_in_the_glossary_fails(self):
        text = re.sub(r"^\| Variant perception \|.*\n", "", self.revision, flags=re.M)
        fails, _ = self.check(text)
        self.assertTrue(fails_matching(fails, r"body: finance term\(s\) not defined .*variant perception"), fails)

    def test_front_matter_terms_must_be_in_the_glossary(self):
        text = self.swap(self.revision, "At $228.87 I would own none,", "At 28.9 times EPS I would own none,")
        fails, _ = self.check(text)
        self.assertEqual(fails_matching(fails, r"key_claim"), [])
        text = re.sub(r"^\| Earnings per share \(EPS\) \|.*\n", "", text, flags=re.M)
        fails, _ = self.check(text)
        self.assertTrue(fails_matching(fails, r"key_claim: finance term\(s\) not defined .*EPS"), fails)

    def test_code_formatting_only_in_sources(self):
        # The fixture's SOURCES table formats file and field names as code and passes.
        sources = self.revision.split("## SOURCES", 1)[1].split("## GLOSSARY", 1)[0]
        self.assertIn("`market_cap`", sources)
        text = self.swap(self.revision, "The expected return sits 0.7 percentage points",
                         "The `expected_return` sits 0.7 percentage points")
        fails, _ = self.check(text)
        self.assertTrue(fails_matching(fails, r"body: 1 code-formatted name"), fails)

    def test_file_names_only_in_sources(self):
        text = self.swap(self.revision, "Written from prices and filings", "Written from reported.csv and filings")
        fails, _ = self.check(text)
        self.assertTrue(fails_matching(fails, r"file name"), fails)

    def test_glossary_definitions_are_the_canonical_ones(self):
        text = self.swap(self.revision, "| Guidance | Management's own published forecast. |",
                         "| Guidance | The company's outlook. |")
        fails, warns = self.check(text)
        self.assertEqual(fails, [])
        self.assertTrue([w for w in warns if "definition differs from theses/GLOSSARY.md for Guidance" in w], warns)

    def test_en_dash_fails_in_a_memo(self):
        fails, _ = self.check(self.swap(self.revision, "limits 3% to 12%", "limits 3%" + chr(0x2013) + "12%"))
        self.assertTrue(fails_matching(fails, r"1 en dash"), fails)

    def test_bold_headline_and_labels_allowed_but_bold_endings_are_not(self):
        fails, _ = validate.check(REVISION)
        self.assertEqual(fails_matching(fails, r"bolded line"), [])
        text = self.swap(self.revision, "My price target is that value rounded to the nearest $5.",
                         "My price target is that value rounded to the nearest $5. **That is the number I am graded on.**")
        fails, _ = self.check(text)
        self.assertTrue(fails_matching(fails, r"paragraph ends on a bolded line"), fails)


class ActFollowsTheNumbers(MemoCase):
    """The ACT rule and rule e are arithmetic, so the checker enforces them."""

    def long_initiation(self, size="0.037"):
        text = self.swap(self.initiation, "action: Avoid", "action: Initiate")
        text = self.swap(text, "direction: watch", "direction: long")
        text = self.swap(text, "**Avoid for now:", "**Initiate:")
        return self.swap(text, "size_now: 0.000", f"size_now: {size}")

    def test_initiate_below_the_required_return_fails(self):
        fails, _ = self.check(self.long_initiation())
        self.assertTrue(fails_matching(fails, r"not above required_return"), fails)

    def test_initiate_above_the_required_return_passes_that_check(self):
        text = self.swap(self.long_initiation(), "required_return: 0.120", "required_return: 0.100")
        fails, _ = self.check(text)
        self.assertEqual(fails_matching(fails, r"not above required_return"), [])

    def test_long_with_bear_likelier_than_bull_fails(self):
        text = self.swap(self.long_initiation(), "required_return: 0.120", "required_return: 0.100")
        text = self.swap(text, "{case: bull, value: 370.00, probability: 0.25}",
                         "{case: bull, value: 370.00, probability: 0.20}")
        text = self.swap(text, "{case: bear, value: 125.00, probability: 0.25}",
                         "{case: bear, value: 125.00, probability: 0.30}")
        fails, _ = self.check(text)
        self.assertTrue(fails_matching(fails, r"bear case .* likelier"), fails)

    def test_bear_cost_above_the_draft_limit_warns(self):
        text = self.swap(self.long_initiation("0.074"), "required_return: 0.120", "required_return: 0.100")
        _, warns = self.check(text)
        self.assertTrue(fails_matching(warns, r"draft 2% limit"), warns)

    def test_missing_page_one_labels_and_consensus_sentence_warn(self):
        text = self.swap(self.initiation, "**Thesis.** ", "")
        text = self.swap(text, "\n- No analyst forecasts are available, so I cannot say whether my figures sit "
                               "above or below what other analysts expect.", "")
        _, warns = self.check(text)
        self.assertTrue(fails_matching(warns, r"no bold 'Thesis\.' paragraph"), warns)
        self.assertTrue(fails_matching(warns, r"no analyst forecasts"), warns)


class OldFormatNotes(unittest.TestCase):

    def test_every_existing_note_still_validates(self):
        # Every note in the archive, of either format, still passes. The first
        # memo lands on 2026-09-28; before this split the test asserted that no
        # note was a memo, so the first real memo turned CI red.
        self.assertTrue(NOTES)
        for note in NOTES:
            with self.subTest(note=str(note.relative_to(H.REPO))):
                fm, _ = validate.parse(note.read_text(encoding="utf-8"))
                fmt = (fm.get("format") or "").strip()
                if fmt == "memo":
                    self.assertGreaterEqual(str(fm.get("written_on", "")), "2026-09-28",
                                            "a memo dated before the memo format was switched on")
                fails, _ = validate.check(note)
                self.assertEqual(fails, [])

    def test_format_note_is_the_same_as_no_format(self):
        with H.temp_dir() as tmp:
            for note in NOTES:
                with self.subTest(note=note.name):
                    text = note.read_text(encoding="utf-8")
                    p = tmp / note.name
                    p.write_text(text.replace("---\n", "---\nformat: note\n", 1), encoding="utf-8")
                    self.assertEqual(validate.check(p), validate.check(note))


class CanonicalGlossary(unittest.TestCase):

    def setUp(self):
        self.text = validate.GLOSSARY_FILE.read_text(encoding="utf-8")
        self.canon = validate.canonical_glossary()

    def test_it_parses_and_covers_the_reference_memo(self):
        self.assertGreaterEqual(len(self.canon), 80)
        fixture = validate._load_glossary(INITIATION.read_text(encoding="utf-8").split("## GLOSSARY", 1)[1])
        self.assertGreaterEqual(len(fixture), 15)
        for key, (term, dfn) in fixture.items():
            with self.subTest(term=term):
                self.assertIn(key, self.canon)
                self.assertEqual(dfn, self.canon[key][1])

    def test_alphabetical_one_sentence_no_dashes(self):
        terms = [t for t, _ in self.canon.values()]
        self.assertEqual(terms, sorted(terms, key=str.lower))
        for term, dfn in self.canon.values():
            with self.subTest(term=term):
                self.assertTrue(dfn.endswith("."))
                self.assertEqual(len(validate._sentences(dfn)), 1, dfn)
        self.assertNotIn(chr(0x2014), self.text)
        self.assertNotIn(chr(0x2013), self.text)

    def test_every_hard_term_has_a_canonical_definition_or_a_plain_substitute(self):
        # The hard terms the owner is most likely to meet in a memo.
        defined = validate._defined_labels([t for t, _ in self.canon.values()])
        for label in ("EPS", "EBITDA", "free cash flow yield", "TTM", "YoY", "P/E", "EV", "ROE",
                      "net debt / leverage", "the multiple", "the cycle", "priced in",
                      "variant perception", "basis points", "bear / base / bull case", "capex",
                      "long / short", "market cap"):
            with self.subTest(label=label):
                self.assertIn(label, defined)


class LedgerMigration(MemoCase):

    def test_migration_appends_the_columns_and_changes_nothing_else(self):
        path = self.ledger / "events.csv"
        before = path.read_bytes()
        self.assertEqual(events.migrate_events(path, write=False), "would migrate")
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(events.migrate_events(path), "migrated")
        after = path.read_bytes()
        expect = (OLD_HEADER + ",action,size_now,expected_return,bear_return\n"
                  + PRIOR_CF + ",,,,\r\n" + PRIOR_NVDA + ",,,,\r\n").encode("utf-8")
        self.assertEqual(after, expect)
        self.assertEqual(events.migrate_events(path), "current")
        self.assertEqual(path.read_bytes(), after)
        self.assertFalse((self.ledger / "events.csv.migrating").exists())

    def test_the_live_ledger_migrates_cleanly_on_a_copy(self):
        live = H.REPO / "theses" / "ledger" / "events.csv"
        original = live.read_bytes()
        copy = self.tmp / "live-copy.csv"
        shutil.copy(live, copy)
        self.assertIn(events.migrate_events(copy), ("migrated", "current"))
        self.assertEqual(live.read_bytes(), original)
        rows = list(csv.DictReader(io.StringIO(copy.read_text(encoding="utf-8"), newline="")))
        self.assertEqual(list(rows[0]), events.EVENT_COLUMNS)
        for r in rows:
            if not r.get("action"):
                self.assertEqual([r[c] for c in events.MEMO_EVENT_COLUMNS], ["", "", "", ""])

    def test_an_unknown_header_is_refused_and_left_alone(self):
        path = self.ledger / "events.csv"
        path.write_text("event_id,date,ticker\nX-1,2026-09-01,X\n", encoding="utf-8")
        before = path.read_bytes()
        with self.assertRaises(SystemExit):
            events.migrate_events(path)
        self.assertEqual(path.read_bytes(), before)

    def test_recording_a_memo_writes_the_new_columns(self):
        with H.quiet():
            ev = events.record(REVISION, "weekly run, review", "first memo")
        rows = events.read_csv_rows(self.ledger / "events.csv")
        self.assertEqual(len(rows), 3)
        last = rows[-1]
        self.assertEqual(last["event_id"], ev["event_id"])
        self.assertEqual(last["kind"], "revise")
        self.assertEqual(last["prior_target"], "240.00")
        self.assertEqual((last["action"], last["size_now"], last["expected_return"], last["bear_return"]),
                         ("Avoid", "0.000", "0.113", "-0.454"))
        self.assertEqual([rows[0][c] for c in events.MEMO_EVENT_COLUMNS], ["", "", "", ""])
        # Avoid scored as watch makes no gradeable prediction.
        self.assertFalse((self.ledger / "predictions.csv").exists())

    def test_recording_an_old_note_leaves_the_new_columns_blank(self):
        note = H.REPO / "theses" / "notes" / "CF" / "2026-09-19-revision.md"
        with H.quiet():
            events.record(note, "test", "test")
        last = events.read_csv_rows(self.ledger / "events.csv")[-1]
        self.assertEqual(last["ticker"], "CF")
        self.assertEqual([last[c] for c in events.MEMO_EVENT_COLUMNS], ["", "", "", ""])

    def test_dry_run_writes_nothing(self):
        before = (self.ledger / "events.csv").read_bytes()
        ev = events.record(REVISION, "t", "r", dry_run=True)
        self.assertEqual(ev["migration"], "would migrate")
        self.assertEqual(ev["action"], "Avoid")
        self.assertEqual((self.ledger / "events.csv").read_bytes(), before)
        self.assertFalse((self.ledger / "predictions.csv").exists())

    def test_a_failing_memo_is_not_recorded(self):
        bad = self.tmp / "bad.md"
        bad.write_text(self.revision.replace("target_price: 255.00", "target_price: 300.00"), encoding="utf-8")
        before = (self.ledger / "events.csv").read_bytes()
        with self.assertRaises(SystemExit):
            events.record(bad, "t", "r")
        self.assertEqual((self.ledger / "events.csv").read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
