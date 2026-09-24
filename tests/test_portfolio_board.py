"""The Portfolios board and the agents' instructions on Research and Portfolios.

The board is drawn by lambda_function._portfolio_board_html from
portfolio.engine.site_data, so these tests build a small ledger in a temporary
directory, run the engine over it and read the HTML. Nothing here touches the
committed ledger.
"""
import csv
import json
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests import helpers as H  # noqa: E402

LF = H.LF
from portfolio import engine as E  # noqa: E402

EN_DASH = chr(0x2013)
D0, D1 = "2026-09-22", "2026-09-23"
MINUS = "−"


def _panel(tmp, rows_by_day):
    tmp.mkdir(parents=True, exist_ok=True)
    cols = ["date", "ticker", "name", "sector", "security_type", "price", "market_cap"]
    with (tmp / "2026-09.csv").open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        for day, rows in rows_by_day:
            for r in rows:
                w.writerow(dict(r, date=day))


def _trade(tid, day, book, ticker, side, shares, price, cost=0, decision=""):
    return {"trade_id": tid, "date": day, "book": book, "ticker": ticker, "side": side,
            "shares": shares, "price": price, "cost": cost, "lot_id": "",
            "reason_code": "test", "decision_id": decision}


def board_fixture(tmp, ccc_close_today=False, bbb_prev_close=True):
    """A hedge book with a long (AAA), a short (BBB) and a long whose close is
    missing on D1 unless ccc_close_today (CCC); a neural book in cash; no style
    book started. The analyst has a memo on AAA."""
    rows = [{"ticker": "AAA", "name": "Alpha Industries", "sector": "Industrials",
             "security_type": "operating", "price": 55, "market_cap": 5e9},
            {"ticker": "BBB", "name": "Beta <Holdings> & Co", "sector": "Energy",
             "security_type": "operating", "price": 36, "market_cap": 3e9},
            {"ticker": "CCC", "name": "Gamma Corp", "sector": "Utilities",
             "security_type": "operating", "price": 10, "market_cap": 1e9}]
    _panel(tmp / "panel", [(D0, rows), (D1, rows)])
    prices = tmp / "prices"
    prices.mkdir()
    H.write_price_file(prices, "AAA", [D0, D1], [50.0, 55.0])
    if bbb_prev_close:
        H.write_price_file(prices, "BBB", [D0, D1], [40.0, 36.0])
    else:
        H.write_price_file(prices, "BBB", [D1], [36.0])
    H.write_price_file(prices, "CCC", [D0, D1] if ccc_close_today else [D0],
                       [10.0, 11.0] if ccc_close_today else [10.0])
    ledger = tmp / "ledger"
    E.append_rows(ledger / "trades.csv", E.TRADE_COLUMNS, [
        _trade("hedge-1", D0, "hedge", "CASH", "deposit", 1000000, 1),
        _trade("neural-1", D0, "neural", "CASH", "deposit", 1000000, 1),
        _trade("hedge-2", D0, "hedge", "AAA", "buy", 1000, 50.0, 25, "hedge-d2"),
        _trade("hedge-3", D0, "hedge", "BBB", "short", 500, 40.0, 10, "hedge-d2"),
        _trade("hedge-4", D0, "hedge", "CCC", "buy", 100, 10.0, 0.5, "hedge-d2"),
    ])
    E.append_rows(ledger / "decisions.csv", E.DECISION_COLUMNS, [
        {"date": D0, "book": "hedge", "decision_id": "hedge-d1", "action": "inception",
         "reason": "Start.", "author": "rules"},
        {"date": D0, "book": "hedge", "decision_id": "hedge-d2", "action": "trade",
         "reason": "First picks.", "author": "pm"},
        {"date": D0, "book": "neural", "decision_id": "neural-d1", "action": "inception",
         "reason": "Start.", "author": "rules"},
    ])
    events = tmp / "events.csv"
    with events.open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["event_id", "date", "ticker", "kind", "note_path", "direction"])
        w.writerow(["AAA-1", "2026-09-14", "AAA", "initiate",
                    "theses/notes/AAA/2026-09-14-initiation.md", "long"])
    return E.site_data(ledger_dir=ledger, books_dir=tmp / "books", panel_dir=tmp / "panel",
                       prices=E.PriceStore(prices), benchmarks=E.BenchmarkStore(prices),
                       nav_csv=tmp / "nav.csv", events=events, history=tmp / "history.csv")


def columns(html):
    """{book id: the column's HTML}."""
    out = {}
    for m in re.finditer(r'<section class="ld-kb-col[^"]*" aria-labelledby="kb-([^"]+)">(.*?)</section>',
                         html, re.S):
        out[m.group(1)] = m.group(2)
    return out


def cards(col):
    return {re.search(r'class="ld-kb-tk" href="[^"]+">([^<]+)<', c).group(1): c
            for c in re.findall(r'<article class="ld-kb-card[^"]*">.*?</article>', col, re.S)}


class Board(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        with H.temp_dir() as tmp:
            cls.data = board_fixture(tmp)
        with H.temp_dir() as tmp:
            cls.full = board_fixture(tmp, ccc_close_today=True)
        cls.html = LF._portfolio_board_html(cls.data)
        cls.cols = columns(cls.html)

    def test_one_column_per_book_in_four_groups(self):
        self.assertEqual(list(self.cols), ["lg-growth", "mid-growth", "sm-growth", "lg-value",
                                           "mid-value", "sm-value", "hedge", "neural"])
        self.assertEqual(re.findall(r'<p class="ld-kb-gh">([^<]+)</p>', self.html),
                         ["Style growth", "Style value", "Hedge", "Neural"])
        for bid, col in self.cols.items():
            self.assertIn(f'<a class="ld-kb-head" href="book.html#{bid}">', col)
        self.assertNotIn(H.EM_DASH, self.html)
        self.assertNotIn(EN_DASH, self.html)

    def test_empty_books_say_why(self):
        style = self.cols["lg-growth"]
        self.assertIn("Waiting for three years of history.", style)
        self.assertIn("Not started", style)
        self.assertNotIn("<article", style)
        self.assertIn('class="ld-kb-col ld-kb-wait"', self.html)
        neural = self.cols["neural"]
        self.assertIn("<b>Holds cash.</b> The PM builds this book on its next run.", neural)
        self.assertIn("$1,000,000", neural)
        self.assertRegex(neural, r"<dt>Cash</dt><dd[^>]*>100\.0%</dd>")
        self.assertRegex(neural, r"<dt>PM decision</dt><dd[^>]*>None yet</dd>")
        self.assertIn("Gross, net", neural)
        self.assertNotIn("Gross, net", style)

    def test_long_holding_card(self):
        c = cards(self.cols["hedge"])["AAA"]
        self.assertIn('<a class="ld-kb-tk" href="company.html#AAA">AAA</a>', c)
        self.assertNotIn("ld-kb-short", c)
        self.assertIn("Alpha Industries", c)
        self.assertIn("Industrials", c)
        self.assertRegex(c, r"<dt>Shares</dt><dd[^>]*>1,000</dd>")
        self.assertRegex(c, r"<dt>Cost</dt><dd[^>]*>\$50,000<span class=\"ld-kb-sub\">at \$50\.00</span>")
        self.assertRegex(c, r"<dt>Value</dt><dd[^>]*>\$55,000</dd>")
        self.assertRegex(c, r"<dt>Gain or loss</dt><dd class=\"ld-num ld-up\">\+10\.0%</dd>")
        self.assertRegex(c, r"<dt>Day</dt><dd class=\"ld-num ld-up\">\+10\.0%</dd>")
        self.assertIn('<a class="ld-kb-memo" href="company.html#AAA/thesis"', c)
        self.assertIn("14 Sep 2026", c)

    def test_short_holding_card(self):
        col = self.cols["hedge"]
        c = cards(col)["BBB"]
        self.assertIn('class="ld-kb-card ld-kb-short"', c)
        self.assertIn('<span class="ld-kb-tag">Short</span>', c)
        self.assertIn('<p class="ld-kb-div">Short positions</p>', col)
        self.assertLess(col.index("AAA"), col.index("Short positions"))
        self.assertLess(col.index("Short positions"), col.index("BBB"))
        self.assertRegex(c, r"<dt>Proceeds</dt><dd[^>]*>\$20,000<span class=\"ld-kb-sub\">sold at \$40\.00</span>")
        self.assertRegex(c, r"<dt>Value</dt><dd[^>]*>" + MINUS + r"\$18,000</dd>")
        # The price fell 10%, which is a 10% gain on a short.
        self.assertRegex(c, r"<dt>Gain or loss</dt><dd class=\"ld-num ld-up\">\+10\.0%</dd>")
        self.assertRegex(c, r"<dt>Day</dt><dd class=\"ld-num ld-dn\">" + MINUS + r"10\.0%</dd>")
        self.assertNotIn("ld-kb-memo", c)
        # The name is escaped, never markup.
        self.assertIn("Beta &lt;Holdings&gt; &amp; Co", c)
        self.assertNotIn("<Holdings>", self.html)

    def test_missing_close_is_na_and_the_column_is_partial(self):
        col = self.cols["hedge"]
        c = cards(col)["CCC"]
        self.assertIn("ld-kb-nopx", c)
        self.assertRegex(c, r"<dt>Value</dt><dd[^>]*>n/a</dd>")
        self.assertRegex(c, r"<dt>Gain or loss</dt><dd[^>]*>n/a</dd>")
        self.assertRegex(c, r"<dt>Day</dt><dd[^>]*>n/a</dd>")
        self.assertRegex(c, r'class="ld-kb-w ld-num" title="Weight">n/a<')
        self.assertIn("No stored close for 23 Sep 2026.", c)
        self.assertRegex(c, r"<dt>Shares</dt><dd[^>]*>100</dd>")
        self.assertIn("Partial: no usable close on 23 Sep 2026 for CCC", col)
        self.assertRegex(col, r"<dt>Value</dt><dd[^>]*>n/a \(partial\)</dd>")
        # Gross and net are left blank on a partial day rather than estimated.
        self.assertRegex(col, r"<dt>Gross, net</dt><dd[^>]*>n/a, n/a</dd>")
        self.assertNotIn("$1,000", c.split("Value")[1].split("</dd>")[0])

    def test_every_stat_comes_from_the_ledger(self):
        hedge = {b["id"]: b for b in self.full["books"]}["hedge"]
        # 968,964.50 in cash (1,000,000 - 50,025 + 19,990 - 1,000.50), plus 55,000 - 18,000 + 1,100.
        cash = 1000000 - 50000 - 25 + 20000 - 10 - 1000 - 0.5
        nav = cash + 55000 - 18000 + 1100
        self.assertAlmostEqual(hedge["nav"], nav, places=2)
        self.assertAlmostEqual(hedge["cashWeight"], round(cash / nav, 6), places=6)
        self.assertAlmostEqual(hedge["gross"], round((55000 + 1100 + 18000) / nav, 6), places=6)
        self.assertAlmostEqual(hedge["net"], round((55000 + 1100 - 18000) / nav, 6), places=6)
        self.assertEqual(hedge["lastPmDecision"]["date"], D0)
        html = LF._portfolio_board_html(self.full)
        col = columns(html)["hedge"]
        self.assertNotIn("Partial", col)
        self.assertRegex(col, r"<dt>Value</dt><dd[^>]*>\$1,007,064</dd>")
        self.assertRegex(col, r"<dt>Holdings</dt><dd[^>]*>3</dd>")
        self.assertRegex(col, r"<dt>Gross, net</dt><dd[^>]*>7%, 4%</dd>")
        self.assertRegex(col, r"<dt>PM decision</dt><dd[^>]*>22 Sep 2026</dd>")
        self.assertRegex(cards(col)["AAA"], r'title="Weight">5\.5%<')
        self.assertRegex(cards(col)["CCC"], r"<dt>Day</dt><dd class=\"ld-num ld-up\">\+10\.0%</dd>")

    def test_day_change_is_blank_without_the_previous_close(self):
        with H.temp_dir() as tmp:
            data = board_fixture(tmp, bbb_prev_close=False)
        hedge = {b["id"]: b for b in data["books"]}["hedge"]
        by = {h["ticker"]: h for h in hedge["holdings"]}
        self.assertIsNone(by["BBB"]["dayChg"])
        self.assertAlmostEqual(by["AAA"]["dayChg"], 0.1)
        c = cards(columns(LF._portfolio_board_html(data))["hedge"])["BBB"]
        self.assertRegex(c, r"<dt>Day</dt><dd[^>]*>n/a</dd>")
        self.assertRegex(c, r"<dt>Value</dt><dd[^>]*>" + MINUS + r"\$18,000</dd>")


class Pages(unittest.TestCase):

    def test_portfolios_and_research_pages_carry_the_board_and_the_instructions(self):
        with H.temp_dir() as tmp:
            board_fixture(tmp)
            (tmp / "portfolio").mkdir()
            (tmp / "ledger").rename(tmp / "portfolio" / "ledger")
            docs = tmp / "docs"
            docs.mkdir()
            theses = tmp / "theses"
            (theses / "ledger").mkdir(parents=True)
            with H.patched(LF, DOCS_DIR=docs, ASSETS_DIR=docs / "assets", PRICES_DIR=tmp / "prices",
                           PORTFOLIO_DIR=tmp / "portfolio", PORTFOLIO_NAV_CSV=tmp / "nav.csv",
                           FUNDAMENTALS_CSV_DIR=tmp / "panel", THESES_DIR=theses,
                           STYLE_HISTORY_CSV=tmp / "history.csv"), H.quiet():
                v = LF._write_ledger_assets()
                LF.generate_portfolios({"date": D1, "stocks": []}, v)
                LF.generate_research({"date": D1, "stocks": []}, v)
            pages = {n: (docs / n).read_text(encoding="utf-8")
                     for n in ("portfolios.html", "research.html")}
        cfg = {n: json.loads(re.search(r"window\.APT_PAGE = (\{.*?\});\n", h).group(1))
               for n, h in pages.items()}
        board = cfg["portfolios.html"]["board"]
        self.assertEqual(len(columns(board)), 8)
        self.assertIn('href="company.html#AAA"', board)
        # The board is data inside the script, so it cannot close the script tag.
        self.assertNotIn("<article", pages["portfolios.html"].split("window.APT_PAGE")[1].split("</script>")[0])
        how = cfg["portfolios.html"]["howPms"]
        self.assertEqual([p["name"] for p in how["pms"]], ["Style PM", "Neural PM"])
        self.assertEqual([p["schedule"] for p in how["pms"]], ["Mondays 12:00 ET", "not scheduled yet"])
        self.assertIn("ONLY the six style books", how["pms"][0]["html"])
        self.assertIn("and the hedge book", how["pms"][0]["html"])
        self.assertIn("ONLY the neural book", how["pms"][1]["html"])
        self.assertEqual([b["name"] for b in how["briefs"]],
                         ["Growth brief", "Value brief", "Hedge brief", "Neural brief"])
        self.assertEqual([b["path"] for b in how["briefs"]],
                         ["portfolio/books/growth.md", "portfolio/books/value.md",
                          "portfolio/books/hedge.md", "portfolio/books/neural.md"])
        self.assertIn("Margin of safety", how["briefs"][1]["html"])
        self.assertNotIn("Value brief</h5>", how["briefs"][1]["html"], "the file title is the summary")
        self.assertIn("=== 4. SIZE ===", how["prompt"])
        self.assertIn("<pre><code>You are the portfolio manager (PM)", how["prompt"])
        self.assertEqual(how["promptPath"], "portfolio/PROMPTS.md")
        self.assertEqual(cfg["research.html"]["howAnalyst"]["promptPath"], "theses/PROMPTS.md")
        self.assertNotIn("Agent 1: the analyst", how["prompt"])
        research = cfg["research.html"]["howAnalyst"]
        self.assertEqual(research["routine"]["schedule"], "Weekdays 07:00 ET")
        self.assertIn("Apterreon weekly analyst run.", research["routine"]["html"])
        self.assertNotIn("Schedule:", research["routine"]["html"], "the file header is not the prompt")
        self.assertIn("<pre><code>You are the analyst for Apterreon", research["prompt"])
        self.assertNotIn("You are the portfolio manager", research["prompt"])
        for name, html in pages.items():
            self.assertNotIn(H.EM_DASH, html, name)
            self.assertNotIn(EN_DASH, html, name)

    def test_the_page_script_draws_both_sections(self):
        js = (H.REPO / "web" / "ledger.js").read_text(encoding="utf-8")
        for needle in ("How the analyst works", "How the PMs work", "function pmInstructionsHTML(d)",
                       "function analystInstructionsHTML(d)", "cleanHtml(CFG.board)"):
            self.assertIn(needle, js)
        css = (H.REPO / "web" / "ledger.css").read_text(encoding="utf-8")
        self.assertIn("html .ld-kb{", css)
        # The board wraps into rows rather than scrolling sideways (owner: it looked cut off).
        self.assertRegex(css, r"html \.ld-kb-track\{[^}]*flex-wrap:wrap")
        self.assertNotRegex(css, r"html \.ld-kb\{[^}]*overflow-x:auto")


class Instructions(unittest.TestCase):

    def test_routine_files_are_in_the_repository_and_marked(self):
        style = (H.REPO / "portfolio" / "routines" / "style-pm.md").read_text(encoding="utf-8")
        self.assertIn("Schedule: Mondays 12:00 ET", style)
        routines = sorted(p.name for p in (H.REPO / "portfolio" / "routines").glob("*.md"))
        self.assertEqual(routines, ["neural-pm.md", "style-pm.md"])
        neural = (H.REPO / "portfolio" / "routines" / "neural-pm.md").read_text(encoding="utf-8")
        head = neural.split("\n---\n")[0]
        self.assertIn("Schedule: not scheduled yet", head)
        self.assertIn("It is not scheduled yet", head)
        for name, text, brief in (("style-pm.md", style, "portfolio/books/hedge.md"),
                                  ("neural-pm.md", neural, "portfolio/books/neural.md")):
            body = text.split("\n---\n", 1)[1]
            for needle in ("Follow portfolio/PROMPTS.md", "Never use the internet", "portfolio/bin/trade.py",
                           "Stage only portfolio/", "never git add -A", brief):
                self.assertIn(needle, body, name)
        research = (H.REPO / "theses" / "routines" / "research-agent.md").read_text(encoding="utf-8")
        self.assertIn('"## Agent 1: the analyst"', research)
        runbook = (H.REPO / "theses" / "RUNBOOK.md").read_text(encoding="utf-8")
        self.assertIn("theses/routines/research-agent.md", runbook)
        self.assertIn("The routine prompts are mirrored in the repository, by hand.", runbook)

    def test_the_renderer_escapes_everything_and_keeps_code(self):
        text = ("# Title <b>\n\nA line with <script>alert(1)</script> and `a<b`.\n\n"
                "```text\n  indented <img src=x onerror=y>\n    deeper\n```\n\n"
                "1. first\n2. second\n\n- bullet [x](javascript:alert(1))\n")
        html = LF._doc_md_to_html(text)
        self.assertNotIn("<script", html)
        self.assertNotIn("<img", html)
        self.assertNotIn('href="javascript', html)
        self.assertIn("<h5>Title &lt;b&gt;</h5>", html)
        self.assertIn("<code>a&lt;b</code>", html)
        self.assertIn("<pre><code>  indented &lt;img src=x onerror=y&gt;\n    deeper</code></pre>", html)
        self.assertIn("<p>1. first</p><p>2. second</p>", html)
        self.assertIn("<ul><li>bullet", html)

    def test_sections_are_read_from_prompts(self):
        with H.temp_dir() as tmp:
            p = tmp / "PROMPTS.md"
            p.write_text("# X\n\n## Agent 1: the analyst\n\nOne.\n\n## Agent 2: the PM\n\nTwo.\n\n## Other\n")
            self.assertEqual(LF._prompts_section("## Agent 1: the analyst", p), "One.")
            self.assertEqual(LF._prompts_section("## Agent 2: the PM", p), "Two.")
            self.assertEqual(LF._prompts_section("## Missing", p), "")
            r = tmp / "r.md"
            r.write_text("# R\n\nSchedule: Fridays 09:00 ET\n\n---\n\nDo <this>.\n")
            doc = LF._routine_doc(r)
            self.assertEqual(doc["schedule"], "Fridays 09:00 ET")
            self.assertEqual(doc["html"], "<p>Do &lt;this&gt;.</p>")
            self.assertIsNone(LF._routine_doc(tmp / "missing.md"))


if __name__ == "__main__":
    unittest.main()
