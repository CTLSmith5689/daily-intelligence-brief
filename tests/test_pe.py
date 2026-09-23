"""P/E: the per-share trailing sum, the annual fallback, and the pe status.

The oracle is arithmetic the filing itself fixes. A trailing EPS on one share
basis is a sum of four quarters that tile a year; a quarter nobody filed must
agree with that quarter's earnings; and a P/E on a company whose filings show a
loss is not a number at all. The worst case on record is Alight: a Q4 derived
across its 1-for-20 reverse split came out +36.81 for a quarter that lost
$932m, and its trailing EPS of 34.37 printed a P/E of 0.34. That case is
rebuilt here from data/financials/reported.csv, which holds what Alight filed.
"""
import csv
import os
import sys
import unittest
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests import helpers as H  # noqa: E402

LF = H.LF


def _month_start(end, months_back):
    y, m = int(end[:4]), int(end[5:7]) - months_back
    while m <= 0:
        m += 12
        y -= 1
    return date(y, m, 1).isoformat()


def rec(start, end, val, filed, form="10-Q"):
    return {"start": start, "end": end, "val": val, "filed": filed, "form": form}


def facts_of(**concepts):
    """{"us-gaap": {concept: {"units": {unit: records}}}} from concept=(unit, records)."""
    return {"us-gaap": {c: {"units": {u: r}} for c, (u, r) in concepts.items()}}


def reported_facts(ticker, through):
    """Company facts rebuilt from reported.csv for one ticker, periods ending
    on or before `through`. The file keeps one value per period (the latest
    filing), which is exactly what _quarterly_from_records keeps too; filing
    dates are not recorded, so each is put a normal lag after its period."""
    path = H.REPO / "data" / "financials" / "reported.csv"
    if not path.exists():
        return None
    with path.open(encoding="utf-8", newline="") as fh:
        rows = [r for r in csv.DictReader(fh)
                if r["ticker"] == ticker and r["period_end"] <= through]
    if not rows:
        return None
    cols = {"eps_diluted": ("EarningsPerShareDiluted", "USD/shares"),
            "net_income": ("NetIncomeLoss", "USD"),
            "shares_diluted": ("WeightedAverageNumberOfDilutedSharesOutstanding", "shares"),
            "revenue": ("Revenues", "USD")}
    out = {c: (u, []) for c, u in cols.values()}
    # Starts are not recorded either. A period starts the day after the one
    # before it where that one is adjacent, which keeps a 52-53 week
    # retailer's quarters tiling its year, and otherwise on the calendar.
    ends = {"Q": sorted({r["period_end"] for r in rows}),
            "FY": sorted(r["period_end"] for r in rows if r["period"] == "FY")}
    for r in rows:
        end, period = r["period_end"], r["period"]
        earlier = [e for e in ends[period] if e < end]
        gap = LF._period_days(earlier[-1], end) if earlier else None
        lo, hi = (80, 120) if period == "Q" else (350, 380)
        if gap and lo <= gap <= hi:
            start = LF._shift_iso(earlier[-1], 1)
        else:
            start = _month_start(end, 2 if period == "Q" else 11)
        if period == "Q":
            filed, form = LF._shift_iso(end, 40), "10-Q"
        else:
            filed, form = LF._shift_iso(end, 60), "10-K"
        for col, (concept, _unit) in cols.items():
            if r.get(col):
                out[concept][1].append(rec(start, end, float(r[col]), filed, form))
    return facts_of(**out)


def by_end(series):
    return {q["end"]: q for q in series}


class AlightFromReportedCsv(unittest.TestCase):
    """The audit's worst row, from what the company actually filed."""

    def setUp(self):
        self.facts = reported_facts("ALIT", "2026-06-30")
        if self.facts is None:
            self.skipTest("ALIT not in reported.csv")

    def test_old_arithmetic_reproduces_the_bug(self):
        # Without the per-share checks the audit's numbers come straight back:
        # Q4 2025 = -5.87 - (-0.05 - 40.61 - 2.02) = +36.81, TTM 34.37.
        recs = self.facts["us-gaap"]["EarningsPerShareDiluted"]["units"]["USD/shares"]
        plain = LF._quarterly_from_records(recs)
        self.assertAlmostEqual(by_end(plain)["2025-12-31"]["val"], 36.81, places=6)
        self.assertAlmostEqual(LF._ttm(plain), 34.37, places=6)

    def test_trailing_eps_is_withheld(self):
        with H.quiet():
            out = LF.compute_edgar_factors(self.facts, as_of=date(2026, 9, 21))
        eps = out.get("ttm_eps_diluted")
        self.assertTrue(eps is None or eps < 0, f"ALIT trailing EPS {eps}")
        # The quarters straddle the split (527m shares against 26m), so there
        # is nothing honest to sum, and no annual figure is substituted either.
        self.assertIsNone(eps)
        self.assertNotIn("eps_basis", out)
        self.assertLess(out["ttm_net_income"], 0)

    def test_derived_q4_is_rebuilt_from_its_earnings(self):
        recs = self.facts["us-gaap"]
        eps = LF._quarterly_from_records(
            recs["EarningsPerShareDiluted"]["units"]["USD/shares"], per_share=True)
        ni = LF._quarterly_from_records(recs["NetIncomeLoss"]["units"]["USD"])
        q_sh, fy_sh = LF._share_counts(LF._durations_from_records(
            recs["WeightedAverageNumberOfDilutedSharesOutstanding"]["units"]["shares"]))
        fixed = by_end(LF._reconcile_eps_quarters(eps, {q["end"]: q["val"] for q in ni},
                                                  q_sh, fy_sh))
        q4 = fixed["2025-12-31"]["val"]
        # -932m over the year's 527.6m diluted shares.
        self.assertAlmostEqual(q4, -932e6 / 527567685, places=6)

    def test_no_pe_reaches_the_row(self):
        with H.quiet():
            out = LF.compute_edgar_factors(self.facts, as_of=date(2026, 9, 21))
            s = dict(out, ticker="ALIT", name="Alight, Inc. Class A Common Stock",
                     price=11.77, pe=0.34245)
            LF.derive_ratios_from_fundamentals([s])
        self.assertNotIn("pe", s)
        self.assertEqual(s["status"]["pe"], "not_meaningful")


class ReportedCsvOtherNames(unittest.TestCase):

    def test_named_suspects_are_not_in_reported_csv(self):
        # BKNG, HSY and IFF were named by the audit. When reported.csv starts
        # carrying them this fails, as a prompt to add them to the class above.
        for t in ("BKNG", "HSY", "IFF"):
            with self.subTest(ticker=t):
                self.assertIsNone(reported_facts(t, "2026-06-30"))


def split_year_records():
    """EPS of 1.00 a quarter through 2025 and a 1-for-20 reverse split in mid
    2026. The Q2 2026 10-Q refiles Q2 2025 as a comparative at 20.00; the
    others are never refiled. The 9M year-to-date column is in the Q3 10-Q."""
    return [
        rec("2025-01-01", "2025-03-31", 1.0, "2025-05-01"),
        rec("2025-04-01", "2025-06-30", 1.0, "2025-08-01"),
        rec("2025-04-01", "2025-06-30", 20.0, "2026-08-01"),
        rec("2025-07-01", "2025-09-30", 1.0, "2025-11-01"),
        rec("2025-01-01", "2025-09-30", 3.0, "2025-11-01"),
        rec("2025-01-01", "2025-12-31", 4.0, "2026-02-15", "10-K"),
    ]


class PerShareQuarters(unittest.TestCase):

    def test_split_restatement_is_detected(self):
        self.assertEqual(LF._per_share_breaks(split_year_records()), ["2026-08-01"])

    def test_ordinary_restatement_is_not_a_split(self):
        recs = [rec("2025-01-01", "2025-03-31", 1.00, "2025-05-01"),
                rec("2025-01-01", "2025-03-31", 1.12, "2026-05-01")]
        self.assertEqual(LF._per_share_breaks(recs), [])

    def test_subtraction_across_a_split_is_refused(self):
        recs = [r for r in split_year_records() if r["end"] != "2025-09-30" or r["start"] != "2025-01-01"]
        plain = by_end(LF._quarterly_from_records(recs))
        self.assertEqual(plain["2025-12-31"]["val"], 4.0 - 22.0)  # the bug, reproduced
        split_safe = by_end(LF._quarterly_from_records(recs, per_share=True))
        self.assertNotIn("2025-12-31", split_safe)

    def test_year_to_date_chain_on_one_basis_is_kept(self):
        # FY and 9M were both filed before the split, so FY - 9M is on one
        # basis and is the right fourth quarter even though FY - (Q1+Q2+Q3)
        # is not.
        got = by_end(LF._quarterly_from_records(split_year_records(), per_share=True))
        self.assertEqual(got["2025-12-31"]["val"], 1.0)
        self.assertTrue(got["2025-12-31"]["derived"])

    def test_consistent_year_still_derives_q4(self):
        # Coca-Cola shape, per share: nothing restated, so nothing changes.
        recs = H.income_records(2025, [0.70, 0.88, 0.82, 0.55])
        got = by_end(LF._quarterly_from_records(recs, per_share=True))
        self.assertAlmostEqual(got["2025-12-31"]["val"], 0.55, places=9)


def quarters(vals_by_end, derived=()):
    out = []
    for end, v in sorted(vals_by_end.items(), reverse=True):
        out.append({"start": _month_start(end, 2), "end": end, "val": v,
                    "filed": LF._shift_iso(end, 40), "derived": end in derived})
    return out


class Reconcile(unittest.TestCase):

    ENDS = ["2025-03-31", "2025-06-30", "2025-09-30", "2025-12-31"]

    def test_incoherent_derived_quarter_is_rebuilt(self):
        eps = quarters(dict(zip(self.ENDS, [-0.05, -2.0, -2.0, 4.1])), derived={"2025-12-31"})
        ni = dict(zip(self.ENDS, [-25.0, -1000.0, -1000.0, -900.0]))
        got = by_end(LF._reconcile_eps_quarters(eps, ni, {}, {"2025-12-31": 500.0}))
        self.assertAlmostEqual(got["2025-12-31"]["val"], -1.8)
        self.assertTrue(got["2025-12-31"]["rebuilt"])

    def test_magnitude_off_derived_quarter_is_rebuilt(self):
        # Same sign, but it implies 20x the shares its siblings do.
        eps = quarters(dict(zip(self.ENDS, [1.0, 1.0, 1.0, 0.05])), derived={"2025-12-31"})
        ni = dict(zip(self.ENDS, [100.0, 100.0, 100.0, 100.0]))
        got = by_end(LF._reconcile_eps_quarters(eps, ni, {}, {"2025-12-31": 100.0}))
        self.assertAlmostEqual(got["2025-12-31"]["val"], 1.0)

    def test_coherent_derived_quarter_is_kept(self):
        eps = quarters(dict(zip(self.ENDS, [1.0, 1.1, 0.9, 1.2])), derived={"2025-12-31"})
        ni = dict(zip(self.ENDS, [100.0, 110.0, 90.0, 118.0]))
        got = by_end(LF._reconcile_eps_quarters(eps, ni, {}, {"2025-12-31": 104.0}))
        self.assertEqual(got["2025-12-31"]["val"], 1.2)
        self.assertNotIn("rebuilt", got["2025-12-31"])

    def test_tagged_quarter_shares_win(self):
        eps = quarters(dict(zip(self.ENDS, [1.0, 1.1, 0.9, 1.2])), derived={"2025-12-31"})
        ni = dict(zip(self.ENDS, [100.0, 110.0, 90.0, 125.0]))
        got = by_end(LF._reconcile_eps_quarters(eps, ni, {"2025-12-31": 100.0}, {}))
        self.assertAlmostEqual(got["2025-12-31"]["val"], 1.25)

    def test_share_tag_in_thousands_is_not_used(self):
        # Hub Group: 61,104 "shares" for 61.1m. The subtraction (0.40) is
        # right; rebuilding from that tag gave 398.
        eps = quarters(dict(zip(self.ENDS, [0.44, 0.47, 0.39, 0.40])), derived={"2025-12-31"})
        ni = dict(zip(self.ENDS, [27.0e6, 29.0e6, 23.6e6, 24.3e6]))
        q_sh = dict(zip(self.ENDS[:3], [61666.0, 61108.0, 60949.0]))
        got = by_end(LF._reconcile_eps_quarters(eps, ni, q_sh, {"2025-12-31": 61104.0}))
        self.assertEqual(got["2025-12-31"]["val"], 0.40)

    def test_fourth_quarter_share_count_tracks_issuance(self):
        # Tulip: 1.77m shares for three quarters, 2.52m for the year, so about
        # 4.8m in the fourth. The year's average would overstate the loss.
        self.assertAlmostEqual(
            LF._quarter_share_count("2025-12-31", dict(zip(self.ENDS[:3], [1.77, 1.77, 1.773])),
                                    {"2025-12-31": 2.521}), 4.771, places=6)
        # A year on another basis than its quarters is not estimated from.
        self.assertEqual(
            LF._quarter_share_count("2025-12-31", dict(zip(self.ENDS[:3], [500.0, 500.0, 500.0])),
                                    {"2025-12-31": 25.0}), 25.0)

    def test_refused_quarter_is_filled_from_its_halves(self):
        eps = quarters(dict(zip(self.ENDS[:3], [1.0, 1.0, 1.0])))
        ni = dict(zip(self.ENDS, [100.0, 100.0, 100.0, 120.0]))
        got = LF._reconcile_eps_quarters(eps, ni, {}, {"2025-12-31": 100.0})
        self.assertEqual(got[0]["end"], "2025-12-31")
        self.assertEqual(got[0]["start"], "2025-10-01")
        self.assertAlmostEqual(got[0]["val"], 1.2)


class PerShareTtm(unittest.TestCase):

    ENDS = ["2025-09-30", "2025-12-31", "2026-03-31", "2026-06-30"]

    def test_one_basis_sums(self):
        q = quarters(dict(zip(self.ENDS, [1.0, 1.0, 1.0, 1.0])))
        ni = dict(zip(self.ENDS, [100.0] * 4))
        self.assertEqual(LF._per_share_ttm(q, ni, dict(zip(self.ENDS, [100.0] * 4))), 4.0)

    def test_tagged_share_counts_across_a_split_withhold(self):
        q = quarters(dict(zip(self.ENDS, [1.0, 1.0, 1.0, 20.0])))
        ni = dict(zip(self.ENDS, [100.0] * 4))
        shares = dict(zip(self.ENDS, [100.0, 100.0, 100.0, 5.0]))
        self.assertIsNone(LF._per_share_ttm(q, ni, shares))

    def test_implied_share_counts_used_without_tags(self):
        q = quarters(dict(zip(self.ENDS, [1.0, 1.0, 1.0, 20.0])))
        ni = dict(zip(self.ENDS, [100.0] * 4))
        self.assertIsNone(LF._per_share_ttm(q, ni, {}))

    def test_real_dilution_below_the_bar_is_kept(self):
        # A 40% share issuance is not a split and the sum stands.
        q = quarters(dict(zip(self.ENDS, [1.0, 1.0, 1.0, 0.75])))
        ni = dict(zip(self.ENDS, [100.0] * 4))
        shares = dict(zip(self.ENDS, [100.0, 100.0, 100.0, 140.0]))
        self.assertEqual(LF._per_share_ttm(q, ni, shares), 3.75)

    def test_stepwise_issuance_is_not_a_split(self):
        # 12m to 27m over a year of offerings: no neighbouring pair jumps by
        # a split factor, so the sum stands.
        q = quarters(dict(zip(self.ENDS, [-0.07, -0.09, -0.17, -0.11])))
        shares = dict(zip(self.ENDS, [12.2, 16.6, 21.4, 27.1]))
        self.assertAlmostEqual(LF._per_share_ttm(q, {}, shares), -0.44)

    def test_a_gap_is_not_a_year(self):
        q = quarters({"2025-06-30": 1.0, "2025-12-31": 1.0, "2026-03-31": 1.0,
                      "2026-06-30": 1.0})
        self.assertIsNone(LF._per_share_ttm(q, {}, {}))

    def test_prior_window_checks_all_eight_quarters(self):
        ends = ["2024-09-30", "2024-12-31", "2025-03-31", "2025-06-30"] + self.ENDS
        q = quarters(dict(zip(ends, [20.0] * 4 + [1.0] * 4)))
        ni = dict(zip(ends, [100.0] * 8))
        self.assertEqual(LF._per_share_ttm(q, ni, {}), 4.0)
        self.assertIsNone(LF._per_share_ttm(q, ni, {}, 4))


def annual(concept, unit, vals_by_year, form="20-F"):
    return {concept: (unit, [rec(f"{y}-01-01", f"{y}-12-31", v, f"{y + 1}-04-20", form)
                             for y, v in vals_by_year.items()])}


class AnnualFallback(unittest.TestCase):

    def facts(self, eps=2.5, ni=250.0):
        return facts_of(**annual("EarningsPerShareDiluted", "USD/shares", {2024: 2.0, 2025: eps}),
                        **annual("NetIncomeLoss", "USD", {2024: 200.0, 2025: ni}))

    def test_annual_only_filer_gets_its_year(self):
        out = LF.compute_edgar_factors(self.facts(), as_of=date(2026, 9, 21))
        self.assertEqual(out["ttm_eps_diluted"], 2.5)
        self.assertEqual(out["eps_basis"], LF.EPS_BASIS_ANNUAL)
        self.assertEqual(out["ttm_net_income"], 250.0)
        self.assertEqual(out["fiscal_period_end"], "2025-12-31")

    def test_stale_year_is_not_used(self):
        out = LF.compute_edgar_factors(self.facts(), as_of=date(2027, 4, 15))
        self.assertNotIn("ttm_eps_diluted", out)
        self.assertNotIn("ttm_net_income", out)

    def test_foreign_currency_eps_is_not_used(self):
        facts = facts_of(**annual("EarningsPerShareDiluted", "EUR/shares", {2025: 2.5}))
        self.assertEqual(LF._latest_annual_value(facts, ["EarningsPerShareDiluted"],
                                                 unit_keys=("USD", "USD/shares")),
                         (None, None, None))
        self.assertNotIn("ttm_eps_diluted", LF.compute_edgar_factors(facts, as_of=date(2026, 9, 21)))

    def test_default_lookup_is_unchanged(self):
        # Revenue's annual guard calls this with no new arguments: USD only,
        # no age limit.
        facts = facts_of(**annual("Revenues", "USD", {2015: 9.0}))
        self.assertEqual(LF._latest_annual_value(facts, ["Revenues"]), (9.0, "2015-12-31", "Revenues"))
        facts = facts_of(**annual("EarningsPerShareDiluted", "USD/shares", {2025: 2.5}))
        self.assertEqual(LF._latest_annual_value(facts, ["EarningsPerShareDiluted"]), (None, None, None))

    def test_retired_tag_gives_way_to_a_live_one(self):
        facts = facts_of(**annual("EarningsPerShareDiluted", "USD/shares", {2019: 9.0}),
                         **annual("EarningsPerShareBasicAndDiluted", "USD/shares", {2025: 3.0}))
        val, end, concept = LF._latest_annual_value(
            facts, LF.EDGAR_CONCEPT_FALLBACKS["eps_diluted"], unit_keys=("USD/shares",),
            max_age_days=LF._ANNUAL_FALLBACK_MAX_AGE_DAYS, as_of=date(2026, 9, 21))
        self.assertEqual((val, concept), (3.0, "EarningsPerShareBasicAndDiluted"))

    def test_quarterly_filer_withheld_for_a_split_does_not_fall_back(self):
        facts = reported_facts("ALIT", "2026-06-30")
        if facts is None:
            self.skipTest("ALIT not in reported.csv")
        out = LF.compute_edgar_factors(facts, as_of=date(2026, 9, 21))
        self.assertNotIn("ttm_eps_diluted", out)


def quarterly_eps_facts(concept, unit="USD/shares"):
    recs = [r for y in (2024, 2025) for r in H.income_records(y, [1.0, 1.1, 1.2, 1.3])]
    return facts_of(**{concept: (unit, recs)})


class ConceptWidening(unittest.TestCase):

    def basis(self, concept):
        out = LF.compute_edgar_factors(quarterly_eps_facts(concept), as_of=date(2026, 9, 21))
        return out.get("ttm_eps_diluted"), out.get("eps_basis")

    def test_basic_and_diluted_counts_as_diluted(self):
        eps, basis = self.basis("EarningsPerShareBasicAndDiluted")
        self.assertAlmostEqual(eps, 4.6)
        self.assertEqual(basis, LF.EPS_BASIS_TTM)

    def test_partnership_units(self):
        eps, basis = self.basis("NetIncomeLossPerOutstandingLimitedPartnershipUnitDilutedNetOfTax")
        self.assertAlmostEqual(eps, 4.6)
        self.assertEqual(basis, LF.EPS_BASIS_TTM)

    def test_basic_is_the_labeled_last_resort(self):
        eps, basis = self.basis("EarningsPerShareBasic")
        self.assertAlmostEqual(eps, 4.6)
        self.assertEqual(basis, LF.EPS_BASIS_BASIC)

    def test_diluted_beats_basic(self):
        facts = quarterly_eps_facts("EarningsPerShareDiluted")
        basic = quarterly_eps_facts("EarningsPerShareBasic")["us-gaap"]
        for node in basic.values():
            for r in node["units"]["USD/shares"]:
                r["val"] += 0.1
        facts["us-gaap"].update(basic)
        out = LF.compute_edgar_factors(facts, as_of=date(2026, 9, 21))
        self.assertAlmostEqual(out["ttm_eps_diluted"], 4.6)
        self.assertEqual(out["eps_basis"], LF.EPS_BASIS_TTM)


def row(**kw):
    s = {"ticker": "AAA", "name": "AAA Corp Common Stock", "price": 50.0,
         "fiscal_period_end": "2026-06-30"}
    s.update(kw)
    return s


class PeStatus(unittest.TestCase):

    def run_one(self, s):
        with H.quiet():
            LF.derive_ratios_from_fundamentals([s])
        return s

    def test_filing_pe_replaces_vendor(self):
        s = self.run_one(row(ttm_eps_diluted=2.0, pe=30.0))
        self.assertEqual(s["pe"], 25.0)
        self.assertEqual(s["status"]["pe"], "awaiting_filing")

    def test_negative_eps_is_not_meaningful(self):
        s = self.run_one(row(ttm_eps_diluted=-1.0, pe=30.0))
        self.assertNotIn("pe", s)
        self.assertEqual(s["status"]["pe"], "not_meaningful")

    def test_vendor_pe_on_a_filed_loss_is_withheld(self):
        s = self.run_one(row(ttm_net_income=-5e6, pe=30.0))
        self.assertNotIn("pe", s)
        self.assertEqual(s["status"]["pe"], "not_meaningful")

    def test_vendor_pe_is_labeled_as_vendor(self):
        s = self.run_one(row(ttm_net_income=5e6, pe=30.0))
        self.assertEqual(s["pe"], 30.0)
        self.assertEqual(s["status"]["pe"], "vendor_value")
        s = self.run_one(row(pe=30.0))
        self.assertEqual(s["status"]["pe"], "vendor_value")

    def test_depositary_share_keeps_the_vendor(self):
        s = self.run_one(row(name="DoubleDown Interactive Co., Ltd. - American Depositary Shares",
                             price=12.75, ttm_eps_diluted=46.2, pe=5.5))
        self.assertEqual(s["pe"], 5.5)
        self.assertEqual(s["status"]["pe"], "vendor_value")
        s = self.run_one(row(name="ADS-TEC ENERGY PLC - Ordinary Shares", ttm_eps_diluted=2.0))
        self.assertEqual(s["pe"], 25.0)

    def test_out_of_bounds_filing_pe_is_not_left_as_vendor(self):
        s = self.run_one(row(ttm_eps_diluted=0.01, pe=30.0))
        self.assertNotIn("pe", s)
        self.assertEqual(s["status"]["pe"], "not_meaningful")

    def test_stale_stamp_is_cleared(self):
        s = self.run_one(row(status={"pe": "vendor_value", "roe_ttm": "no_coverage"}))
        self.assertEqual(s["status"], {"roe_ttm": "no_coverage"})

    def test_field_status_codes_exist(self):
        for code in LF._PE_STATUS_CODES:
            self.assertIn(code, LF.FIELD_STATUS)


class FreshFetchWithholds(unittest.TestCase):

    def test_carried_eps_is_dropped_when_the_filings_refuse_it(self):
        facts = reported_facts("ALIT", "2026-06-30")
        if facts is None:
            self.skipTest("ALIT not in reported.csv")
        s = {"ticker": "ALIT", "security_type": "operating", "ttm_eps_diluted": 34.37,
             "prior_ttm_eps_diluted": -40.78, "eps_basis": "ttm"}
        with H.patched(LF, fetch_edgar_company_facts=lambda cik: facts), H.quiet():
            n = LF.enrich_with_edgar([s], {"ALIT": 1809104}, max_workers=1)
        self.assertEqual(n, 1)
        for f in LF._EDGAR_WITHHELD_ON_ABSENCE:
            self.assertNotIn(f, s)
        self.assertLess(s["ttm_net_income"], 0)


class ScreenUsablePe(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, str(H.REPO / "theses" / "bin"))
        import screen
        cls.screen = screen

    def test_sign_disagreement_is_refused(self):
        r = {"pe": "0.34", "ttm_eps_diluted": "34.37", "ttm_net_income": "-2028000000",
             "shares_outstanding": "446790011"}
        self.assertIsNone(self.screen.usable_pe(r))

    def test_magnitude_disagreement_alone_is_kept(self):
        # Mastercard: right EPS, cover page counting one share class.
        r = {"pe": "30.5", "ttm_eps_diluted": "18.18", "ttm_net_income": "17000000000",
             "shares_outstanding": "128000000", "eps_basis": "ttm"}
        self.assertEqual(self.screen.usable_pe(r), 30.5)

    def test_rows_from_before_the_fix_keep_the_magnitude_test(self):
        # BKNG on 2026-09-21: 167.96 against 9.59 of net income per share,
        # summed by the code that crossed splits. No eps_basis on the row.
        r = {"pe": "0.9996", "ttm_eps_diluted": "167.96", "ttm_net_income": "7209000000",
             "shares_outstanding": "751380500", "eps_basis": ""}
        self.assertIsNone(self.screen.usable_pe(r))


if __name__ == "__main__":
    unittest.main()
