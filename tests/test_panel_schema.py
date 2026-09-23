"""The append-only fundamentals panel.

A column added by accident is a column forever, and a header that disagrees
with its rows misaligns every value after the insertion point while still
parsing. These tests write into a temporary directory by patching
FUNDAMENTALS_CSV_DIR, then read the result back with the csv module, which
knows nothing about how it was written. The last test reads the real panel.
"""
import csv
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests import helpers as H  # noqa: E402

LF = H.LF


def stock(ticker, **extra):
    s = {"ticker": ticker, "name": ticker + " Inc", "sector": "Industrials",
         "price": 10.5, "market_cap": 1_000_000.0, "pe": 12.25,
         # Everything below must never reach the panel.
         "benford": {"mad": 0.0123, "fit": "close"},
         "op_margin_history": [{"end": "2026-06-30", "margin": 0.2}],
         "g": 1.0, "v": 2.0, "m": 3.0, "q": 4.0, "pct": {"pe": 0.5},
         "scorable": True, "dims_present": 4, "neglect_parts": {"a": 1},
         "status": {"pe": "not_meaningful"}}
    s.update(extra)
    return s


def read_panel(path):
    with path.open(encoding="utf-8", newline="") as fh:
        rows = list(csv.reader(fh))
    return rows[0], rows[1:]


class RecordFundamentals(unittest.TestCase):

    def run_record(self, d, stocks, day):
        with H.patched(LF, FUNDAMENTALS_CSV_DIR=d), H.quiet():
            return LF.record_fundamentals(stocks, day)

    def test_skipped_fields_never_written(self):
        with H.temp_dir() as d:
            n = self.run_record(d, [stock("AAA"), stock("BBB")], "2026-09-01")
            self.assertEqual(n, 2)
            header, rows = read_panel(d / "2026-09.csv")
            # Spelled out rather than read from FUNDAMENTAL_SKIP_FIELDS, so
            # dropping a name from that set fails here instead of agreeing.
            # The pinned lead columns always appear; the only other column
            # this fixture may produce is the flattened Benford MAD.
            self.assertEqual(header, LF.FUNDAMENTAL_LEAD + ["benford_mad"])
            for r in rows:
                self.assertEqual(len(r), len(header))
            self.assertEqual(rows[0][header.index("benford_mad")], "0.0123")

    def test_new_field_widens_header_without_losing_rows(self):
        with H.temp_dir() as d:
            self.run_record(d, [stock("AAA"), stock("BBB")], "2026-09-01")
            _, before = read_panel(d / "2026-09.csv")
            self.run_record(d, [stock("AAA", new_metric=0.5), stock("BBB")], "2026-09-02")
            header, rows = read_panel(d / "2026-09.csv")
            self.assertIn("new_metric", header)
            self.assertEqual(len(rows), 4)
            for r in rows:
                self.assertEqual(len(r), len(header), "row width disagrees with header")
            # The earlier rows are unchanged in every column they had, and
            # blank in the new one.
            old_width = len(before[0])
            for old, new in zip(before, rows[:2]):
                self.assertEqual(new[:old_width], old)
                self.assertEqual(new[header.index("new_metric")], "")
            aaa2 = dict(zip(header, rows[2]))
            self.assertEqual((aaa2["ticker"], aaa2["new_metric"], aaa2["price"]),
                             ("AAA", "0.5", "10.5"))

    def test_same_date_twice_is_a_no_op(self):
        with H.temp_dir() as d:
            self.run_record(d, [stock("AAA")], "2026-09-01")
            self.assertEqual(self.run_record(d, [stock("AAA")], "2026-09-01"), 0)
            _, rows = read_panel(d / "2026-09.csv")
            self.assertEqual(len(rows), 1)

    def test_row_without_price_or_cap_is_not_recorded(self):
        with H.temp_dir() as d:
            empty = stock("ZZZ")
            empty.pop("price")
            empty.pop("market_cap")
            self.assertEqual(self.run_record(d, [stock("AAA"), empty], "2026-09-01"), 1)


# Scoring state that reached the panel on 2026-09-08, before these names were
# added to FUNDAMENTAL_SKIP_FIELDS. The panel is append-only, so the columns
# stay; what must not happen is a later row filling them again.
LEGACY_SKIP_COLUMNS = {"g", "v", "m", "q", "pct", "scorable", "dims_present"}
LEGACY_LAST_DATE = "2026-09-08"


class PanelOnDisk(unittest.TestCase):
    """The committed panel, read as a stranger would read it."""

    def test_committed_panel_is_well_formed(self):
        # The two newest months: the only files a run appends to, or a union
        # merge touches. Older months are closed, and reading every one would
        # grow this test by about a second a month.
        files = sorted(LF.FUNDAMENTALS_CSV_DIR.glob("*.csv"))[-2:]
        if not files:
            self.skipTest("no panel files on disk")
        for path in files:
            with self.subTest(file=path.name):
                with path.open(encoding="utf-8", newline="") as fh:
                    reader = csv.reader(fh)
                    header = next(reader)
                    self.assertEqual(len(header), len(set(header)), "duplicate column")
                    self.assertEqual(set(header) & LF.FUNDAMENTAL_SKIP_FIELDS
                                     - LEGACY_SKIP_COLUMNS, set())
                    legacy = [header.index(c) for c in LEGACY_SKIP_COLUMNS if c in header]
                    width = len(header)
                    di, ti = header.index("date"), header.index("ticker")
                    month = path.stem
                    seen = set()
                    for lineno, row in enumerate(reader, start=2):
                        self.assertEqual(len(row), width, f"line {lineno} width")
                        self.assertTrue(row[di].startswith(month), f"line {lineno} date")
                        key = (row[di], row[ti])
                        self.assertNotIn(key, seen, f"line {lineno} duplicate {key}")
                        seen.add(key)
                        if row[di] > LEGACY_LAST_DATE:
                            filled = [header[i] for i in legacy if row[i] != ""]
                            self.assertEqual(filled, [], f"line {lineno} skipped field written")


if __name__ == "__main__":
    unittest.main()
