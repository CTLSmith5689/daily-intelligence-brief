"""The site renders from a small synthetic universe, offline.

The pages are shells that web/ledger.js fills in the browser, so what can go
wrong on the Python side is the shell: a missing mount point, an asset that is
not copied, a placeholder left unfilled, data the page cannot parse, or a dash
from a headline reaching the page. The screen of the day is computed here and
recomputed in the browser, so the two must agree; when node is installed the
same rows are run through web/zengine.js and the counts compared.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests import helpers as H  # noqa: E402

LF = H.LF
EN_DASH = chr(0x2013)


def universe():
    """Forty operating rows in two sectors, a note and a shell."""
    rows = []
    for i in range(40):
        rows.append({
            "ticker": f"T{i:02d}", "name": f"Test Company {i} - Common Stock",
            "sector": "Energy" if i % 2 else "Industrials", "sub_industry": "Things",
            "index": "Nasdaq", "security_type": "operating", "scorable": 1,
            "price": 10.0 + i, "price_date": "2026-09-18", "change_pct": 0.5 - i / 40,
            "market_cap": 1e8 * (i + 1), "volume": 1000 * (i + 3),
            "roe_ttm": 0.02 * i - 0.1, "earnings_consistency": 0.3 + 0.015 * i,
            "pe": 40.0 - 0.8 * i, "net_debt_ebitda": 3.0 - 0.07 * i,
            "gross_margin": 0.2 + 0.01 * (i % 17), "fcf_yield": 0.01 * (i % 9),
            "g": 0.1 * (i % 5) - 0.2, "v": 0.05 * (i % 7), "q": 0.3, "m": -0.1,
        })
    rows.append({"ticker": "NOTEZ", "name": "Test Trust " + H.EM_DASH + " 9.125% Senior Notes Due 2030",
                 "index": "Nasdaq", "security_type": "debt", "price": 25.0,
                 "price_date": "2026-09-21", "status": {"market_cap": "not_applicable",
                                                        "pe": "not_applicable"}})
    rows.append({"ticker": "SHLL", "name": "Blank Check Acquisition Corp", "index": "Nasdaq",
                 "security_type": "spac", "price": 10.1, "price_date": "2026-09-18",
                 "roe_ttm": 5.0, "pe": 1.0})
    return {"date": "2026-09-21", "stocks": rows}


def briefs():
    head = "Markets rally " + H.EM_DASH + " again, 2025" + EN_DASH + "26 outlook - Example Wire"
    return [{"key": "briefs/2026-09-21-daily.html", "date": "2026-09-21", "type": "daily",
             "timestamp": "08:54 PM ET",
             "sections": [{"name": "Finance & Markets",
                           "stories": [{"headline": head, "source": "Example Wire",
                                        "link": "https://example.invalid/a"}]}]}]


class SiteRender(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.tmp_ctx = H.temp_dir()
        cls.docs = cls.tmp_ctx.__enter__()
        for d in ("prices", "news", "company", "thesis"):
            (cls.docs / d).mkdir()
        (cls.docs / "prices" / "T01.json").write_text(json.dumps(
            {"ticker": "T01", "closes": [["2026-09-17", 10.5], ["2026-09-18", 11.0]]}))
        (cls.docs / "company" / "T01.json").write_text(json.dumps({"ticker": "T01"}))
        (cls.docs / "thesis" / "T01.json").write_text(json.dumps({"ticker": "T01"}))
        # A small theses/ for the Stocks page's Research filter: one note and a watchlist.
        theses = cls.docs / "theses"
        (theses / "notes" / "T01").mkdir(parents=True)
        (theses / "notes" / "T01" / "2026-09-20-initiation.md").write_text(
            "---\nticker: T01\ndirection: long\nconviction: 3\n---\nBody.\n", encoding="utf-8")
        (theses / "watchlist.txt").write_text("# comment\nT02\nt03  # trailing\n", encoding="utf-8")
        cls.universe = universe()
        with H.patched(LF, DOCS_DIR=cls.docs, ASSETS_DIR=cls.docs / "assets",
                       PRICES_DIR=cls.docs / "prices", NEWS_DIR=cls.docs / "news",
                       COMPANY_VIEW_DIR=cls.docs / "company", THESIS_VIEW_DIR=cls.docs / "thesis",
                       THESES_DIR=theses), H.quiet():
            version = LF._write_ledger_assets()
            LF.generate_stocks_page(cls.universe, version)
            LF.generate_company_page(cls.universe, version)
            LF.generate_home(briefs(), cls.universe, version)
            LF.generate_today(briefs(), cls.universe, version)
            LF.generate_stories(briefs(), cls.universe, version)
        cls.version = version

    @classmethod
    def tearDownClass(cls):
        cls.tmp_ctx.__exit__(None, None, None)

    def page(self, name):
        return (self.docs / name).read_text(encoding="utf-8")

    def page_data(self, html):
        m = re.search(r"window\.APT_PAGE = (\{.*?\});\n", html)
        self.assertIsNotNone(m, "no page data")
        return json.loads(m.group(1))

    def test_shell_has_mount_point_nav_and_assets(self):
        for name, page in (("stocks.html", "stocks"), ("company.html", "company"),
                           ("index.html", "home"), ("today.html", "today")):
            with self.subTest(page=name):
                html = self.page(name)
                self.assertIn(f'<body data-page="{page}">', html)
                self.assertIn('<main class="ld-main" id="ld-main">', html)
                self.assertIn(f'assets/ledger.js?v={self.version}', html)
                self.assertIn(f'assets/ledger.css?v={self.version}', html)
                for href in ("index.html", "today.html", "stories.html", "stocks.html", "research.html"):
                    self.assertIn(f'<a href="{href}"', html)
                self.assertNotRegex(html, r"__[A-Z_]+__", "a template placeholder was left in")
        self.assertIn('<a href="stocks.html" aria-current="page">', self.page("company.html"))
        for asset in LF.LEDGER_ASSETS:
            self.assertTrue((self.docs / "assets" / asset).is_file(), asset)

    def test_stocks_page_data(self):
        html = self.page("stocks.html")
        self.assertIn("assets/zengine.js", html)
        cfg = self.page_data(html)
        self.assertEqual(len(cfg["metrics"]), len(LF.LEDGER_METRICS))
        self.assertEqual(cfg["asof"], "2026-09-21")
        self.assertEqual(cfg["close"], "2026-09-18")          # the majority price_date
        self.assertEqual(cfg["minCohort"], LF.MIN_COHORT_FOR_ZSCORE)
        self.assertEqual([p["id"] for p in cfg["presets"]], [s["id"] for s in LF.LEDGER_SCREENS])
        nonop = json.loads(re.search(r"window\.APT_PAGE\.nonop = (\[.*?\]);", html).group(1))
        self.assertEqual(nonop, sorted(LF.sectype.NON_OPERATING))
        methods = re.search(r"window\.APT_PAGE\.fieldMethods = (\{.*?\});\n", html).group(1)
        self.assertEqual(json.loads(methods), LF.FIELD_METHODS)
        rows = json.loads(self.page("stocks-data.json"))
        self.assertEqual(len(rows), len(self.universe["stocks"]))
        self.assertEqual(rows[-2]["security_type"], "debt")

    def test_stocks_page_has_the_map_view(self):
        """The Stocks page's second view, the factor map: a Grid | Map toggle kept in the hash, a
        focusable, labelled canvas with its count below, and the four factor corners. It is drawn by
        the copied ledger.js, so that is what is checked, plus that the script parses."""
        js = (self.docs / "assets" / "ledger.js").read_text(encoding="utf-8")
        css = (self.docs / "assets" / "ledger.css").read_text(encoding="utf-8")
        for needle in ('data-view="map"', 'id="ld-map"', '<canvas id="ld-mapc" tabindex="0" role="img"',
                       'aria-describedby="ld-mapcount"', '"view=map"', '"map=axes"', '"ax="',
                       'g: "Growth", v: "Value", m: "Momentum", q: "Quality"',
                       "prefers-reduced-motion: reduce", "matching \" + what + \" placed; ",
                       "enough dimensions", "ArrowLeft", "FOCUS_MAX = 5", "AX_CLIP = 4",
                       "themeHooks.push("):
            with self.subTest(needle=needle):
                self.assertIn(needle, js)
        for needle in (".ld-mapbox", "touch-action:none", ".ld-mapfocus", ".ld-vseg"):
            with self.subTest(needle=needle):
                self.assertIn(needle, css)
        if shutil.which("node"):
            out = subprocess.run(["node", "--check", str(self.docs / "assets" / "ledger.js")],
                                 capture_output=True, text=True, timeout=60)
            self.assertEqual(out.returncode, 0, out.stderr)

    def test_every_page_carries_the_disclaimer(self):
        strip = ('<div class="ld-disc" role="note">A personal project. The data is collected '
                 'automatically and not checked by hand. Nothing here is investment advice.</div>')
        for name in ("stocks.html", "company.html", "index.html", "today.html", "stories.html"):
            with self.subTest(page=name):
                html = self.page(name)
                self.assertEqual(html.count(strip), 1)
                self.assertLess(html.index(strip), html.index('<header class="ld-mast">'))
        css = (self.docs / "assets" / "ledger.css").read_text(encoding="utf-8")
        self.assertIn("html .ld-disc{", css)

    def test_stocks_page_carries_the_rail_data(self):
        """What the rail's Research and Hygiene filters read: every thesis ticker with its current
        view's direction, the watchlist, and the scored fields of each dimension."""
        cfg = self.page_data(self.page("stocks.html"))
        self.assertEqual(cfg["research"], {"thesis": {"T01": "long"}, "watchlist": ["T02", "T03"]})
        self.assertEqual(cfg["dims"], {d: g["fields"] for d, g in LF.SCORE_GROUPS_PY.items()})
        for d, fields in cfg["dims"].items():
            with self.subTest(dimension=d):
                self.assertEqual(len(fields), 5)
                self.assertTrue(set(fields) <= {m["key"] for m in cfg["metrics"]})

    def test_stocks_page_has_the_rail(self):
        """The Stocks page's layout: a full-width app with a filter rail on the left (a drawer under
        900px) and results that scroll in their own box. Each rail filter writes a query token, so the
        script must know every token the rail writes; the page body must not scroll on a desktop."""
        js = (self.docs / "assets" / "ledger.js").read_text(encoding="utf-8")
        css = (self.docs / "assets" / "ledger.css").read_text(encoding="utf-8")
        for needle in ('id="ld-rail"', 'id="ld-app"', 'class="ld-res"', "data-rail-open", "data-rail-close",
                       'data-idx="', 'data-sec="', 'data-res="', 'data-cap="lo"', 'data-units="raw"',
                       'data-lo="', 'data-hi="', 'data-dp="', "data-needcap", 'data-sw="', "data-vsave",
                       "data-vload", "data-vdel", '"apt-stocks-views"', "localStorage.setItem(VIEWS_KEY",
                       '"idx:" + k', '"sector:" +', '"research:" + v', '"dp:" + d.toLowerCase() + ">=" + n',
                       '"has:cap"', "RAW_CMP", "RAW_RANGE", "Filters (", "max-width: 899px",
                       '"Market cap ($M)"', '"Hygiene"', '"Listings"', '"Saved views"', '"Ready-made"'):
            with self.subTest(needle=needle):
                self.assertIn(needle, js)
        self.assertNotIn('class="ld-shead"', js)             # the title row is gone
        self.assertNotIn('class="ld-cmd"', js)               # and the boxed query banner
        for needle in ('html body[data-page="stocks"]{overflow:hidden}', "height:100dvh",
                       "html .ld-app{", "html .ld-rail{", "html .ld-res .ld-gwrap{flex:1 1 auto;min-height:0",
                       "@media (max-width:899px)", "html .ld-rail.open{"):
            with self.subTest(needle=needle):
                self.assertIn(needle, css)
        # Every localStorage call on the page is guarded: storage can be missing or refuse writes.
        lines = js.splitlines()
        for i, line in enumerate(lines):
            if "localStorage." in line:
                with self.subTest(line=line.strip()[:80]):
                    self.assertTrue("try {" in line or lines[i - 1].rstrip().endswith("try {"))

    def test_metric_directions_follow_the_score(self):
        by = {m["key"]: m for m in LF.LEDGER_METRICS}
        for group, spec in LF.SCORE_GROUPS_PY.items():
            for f in spec["fields"]:
                with self.subTest(field=f):
                    self.assertEqual(by[f]["group"], group)
                    self.assertEqual(by[f]["better"], -1 if f in spec["invert"] else 1)
        self.assertEqual(len({m["alias"] for m in LF.LEDGER_METRICS}), len(LF.LEDGER_METRICS))

    def test_company_page_lists_the_files_it_may_fetch(self):
        have = self.page_data(self.page("company.html"))["have"]
        self.assertEqual(have["company"], ["T01"])
        self.assertEqual(have["thesis"], ["T01"])
        self.assertNotIn("T01", have["noPrices"])
        self.assertIn("T00", have["noPrices"])
        self.assertIn("NOTEZ", have["noNews"])

    def test_no_dashes_reach_the_pages(self):
        for name in ("stocks.html", "company.html", "index.html", "today.html", "stories.html"):
            with self.subTest(page=name):
                html = self.page(name)
                self.assertNotIn(H.EM_DASH, html)
                self.assertNotIn(EN_DASH, html)
                self.assertNotIn("\\u2014", html)
                self.assertNotIn("\\u2013", html)
        for asset in LF.LEDGER_ASSETS:
            with self.subTest(asset=asset):
                text = (self.docs / "assets" / asset).read_text(encoding="utf-8")
                self.assertNotIn(H.EM_DASH, text)
                self.assertNotIn(EN_DASH, text)
        story = self.page_data(self.page("today.html"))["brief"]["sections"][0]["stories"][0]
        self.assertEqual(story["h"], "Markets rally, again, 2025-26 outlook - Example Wire")

    def test_stat_matches_the_engine_definition(self):
        st = LF._ledger_stat([1.0, 2.0, 3.0, 4.0, 100.0])
        self.assertEqual((st["method"], st["center"], st["scale"]), ("robust", 3.0, 1.4826))
        st = LF._ledger_stat([0.0, 0.0, 0.0, 0.0, 1.0])       # MAD 0: mean and sd instead
        self.assertEqual(st["method"], "sd")
        self.assertAlmostEqual(st["center"], 0.2)

    def test_screen_of_the_day(self):
        sod = self.page_data(self.page("index.html"))["sod"]
        self.assertEqual(sod["of"], 40)                        # the note and the shell are out
        self.assertLessEqual(len(sod["hits"]), 8)
        self.assertEqual(sod["match"], self.zengine_count(sod["q"]) if shutil.which("node") else sod["match"])
        scores = [h["score"] for h in sod["hits"] if h["score"] is not None]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def zengine_count(self, query):
        """The same screen run by web/zengine.js in node, over stocks-data.json."""
        bands = {k: [(-1e308 if lo is None else lo), (1e308 if hi is None else hi)]
                 for k, (lo, hi) in LF._ledger_bands(query).items()}
        script = """
const Z = require(process.argv[1]); const fs = require('fs');
const rows = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const metrics = JSON.parse(process.argv[3]), nonop = JSON.parse(process.argv[4]), bands = JSON.parse(process.argv[5]);
const A = {stocks: {ticker: [], sector: [], kind: []}, vals: {}, metrics: metrics, nonop: nonop, minCohort: 20};
metrics.forEach(m => A.vals[m.key] = []);
rows.forEach(r => { A.stocks.ticker.push(r.ticker); A.stocks.sector.push(r.sector || ''); A.stocks.kind.push(r.security_type || 'operating');
  metrics.forEach(m => A.vals[m.key].push(typeof r[m.key] === 'number' ? r[m.key] : null)); });
Z.init(A); const U = Z.compute({scope: 'universe'}); const op = [];
for (let i = 0; i < rows.length; i++) if (U.cohortMask[i]) op.push(i);
console.log(Z.filter(U, bands, {idx: Int32Array.from(op)}).length);
"""
        out = subprocess.run(["node", "-e", script, str(H.REPO / "web" / "zengine.js"),
                              str(self.docs / "stocks-data.json"), json.dumps(LF.LEDGER_METRICS),
                              json.dumps(sorted(LF.sectype.NON_OPERATING)), json.dumps(bands)],
                             capture_output=True, text=True, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        return int(out.stdout.strip())


if __name__ == "__main__":
    unittest.main()
