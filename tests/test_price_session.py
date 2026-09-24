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


def _weekdays(first, last, skip=()):
    d, out = date.fromisoformat(first), []
    while d <= date.fromisoformat(last):
        if d.weekday() < 5 and d.isoformat() not in skip:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


class MergePriceSeriesTest(unittest.TestCase):
    """The 2026-09-23 download dropped the 09-22 bar and the file was replaced
    with it. A bar Yahoo returned before must survive a response that omits it."""

    def _stored(self, dates, base=100.0, vol=True):
        blob = {"closes": [[d, round(base + i, 4)] for i, d in enumerate(dates)]}
        if vol:
            blob["volumes"] = [1000 + i for i in range(len(dates))]
        return blob

    def test_bar_missing_from_the_fresh_series_is_kept_as_stored(self):
        days = _weekdays("2026-09-14", "2026-09-23")
        stored = self._stored(days[:-1])                        # through 09-22
        fresh_days = [d for d in days if d != "2026-09-22"]      # 09-23 arrives, 09-22 dropped
        closes = [[d, round(100.0 + days.index(d), 4)] for d in fresh_days]
        volumes = [2000 + i for i in range(len(closes))]
        out, vols, kept = lf._merge_price_series(closes, volumes, stored)
        self.assertEqual(kept, 1)
        self.assertEqual([c[0] for c in out], days)
        k = days.index("2026-09-22")
        self.assertEqual(out[k], stored["closes"][k])             # the stored close, untouched
        self.assertEqual(vols[k], stored["volumes"][k])           # and its own volume
        self.assertEqual(len(vols), len(out))
        self.assertEqual(vols[-1], volumes[-1])                   # fresh volumes stay aligned

    def test_nothing_is_kept_when_the_basis_changed(self):
        # After a split or dividend Yahoo re-adjusts the whole history. A stored
        # bar from before would sit on a different basis from its neighbours.
        days = _weekdays("2026-09-14", "2026-09-23")
        stored = self._stored(days[:-1])
        closes = [[d, round((100.0 + days.index(d)) / 2, 4)] for d in days if d != "2026-09-22"]
        out, _, kept = lf._merge_price_series(closes, None, stored)
        self.assertEqual(kept, 0)
        self.assertNotIn("2026-09-22", [c[0] for c in out])

    def test_bars_outside_the_fresh_window_are_not_kept(self):
        stored = self._stored(_weekdays("2026-09-01", "2026-09-23"))
        closes = [[d, 100.0] for d in _weekdays("2026-09-10", "2026-09-18")]
        out, _, kept = lf._merge_price_series(closes, None, stored)
        self.assertEqual(kept, 0)
        self.assertEqual(out, closes)

    def test_no_stored_file_returns_the_fresh_series(self):
        closes = [["2026-09-21", 1.0], ["2026-09-23", 2.0]]
        self.assertEqual(lf._merge_price_series(closes, [5, 6], None), (closes, [5, 6], 0))

    def test_kept_bar_without_a_stored_volume_gets_none(self):
        days = _weekdays("2026-09-14", "2026-09-23")
        stored = self._stored(days, vol=False)
        closes = [c for c in stored["closes"] if c[0] != "2026-09-17"]
        out, vols, kept = lf._merge_price_series(closes, [7] * len(closes), stored)
        self.assertEqual(kept, 1)
        self.assertIsNone(vols[days.index("2026-09-17")])


class _FakeStamp:
    def __init__(self, day):
        self.day = day

    def strftime(self, fmt):
        return self.day

    def __hash__(self):
        return hash(self.day)

    def __eq__(self, other):
        return isinstance(other, _FakeStamp) and other.day == self.day


class _FakeSeries:
    """The few pandas Series operations enrich_with_prices uses."""

    def __init__(self, pairs):
        self.pairs = [(_FakeStamp(d), v) for d, v in pairs]
        self.empty = not pairs

    def dropna(self):
        return self

    def items(self):
        return iter(self.pairs)

    def get(self, idx):
        for k, v in self.pairs:
            if k == idx:
                return v
        return None


class _FakeFrame:
    def __init__(self, closes, volumes):
        self.cols = {"Close": _FakeSeries(closes), "Volume": _FakeSeries(volumes)}
        self.columns = list(self.cols)
        self.empty = False

    def __getitem__(self, k):
        return self.cols[k]


class EnrichWithPricesMergeTest(unittest.TestCase):
    """The price writer keeps the stored bar end to end, through a fake download."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        for p in (mock.patch.object(lf, "PRICES_DIR", self.tmp),
                  mock.patch.object(lf, "datetime", _FrozenDatetime)):
            p.start()
            self.addCleanup(p.stop)

    def test_download_without_a_stored_bar_keeps_it(self):
        # The frozen clock is 20:34 ET on 09-21, so the expected session is 09-21.
        days = _weekdays("2026-08-24", "2026-09-21")
        stored_days = days[:-1]
        (self.tmp / "CF.json").write_text(json.dumps({
            "ticker": "CF", "updated": _iso_hours_ago(30),
            "closes": [[d, 100.0 + i] for i, d in enumerate(stored_days)],
            "volumes": [500 + i for i in range(len(stored_days))]}), encoding="utf-8")
        dropped = "2026-09-16"
        fresh = [(d, 100.0 + i) for i, d in enumerate(days) if d != dropped]
        frame = _FakeFrame(fresh, [(d, 900) for d, _ in fresh])
        fake_yf = mock.MagicMock()
        fake_yf.download.return_value = frame
        with mock.patch.dict(sys.modules, {"yfinance": fake_yf}), \
                mock.patch("builtins.print"):
            n = lf.enrich_with_prices([{"ticker": "CF"}])
        self.assertEqual(n, 1)
        blob = json.loads((self.tmp / "CF.json").read_text(encoding="utf-8"))
        got = [c[0] for c in blob["closes"]]
        self.assertEqual(got, days)
        k = days.index(dropped)
        self.assertEqual(blob["closes"][k], [dropped, 100.0 + k])
        self.assertEqual(blob["volumes"][k], 500 + k)             # the stored volume
        self.assertEqual(blob["volumes"][-1], 900)                # the fresh one


class ConsecutiveSessionTest(unittest.TestCase):
    """change_pct only from two consecutive sessions. On 2026-09-23 CF's series
    went 09-21 (123.27) then 09-23 (120.64): -2.13% published as one day's move."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        frozen = datetime(2026, 9, 24, 0, 30, tzinfo=timezone.utc)

        class _Frozen(datetime):
            @classmethod
            def now(cls, tz=None):
                return frozen.astimezone(tz) if tz else frozen.replace(tzinfo=None)

        for p in (mock.patch.object(lf, "PRICES_DIR", self.tmp),
                  mock.patch.object(lf, "datetime", _Frozen)):
            p.start()
            self.addCleanup(p.stop)

    def _write(self, ticker, days, last=None):
        closes = [[d, 100.0 + i] for i, d in enumerate(days)]
        if last is not None:
            closes[-2][1], closes[-1][1] = last
        (self.tmp / f"{ticker}.json").write_text(json.dumps(
            {"ticker": ticker, "closes": closes}), encoding="utf-8")

    def _derive(self, stocks):
        with mock.patch("builtins.print"):
            lf.derive_from_price_history(stocks)
        return {s["ticker"]: s for s in stocks}

    def test_change_across_a_missing_session_is_blank_and_flagged(self):
        full = _weekdays("2026-08-24", "2026-09-23")
        holed = [d for d in full if d != "2026-09-22"]
        self._write("CF", holed, last=(123.27, 120.64))
        self._write("SPY", full)                      # 09-22 traded: another series has it
        got = self._derive([{"ticker": "CF", "change_pct": -2.1, "change_gap": 0},
                            {"ticker": "SPY"}])
        cf = got["CF"]
        self.assertEqual(cf["price"], 120.64)
        self.assertNotIn("change_pct", cf)            # an older value is not left behind
        self.assertEqual(cf["change_gap"], 1)
        self.assertEqual(cf["status"]["change_pct"], "gap")
        self.assertIn("gap", lf.FIELD_STATUS)
        self.assertIn("change_pct", got["SPY"])
        self.assertNotIn("change_gap", got["SPY"])

    def test_holiday_is_not_a_missing_session(self):
        # A weekday no series has a bar for is a holiday, so Friday to Tuesday is one step.
        days = _weekdays("2026-08-17", "2026-09-15", skip=("2026-09-14",))
        self._write("AAA", days, last=(50.0, 51.0))
        self._write("BBB", days)
        got = self._derive([{"ticker": "AAA"}, {"ticker": "BBB"}])
        self.assertAlmostEqual(got["AAA"]["change_pct"], 2.0)
        self.assertNotIn("change_gap", got["AAA"])

    def test_session_calendar_needs_more_than_one_stray_bar(self):
        lists = [["2026-09-04", "2026-09-08"]] * 999 + [["2026-09-04", "2026-09-07", "2026-09-08"]]
        cal = lf._session_calendar(lists)
        self.assertNotIn("2026-09-07", cal)
        self.assertTrue(lf._follows_previous_session("2026-09-04", "2026-09-08", cal))
        cal = lf._session_calendar(lists, ["2026-09-07"])       # the benchmark traded it
        self.assertFalse(lf._follows_previous_session("2026-09-04", "2026-09-08", cal))

    def test_stale_row_drops_the_gap_flag_with_the_change(self):
        rows = lf._panel_rows_for_session(
            [{"ticker": "X", "price": 1.0, "price_date": "2026-09-21", "change_gap": 1}],
            "2026-09-23")
        self.assertEqual(rows[0]["price_stale"], 1)
        self.assertNotIn("change_gap", rows[0])


class FlagChangeGapTest(unittest.TestCase):
    """tools/flag_change_gap.py, on CF's real 2026-09 figures."""

    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
        import flag_change_gap
        cls.tool = flag_change_gap

    HEADER = ["date", "ticker", "price", "change_pct", "price_date", "price_stale"]

    def rows(self, p23="120.64", c23="-2.133528"):
        return [
            {"date": "2026-09-22", "ticker": "CF", "price": "120.59", "change_pct": "-2.174089",
             "price_date": "", "price_stale": ""},
            {"date": "2026-09-23", "ticker": "CF", "price": p23, "change_pct": c23,
             "price_date": "2026-09-23", "price_stale": ""},
        ]

    HIST = {"CF": {"2026-09-18": 127.7, "2026-09-21": 123.27, "2026-09-23": 120.64}}

    def test_two_day_change_is_flagged_and_nothing_else_changes(self):
        rows = self.rows()
        cols, out, counts = self.tool.flag_rows(rows, list(self.HEADER), self.HIST)
        self.assertEqual(cols, self.HEADER + ["change_gap"])
        self.assertEqual(out[1]["change_gap"], "1")
        self.assertEqual(out[0]["change_gap"], "")
        self.assertEqual(out[1]["change_pct"], "-2.133528")      # the recorded value stays
        self.assertEqual(counts["gap"], 1)
        # Idempotent.
        _, again, counts2 = self.tool.flag_rows(out, cols, self.HIST)
        self.assertEqual(again, out)
        self.assertEqual(counts2["already_flagged"], 1)

    def test_true_one_day_change_is_not_flagged(self):
        c = f"{(120.64 / 120.59 - 1) * 100:.6f}"
        hist = {"CF": dict(self.HIST["CF"], **{"2026-09-22": 120.59})}
        _, out, counts = self.tool.flag_rows(self.rows(c23=c), list(self.HEADER), hist)
        self.assertEqual(out[1]["change_gap"], "")
        self.assertEqual(counts["current"], 1)

    def test_flat_day_is_a_tie_not_a_flag(self):
        # The stored 09-21 close equals the 09-22 panel price, so the two-session
        # and one-session formulas give the same figure.
        hist = {"CF": {"2026-09-21": 120.59, "2026-09-23": 120.64}}
        c = f"{(120.64 / 120.59 - 1) * 100:.6f}"
        _, out, counts = self.tool.flag_rows(self.rows(c23=c), list(self.HEADER), hist)
        self.assertEqual(out[1]["change_gap"], "")
        self.assertEqual(counts["tie"], 1)

    def test_row_without_a_series_price_is_not_examined(self):
        rows = self.rows()
        rows[1]["price_date"] = ""
        _, out, counts = self.tool.flag_rows(rows, list(self.HEADER), self.HIST)
        self.assertEqual(out[1]["change_gap"], "")
        self.assertEqual(counts["not_examined"], 1)


if __name__ == "__main__":
    unittest.main()
