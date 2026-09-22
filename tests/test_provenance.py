"""FIELD_METHODS is complete, and the README's provenance section agrees with it.

The README says its table is generated from FIELD_METHODS, but no generator is
in the repository, so nothing kept the two in step. This file holds the
generator's rules (one row per field, alphabetical, refresh class written in
lower case) and checks the committed README against them, character for
character. If a formula changes in code and not in the README, this fails.
"""
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests import helpers as H  # noqa: E402

LF = H.LF
REQUIRED = ("label", "units", "formula", "source", "refresh", "asof", "note")


def lower_first(text):
    return text[:1].lower() + text[1:]


def expected_provenance_rows():
    rows = []
    for field in sorted(LF.FIELD_METHODS):
        m = LF.FIELD_METHODS[field]
        rows.append(f"| `{field}` | {m['units']} | `{m['formula']}` | {m['source']} | "
                    f"{lower_first(LF.REFRESH_CLASSES[m['refresh']])} |")
    return rows


def readme_section(title):
    text = (H.REPO / "README.md").read_text(encoding="utf-8")
    start = text.index(title)
    nxt = text.find("\n#", start + len(title))
    return text[start:nxt if nxt >= 0 else None]


def table_rows(section, first_cell_pattern):
    return [ln.rstrip() for ln in section.splitlines()
            if re.match(r"^\| " + first_cell_pattern, ln)]


class FieldMethods(unittest.TestCase):

    def test_every_entry_is_complete(self):
        for field, m in LF.FIELD_METHODS.items():
            with self.subTest(field=field):
                for key in REQUIRED:
                    self.assertIsInstance(m.get(key), str, f"{key} missing")
                    self.assertTrue(m[key].strip(), f"{key} empty")
                self.assertIn(m["source"], LF.FIELD_SOURCES)
                self.assertIn(m["refresh"], LF.REFRESH_CLASSES)

    def test_screener_receives_the_same_dict(self):
        # The methodology panel is FIELD_METHODS serialized into the page.
        self.assertIn("__FIELD_METHODS_JSON__", LF.STOCKS_JS_TEMPLATE)


class ReadmeProvenance(unittest.TestCase):

    def test_table_matches_field_methods(self):
        section = readme_section("## Data provenance")
        got = table_rows(section, "`")
        self.assertEqual(got, expected_provenance_rows())

    def test_notes_match_field_methods(self):
        section = readme_section("## Data provenance")
        notes = re.findall(r"^- \*\*`(\w+)`\*\* \S (.+)$", section, re.M)
        self.assertTrue(notes, "no field notes found under Data provenance")
        for field, note in notes:
            with self.subTest(field=field):
                self.assertIn(field, LF.FIELD_METHODS)
                self.assertEqual(note.strip(), LF.FIELD_METHODS[field]["note"])

    def test_status_table_matches_field_status(self):
        section = readme_section("### Why a number is missing or old")
        got = table_rows(section, "`")
        want = [f"| `{k}` | {v} |" for k, v in LF.FIELD_STATUS.items()]
        self.assertEqual(got, want)


if __name__ == "__main__":
    unittest.main()
