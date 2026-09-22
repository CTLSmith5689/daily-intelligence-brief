"""Risk metrics against invariants that hold for any correct implementation.

A series regressed on itself has slope 1, and a series whose every return is
twice the benchmark's has slope 2, whatever the data. Volatility and drawdown
are checked against an independent computation (statistics.stdev and a plain
running maximum) rather than against a restatement of the pipeline's loop.

The pipeline has no correlation function, so the "correlation 1" half of the
invariant has nothing to test and is not faked here.
"""
import json
import math
import os
import statistics
import sys
import unittest
from datetime import date, datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests import helpers as H  # noqa: E402

LF = H.LF
N = 253  # a stored year of closes


def risk_for(stock_closes, bench_closes):
    dates = H.trading_days(len(stock_closes))
    with H.temp_dir() as d, H.quiet():
        H.write_price_file(d, "TEST", dates, stock_closes)
        H.write_market_file(d, dates, bench_closes)
        stock = {"ticker": "TEST"}
        with H.patched(LF, PRICES_DIR=d):
            LF.derive_risk_metrics([stock])
    return stock


class BetaInvariants(unittest.TestCase):

    def setUp(self):
        self.bench_rets = H.zigzag_returns(N - 1)
        self.bench = H.closes_from_returns(self.bench_rets, 4000.0)

    def test_benchmark_against_itself_has_beta_one(self):
        stock = risk_for(self.bench, self.bench)
        self.assertAlmostEqual(stock["beta_1y"], 1.0, places=9)

    def test_twice_the_benchmark_returns_has_beta_two(self):
        doubled = H.closes_from_returns([2 * r for r in self.bench_rets], 50.0)
        stock = risk_for(doubled, self.bench)
        self.assertAlmostEqual(stock["beta_1y"], 2.0, places=9)

    def test_beta_pairs_by_date_not_position(self):
        # Drop one session from the stock only, as a holiday on one calendar
        # would. Pairing by position would shift every later return by a day
        # and wreck the slope; pairing by date loses one observation.
        dates = H.trading_days(N)
        drop = 150
        s_dates = dates[:drop] + dates[drop + 1:]
        s_closes = self.bench[:drop] + self.bench[drop + 1:]
        with H.temp_dir() as d, H.quiet():
            H.write_price_file(d, "TEST", s_dates, s_closes)
            H.write_market_file(d, dates, self.bench)
            stock = {"ticker": "TEST"}
            with H.patched(LF, PRICES_DIR=d):
                LF.derive_risk_metrics([stock])
        # The one two-day return pairs with a one-day index return; everything
        # else is identical, so beta stays close to 1 rather than collapsing
        # toward 0 as a one-day misalignment of uncorrelated noise would.
        self.assertGreater(stock["beta_1y"], 0.9)
        self.assertLess(stock["beta_1y"], 1.1)

    def test_volatility_and_drawdown_match_independent_computation(self):
        stock = risk_for(self.bench, self.bench)
        rets = [b / a - 1 for a, b in zip(self.bench, self.bench[1:])]
        self.assertAlmostEqual(stock["volatility_1y"],
                               statistics.stdev(rets) * math.sqrt(252), places=12)
        peak, worst = self.bench[0], 0.0
        for p in self.bench:
            peak = max(peak, p)
            worst = min(worst, p / peak - 1)
        self.assertAlmostEqual(stock["max_drawdown_1y"], worst, places=12)
        self.assertLessEqual(stock["max_drawdown_1y"], 0.0)

    def test_sharpe_withheld_without_a_rate(self):
        dates = H.trading_days(N)
        with H.temp_dir() as d, H.quiet():
            H.write_price_file(d, "TEST", dates, self.bench)
            stock = {"ticker": "TEST"}
            with H.patched(LF, PRICES_DIR=d):
                LF.derive_risk_metrics([stock])
        self.assertIn("volatility_1y", stock)
        self.assertNotIn("sharpe_1y", stock)
        self.assertNotIn("beta_1y", stock)


class OnDiskMarketSeries(unittest.TestCase):
    """SPY is the S&P 500 in a wrapper, so its beta against ^GSPC must be about
    1. The price files live on gh-pages and are restored by the workflow before
    this runs; they are gitignored in main, so locally this usually skips."""

    def test_spy_beta_against_gspc(self):
        prices = H.REPO / "docs" / "prices"
        spy = prices / LF._news_filename("SPY")
        market = prices / LF.MARKET_FILE
        if not (spy.exists() and market.exists()):
            self.skipTest("docs/prices/SPY.json or _MARKET.json not on disk")
        closes = json.loads(spy.read_text(encoding="utf-8")).get("closes") or []
        cutoff = (datetime.now(timezone.utc).date() - timedelta(days=10)).isoformat()
        if len(closes) < LF._MIN_RISK_OBS or str(closes[-1][0]) < cutoff:
            self.skipTest("SPY history on disk is too short or too old to measure")
        stock = {"ticker": "SPY"}
        with H.quiet():
            LF.derive_risk_metrics([stock])
        if "beta_1y" not in stock:
            self.skipTest("market series on disk did not overlap SPY enough")
        self.assertGreaterEqual(stock["beta_1y"], 0.98)
        self.assertLessEqual(stock["beta_1y"], 1.02)


if __name__ == "__main__":
    unittest.main()
