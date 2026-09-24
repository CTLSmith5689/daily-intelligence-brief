"""The model portfolios: classification, the ledger, seeding, NAV, benchmarks, pages.

Offline. Every file the engine reads or writes is redirected into a temporary
directory; the committed ledger is only read, to check its own consistency.
"""
import csv
import json
import math
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

EN_DASH = chr(0x2013)
DAY = "2026-09-23"
PANEL_COLS = ["date", "ticker", "name", "sector", "security_type", "price", "volume",
              "market_cap", "pe", "price_book", "fcf_yield", "ttm_eps_diluted",
              "revenue_growth_yoy", "eps_growth_yoy", "revenue_acceleration", "roe_ttm",
              "earnings_consistency", "net_debt_ebitda", "op_margin_stability", "accruals_ratio"]
SECTORS = ["Energy", "Industrials", "Financials", "Health Care", "Utilities"]


def synthetic_rows(n=300, day=DAY):
    """n operating companies with caps falling geometrically, plus a note, a share
    class twin of T000 and a company with too few growth inputs."""
    rows = []
    for i in range(n):
        g = ((i * 37) % 101) / 100.0          # scrambled, so style is not size
        v = ((i * 53) % 97) / 100.0
        rows.append({"date": day, "ticker": f"T{i:03d}", "name": f"Company {i}",
                     "sector": SECTORS[i % 5], "security_type": "operating",
                     "price": 10 + i % 50, "volume": 1000 + i, "market_cap": 1e12 * (0.97 ** i),
                     "pe": 8 + 30 * (1 - v), "price_book": 1 + 5 * (1 - v), "fcf_yield": 0.1 * v,
                     "ttm_eps_diluted": 1, "revenue_growth_yoy": g, "eps_growth_yoy": g * 1.5,
                     "revenue_acceleration": g - 0.5, "roe_ttm": 0.1, "earnings_consistency": 0.5,
                     "net_debt_ebitda": 1, "op_margin_stability": 0.1, "accruals_ratio": 0.02})
    rows.append(dict(rows[0], ticker="T000B", name="Company 0 (Class B)", volume=1))
    rows.append({"date": day, "ticker": "NOTE", "name": "Company 1 Notes", "security_type": "debt",
                 "market_cap": 5e12})
    rows.append(dict(rows[5], ticker="THIN", name="Thin Co", revenue_growth_yoy="",
                     eps_growth_yoy="", market_cap=rows[5]["market_cap"] * 0.999))
    return rows


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


class Classification(unittest.TestCase):

    def test_robust_z_matches_the_page_engine(self):
        vals = [0.3, -1.2, 4.0, 0.0, 2.5, 2.5, 7.1, -0.4, 1.1, 90.0]
        c, s = E.robust_stat(vals)
        ref = LF._ledger_stat(vals)
        self.assertAlmostEqual(c, ref["center"])
        self.assertAlmostEqual(s, ref["scale"])
        z = E.robust_z({i: v for i, v in enumerate(vals)}, min_n=5)
        self.assertEqual(z[9], 5.0, "clipped at +5")
        self.assertIsNone(E.robust_z({1: 1.0, 2: 2.0}, min_n=5)[1])

    def test_size_buckets_follow_cumulative_cap(self):
        rows = synthetic_rows()
        c = E.classify(rows)
        self.assertEqual(c["NOTE"]["why"], "not an operating company")
        self.assertIsNone(c["NOTE"]["size"])
        self.assertEqual(c["T000B"]["share_class_of"], "T000")
        # Independent recomputation of the rule, share-class twin and note excluded.
        caps = sorted(((float(r["market_cap"]), r["ticker"]) for r in rows
                       if r["security_type"] == "operating" and r["ticker"] != "T000B"),
                      key=lambda x: (-x[0], x[1]))
        total, before, want = sum(x for x, _ in caps), 0.0, {}
        for cap, t in caps:
            share = before / total
            want[t] = "large" if share < .7 else "mid" if share < .9 else "small" if share < .98 else "micro"
            before += cap
        for t, size in want.items():
            self.assertEqual(c[t]["size"], size, t)
        self.assertEqual({v for v in want.values()}, {"large", "mid", "small", "micro"})

    def test_style_terciles_and_the_two_input_rule(self):
        c = E.classify(synthetic_rows())
        self.assertIsNone(c["THIN"]["style"])
        self.assertIn("growth inputs", c["THIN"]["why"])
        for size in ("large", "mid", "small"):
            scored = sorted((i for i in c.values() if i["size"] == size and i["style"]),
                            key=lambda i: (i["style_score"], i["ticker"]))
            n, third = len(scored), len(scored) // 3
            with self.subTest(size=size):
                self.assertGreaterEqual(n, E.MIN_COHORT)
                self.assertEqual([i["style"] for i in scored],
                                 ["value"] * third + ["core"] * (n - 2 * third) + ["growth"] * third)
                for i in scored:
                    self.assertAlmostEqual(i["style_score"], i["growth"] - i["value"])
                    self.assertEqual(i["box"], f"{size}-{i['style']}")

    def test_candidate_respects_views_sector_cap_and_weights(self):
        c = E.classify(synthetic_rows())
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
        self.assertAlmostEqual(sum(p["weight"] for p in got), 1.0)

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


def ledger_fixture(tmp, extra_days=("2026-09-24", "2026-09-25")):
    """A panel on DAY plus later days, and stored closes for every operating row."""
    rows = synthetic_rows()
    write_panel(tmp / "panel", rows)
    for d in extra_days:
        write_panel(tmp / "panel", [dict(r, date=d) for r in rows])
    closes = {r["ticker"]: [(d, 20.0 + (i % 7)) for d in (DAY,) + tuple(extra_days)]
              for i, r in enumerate(rows) if r["security_type"] == "operating"}
    write_prices(tmp / "prices", closes)
    return rows


class Seeding(unittest.TestCase):

    def seed(self, tmp, **kw):
        with H.quiet():
            return E.seed(ledger_dir=tmp / "ledger", books_dir=tmp / "books", panel_dir=tmp / "panel",
                          prices=E.PriceStore(tmp / "prices"), events=tmp / "none.csv", **kw)

    def test_seed_is_idempotent(self):
        with H.temp_dir() as tmp:
            ledger_fixture(tmp)
            first = self.seed(tmp, day=DAY)
            self.assertEqual(first["date"], DAY)
            self.assertEqual(len(first["seeded"]), 9)
            snap = {p.name: p.read_bytes() for p in (tmp / "ledger").iterdir()}
            second = self.seed(tmp)
            self.assertEqual(second["seeded"], [])
            self.assertEqual(snap, {p.name: p.read_bytes() for p in (tmp / "ledger").iterdir()})
            trades = E.read_rows(tmp / "ledger" / "trades.csv")
            deposits = [t for t in trades if t["side"] == "deposit"]
            self.assertEqual(sorted(t["book"] for t in deposits), sorted(b["id"] for b in E.STYLE_BOOKS))
            prices = E.PriceStore(tmp / "prices")
            for t in trades:
                if t["side"] == "buy":
                    self.assertEqual(float(t["price"]), prices.close(t["ticker"], t["date"]))
                    self.assertAlmostEqual(float(t["cost"]), round(float(t["shares"]) * float(t["price"]) * 0.0005, 2))
                    self.assertEqual(t["lot_id"], t["trade_id"])
            for b in E.STYLE_BOOKS:
                st = E.book_state(E.trades_for(trades, b["id"]))
                self.assertGreaterEqual(st["cash"], 0)
                self.assertEqual(st["capital"], E.INCEPTION_CAPITAL)
                hist = E.mandate_history(b["id"], tmp / "ledger")
                self.assertEqual(len(hist), 1)
                self.assertEqual(hist[-1]["mandate"], E.load_mandate(b["id"], tmp / "books"))

    def test_seed_steps_back_when_a_close_is_missing(self):
        with H.temp_dir() as tmp:
            ledger_fixture(tmp, extra_days=("2026-09-24",))
            dry = self.seed(tmp, dry_run=True)
            victim = dry["books"]["lg-core"][0]["ticker"]
            path = tmp / "prices" / E.price_filename(victim)
            blob = json.loads(path.read_text())
            blob["closes"] = [c for c in blob["closes"] if c[0] != "2026-09-24"]
            path.write_text(json.dumps(blob))
            out = self.seed(tmp, dry_run=True)
            self.assertEqual(out["date"], DAY)


class Nav(unittest.TestCase):

    def trades(self):
        rows = [("d", DAY, "b", "CASH", "deposit", 1000, 1, 0, ""),
                ("t1", DAY, "b", "AAA", "buy", 10, 50, 0.25, "t1"),
                ("t2", DAY, "b", "BBB", "buy", 5, 40, 0.10, "t2"),
                ("t3", DAY, "b", "CCC", "short", 4, 25, 0.05, "t3")]
        return [dict(zip(["trade_id", "date", "book", "ticker", "side", "shares", "price", "cost",
                          "lot_id"], map(str, r))) for r in rows]

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
            self.assertEqual(v1["missing"], ["BBB"])
            # BBB is not valued at its 09-23 close, or at anything.
            self.assertAlmostEqual(v1["priced_nav"], cash + 550 - 80)
            self.assertAlmostEqual(v1["short_value"], -80)

    def test_basis_break_is_not_valued(self):
        with H.temp_dir() as tmp:
            # A 2:1 split rebased the stored 09-23 close to half the price paid.
            write_prices(tmp, {"AAA": [(DAY, 25), ("2026-09-24", 26)], "BBB": [(DAY, 40), ("2026-09-24", 40)],
                               "CCC": [(DAY, 25), ("2026-09-24", 25)]})
            v = E.value_book(self.trades(), "b", "2026-09-24", E.PriceStore(tmp))
            self.assertEqual(v["basis_break"], ["AAA"])
            self.assertTrue(v["partial"])

    def test_record_nav_appends_once_and_supersedes_partial(self):
        with H.temp_dir() as tmp:
            write_prices(tmp, {"AAA": [(DAY, 50)], "BBB": [(DAY, 40)], "CCC": [(DAY, 25)]})
            trades = [dict(t, book="lg-core") for t in self.trades()]
            nav = tmp / "nav.csv"
            bench = E.BenchmarkStore(tmp)
            self.assertEqual(E.record_nav(trades, [DAY], E.PriceStore(tmp), bench, nav, now="x"), 1)
            self.assertEqual(E.record_nav(trades, [DAY], E.PriceStore(tmp), bench, nav, now="y"), 0)
            (tmp / E.BENCHMARKS_FILE).write_text(json.dumps(
                {"updated": "z", "series": {"IWB": {"closes": [[DAY, 300.5]]}}}))
            self.assertEqual(E.record_nav(trades, [DAY], E.PriceStore(tmp), E.BenchmarkStore(tmp), nav, now="z"), 1)
            self.assertEqual(E.record_nav(trades, [DAY], E.PriceStore(tmp), E.BenchmarkStore(tmp), nav, now="w"), 0)
            rows = E.read_rows(nav)
            self.assertEqual(len(rows), 2)
            self.assertEqual(E.latest_nav_rows(rows)[("lg-core", DAY)]["benchmark_close"], "300.5")

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
        for d in decisions.values():
            self.assertIn(d["action"], E.DECISION_ACTIONS)
            self.assertIn(d["author"], E.AUTHORS)
        for b in E.STYLE_BOOKS:
            self.assertEqual(sum(1 for t in trades if t["book"] == b["id"] and t["side"] == "deposit"), 1)
            E.derive_lots(E.trades_for(trades, b["id"]))
            hist = E.mandate_history(b["id"])
            self.assertEqual(hist[-1]["mandate"], E.load_mandate(b["id"]))


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
        series = {s: [["2026-09-22", 100.0 + i], [DAY, 101.0 + i]]
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
        self.assertEqual(n, 10)
        self.assertEqual(again, 10)
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
                       panel_dir=tmp / "panel", prices=E.PriceStore(tmp / "prices"), events=tmp / "no.csv")
            (tmp / "portfolio" / "drafts").mkdir()
            (tmp / "portfolio" / "drafts" / "PM-agent-draft.md").write_text("x")
            docs = tmp / "docs"
            docs.mkdir()
            theses = tmp / "theses"
            (theses / "ledger").mkdir(parents=True)
            with H.patched(LF, DOCS_DIR=docs, ASSETS_DIR=docs / "assets", PRICES_DIR=tmp / "prices",
                           PORTFOLIO_DIR=tmp / "portfolio", PORTFOLIO_NAV_CSV=tmp / "nav.csv",
                           FUNDAMENTALS_CSV_DIR=tmp / "panel", THESES_DIR=theses), H.quiet():
                self.assertEqual(LF.record_portfolio_nav(), 9)
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
        self.assertIn('<body data-page="portfolios">', pages["portfolios.html"])
        self.assertIn('<a href="portfolios.html" aria-current="page">', pages["book.html"])
        cfg = json.loads(re.search(r"window\.APT_PAGE = (\{.*?\});\n", pages["book.html"]).group(1))
        books = cfg["portfolios"]["books"]
        self.assertEqual(len(books), 9)
        for b in books:
            self.assertEqual(b["inception"], DAY)
            self.assertAlmostEqual(b["ret"], b["nav"] / 1e6 - 1, places=5)
            self.assertLess(b["ret"], 0, "the only change on day one is the trading cost")
            self.assertGreater(b["ret"], -0.001)
            self.assertIsNone(b["benchRet"])
            self.assertEqual(len(b["series"]), 1)
            self.assertTrue(b["trades"] and b["decisions"] and b["holdings"])
        cards = json.loads(re.search(r"window\.APT_PAGE = (\{.*?\});\n", pages["portfolios.html"]).group(1))
        self.assertNotIn("trades", cards["portfolios"]["books"][0])
        self.assertEqual(cards["drafts"][0]["path"], "portfolio/drafts/PM-agent-draft.md")
        research = json.loads(re.search(r"window\.APT_PAGE = (\{.*?\});\n", pages["research.html"]).group(1))
        self.assertNotIn("portfolio", research)

    def test_the_page_script_has_the_new_pages_and_not_the_old_section(self):
        js = (H.REPO / "web" / "ledger.js").read_text(encoding="utf-8")
        for needle in ("function renderPortfolios(main)", "function renderBook(main)",
                       'page === "portfolios"', 'page === "book"', "portfoliosLinkHTML()",
                       "Returns are price-only", "5 basis points"):
            self.assertIn(needle, js)
        self.assertNotIn("portfolioHTML(", js)
        self.assertNotIn(EN_DASH, js)


if __name__ == "__main__":
    unittest.main()
