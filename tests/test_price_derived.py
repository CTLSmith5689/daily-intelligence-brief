"""Price-derived fields against the formulas FIELD_METHODS publishes.

The oracle here is the documented formula string itself, evaluated against the
same closes the pipeline read. The methodology panel and the README print that
string, so if the code and the string disagree, a reader is being told
something false. That is exactly how return_1m shipped as distance from the
50-day moving average while labelled a one-month return, with the opposite sign
for 21% of tickers.
"""
import os
import statistics
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests import helpers as H  # noqa: E402

LF = H.LF

# Fields whose FIELD_METHODS formula is a Python expression over closes/volumes.
EVALUABLE = ("price", "change_pct", "return_1m", "return_12_2", "return_52w",
             "high52w_proximity", "volume", "volume_trend")


def formula_value(field, closes, volumes):
    expr = LF.FIELD_METHODS[field]["formula"]
    env = {"closes": closes, "volumes": volumes, "max": max, "min": min,
           "mean": statistics.mean, "__builtins__": {}}
    return eval(expr, env)  # the string is a constant in the repository


def derive(closes, volumes=None, bench=None, n_dates=None):
    """Run derive_from_price_history on one synthetic ticker; return its dict."""
    dates = H.trading_days(n_dates or len(closes))
    with H.temp_dir() as d, H.quiet():
        H.write_price_file(d, "TEST", dates, closes, volumes)
        if bench is not None:
            H.write_market_file(d, dates, bench)
        stock = {"ticker": "TEST"}
        with H.patched(LF, PRICES_DIR=d):
            LF.derive_from_price_history([stock])
    return stock


def volumes_for(n):
    return [1_000_000 + (i * 7919) % 400_000 for i in range(n)]


class PriceFormulas(unittest.TestCase):

    def assert_matches_formulas(self, closes, volumes, stock):
        for field in EVALUABLE:
            with self.subTest(field=field):
                self.assertIn(field, stock, f"{field} was not produced")
                expected = formula_value(field, closes, volumes)
                if field == "volume_trend":
                    self.assertAlmostEqual(stock[field], expected, places=12)
                else:
                    self.assertEqual(stock[field], expected)

    def test_rising_series_matches_every_formula(self):
        # 260 sessions, longer than the 253 the 52-week anchor needs, so an
        # anchor taken from the wrong end of the series would show.
        closes = [100 * 1.002 ** i for i in range(260)]
        vols = volumes_for(260)
        stock = derive(closes, vols)
        self.assert_matches_formulas(closes, vols, stock)
        self.assertGreater(stock["return_1m"], 0)
        self.assertGreater(stock["return_52w"], 0)
        self.assertEqual(stock["high52w_proximity"], 0.0)

    def test_noisy_series_matches_every_formula(self):
        closes = H.closes_from_returns(H.zigzag_returns(259))
        vols = volumes_for(260)
        self.assert_matches_formulas(closes, vols, derive(closes, vols))

    def test_return_1m_is_not_distance_from_moving_average(self):
        # A long climb and then a month of decline: the price still sits above
        # its 50-day average, so the old construction called this positive.
        # Over the month it fell, and the field says one-month return.
        closes = [100 * 1.008 ** i for i in range(239)]
        closes += [closes[-1] * 0.998 ** k for k in range(1, 22)]
        ma50 = sum(closes[-50:]) / 50
        self.assertGreater(closes[-1], ma50, "fixture must sit above its 50-day average")
        stock = derive(closes, volumes_for(len(closes)))
        self.assertLess(stock["return_1m"], 0)
        self.assertEqual(stock["return_1m"], closes[-1] / closes[-22] - 1)

    def test_return_12_2_skips_the_last_month(self):
        # Flat for eleven months, then a jump inside the last month. 12-2
        # momentum must not see the jump; the 52-week return must.
        closes = [50.0] * 239 + [50.0 * 1.01 ** k for k in range(1, 22)]
        stock = derive(closes, volumes_for(len(closes)))
        self.assertEqual(stock["return_12_2"], 0.0)
        self.assertGreater(stock["return_52w"], 0.2)

    def test_rel_strength_is_difference_of_matched_returns(self):
        closes = [100 * 1.002 ** i for i in range(260)]
        bench = [4000 * 1.001 ** i for i in range(260)]
        stock = derive(closes, volumes_for(260), bench=bench)
        expected = (closes[-1] / closes[-253] - 1) - (bench[-1] / bench[-253] - 1)
        self.assertAlmostEqual(stock["rel_strength_sp500"], expected, places=12)

    def test_short_history_is_withheld_not_guessed(self):
        closes = [100 + i for i in range(150)]
        stock = derive(closes, volumes_for(150))
        self.assertIn("return_1m", stock)
        for field in ("return_52w", "return_12_2"):
            self.assertNotIn(field, stock)
            self.assertEqual(stock["status"][field], "insufficient_history")
        tiny = derive([10.0 + i for i in range(15)], volumes_for(15))
        self.assertNotIn("return_1m", tiny)
        self.assertEqual(tiny["status"]["return_1m"], "insufficient_history")

    def test_every_price_derived_field_is_documented(self):
        closes = H.closes_from_returns(H.zigzag_returns(259))
        stock = derive(closes, volumes_for(260), bench=[4000 + i for i in range(260)])
        produced = set(stock) - {"ticker", "status", "prices_updated"}
        self.assertTrue(produced)
        self.assertEqual(produced - set(LF.FIELD_METHODS), set(),
                         "fields published with no FIELD_METHODS entry")


if __name__ == "__main__":
    unittest.main()
