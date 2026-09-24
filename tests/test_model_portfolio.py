"""The model portfolios: classification, history, the ledger, seeding, NAV, shorts,
PM orders, benchmarks, pages.

Offline. Every file the engine reads or writes is redirected into a temporary
directory; the committed ledger is only read, to check its own consistency.
"""
import csv
import json
import os
import re
import sys
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests import helpers as H  # noqa: E402

LF = H.LF
from portfolio import engine as E  # noqa: E402
from portfolio.bin import trade as TRADE  # noqa: E402

EN_DASH = chr(0x2013)
DAY = "2026-09-23"
PANEL_COLS = ["date", "ticker", "name", "sector", "security_type", "price", "volume",
              "market_cap", "pe", "price_book", "ttm_revenue", "ttm_eps_diluted", "roe_ttm",
              "earnings_consistency", "net_debt_ebitda", "op_margin_stability", "accruals_ratio",
              "eps_basis"]
SECTORS = ["Energy", "Industrials", "Financials", "Health Care", "Utilities"]


def synthetic_rows(n=300, day=DAY):
    """n operating companies with caps falling geometrically, plus a note, a share
    class twin of T000, a company with no history and two foreign listings."""
    rows = []
    for i in range(n):
        v = ((i * 53) % 97) / 100.0
        cap = 1e12 * (0.97 ** i)
        rows.append({"date": day, "ticker": f"T{i:03d}", "name": f"Company {i}",
                     "sector": SECTORS[i % 5], "security_type": "operating",
                     "price": 10 + i % 50, "volume": 1000 + i, "market_cap": cap,
                     "pe": 8 + 30 * (1 - v), "price_book": 1 + 5 * (1 - v),
                     "ttm_revenue": cap * (0.2 + v), "ttm_eps_diluted": 1, "roe_ttm": 0.1,
                     "earnings_consistency": 0.5, "net_debt_ebitda": 1,
                     "op_margin_stability": 0.1, "accruals_ratio": 0.02})
    rows.append(dict(rows[0], ticker="T000B", name="Company 0 (Class B)", volume=1))
    rows.append({"date": day, "ticker": "NOTE", "name": "Company 1 Notes", "security_type": "debt",
                 "market_cap": 5e12})
    rows.append(dict(rows[5], ticker="NOHIST", name="No History Co",
                     market_cap=rows[5]["market_cap"] * 0.999))
    rows.append(dict(rows[6], ticker="ADRX", name="Example Ltd American Depositary Shares"))
    rows.append(dict(rows[7], ticker="FGN", name="Foreign Filer Co", eps_basis="annual"))
    rows.append(dict(rows[8], ticker="FGF", name="Form Twenty Co"))
    return rows


def synthetic_history(rows, fy_end="2025-12-31"):
    out = {}
    for r in rows:
        t = r["ticker"]
        if t in ("NOHIST", "NOTE") or not t.startswith(("T", "F", "A")):
            continue
        i = int(t[1:4]) if t[1:4].isdigit() else 7
        g = ((i * 37) % 101) / 100.0          # scrambled, so style is not size
        out[t] = {"ticker": t, "fy_end": fy_end, "annual_form": "20-F" if t == "FGF" else "10-K",
                  "sales_ps_growth_3y": g * 0.3, "eps_growth_3y": g * 0.5 if i % 7 else "",
                  "ocf": r.get("market_cap", 0) * 0.05 * (1 + (i % 9) / 10.0)}
    return out


def write_panel(panel_dir, rows):
    panel_dir.mkdir(parents=True, exist_ok=True)
    path = panel_dir / (rows[0]["date"][:7] + ".csv")
    new = not path.exists()
    with path.open("a", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=PANEL_COLS, extrasaction="ignore")
        if new:
            w.writeheader()
        for r in rows:
            w.writerow(r)


def write_prices(prices_dir, closes_by_ticker):
    prices_dir.mkdir(parents=True, exist_ok=True)
    for t, closes in closes_by_ticker.items():
        H.write_price_file(prices_dir, t, [d for d, _ in closes], [c for _, c in closes])


def write_history(path, hist):
    rows = [{k: h.get(k, "") for k in E.STYLE_HISTORY_COLUMNS} for h in hist.values()]
    E.append_rows(path, E.STYLE_HISTORY_COLUMNS, rows)


# The synthetic panel has about 300 companies, so the tests rank them into smaller
# groups; the production cuts (200, 1,000, 3,000) are checked on their own.
REAL_SIZE_RANKS = E.SIZE_RANKS


def setUpModule():
    E.SIZE_RANKS = (("large", 60), ("mid", 150), ("small", 280))


def tearDownModule():
    E.SIZE_RANKS = REAL_SIZE_RANKS


class Books(unittest.TestCase):

    def test_eight_live_books_and_reserved_ids(self):
        self.assertEqual([b["id"] for b in E.LIVE_BOOKS],
                         ["lg-growth", "lg-value", "mid-growth", "mid-value", "sm-growth",
                          "sm-value", "hedge", "neural"])
        planned = {b["id"] for b in E.PLANNED_BOOKS}
        self.assertTrue({"lg-core", "mid-core", "sm-core", "tax-core", "momentum"} <= planned)
        self.assertEqual(REAL_SIZE_RANKS, (("large", 200), ("mid", 1000), ("small", 3000)))
        self.assertEqual(E.STYLE_BENCHMARK[("large", "growth")], "IWY")
        self.assertEqual(E.STYLE_BENCHMARK[("small", "value")], "IWN")
        for sym in set(E.STYLE_BENCHMARK.values()) | {"IWF", "IWD", "IWV"}:
            self.assertIn(sym, E.BENCHMARK_SYMBOLS)
        h = E.default_mandate("hedge")
        self.assertEqual((h["gross_max"], h["net_range"], h["max_long_position"],
                          h["max_short_position"]), (2.0, [-0.2, 0.6], 0.05, 0.03))
        self.assertEqual(E.default_mandate("neural")["gross_max"], 2.0)
        self.assertTrue(E.default_mandate("lg-value")["long_only"])


class Classification(unittest.TestCase):

    def classify(self, rows=None, hist=None):
        rows = rows or synthetic_rows()
        return E.classify(rows, hist if hist is not None else synthetic_history(rows), DAY)

    def test_robust_z_matches_the_page_engine(self):
        vals = [0.3, -1.2, 4.0, 0.0, 2.5, 2.5, 7.1, -0.4, 1.1, 90.0]
        c, s = E.robust_stat(vals)
        ref = LF._ledger_stat(vals)
        self.assertAlmostEqual(c, ref["center"])
        self.assertAlmostEqual(s, ref["scale"])
        z = E.robust_z({i: v for i, v in enumerate(vals)}, min_n=5)
        self.assertEqual(z[9], 5.0, "clipped at +5")
        self.assertIsNone(E.robust_z({1: 1.0, 2: 2.0}, min_n=5)[1])

    def test_size_follows_cap_rank_after_folding_and_foreign_exclusion(self):
        rows = synthetic_rows()
        c = self.classify(rows)
        self.assertEqual(c["NOTE"]["why"], "not an operating company")
        self.assertEqual(c["T000B"]["share_class_of"], "T000")
        for t, why in (("ADRX", "depositary"), ("FGN", "20-F"), ("FGF", "20-F")):
            self.assertIsNone(c[t]["size"], t)
            self.assertIn(why, c[t]["why"])
        caps = sorted(((float(r["market_cap"]), r["ticker"]) for r in rows
                       if r["security_type"] == "operating"
                       and r["ticker"] not in ("T000B", "ADRX", "FGN", "FGF")),
                      key=lambda x: (-x[0], x[1]))
        for rank, (cap, t) in enumerate(caps, 1):
            want = "large" if rank <= 60 else "mid" if rank <= 150 else "small" if rank <= 280 else "micro"
            self.assertEqual(c[t]["size"], want, t)

    def test_median_split_puts_every_scored_name_in_one_box(self):
        c = self.classify()
        self.assertIsNone(c["NOHIST"]["box"])
        self.assertIn("history", c["NOHIST"]["why"])
        self.assertIsNotNone(c["NOHIST"]["size"], "no history still counts for the size rank")
        for size in ("large", "mid", "small"):
            scored = sorted((i for i in c.values() if i["size"] == size and i["style"]),
                            key=lambda i: (i["style_score"], i["ticker"]))
            n = len(scored)
            with self.subTest(size=size):
                self.assertGreaterEqual(n, E.MIN_COHORT)
                self.assertEqual([i["style"] for i in scored],
                                 ["value"] * (n - n // 2) + ["growth"] * (n // 2))
                for i in scored:
                    self.assertAlmostEqual(i["style_score"], i["growth"] - i["value"])

    def test_input_minimums_and_stale_history(self):
        rows = synthetic_rows()
        hist = synthetic_history(rows)
        hist["T010"].update(sales_ps_growth_3y="", eps_growth_3y="")
        hist["T011"].update(sales_ps_growth_3y="")          # one growth input is enough
        hist["T012"]["fy_end"] = "2024-03-31"                # older than 15 months
        rows = [dict(r, pe="", price_book="", ttm_eps_diluted="", ttm_revenue="")
                if r["ticker"] == "T013" else r for r in rows]
        c = E.classify(rows, hist, DAY)
        self.assertIn("growth", c["T010"]["why"])
        self.assertIsNotNone(c["T011"]["box"])
        self.assertIn("history", c["T012"]["why"])
        self.assertIn("value inputs", c["T013"]["why"])

    def test_style_inputs_are_sector_neutral(self):
        rows = synthetic_rows()
        hist = synthetic_history(rows)
        for h in hist.values():      # every input present, so no sector falls back
            if h["eps_growth_3y"] == "":
                h["eps_growth_3y"] = 0.01
        boom = json.loads(json.dumps(hist))
        for r in rows:
            h = boom.get(r["ticker"])
            if h and r.get("sector") == "Energy":
                h["sales_ps_growth_3y"] = h["sales_ps_growth_3y"] + 1.0
                if h["eps_growth_3y"] != "":
                    h["eps_growth_3y"] = h["eps_growth_3y"] + 2.0
        a, b = E.classify(rows, hist, DAY), E.classify(rows, boom, DAY)
        for t, info in a.items():
            if info["growth"] is not None and info["size"] == "large":
                self.assertAlmostEqual(info["growth"], b[t]["growth"], msg=t)

    def test_candidate_respects_views_sector_cap_and_weights(self):
        c = self.classify()
        book = E.BOOKS["sm-growth"]
        mandate = dict(E.DEFAULT_STYLE_MANDATE, holdings_range=[4, 6], sector_cap=0.4)
        base = E.rules_candidate(book, c, mandate)
        self.assertEqual(len(base), 5)
        self.assertAlmostEqual(sum(p["weight"] for p in base), 1.0)
        per = {}
        for p in base:
            per[p["sector"]] = per.get(p["sector"], 0) + 1
        self.assertLessEqual(max(per.values()), 2)
        first, second = base[0]["ticker"], base[-1]["ticker"]
        views = {first: {"exclude": True, "overweight": False, "why": "analyst: avoid"},
                 second: {"exclude": False, "overweight": True, "why": "analyst: add"}}
        got = E.rules_candidate(book, c, mandate, views)
        self.assertNotIn(first, [p["ticker"] for p in got])
        heavy = [p for p in got if p["overweight"]]
        self.assertEqual([p["ticker"] for p in heavy], [second])
        self.assertAlmostEqual(heavy[0]["weight"], min(0.05, 1.5 / 5))

    def test_views_use_action_then_direction(self):
        with H.temp_dir() as tmp:
            p = tmp / "events.csv"
            p.write_text("event_id,date,ticker,kind,direction,conviction,action\n"
                         "1,2026-09-12,AAA,initiate,long,3,initiate\n"
                         "2,2026-09-12,BBB,initiate,avoid,3,\n"
                         "3,2026-09-12,CCC,initiate,long,3,exit\n"
                         "4,2026-09-12,DDD,initiate,watch,3,\n", encoding="utf-8")
            v = E.analyst_views(p)
        self.assertTrue(v["AAA"]["overweight"])
        self.assertTrue(v["BBB"]["exclude"])
        self.assertTrue(v["CCC"]["exclude"])
        self.assertNotIn("DDD", v)


def annual(end, revenue, net_income, shares, ocf=None):
    return {"period_end": end, "revenue": revenue, "net_income": net_income,
            "shares_diluted": shares, "ocf": ocf}


class History(unittest.TestCase):

    def test_three_year_growth_per_share(self):
        g = E.style_growth([annual("2021-12-31", 100, 10, 10, 5), annual("2022-12-31", 110, 11, 10),
                            annual("2023-12-31", 120, 12, 10), annual("2024-12-31", 216, 27, 12, 30)])
        self.assertEqual((g["fy_end"], g["start_fy_end"]), ("2024-12-31", "2021-12-31"))
        self.assertAlmostEqual(g["sales_ps_growth_3y"], (18 / 10) ** (1 / 3) - 1)
        self.assertAlmostEqual(g["eps_growth_3y"], (2.25 / 1.0) ** (1 / 3) - 1)
        self.assertEqual(g["ocf"], 30)

    def test_loss_split_and_short_history_leave_blanks(self):
        loss = E.style_growth([annual("2021-12-31", 100, -5, 10), annual("2024-12-31", 150, 9, 10)])
        self.assertIsNotNone(loss["sales_ps_growth_3y"])
        self.assertIsNone(loss["eps_growth_3y"])
        self.assertIn("start EPS not positive", loss["note"])
        split = E.style_growth([annual("2021-12-31", 100, 10, 10), annual("2022-12-31", 110, 11, 10),
                                annual("2023-12-31", 120, 12, 40), annual("2024-12-31", 130, 13, 40)])
        self.assertIsNone(split["sales_ps_growth_3y"])
        self.assertIsNone(split["eps_growth_3y"])
        self.assertIn("jumps", split["note"])
        short = E.style_growth([annual("2023-12-31", 100, 10, 10), annual("2024-12-31", 110, 11, 10)])
        self.assertIsNone(short["sales_ps_growth_3y"])
        self.assertIn("three years", short["note"])

    def facts(self, form="10-K"):
        def node(vals, unit="USD"):
            return {"units": {unit: [{"start": f"{y}-01-01", "end": f"{y}-12-31", "val": v,
                                      "form": form, "filed": f"{y + 1}-02-15"}
                                     for y, v in vals.items()]}}
        years = {2021: 1, 2022: 1.1, 2023: 1.2, 2024: 1.5}
        return {"us-gaap": {
            "Revenues": node({y: 1000 * k for y, k in years.items()}),
            "NetIncomeLoss": node({y: 100 * k for y, k in years.items()}),
            "WeightedAverageNumberOfDilutedSharesOutstanding": node({y: 50 for y in years}, "shares"),
            "NetCashProvidedByUsedInOperatingActivities": node({y: 120 * k for y, k in years.items()}),
            # A quarter from a 10-Q must not be read as a year.
            "EarningsPerShareDiluted": node({2024: 3.0}, "USD/shares")}}

    def test_history_from_companyfacts_and_its_record(self):
        row = LF.compute_style_history(self.facts(), "XYZ", 123)
        self.assertEqual(row["annual_form"], "10-K")
        self.assertEqual(row["fy_end"], "2024-12-31")
        self.assertAlmostEqual(row["sales_ps_growth_3y"], 1.5 ** (1 / 3) - 1)
        self.assertAlmostEqual(row["eps_growth_3y"], 1.5 ** (1 / 3) - 1)
        foreign = LF.compute_style_history(self.facts("20-F"), "FOR", 9)
        self.assertEqual(foreign["annual_form"], "20-F")
        self.assertIsNone(foreign["fy_end"], "20-F facts are not used for growth")
        with H.temp_dir() as tmp:
            path = tmp / "style_history.csv"
            with H.patched(LF, STYLE_HISTORY_CSV=path), H.quiet():
                self.assertEqual(LF.record_style_history([row, foreign], "t1"), 2)
                self.assertEqual(LF.record_style_history([row], "t2"), 0)
                self.assertEqual(LF.record_style_history([dict(row, revenue=1)], "t3"), 1)
            got = E.load_style_history(path)
        self.assertEqual(got["XYZ"]["revenue"], "1")
        self.assertEqual(got["FOR"]["annual_form"], "20-F")


def ledger_fixture(tmp, extra_days=("2026-09-24", "2026-09-25")):
    """A panel on DAY plus later days, stored closes for every operating row, and the
    three-year history on disk."""
    rows = synthetic_rows()
    write_panel(tmp / "panel", rows)
    for d in extra_days:
        write_panel(tmp / "panel", [dict(r, date=d) for r in rows])
    closes = {r["ticker"]: [(d, 20.0 + (i % 7)) for d in (DAY,) + tuple(extra_days)]
              for i, r in enumerate(rows) if r["security_type"] == "operating"}
    write_prices(tmp / "prices", closes)
    write_history(tmp / "history.csv", synthetic_history(rows))
    return rows


class Seeding(unittest.TestCase):

    def seed(self, tmp, **kw):
        kw.setdefault("history", tmp / "history.csv")
        with H.quiet():
            return E.seed(ledger_dir=tmp / "ledger", books_dir=tmp / "books", panel_dir=tmp / "panel",
                          prices=E.PriceStore(tmp / "prices"), events=tmp / "none.csv", **kw)

    def test_seed_is_idempotent(self):
        with H.temp_dir() as tmp:
            ledger_fixture(tmp)
            first = self.seed(tmp, day=DAY)
            self.assertEqual(first["date"], DAY)
            self.assertEqual(sorted(first["seeded"]), sorted(b["id"] for b in E.LIVE_BOOKS))
            snap = {p.name: p.read_bytes() for p in (tmp / "ledger").iterdir()}
            second = self.seed(tmp)
            self.assertEqual(second["seeded"], [])
            self.assertEqual(snap, {p.name: p.read_bytes() for p in (tmp / "ledger").iterdir()})
            trades = E.read_rows(tmp / "ledger" / "trades.csv")
            prices = E.PriceStore(tmp / "prices")
            for t in trades:
                if t["side"] == "buy":
                    self.assertEqual(float(t["price"]), prices.close(t["ticker"], t["date"]))
                    self.assertEqual(t["lot_id"], t["trade_id"])
            for b in E.LIVE_BOOKS:
                ts = E.trades_for(trades, b["id"])
                self.assertEqual(sum(1 for t in ts if t["side"] == "deposit"), 1)
                st = E.book_state(ts)
                self.assertEqual(st["capital"], E.INCEPTION_CAPITAL)
                if b["kind"] == "style":
                    self.assertEqual(len(st["shares"]), len(first["books"][b["id"]]))
                    self.assertGreaterEqual(len(st["shares"]), 25)
                else:
                    self.assertEqual(st["shares"], {}, "the PM builds it; it starts in cash")
                    self.assertEqual(st["cash"], E.INCEPTION_CAPITAL)
                self.assertEqual(E.mandate_history(b["id"], tmp / "ledger")[-1]["mandate"],
                                 E.load_mandate(b["id"], tmp / "books"))

    def test_style_books_wait_for_the_history_but_pm_books_start(self):
        with H.temp_dir() as tmp:
            ledger_fixture(tmp)
            out = self.seed(tmp, history={})
            self.assertEqual(sorted(out["seeded"]), ["hedge", "neural"])
            self.assertEqual(len(out["skipped"]), 6)
            again = self.seed(tmp)
            self.assertEqual(len(again["seeded"]), 6)

    def test_seed_steps_back_when_a_close_is_missing(self):
        with H.temp_dir() as tmp:
            ledger_fixture(tmp, extra_days=("2026-09-24",))
            dry = self.seed(tmp, dry_run=True)
            victim = dry["books"]["lg-value"][0]["ticker"]
            path = tmp / "prices" / E.price_filename(victim)
            blob = json.loads(path.read_text())
            blob["closes"] = [c for c in blob["closes"] if c[0] != "2026-09-24"]
            path.write_text(json.dumps(blob))
            self.assertEqual(self.seed(tmp, dry_run=True)["date"], DAY)


def trade_rows(rows):
    return [dict(zip(["trade_id", "date", "book", "ticker", "side", "shares", "price", "cost",
                      "lot_id"], map(str, r))) for r in rows]


class Nav(unittest.TestCase):

    def trades(self):
        return trade_rows([("d", DAY, "b", "CASH", "deposit", 1000, 1, 0, ""),
                           ("t1", DAY, "b", "AAA", "buy", 10, 50, 0.25, "t1"),
                           ("t2", DAY, "b", "BBB", "buy", 5, 40, 0.10, "t2"),
                           ("t3", DAY, "b", "CCC", "short", 4, 25, 0.05, "t3")])

    def test_missing_close_marks_partial_and_carries_nothing(self):
        with H.temp_dir() as tmp:
            write_prices(tmp, {"AAA": [(DAY, 50), ("2026-09-24", 55)],
                               "BBB": [(DAY, 40)],
                               "CCC": [(DAY, 25), ("2026-09-24", 20)]})
            prices = E.PriceStore(tmp)
            v0 = E.value_book(self.trades(), "b", DAY, prices)
            self.assertFalse(v0["partial"])
            cash = 1000 - 500.25 - 200.10 + 100 - 0.05
            self.assertAlmostEqual(v0["cash"], cash)
            self.assertAlmostEqual(v0["nav"], cash + 500 + 200 - 100)
            v1 = E.value_book(self.trades(), "b", "2026-09-24", prices)
            self.assertTrue(v1["partial"])
            self.assertIsNone(v1["nav"])
            self.assertIsNone(v1["gross"])
            self.assertEqual(v1["missing"], ["BBB"])
            self.assertAlmostEqual(v1["priced_nav"], cash + 550 - 80)

    def test_short_accounting(self):
        """Proceeds credited to cash, the position negative, a gain when the price
        falls, and gross and net exposure from both sides."""
        ts = trade_rows([("d", DAY, "h", "CASH", "deposit", 1000, 1, 0, ""),
                         ("s1", DAY, "h", "SSS", "short", 10, 30, 0.15, "s1"),
                         ("b1", DAY, "h", "LLL", "buy", 10, 50, 0.25, "b1")])
        with H.temp_dir() as tmp:
            write_prices(tmp, {"SSS": [(DAY, 30), ("2026-09-24", 24)],
                               "LLL": [(DAY, 50), ("2026-09-24", 50)]})
            p = E.PriceStore(tmp)
            st = E.book_state(ts)
            self.assertAlmostEqual(st["cash"], 1000 + 300 - 0.15 - 500 - 0.25)
            self.assertEqual(st["shares"]["SSS"], -10)
            v0 = E.value_book(ts, "h", DAY, p)
            v1 = E.value_book(ts, "h", "2026-09-24", p)
            self.assertAlmostEqual(v1["nav"] - v0["nav"], 60.0, msg="short gains 6 x 10")
            short = next(m for m in v1["marks"] if m["ticker"] == "SSS")
            self.assertEqual(short["side"], "short")
            self.assertAlmostEqual(short["value"], -240)
            self.assertAlmostEqual(short["ret"], 0.2)
            self.assertAlmostEqual(v1["gross"], (500 + 240) / v1["nav"])
            self.assertAlmostEqual(v1["net"], (500 - 240) / v1["nav"])
            closing = ts + trade_rows([("c1", "2026-09-24", "h", "SSS", "cover", 10, 24, 0.12, "")])
            _, closed = E.derive_lots(closing)
            self.assertAlmostEqual(closed[0]["gain"], 60.0)
            self.assertNotIn("SSS", E.book_state(closing)["shares"])

    def test_basis_break_is_not_valued(self):
        with H.temp_dir() as tmp:
            write_prices(tmp, {"AAA": [(DAY, 25), ("2026-09-24", 26)], "BBB": [(DAY, 40), ("2026-09-24", 40)],
                               "CCC": [(DAY, 25), ("2026-09-24", 25)]})
            v = E.value_book(self.trades(), "b", "2026-09-24", E.PriceStore(tmp))
            self.assertEqual(v["basis_break"], ["AAA"])
            self.assertTrue(v["partial"])

    def test_record_nav_appends_once_and_supersedes_partial(self):
        with H.temp_dir() as tmp:
            write_prices(tmp, {"AAA": [(DAY, 50)], "BBB": [(DAY, 40)], "CCC": [(DAY, 25)]})
            trades = [dict(t, book="lg-value") for t in self.trades()]
            nav = tmp / "nav.csv"
            bench = E.BenchmarkStore(tmp)
            self.assertEqual(E.record_nav(trades, [DAY], E.PriceStore(tmp), bench, nav, now="x"), 1)
            self.assertEqual(E.record_nav(trades, [DAY], E.PriceStore(tmp), bench, nav, now="y"), 0)
            (tmp / E.BENCHMARKS_FILE).write_text(json.dumps(
                {"updated": "z", "series": {"IWX": {"closes": [[DAY, 300.5]]}}}))
            self.assertEqual(E.record_nav(trades, [DAY], E.PriceStore(tmp), E.BenchmarkStore(tmp), nav, now="z"), 1)
            self.assertEqual(E.record_nav(trades, [DAY], E.PriceStore(tmp), E.BenchmarkStore(tmp), nav, now="w"), 0)
            rows = E.read_rows(nav)
            self.assertEqual(len(rows), 2)
            self.assertEqual(E.latest_nav_rows(rows)[("lg-value", DAY)]["benchmark_close"], "300.5")

    def test_cash_return_uses_each_session_rate(self):
        with H.temp_dir() as tmp:
            H.write_market_file(tmp, [DAY, "2026-09-24", "2026-09-25"], [1, 1, 1], rf_percent=5.04)
            b = E.BenchmarkStore(tmp)
            got = E.cash_return(DAY, "2026-09-25", [DAY, "2026-09-24", "2026-09-25"], b)
            self.assertAlmostEqual(got, (1 + 0.0504 / 252) ** 2 - 1)
            self.assertIsNone(E.cash_return(DAY, "2026-09-28", [DAY, "2026-09-28"], b))

    def test_lots_fifo_and_named(self):
        t = [{"trade_id": "a", "date": "2026-01-02", "ticker": "X", "side": "buy", "shares": "10", "price": "10"},
             {"trade_id": "b", "date": "2026-02-02", "ticker": "X", "side": "buy", "shares": "10", "price": "20"},
             {"trade_id": "c", "date": "2026-03-02", "ticker": "X", "side": "sell", "shares": "5", "price": "30"},
             {"trade_id": "d", "date": "2026-03-03", "ticker": "X", "side": "sell", "shares": "5", "price": "30",
              "lot_id": "b"}]
        open_lots, closed = E.derive_lots(t)
        self.assertEqual([(c["lot_id"], c["gain"]) for c in closed], [("a", 100.0), ("b", 50.0)])
        self.assertEqual({l["lot_id"]: l["shares"] for l in open_lots}, {"a": 5.0, "b": 5.0})
        with self.assertRaises(ValueError):
            E.derive_lots(t + [{"trade_id": "e", "date": "2026-03-04", "ticker": "X", "side": "sell",
                                "shares": "11", "price": "1"}])


class Orders(unittest.TestCase):
    """portfolio/bin/trade.py against a seeded temporary ledger."""

    def setUp(self):
        self.ctx = H.temp_dir()
        self.tmp = self.ctx.__enter__()
        self.rows = ledger_fixture(self.tmp)
        with H.quiet():
            E.seed(ledger_dir=self.tmp / "ledger", books_dir=self.tmp / "books",
                   panel_dir=self.tmp / "panel", prices=E.PriceStore(self.tmp / "prices"),
                   events=self.tmp / "none.csv", history=self.tmp / "history.csv", day=DAY)

    def tearDown(self):
        self.ctx.__exit__(None, None, None)

    def run_batch(self, batch, write=False):
        return TRADE.run([batch], write=write, prices=E.PriceStore(self.tmp / "prices"),
                         ledger_dir=self.tmp / "ledger", books_dir=self.tmp / "books",
                         panel_dir=self.tmp / "panel", history=self.tmp / "history.csv",
                         out=lambda *a: None)

    def batch(self, orders, book="hedge", day="2026-09-24", batch_id="w1", **kw):
        return dict({"book": book, "date": day, "batch_id": batch_id, "reason": "Test.",
                     "orders": orders}, **kw)

    def refused(self, batch, needle):
        with self.assertRaises(E.OrderError) as cm:
            self.run_batch(batch)
        self.assertTrue(any(needle in e for e in cm.exception.errors), cm.exception.errors)

    def test_hedge_long_short_batch_dry_run_write_and_idempotence(self):
        b = self.batch([{"id": "1", "ticker": "T100", "side": "buy", "weight": 0.04},
                        {"id": "2", "ticker": "T101", "side": "short", "weight": 0.02}])
        before = (self.tmp / "ledger" / "trades.csv").read_bytes()
        plans = self.run_batch(b)
        self.assertEqual(len(plans[0]["fills"]), 2)
        self.assertEqual((self.tmp / "ledger" / "trades.csv").read_bytes(), before, "dry run")
        self.run_batch(b, write=True)
        trades = E.read_rows(self.tmp / "ledger" / "trades.csv")
        mine = [t for t in trades if t["decision_id"] == "hedge-2026-09-24-w1"]
        self.assertEqual({t["side"] for t in mine}, {"buy", "short"})
        prices = E.PriceStore(self.tmp / "prices")
        for t in mine:
            self.assertEqual(float(t["price"]), prices.close(t["ticker"], "2026-09-24"))
            self.assertAlmostEqual(float(t["cost"]), round(float(t["shares"]) * float(t["price"]) * 0.0005, 2))
        again = self.run_batch(b, write=True)
        self.assertEqual(again[0]["fills"], [])
        self.assertIsNone(again[0]["decision"])
        self.assertEqual(len(E.read_rows(self.tmp / "ledger" / "trades.csv")), len(trades))
        decisions = [d for d in E.read_rows(self.tmp / "ledger" / "decisions.csv")
                     if d["decision_id"] == "hedge-2026-09-24-w1"]
        self.assertEqual(len(decisions), 1)
        self.assertEqual(decisions[0]["author"], "pm")
        v = E.value_book(trades, "hedge", "2026-09-24", prices)
        self.assertGreater(v["gross"], 0.05)
        self.assertAlmostEqual(v["net"], (v["long_value"] + v["short_value"]) / v["nav"])

    def test_refusals(self):
        self.refused(self.batch([{"id": "1", "ticker": "T100", "side": "buy", "weight": 0.2}]),
                     "long position")
        self.refused(self.batch([{"id": "1", "ticker": "T100", "side": "short", "weight": 0.05}]),
                     "short position")
        self.refused(self.batch([{"id": str(i), "ticker": f"T{100 + i}", "side": "buy", "weight": 0.05}
                                 for i in range(14)]), "net exposure")
        self.refused(self.batch([{"id": "1", "ticker": "NOTE", "side": "buy", "shares": 1}]),
                     "not an operating company")
        self.refused(self.batch([{"id": "1", "ticker": "T100", "side": "buy", "shares": 1}],
                                day="2026-09-26"), "no stored close")
        self.refused(self.batch([{"id": "1", "ticker": "T100", "side": "sell", "shares": 1}]),
                     "exceeds")
        self.refused(self.batch([{"id": "1", "ticker": "T100", "side": "buy", "shares": 1}], reason=""),
                     "reason")
        self.refused(self.batch([{"id": "1", "ticker": "T100", "side": "short", "shares": 1}],
                                book="lg-value"), "long only")
        c = E.classify(self.rows, synthetic_history(self.rows), DAY)
        outside = next(t for t, i in c.items() if i["box"] == "mid-growth")
        self.refused(self.batch([{"id": "1", "ticker": outside, "side": "buy", "shares": 1}],
                                book="lg-value"), "box")

    def test_neural_gross_limit(self):
        orders = [{"id": str(i), "ticker": f"T{100 + i}", "side": "buy", "weight": 0.2} for i in range(5)]
        orders += [{"id": str(10 + i), "ticker": f"T{120 + i}", "side": "short", "weight": 0.2}
                   for i in range(6)]
        self.refused(self.batch(orders, book="neural"), "gross exposure")
        ok = self.batch(orders[:5] + orders[5:9], book="neural")
        self.assertEqual(len(self.run_batch(ok)[0]["fills"]), 9)


class LedgerFiles(unittest.TestCase):

    def test_append_refuses_a_changed_header_and_migration_widens_once(self):
        with H.temp_dir() as tmp:
            p = tmp / "x.csv"
            E.append_rows(p, ["a", "b"], [{"a": 1, "b": 2}])
            E.append_rows(p, ["a", "b"], [{"a": 3, "b": 4}])
            with self.assertRaises(E.SchemaError):
                E.append_rows(p, ["a", "b", "c"], [{"a": 5}])
            with self.assertRaises(E.SchemaError):
                E.migrate_add_columns(p, ["b", "a", "c"])
            self.assertEqual(E.migrate_add_columns(p, ["a", "b", "c"]), 2)
            self.assertEqual(E.migrate_add_columns(p, ["a", "b", "c"]), 0)
            E.append_rows(p, ["a", "b", "c"], [{"a": 5, "c": 6}])
            self.assertEqual(p.read_text(), "a,b,c\n1,2,\n3,4,\n5,,6\n")

    def test_union_merge_covers_the_ledgers(self):
        text = (H.REPO / ".gitattributes").read_text(encoding="utf-8")
        self.assertIn("portfolio/ledger/*.csv merge=union", text)
        self.assertIn("data/**/*.csv merge=union", text)

    def test_committed_ledger_is_consistent(self):
        led = H.REPO / "portfolio" / "ledger"
        trades = E.read_rows(led / "trades.csv")
        if not trades:
            self.skipTest("no books incepted")
        for name, cols in (("trades.csv", E.TRADE_COLUMNS), ("decisions.csv", E.DECISION_COLUMNS),
                           ("mandates.csv", E.MANDATE_COLUMNS)):
            self.assertEqual(E._header(led / name), cols)
        decisions = {d["decision_id"]: d for d in E.read_rows(led / "decisions.csv")}
        self.assertEqual(len({t["trade_id"] for t in trades}), len(trades))
        for t in trades:
            self.assertIn(t["side"], E.SIDES)
            self.assertIn(t["decision_id"], decisions)
            self.assertIn(t["book"], {b["id"] for b in E.LIVE_BOOKS})
        for d in decisions.values():
            self.assertIn(d["action"], E.DECISION_ACTIONS)
            self.assertIn(d["author"], E.AUTHORS)
        for bid in {t["book"] for t in trades}:
            self.assertEqual(sum(1 for t in trades if t["book"] == bid and t["side"] == "deposit"), 1)
            E.derive_lots(E.trades_for(trades, bid))
            self.assertEqual(E.mandate_history(bid)[-1]["mandate"], E.load_mandate(bid))
        self.assertTrue({"hedge", "neural"} <= {t["book"] for t in trades})


class Benchmarks(unittest.TestCase):

    def test_merge_keeps_old_points_on_the_same_basis_only(self):
        stored = [["2025-01-02", 100.0], ["2026-01-02", 110.0]]
        fresh = [["2026-01-02", 110.0], ["2026-01-05", 111.0]]
        self.assertEqual(E.merge_benchmark_series(stored, fresh),
                         [["2025-01-02", 100.0], ["2026-01-02", 110.0], ["2026-01-05", 111.0]])
        split = [["2026-01-02", 55.0], ["2026-01-05", 55.5]]
        self.assertEqual(E.merge_benchmark_series(stored, split), split)
        self.assertEqual(E.merge_benchmark_series(stored, []), stored)

    def test_fetch_stores_every_symbol_dated_and_unadjusted(self):
        fake = types.ModuleType("yfinance")
        calls = []

        class Frame:
            empty = False

        def download(*a, **kw):
            calls.append(kw)
            return Frame()
        fake.download = download
        series = {s: [["2026-09-22", 100.0], [DAY, 101.0 + i]]
                  for i, s in enumerate(E.BENCHMARK_SYMBOLS)}
        with H.temp_dir() as tmp:
            (tmp / E.BENCHMARKS_FILE).write_text(json.dumps({"updated": "2020-01-01T00:00:00+00:00", "series": {
                "IWF": {"closes": [["2025-06-02", 90.0], ["2026-09-22", 100.0]]}}}))
            with mock.patch.dict(sys.modules, {"yfinance": fake}), \
                    mock.patch.object(LF, "_benchmark_closes", lambda f, s: series[s]), \
                    H.patched(LF, PRICES_DIR=tmp), H.quiet():
                n = LF.enrich_with_benchmark_series()
                again = LF.enrich_with_benchmark_series()
            data = json.loads((tmp / E.BENCHMARKS_FILE).read_text())
        self.assertEqual(n, len(E.BENCHMARK_SYMBOLS))
        self.assertEqual(again, len(E.BENCHMARK_SYMBOLS))
        self.assertEqual(len(calls), 1, "the second call reuses the 24h cache")
        self.assertIs(calls[0]["auto_adjust"], False)
        self.assertEqual(set(data["series"]), set(E.BENCHMARK_SYMBOLS))
        self.assertEqual(data["series"]["IWF"]["closes"][0], ["2025-06-02", 90.0])
        for s in E.BENCHMARK_SYMBOLS:
            for d, c in data["series"][s]["closes"]:
                self.assertRegex(d, r"^\d{4}-\d{2}-\d{2}$")
        self.assertIn("not adjusted for dividends", data["basis"])


class Pages(unittest.TestCase):

    def test_portfolios_and_book_pages_render(self):
        with H.temp_dir() as tmp:
            ledger_fixture(tmp, extra_days=())
            with H.quiet():
                E.seed(ledger_dir=tmp / "portfolio" / "ledger", books_dir=tmp / "portfolio" / "books",
                       panel_dir=tmp / "panel", prices=E.PriceStore(tmp / "prices"),
                       events=tmp / "no.csv", history=tmp / "history.csv")
            (tmp / "portfolio" / "drafts").mkdir()
            (tmp / "portfolio" / "drafts" / "PM-agent-draft.md").write_text("x")
            docs = tmp / "docs"
            docs.mkdir()
            theses = tmp / "theses"
            (theses / "ledger").mkdir(parents=True)
            with H.patched(LF, DOCS_DIR=docs, ASSETS_DIR=docs / "assets", PRICES_DIR=tmp / "prices",
                           PORTFOLIO_DIR=tmp / "portfolio", PORTFOLIO_NAV_CSV=tmp / "nav.csv",
                           FUNDAMENTALS_CSV_DIR=tmp / "panel", THESES_DIR=theses,
                           STYLE_HISTORY_CSV=tmp / "history.csv"), H.quiet():
                self.assertEqual(LF.record_portfolio_nav(), 8)
                v = LF._write_ledger_assets()
                LF.generate_portfolios({"date": DAY, "stocks": []}, v)
                LF.generate_research({"date": DAY, "stocks": []}, v)
            pages = {n: (docs / n).read_text(encoding="utf-8")
                     for n in ("portfolios.html", "book.html", "research.html")}
        for name, html in pages.items():
            with self.subTest(page=name):
                self.assertIn('<a href="portfolios.html"', html)
                self.assertNotIn(H.EM_DASH, html)
                self.assertNotIn(EN_DASH, html)
                self.assertNotRegex(html, r"__[A-Z_]+__")
        self.assertIn('<a href="portfolios.html" aria-current="page">', pages["book.html"])
        cfg = json.loads(re.search(r"window\.APT_PAGE = (\{.*?\});\n", pages["book.html"]).group(1))
        books = {b["id"]: b for b in cfg["portfolios"]["books"]}
        self.assertEqual(len(books), 8)
        for bid, b in books.items():
            self.assertEqual(b["inception"], DAY)
            self.assertEqual(len(b["series"]), 1)
            if b["kind"] == "style":
                self.assertLess(b["ret"], 0, "the only change on day one is the trading cost")
                self.assertGreater(b["ret"], -0.001)
                self.assertTrue(b["holdings"] and b["candidate"])
            else:
                self.assertEqual(b["ret"], 0.0)
                self.assertEqual(b["holdingsCount"], 0)
        self.assertIn("cashRet", books["hedge"])
        self.assertEqual(cfg["portfolios"]["coverage"]["classified"],
                         sum(v for k, v in cfg["portfolios"]["boxCounts"].items() if "-" in k))
        cards = json.loads(re.search(r"window\.APT_PAGE = (\{.*?\});\n", pages["portfolios.html"]).group(1))
        self.assertNotIn("trades", cards["portfolios"]["books"][0])
        research = json.loads(re.search(r"window\.APT_PAGE = (\{.*?\});\n", pages["research.html"]).group(1))
        self.assertNotIn("portfolio", research)

    def test_the_page_script_has_the_new_pages_and_not_the_old_section(self):
        js = (H.REPO / "web" / "ledger.js").read_text(encoding="utf-8")
        for needle in ("function renderPortfolios(main)", "function renderBook(main)",
                       'page === "portfolios"', 'page === "book"', "portfoliosLinkHTML()",
                       "Returns are price-only", "5 basis points", "cleanHtml(CFG.board)",
                       "pmInstructionsHTML(CFG.howPms)", "analystInstructionsHTML(CFG.howAnalyst)"):
            self.assertIn(needle, js)
        self.assertNotIn("portfolioHTML(", js)
        self.assertNotIn(EN_DASH, js)


class PmPrompt(unittest.TestCase):

    def test_the_pm_prompt_lives_in_portfolio_and_the_analyst_prompt_is_analyst_only(self):
        text = (H.REPO / "portfolio" / "PROMPTS.md").read_text(encoding="utf-8")
        a2 = text.index("## Agent 2: the PM")
        pm = text[a2:]
        for needle in ("portfolio/bin/trade.py", "portfolio/letters/", "--write", "hedge", "neural",
                       "internet", "Monday"):
            self.assertIn(needle, pm)
        self.assertNotIn("construct.py", pm)
        self.assertNotIn("## Agent 1", text)
        for t in (text, (H.REPO / "theses" / "PROMPTS.md").read_text(encoding="utf-8")):
            self.assertNotIn(H.EM_DASH, t)
            self.assertNotIn(EN_DASH, t)
        analyst = (H.REPO / "theses" / "PROMPTS.md").read_text(encoding="utf-8")
        self.assertIn("## Agent 1: the analyst", analyst)
        self.assertNotIn("## Agent 2", analyst)
        self.assertNotIn("Agent 2 below", analyst)
        self.assertIn("The portfolio managers' instructions are in portfolio/PROMPTS.md.", analyst)
        for name in ("style-pm.md", "hedge-pm.md", "neural-pm.md"):
            routine = (H.REPO / "portfolio" / "routines" / name).read_text(encoding="utf-8")
            self.assertIn("Follow portfolio/PROMPTS.md", routine)
            self.assertNotIn("theses/PROMPTS.md", routine)
        research = (H.REPO / "theses" / "routines" / "research-agent.md").read_text(encoding="utf-8")
        self.assertNotIn("PM section", research)

if __name__ == "__main__":
    unittest.main()
