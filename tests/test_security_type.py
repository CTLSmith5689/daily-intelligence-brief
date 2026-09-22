"""security_type: the listing classifier, the withholding pass and the cohort exclusion.

Stdlib unittest, no network. Names are the exchange Security Names exactly as
data/tickers.csv carried them on 2026-09-21, so a rule change that breaks a real
listing fails here rather than in the next day's panel.

    python -m unittest tests.test_security_type
"""
import contextlib
import io
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

import security_type as st  # noqa: E402


def quiet(fn, *args):
    """Call a pipeline function without its progress print."""
    with contextlib.redirect_stdout(io.StringIO()):
        return fn(*args)


# (ticker, name, index, expected security_type from the name alone)
NAME_CASES = [
    # Operating companies that look like something else.
    ("CPT", "Camden Property Trust", "S&P 500", "operating"),
    ("CPT", "Camden Property Trust Common Shares of Beneficial Interest", "NYSE", "operating"),
    ("PNFP", "Pinnacle Financial Partners", "S&P 400", "operating"),
    ("PNFP", "Pinnacle Financial Partners, Inc. - Common Stock", "Nasdaq", "operating"),
    ("BNS", "Bank Nova Scotia Halifax Pfd 3 Ordinary Shares", "NYSE", "operating"),
    ("SHOP", "Shopify Inc. - Class A Subordinate Voting Shares", "Nasdaq", "operating"),
    ("GOOS", "Canada Goose Holdings Inc. Subordinate Voting Shares", "NYSE", "operating"),
    ("UHT", "Universal Health Realty Income Trust Common Stock", "NYSE", "operating"),
    ("OR", "OR Royalties Inc. Common Shares", "NYSE", "operating"),
    ("AGNC", "AGNC Investment Corp. - Common Stock", "Nasdaq", "operating"),
    ("MAIN", "Main Street Capital Corporation Common Stock", "NYSE", "operating"),
    # Operating partnerships: labeled lp, never non-operating.
    ("ET", "Energy Transfer LP Common Units", "NYSE", "lp"),
    ("MPLX", "MPLX LP Common Units Representing Limited Partner Interests", "NYSE", "lp"),
    ("CQP", "Cheniere Energy Partners, LP Common Units", "NYSE", "lp"),
    ("ARLP", "Alliance Resource Partners, L.P. - Common Units Representing Limited "
             "Partnership Interests", "Nasdaq", "lp"),
    ("EPD", "Enterprise Products Partners L.P. Common Stock", "NYSE", "lp"),
    # Debt.
    ("ADAMG", "Adamas Trust, Inc. - 9.125% Senior Notes Due 2030", "Nasdaq", "debt"),
    ("GAINI", "Gladstone Investment Corporation - 7.875% Notes due 2030", "Nasdaq", "debt"),
    ("EAI", "Entergy Arkansas, LLC First Mortgage Bonds, 4.875% Series Due September 1, 2066",
     "NYSE", "debt"),
    ("ELC", "Entergy Louisiana, Inc. Collateral Trust Mortgage Bonds, 4.875 % Series due "
            "September 1, 2066", "NYSE", "debt"),
    ("TVC", "Tennessee Valley Authority Common Stock", "NYSE", "debt"),
    ("TVE", "Tennessee Valley Authority", "NYSE", "debt"),
    ("CCZ", "Comcast Holdings ZONES", "NYSE", "debt"),
    # Structured and units.
    ("GJH", "Synthetic Fixed-Income Securities Inc 6.375% (STRATS) Cl A-1", "NYSE", "structured"),
    ("DDT", "Dillard's Capital Trust I", "NYSE", "structured"),
    ("DUKU", "Duke Energy Corporation Corporate Units", "NYSE", "equity_units"),
    # SPACs.
    ("AAC", "Ares Acquisition Corporation III Class A Ordinary Shares", "NYSE", "spac"),
    ("NBRG", "Newbridge Acquisition Limited - Class A Ordinary Share", "Nasdaq", "spac"),
    ("CCXI", "Churchill Capital Corp XI - Class A Ordinary Shares", "Nasdaq", "spac"),
    ("GIW", "GigCapital8 Corp. - Class A Ordinary Shares", "Nasdaq", "spac"),
    # Funds and BDCs.
    ("CCD", "Calamos Dynamic Convertible & Income Fund - Closed End Fund", "Nasdaq", "cef"),
    ("BANX", "ArrowMark Financial Corp. - Closed End Fund", "Nasdaq", "cef"),
    ("OXLC", "Oxford Lane Capital Corp. - Closed End Fund", "Nasdaq", "cef"),
    ("MSIF", "MSC Income Fund, Inc. Common Stock", "NYSE", "cef"),
    ("DXR", "Daxor Corporation - Closed End Fund", "Nasdaq", "cef"),
    ("ARCC", "Ares Capital Corporation - Closed End Fund", "Nasdaq", "bdc"),
    ("PSEC", "Prospect Capital Corporation - Closed End Fund", "Nasdaq", "bdc"),
    ("GAIN", "Gladstone Investment Corporation - Business Development Company", "Nasdaq", "bdc"),
    # Royalty trusts.
    ("SJT", "San Juan Basin Royalty Trust Common Stock", "NYSE", "royalty_trust"),
    ("MSB", "Mesabi Trust Common Stock", "NYSE", "royalty_trust"),
]


class ClassifierNames(unittest.TestCase):
    def test_name_rules(self):
        for ticker, name, index, want in NAME_CASES:
            with self.subTest(ticker=ticker, name=name):
                self.assertEqual(st.classify_name(name, index), want)

    def test_every_category_is_covered(self):
        self.assertEqual({c[3] for c in NAME_CASES}, set(st.SECURITY_TYPES))

    def test_lp_units_are_operating(self):
        # The owner's decision: label operating partnerships, never drop them.
        self.assertTrue(st.is_operating("lp"))
        self.assertTrue(st.is_operating("bdc"))
        self.assertTrue(st.is_operating("royalty_trust"))
        for t in ("spac", "debt", "structured", "equity_units", "cef"):
            self.assertFalse(st.is_operating(t))

    def test_blank_label_is_not_refused(self):
        # A deny-list: rows from before the column must not empty a universe.
        self.assertTrue(st.is_operating(""))
        self.assertTrue(st.is_operating(None))

    def test_values_are_the_agreed_vocabulary(self):
        self.assertEqual(set(st.SECURITY_TYPES),
                         {"operating", "lp", "bdc", "royalty_trust", "spac", "debt",
                          "structured", "equity_units", "cef"})
        self.assertEqual(st.NON_OPERATING,
                         {"spac", "debt", "structured", "equity_units", "cef"})


class ClassifierData(unittest.TestCase):
    """The corrections that need the row's data, not just its name."""

    def row(self, name, index="NYSE", **fields):
        return {"name": name, "index": index, **fields}

    def test_shell_companies_sub_industry(self):
        r = self.row("Aperture AC - Class A Ordinary Shares", "Nasdaq",
                     sub_industry="Shell Companies")
        self.assertEqual(st.classify_row_rule(r), ("spac", "d_shell_sub_industry"))

    def test_sp_rows_are_always_operating(self):
        r = self.row("Camden Property Trust", "S&P 500", sub_industry="Shell Companies")
        self.assertEqual(st.classify_row(r), "operating")

    def test_bdc_edgar_fingerprint(self):
        main = self.row("Main Street Capital Corporation Common Stock",
                        sub_industry="Asset Management", edgar_updated="2026-09-21",
                        ttm_net_income=451340000.0, gross_margin=1.0)
        self.assertEqual(st.classify_row(main), "bdc")
        # MSIF: the name says fund, the 10-K says BDC.
        msif = self.row("MSC Income Fund, Inc. Common Stock",
                        sub_industry="Asset Management", edgar_updated="2026-09-21",
                        ttm_net_income="99059000", gross_margin="1")   # panel CSV strings
        self.assertEqual(st.classify_row(msif), "bdc")
        # A mortgage REIT has the same EDGAR shape but a different industry.
        agnc = self.row("AGNC Investment Corp. - Common Stock", "Nasdaq",
                        sub_industry="REIT - Mortgage", edgar_updated="2026-09-21",
                        ttm_net_income=1.0, gross_margin=1.0)
        self.assertEqual(st.classify_row(agnc), "operating")

    def test_vendor_fund_fingerprint(self):
        ty = self.row("Tri Continental Corporation Common Stock",
                      sub_industry="Asset Management", gross_margin=1.0)
        self.assertEqual(st.classify_row(ty), "cef")
        # A class letter is an operating-company capital structure.
        vinp = self.row("Vinci Compass Investments Ltd. - Class A Common Shares", "Nasdaq",
                        sub_industry="Asset Management", gross_margin=1.0)
        self.assertEqual(st.classify_row(vinp), "operating")

    def test_revenue_guard(self):
        dxr = self.row("Daxor Corporation - Closed End Fund", "Nasdaq",
                       edgar_updated="2026-09-21", ttm_revenue=1430000.0,
                       gross_margin=0.53, operating_margin=-0.2)
        self.assertEqual(st.classify_row_rule(dxr), ("operating", "d_revenue_guard"))
        # A shell's placeholder zeros are not revenue evidence.
        shell = self.row("Ares Acquisition Corporation III Class A Ordinary Shares",
                         edgar_updated="2026-09-21", ttm_revenue=5.0,
                         gross_margin=0.0, operating_margin=0.0)
        self.assertEqual(st.classify_row(shell), "spac")

    def test_revenue_guard_never_applies_to_debt(self):
        # A note inherits its parent's revenue through the parent's CIK.
        for name in ("Adamas Trust, Inc. - 9.125% Senior Notes Due 2030",
                     "Duke Energy Corporation Corporate Units",
                     "Synthetic Fixed-Income Securities Inc 6.375% (STRATS) Cl A-1"):
            r = self.row(name, edgar_updated="2026-09-21", ttm_revenue=5e9,
                         gross_margin=0.4, operating_margin=0.2)
            with self.subTest(name=name):
                self.assertIn(st.classify_row(r), st.DEBT_LIKE)

    def test_label_survives_its_own_withholding(self):
        # apply_security_types blanks gross_margin on funds and BDCs; the next
        # run must not read the blank as "not a fund" and flip the label.
        for fields, want in (
            ({"name": "Main Street Capital Corporation Common Stock",
              "sub_industry": "Asset Management", "edgar_updated": "2026-09-21",
              "ttm_net_income": 1.0}, "bdc"),
            ({"name": "Tri Continental Corporation Common Stock",
              "sub_industry": "Asset Management"}, "cef"),
        ):
            with self.subTest(want=want):
                self.assertEqual(st.classify_row({"index": "NYSE", **fields}), want)


class PipelineEffects(unittest.TestCase):
    """The withholding pass and the cohort exclusion in lambda_function.py."""

    @classmethod
    def setUpClass(cls):
        import lambda_function
        cls.L = lambda_function

    def debt_row(self):
        return {
            "ticker": "ADAMG", "index": "Nasdaq", "in_index": 1,
            "name": "Adamas Trust, Inc. - 9.125% Senior Notes Due 2030",
            # The listing's own price series: kept.
            "price": 25.02, "change_pct": 0.1, "volume": 1200, "return_1m": 0.01,
            "volatility_1y": 0.05, "beta_1y": 0.1, "max_drawdown_1y": -0.02,
            # The parent's filings, fetched through the parent's CIK: withheld.
            "shares_outstanding": 89879786.0, "ttm_eps_diluted": 1.68,
            "ttm_revenue": 1.0e9, "ttm_net_income": 1.5e8, "equity": 1.4e9,
            "market_cap": 25.02 * 89879786, "pe": 25.02 / 1.68, "price_book": 1.6,
            "roe_ttm": 0.1, "gross_margin": 0.79, "operating_margin": 0.52,
            "fcf_yield": 0.05, "ev_revenue": 36.4, "accruals_ratio": 0.01,
            "earnings_consistency": 0.8, "edgar_updated": "2026-09-21",
            "fiscal_period_end": "2026-06-30", "benford": {"mad": 0.01},
            "op_margin_history": [0.5, 0.52],
            "status": {"pe": "awaiting_filing", "return_52w": "insufficient_history"},
        }

    def test_debt_row_is_withheld_after_ratio_derivation(self):
        s = self.debt_row()
        # derive_ratios_from_fundamentals is what rebuilt the fabricated cap
        # and P/E; the withholding pass has to come after it and survive it.
        quiet(self.L.derive_ratios_from_fundamentals, [s])
        quiet(self.L.apply_security_types, [s])
        self.assertEqual(s["security_type"], "debt")
        for f in ("market_cap", "pe", "price_book", "roe_ttm", "gross_margin",
                  "operating_margin", "fcf_yield", "ev_revenue", "accruals_ratio",
                  "earnings_consistency", "ttm_revenue", "ttm_net_income",
                  "ttm_eps_diluted", "shares_outstanding", "equity"):
            with self.subTest(field=f):
                self.assertNotIn(f, s)
                self.assertEqual(s["status"][f], "not_applicable")
        for f in ("edgar_updated", "fiscal_period_end", "benford", "op_margin_history"):
            self.assertNotIn(f, s)
        for f in ("price", "change_pct", "volume", "return_1m", "volatility_1y",
                  "beta_1y", "max_drawdown_1y"):
            self.assertIn(f, s)
        self.assertEqual(s["status"]["return_52w"], "insufficient_history")
        self.assertIn("not_applicable", self.L.FIELD_STATUS)
        # Running the whole thing again changes nothing: it is idempotent, and
        # derive_ratios has nothing left to rebuild a cap or a P/E from.
        before = dict(s, status=dict(s["status"]))
        quiet(self.L.derive_ratios_from_fundamentals, [s])
        quiet(self.L.apply_security_types, [s])
        self.assertEqual(s, before)

    def test_spac_and_fund_withholding(self):
        spac = {"ticker": "AAC", "index": "NYSE", "price": 10.5, "market_cap": 5e8,
                "name": "Ares Acquisition Corporation III Class A Ordinary Shares",
                "gross_margin": 0.0, "operating_margin": 0.0, "pe": 30.1, "price_book": 0.0}
        cef = {"ticker": "CCD", "index": "Nasdaq", "price": 20.0, "market_cap": 6.7e8,
               "name": "Calamos Dynamic Convertible & Income Fund - Closed End Fund",
               "pe": 2.71, "gross_margin": 1.0, "operating_margin": 0.0}
        bdc = {"ticker": "ARCC", "index": "Nasdaq", "price": 20.0, "market_cap": 1.39e10,
               "name": "Ares Capital Corporation - Closed End Fund", "pe": 14.37,
               "gross_margin": 1.0, "operating_margin": 0.8}
        quiet(self.L.apply_security_types, [spac, cef, bdc])
        self.assertNotIn("gross_margin", spac)
        self.assertNotIn("price_book", spac)          # a 0.0 placeholder
        self.assertEqual(spac["pe"], 30.1)             # its own trust arithmetic
        self.assertEqual(spac["market_cap"], 5e8)      # T9: the panel row survives
        self.assertNotIn("pe", cef)
        self.assertEqual(cef["market_cap"], 6.7e8)
        self.assertNotIn("gross_margin", bdc)
        self.assertEqual(bdc["pe"], 14.37)
        self.assertEqual(bdc["status"], {"gross_margin": "not_applicable"})

    def test_relabel_clears_stale_not_applicable(self):
        # A shell that completed its merger keeps its cached status dict on the
        # same-day path; its stamps must go once the label changes.
        s = {"ticker": "XYZ", "index": "Nasdaq", "name": "XYZ Holdings - Common Stock",
             "gross_margin": 0.4, "status": {"gross_margin": "not_applicable",
                                             "operating_margin": "not_applicable"}}
        quiet(self.L.apply_security_types, [s])
        self.assertEqual(s["security_type"], "operating")
        self.assertNotIn("status", s)
        self.assertEqual(s["gross_margin"], 0.4)

    def financials_universe(self):
        """25 operating Financials names and 5 closed-end funds with extreme
        values on every scored field, all with enough data to score."""
        fields = self.L.SCORE_FIELDS
        stocks = []
        for i in range(25):
            s = {"ticker": f"OP{i:02d}", "index": "NYSE", "sector": "Financials",
                 "name": f"Operating Bancorp {i} Common Stock", "security_type": "operating"}
            for j, f in enumerate(fields):
                s[f] = 0.01 * ((i * 7 + j * 3) % 25) + 0.05
            stocks.append(s)
        for i in range(5):
            s = {"ticker": f"CEF{i}", "index": "NYSE", "sector": "Financials",
                 "name": f"Closed Income Fund {i}", "security_type": "cef"}
            for f in fields:
                s[f] = 50.0 + i
            stocks.append(s)
        return stocks

    def test_cohort_excludes_non_operating(self):
        mixed = self.financials_universe()
        alone = [dict(s) for s in mixed if s["security_type"] == "operating"]
        quiet(self.L.compute_peer_scores, mixed)
        summary = quiet(self.L.compute_peer_scores, alone)
        by_t = {s["ticker"]: s for s in mixed}
        for s in alone:
            with self.subTest(ticker=s["ticker"]):
                # The funds' extreme values moved no operating name at all.
                self.assertEqual(by_t[s["ticker"]]["pct"], s["pct"])
                self.assertEqual(by_t[s["ticker"]]["m"], s["m"])
                self.assertEqual(by_t[s["ticker"]]["scorable"], 1)
        for s in mixed:
            if s["security_type"] == "cef":
                with self.subTest(ticker=s["ticker"]):
                    self.assertEqual(s["scorable"], 0)
                    self.assertIsNone(s["pct"])
                    self.assertIsNone(s["g"])
                    self.assertEqual(s["dims_present"], 0)
        self.assertEqual(summary["non_operating_excluded"], 0)
        self.assertEqual(quiet(self.L.compute_peer_scores, mixed)["non_operating_excluded"], 5)

    def test_edgar_skips_debt_like(self):
        # enrich_with_edgar must never map a note to its parent's companyfacts.
        # With a CIK map that answers for everyone and a fetch that records its
        # callers, only the operating row is fetched.
        fetched = []
        orig = self.L.fetch_edgar_company_facts
        self.L.fetch_edgar_company_facts = lambda cik: fetched.append(cik) or None
        try:
            stocks = [{"ticker": "ADAM", "security_type": "operating"},
                      {"ticker": "ADAMG", "security_type": "debt"},
                      {"ticker": "DUKU", "security_type": "equity_units"}]
            quiet(self.L.enrich_with_edgar, stocks, {"ADAM": 1, "ADAMG": 1, "DUKU": 2})
        finally:
            self.L.fetch_edgar_company_facts = orig
        self.assertEqual(fetched, [1])

    def test_registry_and_screener_carry_the_label(self):
        self.assertIn("security_type", self.L.TICKER_COLUMNS)
        self.assertNotIn("security_type", self.L.FUNDAMENTAL_SKIP_FIELDS)
        self.assertIn("security_type", self.L.FIELD_METHODS)
        self.assertIn(self.L.FIELD_METHODS["security_type"]["source"], self.L.FIELD_SOURCES)
        self.assertIn(self.L.FIELD_METHODS["security_type"]["refresh"], self.L.REFRESH_CLASSES)
        self.assertIn("__NON_OPERATING_JSON__", self.L.STOCKS_JS_TEMPLATE)


class ThesesGate(unittest.TestCase):
    """theses/bin reads the panel; a blank label is classified, not trusted."""

    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, str(REPO / "theses" / "bin"))
        import common
        cls.common = common

    def test_blank_label_is_classified_from_the_row(self):
        c = self.common
        note = {"ticker": "ADAMG", "security_type": "", "index": "Nasdaq",
                "name": "Adamas Trust, Inc. - 9.125% Senior Notes Due 2030"}
        company = {"ticker": "ADAM", "security_type": "", "index": "Nasdaq",
                   "name": "Adamas Trust, Inc. - Common Stock"}
        self.assertFalse(c.is_operating(note))
        self.assertTrue(c.is_operating(company))
        self.assertTrue(c.is_operating({"ticker": "X", "security_type": "operating",
                                        "name": "Some Fund"}))


if __name__ == "__main__":
    unittest.main()
