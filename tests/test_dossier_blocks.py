"""The dossier's memo-input blocks, and the collection changes that feed them.

Offline and synthetic. The company below is built to look like NVIDIA in the
one way that broke the old revenue check: it grows fast enough that its trailing
twelve months run far ahead of its last fiscal year, and its four latest quarters
(one of them a Q4 that is never filed as a quarter) sum exactly to the trailing
figure the panel stores.
"""
import csv
import json
import os
import sys
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests import helpers as H  # noqa: E402

LF = H.LF
# theses/bin holds a queue.py. Importing dossier puts that directory first on
# sys.path, and the next stdlib `import queue` (ThreadPoolExecutor's, in another
# test) would find it instead. So the stdlib one is loaded first and the path is
# put back afterwards.
import queue  # noqa: E402,F401
import concurrent.futures.thread  # noqa: E402,F401
_SAVED_PATH = list(sys.path)
sys.path.insert(0, str(H.REPO / "theses" / "bin"))
sys.path.insert(0, str(H.REPO / "portfolio" / "bin"))
import dossier as D  # noqa: E402
import construct  # noqa: E402
sys.path[:] = _SAVED_PATH

HEADINGS = ["### Key data", "### Guidance", "### Peers", "### Balance sheet and cash flow",
            "### History", "### Calendar", "### Sizing inputs", "### Current view"]
M = 1_000_000


def fy_row(end, revenue, **kw):
    r = {"ticker": "FAST", "period": "FY", "period_end": end, "revenue": str(revenue)}
    r.update({k: str(v) for k, v in kw.items()})
    return r


def q_row(end, revenue, **kw):
    r = {"ticker": "FAST", "period": "Q", "period_end": end, "revenue": str(revenue)}
    r.update({k: str(v) for k, v in kw.items()})
    return r


# Fiscal years end in late January, like NVIDIA's. FY2026 revenue is 215,938 and
# the three quarters inside it are 44,062, 46,743 and 57,006, so Q4 is 68,127.
# The two quarters since are 81,615 and 96,221. Trailing revenue is 302,969.
FY = [fy_row("2024-01-28", 60_922 * M, gross_profit=44_301 * M, operating_income=32_972 * M,
             net_income=29_760 * M, eps_diluted=1.19, ocf=28_090 * M, shares_diluted=24_940 * M),
      fy_row("2025-01-26", 130_497 * M, gross_profit=97_858 * M, operating_income=81_453 * M,
             net_income=72_880 * M, eps_diluted=2.94, ocf=64_089 * M, shares_diluted=24_804 * M),
      fy_row("2026-01-25", 215_938 * M, gross_profit=153_463 * M, operating_income=130_387 * M,
             net_income=120_067 * M, eps_diluted=4.90, ocf=102_718 * M, capex=3_236 * M,
             shares_diluted=24_514 * M)]
Q = [q_row("2025-04-27", 44_062 * M, net_income=18_775 * M, ocf=27_414 * M, shares_diluted=24_611 * M),
     q_row("2025-07-27", 46_743 * M, net_income=26_422 * M, shares_diluted=24_532 * M),
     q_row("2025-10-26", 57_006 * M, net_income=31_910 * M, shares_diluted=24_483 * M),
     q_row("2026-04-26", 81_615 * M, net_income=58_321 * M, ocf=50_344 * M, shares_diluted=24_391 * M),
     q_row("2026-07-26", 96_221 * M, net_income=59_688 * M, shares_diluted=24_285 * M)]
TTM = 302_969 * M

RELEASE = (
    "FAST reported revenue for the quarter. FAST will pay its next quarterly cash dividend of "
    "$0.25 per share on October 1, 2026, to all shareholders of record on September 10, 2026. "
    "Outlook FAST's outlook for the third quarter is as follows: Revenue is expected to be "
    "$108.0 billion, plus or minus 2%. Gross margin is expected to be 74.0%. "
    "Highlights Data Center revenue rose. "
    "Certain statements in this press release are forward-looking statements. "
    "CONDENSED CONSOLIDATED BALANCE SHEETS (In millions) (Unaudited) July 26, January 25, "
    "2026 2026 ASSETS Current assets: Cash and cash equivalents $ 22,443 $ 10,605 Marketable "
    "debt securities 34,143 39,065 Accounts receivable, net 63,059 38,466 Inventories 31,575 "
    "21,403 Total current assets 197,412 125,605 Short-term debt 1,000 999 Long-term debt "
    "32,366 7,469 Total liabilities 91,288 49,510 "
    "CONDENSED CONSOLIDATED STATEMENTS OF CASH FLOWS (In millions) (Unaudited) Three Months "
    "Ended Six Months Ended July 26, July 27, July 26, July 27, 2026 2025 2026 2025 Cash flows "
    "from operating activities: Net income $ 59,688 $ 26,422 $ 118,010 $ 45,197 Net cash "
    "provided by operating activities 24,077 15,365 74,421 42,779 Purchases related to "
    "property and equipment and intangible assets (2,677) (1,894) (4,434) (3,122) Other (15) "
    + H.EM_DASH + " (15) " + H.EM_DASH + " ")


def panel_row(ticker, sub="Semiconductors", sector="Information Technology", **kw):
    r = {"ticker": ticker, "name": f"{ticker} Corp", "sector": sector, "sub_industry": sub,
         "date": "2026-09-22", "security_type": "common"}
    r.update({k: str(v) for k, v in kw.items()})
    return r


ME = panel_row("FAST", price=228.87, market_cap=5_515_767 * M, shares_outstanding=24_100 * M,
               total_debt=33_366 * M, cash_and_investments=71_565 * M, ttm_ebitda=201_266 * M,
               ttm_revenue=TTM, prior_ttm_revenue=165_218 * M, ttm_eps_diluted=7.91,
               ttm_fcf=127_006 * M, ttm_net_income=192_879 * M, ttm_operating_income=197_579 * M,
               fiscal_period_end="2026-07-26", earnings_date="2026-11-17", beta_1y=1.905299,
               volatility_1y=0.378363)


def peers(n):
    return [panel_row(f"P{i}", price=100, market_cap=(100 - i) * 1e9, ttm_eps_diluted=4,
                      total_debt=1e9, cash_and_investments=2e9, ttm_ebitda=5e9,
                      ttm_revenue=20e9, prior_ttm_revenue=16e9, ttm_operating_income=6e9,
                      ttm_fcf=3e9, earnings_date="2026-10-20") for i in range(n)]


def closes(n=252, last=228.87):
    """A year of closes ending at `last`, the NVDA close of 2026-09-22."""
    days = H.trading_days(n)
    px = H.closes_from_returns(H.zigzag_returns(n - 1), 200.0)
    return [[d, round(p * last / px[-1], 4)] for d, p in zip(days, px)]


def section(text, heading):
    """The lines under `heading` up to the next ### or ## heading, blanks dropped."""
    lines = text.split("\n")
    i = lines.index(heading)
    body = []
    for ln in lines[i + 1:]:
        if ln.startswith("### ") or ln.startswith("## "):
            break
        if ln.strip():
            body.append(ln)
    return body


class Harness:
    """Runs memo_inputs with every network read replaced by synthetic data."""

    def __init__(self, prices=None, market=None, release=RELEASE, index_rows=None):
        self.prices, self.market = prices, market
        self.release, self.index_rows = release, index_rows or []

    def run(self, me, universe, fy, q, events=(), preds=(), rel_row=True):
        D._SITE_JSON.clear()
        D._SITE_JSON["prices/FAST.json"] = self.prices
        D._SITE_JSON["prices/_MARKET.json"] = self.market
        row = ({"filed": "2026-08-26", "text_path": "text/FAST/x.txt", "form": "8-K"}
               if rel_row and self.release is not None else None)
        docs = {"earnings_release": (row, self.release)} if row else {}
        fetched = self.release.encode("utf-8") if self.release else None
        with mock.patch.object(D, "fetch", return_value=fetched), \
                mock.patch.object(D, "filing_index_rows", return_value=self.index_rows):
            lines, caveats = D.memo_inputs("FAST", me, [me] + list(universe), universe,
                                           "2026-09-22", fy, q, docs, list(events), list(preds))
        D._SITE_JSON.clear()
        return "\n".join(lines), caveats


class RevenueCheck(unittest.TestCase):

    def test_fast_grower_whose_quarters_sum_to_trailing_does_not_fire(self):
        qs = D.quarter_series(FY, Q)
        chk = D.revenue_check(TTM, "2026-07-26", qs, FY)
        self.assertEqual(chk["basis"], "quarters")
        self.assertAlmostEqual(chk["ttm"], TTM)
        self.assertFalse(chk["bad"])
        # The comparison this replaced: trailing against the last fiscal year.
        self.assertGreater(TTM / (215_938 * M) - 1, 0.25)

    def test_derived_q4_is_the_year_less_three_quarters(self):
        q4 = [r for r in D.quarter_series(FY, Q) if r["derived"] and r["end"] == "2026-01-25"]
        self.assertEqual(len(q4), 1)
        self.assertAlmostEqual(q4[0]["revenue"], 68_127 * M)
        self.assertIsNone(q4[0]["ocf"])

    def test_a_fragment_of_revenue_still_fires(self):
        chk = D.revenue_check(TTM * 0.5, "2026-07-26", D.quarter_series(FY, Q), FY)
        self.assertTrue(chk["bad"])

    def test_falls_back_to_the_year_with_growth_tolerance(self):
        chk = D.revenue_check(TTM, "2026-07-26", D.quarter_series(FY, Q[:2]), FY)
        self.assertEqual(chk["basis"], "fy")
        self.assertFalse(chk["bad"])

    def test_rendered_line_says_the_figure_is_right(self):
        text, caveats = Harness(prices={"closes": closes()}).run(ME, peers(8), FY, Q)
        hist = "\n".join(section(text, "### History"))
        self.assertIn("Trailing revenue check", hist)
        self.assertIn("302,969", hist)
        self.assertNotIn("does not match", hist)
        self.assertFalse(any("ttm_revenue" in c for c in caveats))


class Blocks(unittest.TestCase):

    def setUp(self):
        market = {"risk_free_symbol": "^IRX", "risk_free": [["2026-09-22", 4.005]],
                  "ten_year_symbol": "^TNX", "ten_year": [["2026-09-22", 4.21]],
                  "benchmark": []}
        vols = [120e6] * 252
        self.text, _ = Harness(prices={"closes": closes(), "volumes": vols}, market=market,
                               index_rows=[{"form": "10-K", "filed": "2026-02-25"}]).run(
            ME, peers(8), FY, Q,
            events=[{"ticker": "FAST", "date": "2026-09-22", "kind": "initiate",
                     "thesis_id": "FAST-2026-09-22", "direction": "watch", "conviction": "4"}])

    def test_every_block_renders_under_its_heading_in_order(self):
        lines = self.text.split("\n")
        at = [lines.index(h) for h in HEADINGS]
        self.assertEqual(at, sorted(at))
        for h in HEADINGS:
            with self.subTest(block=h):
                self.assertTrue(section(self.text, h), f"{h} is empty")

    def test_key_data_uses_the_release_balance_sheet(self):
        body = "\n".join(section(self.text, "### Key data"))
        self.assertIn("Net cash | $23.2B", body)          # 22,443 + 34,143 - 1,000 - 32,366
        self.assertIn("Enterprise value | $5,492.5B", body)
        self.assertIn("P/E, past 12 months | 28.9", body)
        self.assertIn("$0.25 a quarter", body)
        self.assertIn("does not reconcile", body)          # panel cash is 71,565

    def test_guidance_quotes_the_outlook(self):
        body = "\n".join(section(self.text, "### Guidance"))
        self.assertIn("$108.0 billion, plus or minus 2%", body)
        self.assertNotIn("Highlights", body)
        self.assertNotIn("forward-looking", body)

    def test_peers_table_and_median(self):
        body = section(self.text, "### Peers")
        rows = [ln for ln in body if ln.startswith("| ")]
        self.assertEqual(len(rows), 1 + 1 + 8 + 1)         # header, this name, 8 peers, median
        self.assertTrue(rows[-1].startswith("| Peer median (excluding FAST)"))
        self.assertIn("| 25.0 |", rows[-1])                # every peer: 100 / 4

    def test_balance_sheet_dso_and_cash_flow(self):
        body = "\n".join(section(self.text, "### Balance sheet and cash flow"))
        self.assertIn("| Accounts receivable | 63,059 | 38,466 |", body)
        self.assertIn("60 days at 2026-07-26", body)
        self.assertIn("51 days at 2026-01-25", body)       # on the derived Q4, 68,127
        self.assertIn("$134.4B", body)                     # 102,718 - 42,779 + 74,421
        self.assertIn("= 63%", body)                       # 74,421 / 118,010

    def test_history_restates_nothing_without_a_split_and_shows_fcf(self):
        body = "\n".join(section(self.text, "### History"))
        self.assertIn("| FY2026 | 215,938 | 65% | 71.1% |", body)
        self.assertIn("| 102,718 | 3,236 | 99,482 |", body)

    def test_calendar_sizing_current_view(self):
        cal = "\n".join(section(self.text, "### Calendar"))
        self.assertIn("2026-11-17: FAST reports", cal)
        self.assertIn("2026-10-01: FAST dividend", cal)
        self.assertNotIn("2026-09-10", cal)                # a record date already past
        siz = "\n".join(section(self.text, "### Sizing inputs"))
        self.assertIn("4.005% on 2026-09-22", siz)
        self.assertIn("4.210% on 2026-09-22", siz)
        self.assertIn("a draft the owner has not approved", siz)
        self.assertIn("| Rule weight |", siz)
        cur = "\n".join(section(self.text, "### Current view"))
        self.assertIn("Write a revision, not an initiation", cur)


class MissingData(unittest.TestCase):
    """With nothing on disk each block states it in one line."""

    def setUp(self):
        bare = panel_row("FAST", sub="Nothing Else Here", sector="Nowhere")
        self.text, self.caveats = Harness(release=None).run(bare, [], [], [], rel_row=False)

    def test_one_line_each(self):
        for h, words in (("### Key data", "Not available"),
                         ("### Guidance", "None given"),
                         ("### Peers", "Not available"),
                         ("### Balance sheet and cash flow", "Not available"),
                         ("### History", "No reported history"),
                         ("### Calendar", "No dated event"),
                         ("### Current view", "No view on record")):
            with self.subTest(block=h):
                body = section(self.text, h)
                self.assertEqual(len(body), 1, body)
                self.assertIn(words, body[0])

    def test_rates_say_not_stored(self):
        siz = "\n".join(section(self.text, "### Sizing inputs"))
        self.assertIn("10-year Treasury yield | not stored yet", siz)
        self.assertTrue(any("10-year" in c for c in self.caveats))


class Sizing(unittest.TestCase):
    """The block's arithmetic is construct.py's, not a copy of it."""

    BOUNDS = (0.288, 0.744)

    def test_rule_weight_matches_construct(self):
        for vol in (0.10, 0.2, 0.3776, 0.5, 0.9, 2.0):
            with self.subTest(vol=vol):
                want, _ = construct.size({"X": {"conviction": "3"}}, {"X": vol}, self.BOUNDS)
                self.assertAlmostEqual(D.rule_weight(vol, self.BOUNDS), want["X"])
                vc = min(max(vol, self.BOUNDS[0]), self.BOUNDS[1])
                self.assertAlmostEqual(
                    want["X"], min(max(construct.CFG["risk_budget"] / vc,
                                       construct.CFG["min_position"]),
                                   construct.CFG["max_position"]))

    def test_nvda_numbers(self):
        w = D.rule_weight(0.3776, self.BOUNDS)
        self.assertAlmostEqual(w, 0.028 / 0.3776)
        self.assertEqual(f"{w:.1%}", "7.4%")
        self.assertEqual(f"{w / 2:.1%}", "3.7%")

    def test_bear_loss_cap(self):
        self.assertAlmostEqual(D.bear_loss_cap(-0.454), 0.02 / 0.454)
        self.assertEqual(f"{D.bear_loss_cap(-0.454):.1%}", "4.4%")
        self.assertAlmostEqual(D.bear_loss_cap(-0.20, cap=0.03), 0.15)
        self.assertIsNone(D.bear_loss_cap(0))
        self.assertEqual(D.BEAR_LOSS_CAP, 0.02)

    def test_volatility_is_constructs_formula(self):
        cl = closes()
        vol, n = D.realised_vol(cl)
        vals = [c[1] for c in cl]
        rets = [vals[i] / vals[i - 1] - 1 for i in range(1, len(vals))]
        import math, statistics
        self.assertEqual(n, len(rets))
        self.assertAlmostEqual(vol, statistics.pstdev(rets) * math.sqrt(252))


class Parsers(unittest.TestCase):

    def test_split_divisors_read_4_and_10_for_1(self):
        fy = [{"period_end": f"{y}-01-28", "shares_diluted": str(s)} for y, s in
              ((2019, 625e6), (2020, 2472e6), (2021, 2510e6), (2022, 2535e6), (2023, 25070e6))]
        divs, jumps = D.split_divisors(fy)
        self.assertEqual(divs, [40.0, 10.0, 10.0, 10.0, 1.0])
        self.assertEqual([j[3] for j in jumps], [4, 10])

    def test_a_jump_that_fits_no_ratio_restates_nothing(self):
        fy = [{"period_end": "2020-12-31", "shares_diluted": "100"},
              {"period_end": "2021-12-31", "shares_diluted": "173"}]
        divs, jumps = D.split_divisors(fy)
        self.assertIsNone(divs)
        self.assertIsNone(jumps[-1][3])

    def test_slide_deck_has_no_statements(self):
        deck = ("In millions, except percentages $ 3,553 $ 4,208 Net sales Balance Sheet at the "
                "end of each period (1) Cash from Operations Capital expenditures")
        self.assertIsNone(D.release_balance_sheet(deck))
        self.assertIsNone(D.release_cash_flow(deck))

    def test_no_guidance_in_boilerplate(self):
        text = ("Certain statements are forward-looking statements, including our outlook for "
                "fiscal 2027, which is expected to be subject to risks.")
        self.assertEqual(D.guidance_excerpt(text), [])

    def test_latest_row_for_a_period_fills_blanks(self):
        rows = [{"period": "FY", "period_end": "2026-01-25", "capex": "", "ocf": "5"},
                {"period": "FY", "period_end": "2026-01-25", "capex": "3", "ocf": "5"}]
        (one,) = D.latest_per_period(rows)
        self.assertEqual(one["capex"], "3")


class Collection(unittest.TestCase):

    def test_capex_reads_the_same_tags_as_ttm_fcf(self):
        self.assertEqual(LF._FIN_CONCEPTS["capex"], LF.EDGAR_CONCEPT_FALLBACKS["capex"])
        self.assertIn("PaymentsToAcquireProductiveAssets", LF._FIN_CONCEPTS["capex"])

    def test_annual_capex_from_the_fallback_tag(self):
        facts = {"Revenues": H.facts_node(H.income_records(2025, [10.0, 11.0, 12.0, 13.0])),
                 "PaymentsToAcquireProductiveAssets":
                     H.facts_node(H.ytd_records(2025, [1.0, 1.0, 1.0, 1.5]))}
        (fy,) = LF._fin_periods(facts, *LF._FIN_ANNUAL)
        self.assertEqual(fy["capex"], 4.5)

    def test_record_financials_fills_blanks_by_appending(self):
        with H.temp_dir() as tmp, H.patched(LF, FINANCIALS_CSV_DIR=tmp), H.quiet():
            base = {"ticker": "FAST", "cik": 1, "period": "FY", "period_end": "2026-01-25",
                    "revenue": 100, "ocf": 50}
            self.assertEqual(LF.record_financials([base], "t1"), 1)
            # Same period again with nothing new: nothing written.
            self.assertEqual(LF.record_financials([base], "t2"), 0)
            # Capex arrives, and a restated revenue with it: only the blank is filled.
            self.assertEqual(LF.record_financials([{**base, "revenue": 999, "capex": 7}], "t3"), 1)
            self.assertEqual(LF.record_financials([{**base, "capex": 7}], "t4"), 0)
            with (tmp / "reported.csv").open(encoding="utf-8", newline="") as fh:
                rows = list(csv.DictReader(fh))
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0]["capex"], "")
            self.assertEqual((rows[1]["capex"], rows[1]["revenue"], rows[1]["collected_at"]),
                             ("7", "100", "t3"))
            self.assertEqual(list(rows[0]), LF.FINANCIAL_COLUMNS)

    def test_market_series_stores_the_ten_year(self):
        from datetime import datetime as dt

        class Column:                      # the slice of a pandas Series the code uses
            def __init__(self, pts):
                self.pts = pts

            def dropna(self):
                return self

            def items(self):
                return iter(self.pts)

        class Frame:
            empty = False

            def __init__(self, level):
                self.col = Column([(dt(2026, 9, 21), level), (dt(2026, 9, 22), level + 0.01)])

            def __getitem__(self, key):
                return self.col

        def download(symbol, **_):
            return Frame({"^IRX": 4.0, "^TNX": 4.2, "^GSPC": 7000.0}[symbol])
        fake = types.SimpleNamespace(download=download)
        with H.temp_dir() as tmp, H.patched(LF, PRICES_DIR=tmp), H.quiet(), \
                mock.patch.dict(sys.modules, {"yfinance": fake}):
            self.assertTrue(LF.enrich_with_market_series())
            data = json.loads((tmp / LF.MARKET_FILE).read_text(encoding="utf-8"))
            self.assertEqual(data["ten_year_symbol"], "^TNX")
            self.assertEqual(data["ten_year"][-1], ["2026-09-22", 4.21])
            self.assertEqual(data["risk_free"][-1][1], 4.01)

    def test_a_cache_without_the_ten_year_is_refetched(self):
        from datetime import datetime, timezone
        calls = []
        fake = types.SimpleNamespace(download=lambda symbol, **_: calls.append(symbol))
        with H.temp_dir() as tmp, H.patched(LF, PRICES_DIR=tmp), H.quiet(), \
                mock.patch.dict(sys.modules, {"yfinance": fake}):
            (tmp / LF.MARKET_FILE).write_text(json.dumps(
                {"updated": datetime.now(timezone.utc).isoformat(),
                 "risk_free": [["2026-09-22", 4.0]], "benchmark": [["2026-09-22", 1.0]]}),
                encoding="utf-8")
            LF.enrich_with_market_series()
            self.assertIn("^TNX", calls)


if __name__ == "__main__":
    unittest.main()
