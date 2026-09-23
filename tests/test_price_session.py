"""Regression tests for the stale-close incident (panel rows on 2026-09-10, 09-11,
09-14 and 09-21 carrying an earlier session's close under their own date).

Standard library only and no network, so they run anywhere the pipeline's own
module imports. Self-contained on purpose: nothing here depends on other test
files or shared fixtures.

Run with: python -m unittest discover tests
"""
import csv
import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import lambda_function as lf  # noqa: E402


# The pipeline reads the wall clock in several places (the 10-day cutoff in
# derive_from_price_history, file ages). Pin it to the evening of the incident so
# these tests mean the same thing whenever they run.
FROZEN_UTC = datetime(2026, 9, 22, 0, 34, tzinfo=timezone.utc)


class _FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return FROZEN_UTC.astimezone(tz) if tz else FROZEN_UTC.replace(tzinfo=None)


def _iso_hours_ago(hours):
    return (FROZEN_UTC - timedelta(hours=hours)).isoformat(timespec="seconds")


def _weekday_closes(last_day, n=30, start_price=100.0):
    """n weekday closes ending on last_day (YYYY-MM-DD), rising by 1 a day."""
    d = date.fromisoformat(last_day)
    days = []
    while len(days) < n:
        if d.weekday() < 5:
            days.append(d.isoformat())
        d -= timedelta(days=1)
    days.reverse()
    return [[day, round(start_price + i, 4)] for i, day in enumerate(days)]


def _et(y, mo, d, h, mi):
    """An Eastern wall-clock time."""
    return datetime(y, mo, d, h, mi, tzinfo=lf.EASTERN)


class LastExpectedSessionTest(unittest.TestCase):
    def test_after_the_close_on_monday_is_monday(self):
        # 00:34Z on the 22nd is 20:34 ET on Monday the 21st: the bad price pass.
        now = datetime(2026, 9, 22, 0, 34, tzinfo=timezone.utc).astimezone(lf.EASTERN)
        self.assertEqual(lf._last_expected_session(now), "2026-09-21")

    def test_monday_before_the_settle_margin_is_friday(self):
        # 20:00Z is 16:00 ET, inside the margin after the close.
        now = datetime(2026, 9, 21, 20, 0, tzinfo=timezone.utc).astimezone(lf.EASTERN)
        self.assertEqual(lf._last_expected_session(now), "2026-09-18")


class PriceFileNeedsFetchTest(unittest.TestCase):
    EXPECTED = "2026-09-21"

    def setUp(self):
        p = mock.patch.object(lf, "datetime", _FrozenDatetime)
        p.start()
        self.addCleanup(p.stop)

    def _file(self, hours_ago, last_day):
        return {"updated": _iso_hours_ago(hours_ago), "closes": _weekday_closes(last_day)}

    def test_missing_or_unreadable_file_is_fetched(self):
        self.assertTrue(lf._price_file_needs_fetch(None, self.EXPECTED, 24, False))

    def test_file_older_than_max_age_is_fetched(self):
        data = self._file(25, self.EXPECTED)
        self.assertTrue(lf._price_file_needs_fetch(data, self.EXPECTED, 24, False))

    def test_file_with_the_expected_bar_is_kept(self):
        data = self._file(1, self.EXPECTED)
        self.assertFalse(lf._price_file_needs_fetch(data, self.EXPECTED, 24, True))
        self.assertFalse(lf._price_file_needs_fetch(data, self.EXPECTED, 24, False))

    def test_lagging_file_is_retried_after_short_floor_when_session_confirmed(self):
        # The 2026-09-21 case: fetched an hour ago, ends on the Friday, and the
        # session is known to have traded.
        data = self._file(1, "2026-09-18")
        self.assertTrue(lf._price_file_needs_fetch(data, self.EXPECTED, 24, True))

    def test_lagging_file_is_held_under_the_short_floor(self):
        data = self._file(0.25, "2026-09-18")
        self.assertFalse(lf._price_file_needs_fetch(data, self.EXPECTED, 24, True))

    def test_holiday_floor_still_holds_when_unconfirmed(self):
        data = self._file(1, "2026-09-18")
        self.assertFalse(lf._price_file_needs_fetch(data, self.EXPECTED, 24, False))
        data = self._file(lf._PRICE_RECHECK_FLOOR_H + 1, "2026-09-18")
        self.assertTrue(lf._price_file_needs_fetch(data, self.EXPECTED, 24, False))


class PanelReproTest(unittest.TestCase):
    """A series ending 2026-09-18 must not be recorded as 2026-09-21's close."""

    SESSION = "2026-09-21"

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        prices = self.tmp / "prices"
        fund = self.tmp / "fundamentals"
        prices.mkdir()
        fund.mkdir()
        for p in (mock.patch.object(lf, "PRICES_DIR", prices),
                  mock.patch.object(lf, "FUNDAMENTALS_CSV_DIR", fund),
                  mock.patch.object(lf, "datetime", _FrozenDatetime)):
            p.start()
            self.addCleanup(p.stop)
        self.prices, self.fund = prices, fund

    def _write_prices(self, ticker, last_day):
        closes = _weekday_closes(last_day)
        (self.prices / f"{ticker}.json").write_text(json.dumps(
            {"ticker": ticker, "updated": FROZEN_UTC.isoformat(timespec="seconds"),
             "closes": closes}), encoding="utf-8")
        return closes

    def _record(self, stocks):
        lf.derive_from_price_history(stocks)
        rows = lf._panel_rows_for_session(stocks, self.SESSION)
        lf.record_fundamentals(rows, self.SESSION)
        with (self.fund / "2026-09.csv").open(encoding="utf-8", newline="") as fh:
            return {r["ticker"]: r for r in csv.DictReader(fh)}

    def test_lagging_series_is_recorded_blank_and_flagged(self):
        self._write_prices("AAA", "2026-09-18")
        closes = self._write_prices("BBB", self.SESSION)
        # AAA has no market cap, so before the fix a withheld price would have
        # dropped the row entirely.
        stocks = [{"ticker": "AAA", "name": "A"},
                  {"ticker": "BBB", "name": "B", "market_cap": 2e9}]
        rows = self._record(stocks)

        self.assertIn("AAA", rows)
        a = rows["AAA"]
        self.assertEqual(a["date"], self.SESSION)
        self.assertEqual(a["price"], "")
        self.assertEqual(a["change_pct"], "")
        self.assertEqual(a["return_1m"], "")
        self.assertEqual(a["price_stale"], "1")
        self.assertEqual(a["price_date"], "2026-09-18")

        b = rows["BBB"]
        self.assertEqual(float(b["price"]), closes[-1][1])
        self.assertEqual(b["price_date"], self.SESSION)
        self.assertEqual(b["price_stale"], "")

        # The site and the universe cache keep the latest close there is.
        self.assertEqual(stocks[0]["price"], _weekday_closes("2026-09-18")[-1][1])
        self.assertNotIn("price_stale", stocks[0])

    def test_stock_without_a_stored_series_is_left_alone(self):
        # A price from Yahoo's live quote has no price_date and is not judged.
        stocks = [{"ticker": "CCC", "price": 12.5, "market_cap": 1e9}]
        rows = lf._panel_rows_for_session(stocks, self.SESSION)
        self.assertEqual(rows[0]["price"], 12.5)
        self.assertNotIn("price_stale", rows[0])


class PanelGateTest(unittest.TestCase):
    SESSION = "2026-09-21"

    def _universe(self, n, lagging):
        return ([{"ticker": f"L{i}", "price": 1.0, "price_date": "2026-09-18"}
                 for i in range(lagging)]
                + [{"ticker": f"C{i}", "price": 1.0, "price_date": self.SESSION}
                   for i in range(n - lagging)]
                + [{"ticker": "NOPRICE"}])

    def test_heavy_lag_early_in_the_evening_defers(self):
        stocks = self._universe(100, 85)
        verdict, lagging, dated = lf._panel_gate(stocks, self.SESSION, True,
                                                 _et(2026, 9, 21, 20, 34))
        self.assertEqual((verdict, lagging, dated), ("defer", 85, 100))

    def test_heavy_lag_late_writes_with_stale_rows_withheld(self):
        stocks = self._universe(100, 85)
        verdict, _, _ = lf._panel_gate(stocks, self.SESSION, True,
                                       _et(2026, 9, 21, 23, 10))
        self.assertEqual(verdict, "write")
        rows = lf._panel_rows_for_session(stocks, self.SESSION)
        stale = [r for r in rows if r.get("price_stale") == 1]
        self.assertEqual(len(stale), 85)
        self.assertTrue(all("price" not in r for r in stale))

    def test_ordinary_evening_writes(self):
        # About 3% of names (illiquid listings) lag on a normal day.
        stocks = self._universe(100, 3)
        verdict, _, _ = lf._panel_gate(stocks, self.SESSION, True,
                                       _et(2026, 9, 21, 18, 30))
        self.assertEqual(verdict, "write")

    def test_unconfirmed_session_with_every_row_lagging_is_a_holiday(self):
        stocks = self._universe(100, 100)
        for hour in (18, 23):
            verdict, _, _ = lf._panel_gate(stocks, self.SESSION, False,
                                           _et(2026, 9, 21, hour, 30))
            self.assertEqual(verdict, "holiday")

    def test_confirmed_session_with_every_row_lagging_defers_not_holiday(self):
        stocks = self._universe(100, 100)
        verdict, _, _ = lf._panel_gate(stocks, self.SESSION, True,
                                       _et(2026, 9, 21, 20, 34))
        self.assertEqual(verdict, "defer")


if __name__ == "__main__":
    unittest.main()
