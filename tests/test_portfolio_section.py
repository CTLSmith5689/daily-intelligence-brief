"""The research page's Portfolio section: read-only, labelled as a draft, and
honest when nothing is held.

The portfolio manager is a draft the owner has not approved. The section shows
what is held (from portfolio/books/, if construct.py has ever written one), the
draft limits, a plain account of the PM's week and the sample decision, and
decides nothing. The page is filled in by web/ledger.js, so these tests check the
data the pipeline embeds and the renderer that reads it.
"""
import json
import os
import re
import shutil
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests import helpers as H  # noqa: E402

LF = H.LF
EN_DASH = chr(0x2013)
DRAFTS = H.REPO / "portfolio" / "drafts"

EVENTS = (
    "event_id,date,ticker,kind,thesis_id,note_path,direction,conviction,target_price,"
    "horizon_days,prior_direction,prior_conviction,prior_target,claim_changed,trigger,rationale\n"
    "T01-2026-09-12-1,2026-09-12,T01,initiate,T01-2026-09-12,x,long,2,10,252,,,,,a,b\n"
    "T02-2026-09-12-1,2026-09-12,T02,initiate,T02-2026-09-12,x,watch,4,10,252,,,,,a,b\n"
    "T01-2026-09-19-2,2026-09-19,T01,revise,T01-2026-09-19,x,avoid,5,10,252,long,2,10,,a,b\n"
)


class PortfolioSection(unittest.TestCase):

    def render(self, book=None, events=EVENTS):
        """research.html from a temporary theses/ and portfolio/, and its page data."""
        with H.temp_dir() as tmp:
            docs, theses, portfolio = tmp / "docs", tmp / "theses", tmp / "portfolio"
            (theses / "ledger").mkdir(parents=True)
            (theses / "notes").mkdir()
            if events is not None:
                (theses / "ledger" / "events.csv").write_text(events, encoding="utf-8")
            shutil.copytree(DRAFTS, portfolio / "drafts")
            if book is not None:
                (portfolio / "books").mkdir()
                (portfolio / "books" / f"{book['date']}.json").write_text(
                    json.dumps(book), encoding="utf-8")
            docs.mkdir()
            with H.patched(LF, DOCS_DIR=docs, ASSETS_DIR=docs / "assets",
                           PRICES_DIR=docs / "prices", THESES_DIR=theses,
                           PORTFOLIO_DIR=portfolio), H.quiet():
                LF.generate_research({"date": "2026-09-21", "stocks": []})
            html = (docs / "research.html").read_text(encoding="utf-8")
        m = re.search(r"window\.APT_PAGE = (\{.*?\});\n", html)
        self.assertIsNotNone(m, "no page data")
        return html, json.loads(m.group(1))

    def test_section_renders_with_an_empty_book(self):
        html, cfg = self.render()
        p = cfg["portfolio"]
        self.assertEqual(p["book"]["positions"], [])
        self.assertEqual(p["book"]["date"], "")
        self.assertEqual(p["draftLabel"], "Draft, not approved by the owner")
        # The current view per ticker, newest event wins, and nothing is holdable.
        self.assertEqual([(v["ticker"], v["direction"], v["holdable"]) for v in p["views"]],
                         [("T01", "avoid", False), ("T02", "watch", False)])
        self.assertEqual({d["key"] for d in p["drafts"]}, {"instructions", "sample"})
        for d in p["drafts"]:
            with self.subTest(draft=d["key"]):
                self.assertIn("<h5>", d["html"])
                self.assertIn("<pre><code>", d["html"])
                self.assertIn("<table", d["html"])
        self.assertNotIn(H.EM_DASH, html)
        self.assertNotIn(EN_DASH, html)

    def test_section_renders_without_a_ledger(self):
        _, cfg = self.render(events=None)
        self.assertEqual(cfg["portfolio"]["views"], [])
        self.assertEqual(cfg["portfolio"]["book"]["positions"], [])

    def test_a_built_book_is_shown(self):
        book = {"date": "2026-09-21", "cash": 0.88, "positions": [
            {"ticker": "T01", "weight": 0.12, "conviction": "4", "direction": "long",
             "sector": "Energy", "thesis_id": "T01-2026-09-12", "volatility_1y": 0.3}]}
        _, cfg = self.render(book=book)
        b = cfg["portfolio"]["book"]
        self.assertEqual(b["date"], "2026-09-21")
        self.assertEqual(b["cash"], 0.88)
        self.assertEqual(b["positions"], [{"ticker": "T01", "weight": 0.12, "conviction": "4",
                                           "direction": "long", "sector": "Energy",
                                           "thesis_id": "T01-2026-09-12"}])

    def test_the_page_script_renders_the_section(self):
        js = (H.REPO / "web" / "ledger.js").read_text(encoding="utf-8")
        css = (H.REPO / "web" / "ledger.css").read_text(encoding="utf-8")
        self.assertIn("recordHTML(CFG.record) + portfolioHTML(CFG.portfolio)", js)
        for needle in ("function portfolioHTML(p)", "No positions yet.", "p.draftLabel",
                       '"Proposed limits", true', '"What the PM would do each Monday", true',
                       '"A sample decision on NVIDIA", true', '"Current holdings", false'):
            with self.subTest(needle=needle):
                self.assertIn(needle, js)
        self.assertIn(".ld-draft{", css)

    def test_the_summaries_match_the_drafts(self):
        """The plain-words limits and sample summary restate the drafts; their numbers must be there."""
        prompt = (DRAFTS / "PM-agent-draft.md").read_text(encoding="utf-8")
        sample = (DRAFTS / "PM-decision-sample.md").read_text(encoding="utf-8")
        for needle in ("more than 2%", "fewer than 10 of the", "half the rule weight",
                       "between 3% and 12%", "above 25%", "above 0.50", "more than 15 percent",
                       "less than 2 percentage points", "more than 0.25",
                       "Once 20 of your decisions", "Weekly, Monday 12:00 ET"):
            with self.subTest(needle=needle):
                self.assertIn(needle, prompt)
        for needle in ("11.3%", "12.0%", "3.7%", "7.4%", "4.4%", "$125 billion",
                       "55 days", "2026-11-17"):
            with self.subTest(needle=needle):
                self.assertIn(needle, sample)
        limits = " ".join(t for _, t, _ in LF.PORTFOLIO_DRAFT_LIMITS)
        for needle in ("2%", "10 of the", "3% and 12%", "25%", "0.50", "15%",
                       "2 percentage points", "0.25", "20 of the PM"):
            with self.subTest(needle=needle):
                self.assertIn(needle, limits)

    def test_drafts_render_escaped_and_dash_free(self):
        out = LF._draft_md_to_html("# Head <b>\n\n```\n<script>x</script>\n```\n\n"
                                   "Text <i>\n\n    indented <tag>\n\n1. one\n2. two\n")
        self.assertNotIn("<script>", out)
        self.assertNotIn("<b>", out)
        self.assertNotIn("<i>", out)
        self.assertIn("<h5>Head &lt;b&gt;</h5>", out)
        self.assertIn("<pre><code>&lt;script&gt;x&lt;/script&gt;</code></pre>", out)
        self.assertIn("<pre><code>indented &lt;tag&gt;</code></pre>", out)
        self.assertIn("<p>1. one</p><p>2. two</p>", out)
        for path in DRAFTS.glob("*.md"):
            with self.subTest(file=path.name):
                text = path.read_text(encoding="utf-8")
                self.assertNotIn(H.EM_DASH, text)
                self.assertNotIn(EN_DASH, text)


if __name__ == "__main__":
    unittest.main()
