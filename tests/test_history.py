"""The per-ticker history files and the per-date universe stats behind the
company page's History section (write_history_views).

A tiny synthetic panel in two month files, the older without the newer columns,
checks what the page must never be handed: a price on a row flagged
price_stale, or a field that does not apply to the row's security_type. The
universe stats are checked against the zengine definition in Python
(_ledger_stat) and, when node is installed, against web/zengine.js itself.
"""
import csv
import json
import math
import os
import shutil
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests import helpers as H  # noqa: E402

LF = H.LF

OLD_COLS = ["date", "ticker", "name", "sector", "sub_industry", "index", "price", "market_cap",
            "pe", "volume", "gross_margin", "insider_ownership"]
NEW_COLS = OLD_COLS + ["security_type", "price_date", "price_stale"]


def op_row(i, date, **kw):
    r = {"date": date, "ticker": f"T{i:02d}", "name": f"Test Company {i}", "sector": "Energy",
         "sub_industry": "Things", "index": "Nasdaq", "price": 10 + i, "market_cap": 1e8 * (i + 1),
         "pe": 5 + 1.5 * i, "volume": 1000 * (i + 2), "gross_margin": 0.2 + 0.01 * i,
         "insider_ownership": 0.0 if i % 4 else 0.3}
    r.update(kw)
    return r


def write_csv(path, cols, rows):
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: ("" if r.get(k) is None else r.get(k)) for k in cols})


class HistoryViews(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.tmp_ctx = H.temp_dir()
        tmp = cls.tmp_ctx.__enter__()
        panel = tmp / "fundamentals"
        panel.mkdir()
        cls.out = tmp / "history"
        cls.out.mkdir()
        (cls.out / "GONE.json").write_text("{}")          # left from an earlier build
        # August: the old schema, no security_type column. The note is classified
        # from its name, the way apply_security_types would.
        aug = [op_row(i, "2026-08-31") for i in range(25)]
        aug.append({"date": "2026-08-31", "ticker": "NOTEZ", "name": "Test Trust 9.125% Senior Notes Due 2030",
                    "index": "Nasdaq", "price": 25.0, "market_cap": 5e9, "pe": 3.0})
        write_csv(panel / "2026-08.csv", OLD_COLS, aug)
        # September: the newer columns. T03 is stale on 09-02 (flag), T04 on 09-02
        # by its price_date only. The shell's P/E is a 0.0 placeholder on 09-01.
        sep = []
        for d in ("2026-09-01", "2026-09-02"):
            for i in range(25):
                kw = {"security_type": "operating", "price_date": d}
                if i == 3 and d == "2026-09-02":
                    kw.update(price_stale=1, price=None, market_cap=None, pe=None)
                if i == 4 and d == "2026-09-02":
                    kw.update(price_date="2026-09-01")
                sep.append(op_row(i, d, **kw))
            sep.append({"date": d, "ticker": "NOTEZ", "name": "Test Trust 9.125% Senior Notes Due 2030",
                        "index": "Nasdaq", "security_type": "debt", "price": 25.1, "pe": 3.0,
                        "price_date": d})
            sep.append({"date": d, "ticker": "SHLL", "name": "Blank Check Acquisition Corp", "index": "Nasdaq",
                        "security_type": "spac", "price": 10.1, "market_cap": 1e8, "gross_margin": 0.0,
                        "pe": 0.0 if d == "2026-09-01" else 40.0, "price_date": d})
        write_csv(panel / "2026-09.csv", NEW_COLS, sep)
        stocks = [{"ticker": f"T{i:02d}"} for i in range(24)] + [{"ticker": "NOTEZ"}, {"ticker": "SHLL"}]
        with H.patched(LF, FUNDAMENTALS_CSV_DIR=panel, HISTORY_VIEW_DIR=cls.out), H.quiet():
            cls.n = LF.write_history_views(stocks)
        cls.sep_rows = sep

    @classmethod
    def tearDownClass(cls):
        cls.tmp_ctx.__exit__(None, None, None)

    def load(self, name):
        return json.loads((self.out / name).read_text(encoding="utf-8"))

    def test_files_for_the_current_universe_only(self):
        names = sorted(p.name for p in self.out.glob("*.json"))
        self.assertEqual(self.n, 26)
        self.assertIn("_universe.json", names)
        self.assertNotIn("T24.json", names)                 # not in the current universe
        self.assertNotIn("GONE.json", names)                # a stale file is removed
        h = self.load("T00.json")
        self.assertEqual(h["d"], ["2026-08-31", "2026-09-01", "2026-09-02"])
        self.assertEqual(h["v"]["price"], [10, 10, 10])
        self.assertEqual(h["v"]["pe"], [5, 5, 5])
        self.assertNotIn("revenue_growth_yoy", h["v"])      # never recorded, not written

    def test_stale_rows_are_withheld(self):
        h = self.load("T03.json")
        self.assertEqual(h["stale"], [2])
        for k in ("price", "market_cap", "pe", "volume"):
            self.assertIsNone(h["v"][k][2], k)
        self.assertEqual(h["v"]["gross_margin"][2], 0.23)   # a filing field survives
        # A price_date older than the row's own date is stale too, flag or not.
        h = self.load("T04.json")
        self.assertEqual(h["stale"], [2])
        self.assertIsNone(h["v"]["price"][2])
        self.assertIsNone(h["v"]["pe"][2])
        self.assertEqual(h["v"]["price"][:2], [14, 14])

    def test_inapplicable_fields_are_withheld(self):
        note = self.load("NOTEZ.json")
        self.assertNotIn("pe", note["v"])
        self.assertNotIn("market_cap", note["v"])           # 5e9 on the old-schema row, withheld
        self.assertEqual(note["na"]["pe"], 1)
        self.assertEqual(note["na"]["market_cap"], 1)
        self.assertEqual(note["v"]["price"], [25, 25.1, 25.1])
        shell = self.load("SHLL.json")
        self.assertEqual(shell["v"]["pe"], [None, 40])      # the 0.0 placeholder only
        self.assertEqual(shell["na"]["pe"], [0])
        self.assertNotIn("gross_margin", shell["v"])
        self.assertEqual(shell["na"]["gross_margin"], 1)

    def expected_stats(self, date, key):
        log = key in ("market_cap", "volume")
        xs = []
        for r in self.sep_rows:
            if r["date"] != date or r.get("security_type") in LF.sectype.NON_OPERATING:
                continue
            v = r.get(key)
            if key in LF._HISTORY_PRICE_FIELDS and (r.get("price_stale") or r.get("price_date") != date):
                v = None
            if v is None or (log and v <= 0):
                continue
            xs.append(math.log10(v) if log else float(v))
        return LF._ledger_stat(xs)

    def test_universe_stats_follow_the_engine(self):
        u = self.load("_universe.json")
        self.assertEqual(u["d"], ["2026-08-31", "2026-09-01", "2026-09-02"])
        self.assertEqual(u["n"][1], 25)                     # the note and the shell are out
        self.assertEqual(set(u["m"]), {m["key"] for m in LF.LEDGER_METRICS})
        for key in ("pe", "market_cap", "volume", "gross_margin", "insider_ownership"):
            for di, d in ((1, "2026-09-01"), (2, "2026-09-02")):
                with self.subTest(key=key, date=d):
                    st = self.expected_stats(d, key)
                    m = u["m"][key]
                    self.assertAlmostEqual(m["c"][di], st["center"], places=5)
                    self.assertAlmostEqual(m["s"][di], st["scale"], places=5)
                    self.assertEqual(m["n"][di], st["n"])
        self.assertEqual(u["m"]["pe"]["n"][2], 23)          # T03 and T04 are stale that day
        self.assertEqual(u["m"]["insider_ownership"]["sd"], [0, 1, 2])   # MAD 0: mean and sd
        self.assertIsNone(u["m"]["roe_ttm"]["c"][0])

    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_universe_stats_match_zengine(self):
        """The same day's rows through web/zengine.js: its centre and scale, and T05's z."""
        d = "2026-09-02"
        rows = []
        for r in self.sep_rows:
            if r["date"] != d:
                continue
            r = dict(r)
            if r.get("price_stale") or r.get("price_date") != d:
                for k in LF._HISTORY_PRICE_FIELDS:
                    r.pop(k, None)
            rows.append(r)
        script = """
const Z = require(process.argv[1]);
const rows = JSON.parse(process.argv[2]), metrics = JSON.parse(process.argv[3]), nonop = JSON.parse(process.argv[4]);
const A = {stocks: {ticker: [], sector: [], kind: []}, vals: {}, metrics: metrics, nonop: nonop, minCohort: 20};
metrics.forEach(m => A.vals[m.key] = []);
rows.forEach(r => { A.stocks.ticker.push(r.ticker); A.stocks.sector.push(''); A.stocks.kind.push(r.security_type || 'operating');
  metrics.forEach(m => A.vals[m.key].push(typeof r[m.key] === 'number' ? r[m.key] : null)); });
Z.init(A); const U = Z.compute({scope: 'universe'}); const out = {};
metrics.forEach(m => { const s = U.stats[m.key]; out[m.key] = s && s.n ? [s.center, s.scale, U.z[m.key][5]] : null; });
console.log(JSON.stringify(out));
"""
        res = subprocess.run(["node", "-e", script, str(H.REPO / "web" / "zengine.js"), json.dumps(rows),
                              json.dumps(LF.LEDGER_METRICS), json.dumps(sorted(LF.sectype.NON_OPERATING))],
                             capture_output=True, text=True, timeout=60)
        self.assertEqual(res.returncode, 0, res.stderr)
        eng = json.loads(res.stdout)
        u = self.load("_universe.json")
        t05 = self.load("T05.json")
        for key in ("pe", "market_cap", "volume", "gross_margin", "insider_ownership"):
            with self.subTest(key=key):
                c, s, z = eng[key]
                self.assertAlmostEqual(u["m"][key]["c"][2], c, places=5)
                self.assertAlmostEqual(u["m"][key]["s"][2], s, places=5)
                v = t05["v"][key][2]
                if key in ("market_cap", "volume"):
                    v = math.log10(v)
                page_z = max(-5, min(5, (v - u["m"][key]["c"][2]) / u["m"][key]["s"][2]))
                self.assertAlmostEqual(page_z, z, places=3)


if __name__ == "__main__":
    unittest.main()
