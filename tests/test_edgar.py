"""EDGAR quarter reconstruction and the schema sentinels.

The oracle for reconstruction is the filing itself: the four quarters must add
up to the annual figure the company reported, and each must equal the quarter
that was actually in the synthetic filing. Year-to-date cash flows read as
quarterly once put Coca-Cola's trailing free cash flow at minus 3.7bn.
"""
import inspect
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests import helpers as H  # noqa: E402

LF = H.LF


def by_end(series):
    return {q["end"]: q for q in series}


class QuarterReconstruction(unittest.TestCase):

    def test_q4_is_annual_minus_three_quarters(self):
        # Coca-Cola shape: three-month columns for Q1..Q3, the year in the
        # 10-K, and no fourth quarter anywhere. The 10-Qs also carry the
        # six- and nine-month year-to-date columns real filings include.
        q = [20.0, 25.0, 30.0, 27.0]
        recs = H.income_records(2024, q)
        recs += [{"start": "2024-01-01", "end": "2024-06-30", "val": 45.0,
                  "form": "10-Q", "filed": "2024-08-01"},
                 {"start": "2024-01-01", "end": "2024-09-30", "val": 75.0,
                  "form": "10-Q", "filed": "2024-11-01"}]
        got = by_end(LF._quarterly_from_records(recs))
        ends = [e for _, e in H.calendar_quarters(2024)]
        self.assertEqual(sorted(got), sorted(ends))
        self.assertEqual([got[e]["val"] for e in ends], q)
        self.assertEqual(got["2024-12-31"]["start"], "2024-10-01")
        self.assertTrue(got["2024-12-31"]["derived"])
        for e in ends[:3]:
            self.assertFalse(got[e]["derived"], "a filed quarter lost to a derived one")
        self.assertEqual(sum(got[e]["val"] for e in ends), 102.0)

    def test_tagged_q4_beats_derived(self):
        # Walmart shape: the 10-K tags Q4 directly. If the year was restated
        # and the difference would disagree, the filed quarter must win.
        recs = H.income_records(2024, [10.0, 10.0, 10.0, 12.0], tag_q4=True)
        for r in recs:
            if r["end"] == "2024-12-31" and r["start"] == "2024-01-01":
                r["val"] = 45.0  # restated year: FY - 9M would say 15
        got = by_end(LF._quarterly_from_records(recs))
        self.assertEqual(got["2024-12-31"]["val"], 12.0)
        self.assertFalse(got["2024-12-31"]["derived"])

    def test_year_to_date_cash_flow_is_differenced(self):
        q = [10.0, 20.0, 15.0, 25.0]
        got = by_end(LF._quarterly_from_records(H.ytd_records(2024, q)))
        ends = [e for _, e in H.calendar_quarters(2024)]
        self.assertEqual([got[e]["val"] for e in ends], q)
        # Differenced from 6M, 9M and FY, never the running totals themselves.
        self.assertNotIn(30.0, [got[e]["val"] for e in ends])
        self.assertEqual(sum(got[e]["val"] for e in ends), 70.0)

    def test_retail_sixteen_week_q4(self):
        # A 4-4-5 calendar gives a 112-day fourth quarter. It must be read,
        # not dropped as too long (Costco's Q4 was, every year, at 100 days).
        recs = [
            {"start": "2023-09-04", "end": "2023-11-26", "val": 50.0, "form": "10-Q", "filed": "2023-12-10"},
            {"start": "2023-11-27", "end": "2024-02-18", "val": 55.0, "form": "10-Q", "filed": "2024-03-10"},
            {"start": "2024-02-19", "end": "2024-05-12", "val": 52.0, "form": "10-Q", "filed": "2024-06-01"},
            {"start": "2023-09-04", "end": "2024-09-01", "val": 230.0, "form": "10-K", "filed": "2024-10-10"},
        ]
        got = by_end(LF._quarterly_from_records(recs))
        self.assertIn("2024-09-01", got)
        self.assertEqual(got["2024-09-01"]["val"], 73.0)
        self.assertEqual(got["2024-09-01"]["start"], "2024-05-13")

    def test_ttm_fcf_equals_filed_annual_cash_flow(self):
        # The Coca-Cola invariant, end to end: when the latest four quarters
        # are exactly a fiscal year, trailing FCF is that year's filed
        # operating cash flow minus its filed capex. Reading year-to-date
        # columns as quarters cannot produce this number.
        facts, src = H.rich_facts()
        out = LF.compute_edgar_factors(facts)
        fy = 2025
        self.assertAlmostEqual(out["ttm_fcf"], sum(src["cfo"][fy]) - sum(src["capex"][fy]), places=6)
        self.assertAlmostEqual(out["ttm_revenue"], sum(src["rev"][fy]), places=6)
        self.assertAlmostEqual(out["prior_ttm_revenue"], sum(src["rev"][fy - 1]), places=6)
        self.assertEqual(out["fiscal_period_end"], "2025-12-31")


# Fields compute_edgar_factors emitted when EDGAR_SCHEMA_SENTINELS was written.
# They shipped in the same change as the sentinels that cover their vintage, so
# they reached every ticker together. Anything emitted that is in neither this
# list nor EDGAR_SCHEMA_SENTINELS is new, and a new field that is not a
# sentinel reaches only the tickers not already stamped this ISO week.
#
# Do not extend this list to make the test pass. Add the new field to
# EDGAR_SCHEMA_SENTINELS in lambda_function.py instead.
REVIEWED_NON_SENTINEL_FIELDS = frozenset({
    "ttm_gross_profit", "ttm_operating_income", "ttm_net_income", "ttm_dep_amort",
    "prior_ttm_revenue", "prior_ttm_eps_diluted", "prior_equity",
    "cash_and_investments", "total_debt", "ttm_fcf", "ttm_ebitda",
    "revenue_acceleration", "gross_margin_trend", "fcf_growth_yoy",
    "earnings_consistency", "op_margin_stability",
})


def statically_emitted_fields():
    """Every key compute_edgar_factors can write, read from its source: the
    out[...] assignments and the (key, value) table of trailing aggregates."""
    src = inspect.getsource(LF.compute_edgar_factors)
    keys = set(re.findall(r'out\["(\w+)"\]\s*=', src))
    table = src.split("for key, val in (", 1)[1].split("):", 1)[0]
    keys |= set(re.findall(r'^\s*\("(\w+)",', table, re.M))
    return keys


class SchemaSentinels(unittest.TestCase):

    def test_rich_fixture_reaches_every_emit_site(self):
        # Without this, the next test could pass by never exercising a branch.
        facts, _ = H.rich_facts()
        emitted = set(LF.compute_edgar_factors(facts))
        self.assertEqual(statically_emitted_fields() - emitted, set(),
                         "fixture does not exercise these outputs")
        self.assertEqual(emitted - statically_emitted_fields(), set())

    def test_every_emitted_field_is_covered_by_a_sentinel(self):
        facts, _ = H.rich_facts()
        emitted = set(LF.compute_edgar_factors(facts))
        uncovered = emitted - set(LF.EDGAR_SCHEMA_SENTINELS) - REVIEWED_NON_SENTINEL_FIELDS
        self.assertEqual(uncovered, set(),
                         "new compute_edgar_factors output(s) missing from "
                         "EDGAR_SCHEMA_SENTINELS; stamped tickers would never get them")

    def test_sentinels_are_all_emitted(self):
        # A sentinel that is never produced would force a full refetch forever.
        facts, _ = H.rich_facts()
        emitted = set(LF.compute_edgar_factors(facts))
        self.assertEqual(set(LF.EDGAR_SCHEMA_SENTINELS) - emitted, set())

    def test_reviewed_list_has_no_stale_names(self):
        self.assertEqual(REVIEWED_NON_SENTINEL_FIELDS - statically_emitted_fields(), set())


if __name__ == "__main__":
    unittest.main()
