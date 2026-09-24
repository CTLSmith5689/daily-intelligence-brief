"""The director's news pack and the headline-label check (theses/bin/news_pack.py,
theses/bin/news_labels_check.py), and where they meet director_inputs.py,
director_check.py and the research page.

Fixtures are in tests/fixtures/news_pack/: a site folder (news/ and prices/, laid
out like gh-pages) and a panel file with a news_count_7d history. Nothing here
touches the network or writes into the repository.
"""
import contextlib
import io
import json
import os
import sys
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests import helpers as H  # noqa: E402

import queue  # noqa: E402,F401  (theses/bin/queue.py would shadow it)
_saved_path = list(sys.path)
sys.path.insert(0, str(H.REPO / "theses" / "bin"))
import news_pack as N  # noqa: E402
import news_labels_check as NL  # noqa: E402
import director_check as DC  # noqa: E402
import director_inputs as DI  # noqa: E402
sys.path[:] = _saved_path

FIX = H.REPO / "tests" / "fixtures" / "news_pack"
EM, EN = chr(0x2014), chr(0x2013)
ASOF = datetime(2026, 9, 24, 20, 0, tzinfo=N.EASTERN)
SCOPE = {"ACME": ["covered"], "QUIET": ["held: lg-growth"], "CON": ["covered"], "NEWCO": ["screen top"]}


def build(top=2, scope=None):
    _, rows, hist = N.panel_history(FIX / "fundamentals")
    with mock.patch.object(N, "TOP_ABNORMAL", top):
        return N.build(ASOF, {k: list(v) for k, v in (scope or SCOPE).items()},
                       N.DirSite(FIX / "site"), rows, hist, week_of=date(2026, 9, 28))


def by_title(pack, ticker, start):
    n = next(n for n in pack["names"] if n["ticker"] == ticker)
    return next(h for h in n["headlines"] if h["title"].startswith(start))


class SourceTiers(unittest.TestCase):
    cfg = N.load_sources()

    def tier(self, source, title="A plain headline", link="https://news.google.com/rss/articles/x"):
        return N.tier_of(source, link, title, self.cfg)[0]

    def test_named_sources(self):
        for s in ("Reuters", "Bloomberg.com", "WSJ", "Barron's", "PR Newswire", "Business Wire", "GlobeNewswire"):
            self.assertEqual(self.tier(s), 1, s)
        for s in ("CNBC", "MarketWatch", "Financial Times", "Investor's Business Daily", "Yahoo Finance",
                  "Fortune", "Fierce Biotech"):
            self.assertEqual(self.tier(s), 2, s)
        for s in ("Simply Wall St", "GuruFocus", "MarketBeat", "Zacks Investment Research", "Quiver Quantitative",
                  "Seeking Alpha", "The Motley Fool", "InvestorPlace", "Benzinga", "TipRanks", "Nasdaq"):
            self.assertEqual(self.tier(s), 3, s)

    def test_unknown_is_tier_3_and_case_and_www_do_not_matter(self):
        self.assertEqual(self.tier("careplusvn.com"), 3)
        self.assertEqual(self.tier("A Blog Nobody Has Heard Of"), 3)
        self.assertEqual(self.tier("reuters"), 1)
        self.assertEqual(self.tier("www.reuters.com"), 1)

    def test_domains_from_the_source_or_a_direct_link(self):
        self.assertEqual(self.tier("finance.yahoo.com"), 2)
        self.assertEqual(self.tier("simplywall.st"), 3)
        self.assertEqual(self.tier("", link="https://www.sec.gov/Archives/x.htm"), 1)
        self.assertEqual(self.tier("", link="https://news.google.com/rss/articles/abc"), 3)
        self.assertEqual(self.tier("", link="https://www.evilreuters.com/x"), 3)

    def test_syndicated_aggregator_titles_drop_to_tier_3_but_tier_1_never_does(self):
        self.assertEqual(self.tier("Yahoo Finance", "Cardinal Health (CAH) Stock Moves -1.64%: What You Should Know"), 3)
        self.assertEqual(self.tier("finance.yahoo.com", "Is Acme (NYSE:ACME) Undervalued After Its Slide?"), 3)
        self.assertEqual(self.tier("Yahoo Finance", "Acme names a new chief executive"), 2)
        self.assertEqual(self.tier("Reuters", "Here's Why Acme Fell"), 1)
        _, rule = N.tier_of("Yahoo Finance", "", "Acme (ACME) Stock Moves -1%: What You Should Know", self.cfg)
        self.assertIn("title pattern", rule)

    def test_the_sources_file_documents_the_rule(self):
        cfg = json.loads((H.REPO / "theses" / "news_sources.json").read_text(encoding="utf-8"))
        self.assertIn("Unknown", " ".join(cfg["_about"]).replace("matches nothing", "Unknown"))
        self.assertEqual(sorted(cfg["tiers"]), ["1", "2", "3"])


class PriceClaims(unittest.TestCase):
    def claim(self, title, ticker="ACME", name="Acme Corp"):
        return N.price_claim(title, ticker, name)

    def test_claims_are_read_with_their_sign(self):
        self.assertEqual(self.claim("Acme shares fall 6.1% after warning")["claimed"], -6.1)
        self.assertEqual(self.claim("Acme (ACME) Is Down 6.1% After Trump Floats Tariffs")["claimed"], -6.1)
        self.assertEqual(self.claim("Acme stock up 12% on deal")["claimed"], 12.0)
        self.assertEqual(self.claim("Acme posts a 3% gain")["claimed"], 3.0)
        self.assertEqual(self.claim("Acme (ACME) Stock Moves -1.64%: What You Should Know")["claimed"], -1.64)
        self.assertEqual(self.claim("Acme Ends Down by 1.33% at $5.20")["claimed"], -1.33)

    def test_what_is_not_a_one_session_price_claim(self):
        self.assertIsNone(self.claim("Acme names a new chief executive"))
        cases = {
            "Acme revenue up 12% in the quarter": "figure",
            "Acme Q2 Earnings: EPS Surges 62.9% Above Expectations": "figure",
            "Short Interest in Acme (NASDAQ:ACME) Drops By 44.9%": "figure",
            "Acme Down 7.2% Since Last Earnings Report": "period",
            "Acme Stock Gains 20.7% in Three Months": "period",
            "A 6-Day Winning Streak Has Acme Stock Up 28%": "period",
            "Acme Stock Looks Overvalued As Shares Fell 77%": "period",
            "Acme shares jump 8% after hours on earnings": "outside market hours",
            "Acme shares jump 8% in premarket trading": "outside market hours",
            "Does Acme Have the Potential to Rally 27.97% as Analysts Expect?": "forecast",
            "Meta stock soars 11% on strong ad sales": "not tied",
        }
        for title, why in cases.items():
            got = self.claim(title)
            self.assertIn("skip", got, title)
            self.assertIn(why, got["skip"], (title, got))

    def test_several_claims_take_the_one_about_this_company(self):
        t = "Lam Research Climbs 5% as Chip Names Outrun the Sector; Applied Materials Rises 4%, KLA Gains 3%"
        self.assertEqual(N.price_claim(t, "AMAT", "Applied Materials Inc")["claimed"], 4.0)
        self.assertEqual(N.price_claim(t, "LRCX", "Lam Research Corp")["claimed"], 5.0)
        self.assertIn("skip", N.price_claim(t, "TGT", "Target Corp"))

    def test_checked_against_exact_stored_closes_only(self):
        cal = ["2026-09-17", "2026-09-18", "2026-09-21", "2026-09-22", "2026-09-23"]
        closes = [("2026-09-17", 100.0), ("2026-09-18", 102.0), ("2026-09-21", 102.0),
                  ("2026-09-22", 100.0), ("2026-09-23", 94.0)]
        c = {"claimed": -6.0, "text": "fall 6%"}
        got = N.check_claim(c, date(2026, 9, 23), closes, cal)
        self.assertEqual((got["status"], got["closest"]["session"]), ("match", "2026-09-23"))
        # The session before counts too: a morning story about yesterday's fall.
        got = N.check_claim({"claimed": -2.0, "text": "x"}, date(2026, 9, 23), closes, cal)
        self.assertEqual((got["status"], got["closest"]["session"]), ("match", "2026-09-22"))
        got = N.check_claim({"claimed": 5.0, "text": "x"}, date(2026, 9, 23), closes, cal)
        self.assertEqual(got["status"], "mismatch")
        self.assertGreater(got["gap_pp"], N.MISMATCH_PP)
        # A weekend headline speaks of the Friday before.
        got = N.check_claim({"claimed": 2.0, "text": "x"}, date(2026, 9, 20), closes, cal)
        self.assertEqual((got["status"], got["closest"]["session"]), ("match", "2026-09-18"))
        # No close stored for the day: unverifiable, never interpolated.
        got = N.check_claim(c, date(2026, 9, 24), closes, cal)
        self.assertEqual(got["status"], "unverifiable")
        self.assertIn("2026-09-24 yet", got["reason"])
        self.assertEqual(N.check_claim(c, date(2026, 9, 23), [], cal)["status"], "unverifiable")

    def test_a_missing_session_is_never_bridged(self):
        # 2026-09-22 was a session (the calendar has it) but this name's file lacks it,
        # so 09-21 to 09-23 is a two-day move and cannot show a mismatch.
        cal = ["2026-09-18", "2026-09-21", "2026-09-22", "2026-09-23"]
        closes = [("2026-09-18", 100.0), ("2026-09-21", 101.0), ("2026-09-23", 90.0)]
        got = N.check_claim({"claimed": -6.0, "text": "x"}, date(2026, 9, 23), closes, cal)
        self.assertEqual(got["status"], "unverifiable")
        self.assertEqual(N.session_calendar([closes, [("2026-09-22", 1.0)]]), sorted(set(cal[1:]) | {"2026-09-18"}))


class Dedupe(unittest.TestCase):
    def test_same_story_keeps_the_highest_tier(self):
        items = [{"id": "a", "title": "Acme Shares Fall 6% After Profit Warning", "tier": 2, "ts": 2, "source": "Yahoo Finance"},
                 {"id": "b", "title": "Acme shares fall 6% after profit warning", "tier": 1, "ts": 3, "source": "Reuters"},
                 {"id": "c", "title": "Acme opens a plant in Ohio", "tier": 3, "ts": 1, "source": "Blog"}]
        kept, removed = N.dedupe(items)
        self.assertEqual(removed, 1)
        self.assertEqual([k["id"] for k in kept], ["b", "c"])
        self.assertEqual(kept[0]["duplicates"], [{"id": "a", "source": "Yahoo Finance", "tier": 2}])
        self.assertFalse(N.same_story("Acme opens a plant in Ohio", "Acme closes a plant in Texas after losses"))


class ThePack(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pack = build()

    def test_totals(self):
        t = self.pack["totals"]
        self.assertEqual(t["names_in_scope"], 5)
        self.assertEqual((t["tier1"], t["tier2"], t["tier3"]), (7, 3, 5))
        self.assertEqual(t["duplicates_removed"], 9)
        self.assertEqual((t["price_claims_checked"], t["price_claims_matched"], t["price_claims_mismatched"],
                          t["price_claims_unverifiable"], t["price_claims_not_checked"]), (3, 2, 1, 1, 1))
        self.assertEqual(t["scope_reasons"], {"covered": 2, "held": 1, "screen top": 1, "abnormal volume": 2})

    def test_window_and_dates(self):
        titles = [h["title"] for n in self.pack["names"] for h in n["headlines"]]
        self.assertNotIn("Acme headline from before the relevance fix", titles)
        self.assertNotIn("Acme headline after the as-of time", titles)
        self.assertNotIn("Acme headline with no date", titles)
        self.assertTrue(all(" - " + "Reuters" not in t for t in titles))
        h = by_title(self.pack, "ACME", "Acme climbs 2%")
        self.assertEqual((h["date"], h["tier"], h["lm"], h["vader"]), ("2026-09-20", 2, 1.0, 0.3))

    def test_abnormal_volume(self):
        top = self.pack["abnormal_top"]
        self.assertEqual([a["ticker"] for a in top], ["LOUD", "ACME"])
        loud = top[0]
        # The 15s on 09-10 and 09-11 are before NEWS_FIX_DATE and are never read.
        self.assertEqual((loud["count_7d"], loud["baseline"], loud["baseline_obs"], loud["method"], loud["ratio"]),
                         (8, 1.0, 4, "own", 4.5))
        names = {n["ticker"]: n for n in self.pack["names"]}
        self.assertEqual(names["ACME"]["why"], ["covered", "abnormal volume, rank 2"])
        # SPAM is loud but all tier 3, so it is not ranked.
        self.assertNotIn("SPAM", names)
        self.assertEqual(names["NEWCO"]["volume"]["method"], "universe median")
        self.assertEqual(self.pack["rules"]["universe_median_7d"], 3.0)
        with_newco = build(top=3)
        self.assertEqual([a["ticker"] for a in with_newco["abnormal_top"]], ["LOUD", "ACME", "NEWCO"])

    def test_baseline_with_little_history(self):
        self.assertEqual(N.baseline_for([("2026-09-14", 2.0), ("2026-09-15", 4.0), ("2026-09-16", 6.0)],
                                         date(2026, 9, 24), 9.0), (4.0, 3, "own"))
        self.assertEqual(N.baseline_for([("2026-09-14", 2.0), ("2026-09-23", 9.0)], date(2026, 9, 24), 9.0),
                         (2.0, 1, "own, thin"))
        self.assertEqual(N.baseline_for([("2026-09-23", 9.0)], date(2026, 9, 24), 7.0), (7.0, 0, "universe median"))

    def test_per_name_figures(self):
        acme = next(n for n in self.pack["names"] if n["ticker"] == "ACME")
        self.assertEqual(acme["counts"], {"tier1": 3, "tier2": 2, "tier3": 3})
        # Averages over tier 1 and 2 only, LM skipping a null.
        self.assertEqual(acme["lm_avg_tier12"], 0.4)
        self.assertEqual(acme["vader_avg_tier12"], 0.12)
        mb = by_title(self.pack, "ACME", "Acme (NYSE:ACME) Stock Price Down 12%")
        self.assertEqual(mb["price_claim"]["status"], "mismatch")
        self.assertEqual(mb["flags"], ["price claim mismatch"])
        self.assertEqual(acme["flagged"], [mb["id"]])
        self.assertEqual(by_title(self.pack, "ACME", "Acme shares fall 6%")["price_claim"]["status"], "match")
        self.assertEqual(by_title(self.pack, "ACME", "Acme rises 5%")["price_claim"]["status"], "unverifiable")
        self.assertEqual(by_title(self.pack, "ACME", "Acme shares fall 6%")["duplicates"][0]["source"], "Yahoo Finance")
        # A Windows device name is read from its _ file.
        con = next(n for n in self.pack["names"] if n["ticker"] == "CON")
        self.assertEqual(con["counts"]["tier1"], 1)

    def test_ids_are_stable_and_batches_hold_tier_1_and_2(self):
        again = build()
        ids = lambda p: [h["id"] for n in p["names"] for h in n["headlines"]]  # noqa: E731
        self.assertEqual(ids(self.pack), ids(again))
        batch = self.pack["label_batches"][0]
        tiers = {h["id"]: h["tier"] for n in self.pack["names"] for h in n["headlines"]}
        self.assertTrue(all(tiers[i] <= 2 for i in batch))
        self.assertEqual(len(batch), 10)

    def test_tier_3_only_names_get_a_few_tier_3_labels(self):
        pack = build(scope={"SPAM": ["covered"]})
        spam = next(n for n in pack["names"] if n["ticker"] == "SPAM")
        self.assertTrue(spam["headlines"])
        self.assertTrue(all("tier 3 only" in h["flags"] for h in spam["headlines"]))
        spam_ids = [i for b in pack["label_batches"] for i in b if i.startswith("SPAM-")]
        self.assertEqual(len(spam_ids), min(N.TIER3_ONLY_LABEL_CAP, len(spam["headlines"])))

    def test_summary_and_files_have_no_dashes(self):
        md = N.summary(self.pack)
        self.assertIn("Headlines are leads, never facts", md)
        self.assertIn("## Price claims that do not match the stored closes", md)
        self.assertIn("Stock Price Down 12%", md)
        text = md + json.dumps(self.pack, ensure_ascii=False)
        self.assertNotIn(EM, text)
        self.assertNotIn(EN, text)
        self.assertEqual(N.dash_free("A " + EM + " B " + EN + " C"), "A, B - C")

    def test_run_writes_news_json_and_md(self):
        with H.temp_dir() as d:
            scope = {"covered": ["ACME"], "held": {"QUIET": ["lg-growth"]}, "candidates": {}, "screen_top": []}
            with mock.patch.object(N, "plan_tickers", lambda w: ["CON"]):
                pack = N.run(date(2026, 9, 28), ASOF, news_scope=scope, out_dir=d, site_dir=FIX / "site",
                             fund_dir=FIX / "fundamentals")
            self.assertTrue((d / "news.md").exists())
            saved = json.loads((d / "news.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["generated_for"]["week_of"], "2026-09-28")
            self.assertEqual(saved["generated_for"]["panel_date"], "2026-09-23")
            why = {n["ticker"]: n["why"] for n in pack["names"]}
            self.assertEqual(why["CON"], ["in this week's plan"])
            self.assertEqual(why["QUIET"], ["held: lg-growth"])
            self.assertIn("news_pack:", N.headline_line(pack))


class Labels(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.news = build()
        cls.batch = cls.news["label_batches"][0]

    def good(self, ids=None):
        return {i: {"relevant_to_company": True, "event_type": "earnings", "tone": -1, "checkable_claim": None}
                for i in (ids or self.batch)}

    def test_a_good_batch_passes(self):
        fails, _ = NL.check_labels(self.good(), self.news, self.batch)
        self.assertEqual(fails, [])

    def test_schema_failures(self):
        i = self.batch[0]
        bad = [
            {"relevant_to_company": "yes", "event_type": "earnings", "tone": 0, "checkable_claim": None},
            {"relevant_to_company": True, "event_type": "rumour", "tone": 0, "checkable_claim": None},
            {"relevant_to_company": True, "event_type": "noise", "tone": 3, "checkable_claim": None},
            {"relevant_to_company": True, "event_type": "noise", "tone": True, "checkable_claim": None},
            {"relevant_to_company": True, "event_type": "noise", "tone": 1.5, "checkable_claim": None},
            {"relevant_to_company": True, "event_type": "noise", "tone": 0},
            {"relevant_to_company": True, "event_type": "noise", "tone": 0, "checkable_claim": None, "why": "x"},
            {"relevant_to_company": True, "event_type": "noise", "tone": 0, "checkable_claim": ""},
            {"relevant_to_company": True, "event_type": "noise", "tone": 0, "checkable_claim": "x" * 201},
            {"relevant_to_company": True, "event_type": "noise", "tone": 0, "checkable_claim": "a " + EM + " b"},
        ]
        for lab in bad:
            labels = self.good()
            labels[i] = lab
            fails, _ = NL.check_labels(labels, self.news, self.batch)
            self.assertTrue(fails, lab)

    def test_ids_must_match_the_batch_and_news_json(self):
        labels = self.good()
        labels["ACME-0000000000"] = labels[self.batch[0]]
        fails, _ = NL.check_labels(labels, self.news, self.batch)
        self.assertTrue(any("not a headline id" in f for f in fails), fails)
        fails, _ = NL.check_labels(self.good(self.batch[1:]), self.news, self.batch)
        self.assertTrue(any("have no label" in f for f in fails), fails)
        spam = next(h["id"] for n in self.news["names"] for h in n["headlines"] if h["tier"] == 3)
        fails, _ = NL.check_labels(self.good(self.batch + [spam]), self.news, self.batch)
        self.assertTrue(any("outside the batch" in f for f in fails), fails)

    def test_merge_show_and_final_check(self):
        with H.temp_dir() as d:
            news_path = d / "news.json"
            news_path.write_text(json.dumps(self.news), encoding="utf-8")
            folder, labels_path = d / "batches", d / "news_labels.json"
            folder.mkdir()
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(NL.main(["--news", str(news_path), "--show-batch", "1"]), 0)
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(NL.main(["--news", str(news_path), "--show-batch", "9"]), 2)
            lines = [json.loads(l) for l in out.getvalue().splitlines()]
            self.assertEqual([l["id"] for l in lines], self.batch)
            self.assertEqual(set(lines[0]), {"id", "company", "source", "title"})
            # A broken batch file fails and is named for relabelling.
            (folder / "batch-01.json").write_text("{not json", encoding="utf-8")
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = NL.main(["--news", str(news_path), "--merge", str(folder), "--labels", str(labels_path)])
            self.assertEqual(rc, 1)
            self.assertIn("Label these batches again: 1", out.getvalue())
            # Fixed, it merges and the final check passes.
            (folder / "batch-01.json").write_text(json.dumps(self.good()), encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()):
                rc = NL.main(["--news", str(news_path), "--merge", str(folder), "--labels", str(labels_path)])
                self.assertEqual(rc, 0)
                self.assertEqual(NL.main(["--news", str(news_path), "--labels", str(labels_path)]), 0)
            saved = json.loads(labels_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["week_of"], "2026-09-28")
            self.assertEqual(sorted(saved["labels"]), sorted(self.batch))
            # Last week's labels are not carried into this week's file.
            saved["week_of"] = "2026-09-21"
            saved["labels"]["OLD-1"] = saved["labels"][self.batch[0]]
            labels_path.write_text(json.dumps(saved), encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(NL.main(["--news", str(news_path), "--labels", str(labels_path)]), 1)
                NL.main(["--news", str(news_path), "--merge", str(folder), "--labels", str(labels_path)])
                self.assertEqual(NL.main(["--news", str(news_path), "--labels", str(labels_path)]), 0)
            self.assertNotIn("OLD-1", json.loads(labels_path.read_text(encoding="utf-8"))["labels"])

    def test_a_missing_batch_file_fails(self):
        with H.temp_dir() as d:
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(NL.merge(self.news, d, d / "labels.json"), 1)


class DirectorWiring(unittest.TestCase):
    def test_change_check_allows_the_news_inputs_only(self):
        ok = [("M", "theses/director/inputs/news.json"), ("A", "theses/director/inputs/news.md"),
              ("A", "theses/director/inputs/news_labels.json"), ("A", "theses/director/inputs/2026-09-28.json"),
              ("A", "theses/director/inputs/2026-09-28.md"), ("A", "theses/director/2026-09-27.md")]
        self.assertEqual(DC.check_changes(ok, {}.get, {}.get), [])
        for bad in (("A", "theses/director/inputs/batch-01.json"), ("A", "theses/director/inputs/notes.txt"),
                    ("D", "theses/director/inputs/news.json")):
            self.assertTrue(DC.check_changes([bad], {}.get, {}.get), bad)

    def test_a_news_failure_never_stops_the_inputs(self):
        with H.temp_dir() as d, mock.patch.object(N, "run", side_effect=RuntimeError("boom")):
            with contextlib.redirect_stderr(io.StringIO()) as err:
                got = DI.news_step(date(2026, 9, 28), {}, d, [])
        self.assertEqual(got, {"error": "RuntimeError: boom"})
        self.assertIn("plan without headlines", err.getvalue())

    def test_the_summary_points_at_the_news(self):
        base = {"generated_for": {"today": "2026-09-27", "week_of": "2026-09-28", "plan_file": "p",
                                  "last_week": ["a", "b"]},
                "slots": {"days": [], "per_day": 4, "max_per_sector_per_day": 1},
                "holdings_without_memo": [], "candidates_without_memo": [], "earnings_ahead": [],
                "earnings_window": ["a", "b"], "stale_views": [], "screen": {}, "last_week_memos": [],
                "last_plan": None, "desk_scorecard": [], "coverage_by_desk": [], "pm_open_questions": []}
        md = DI.summary(dict(base, news={"error": "RuntimeError: boom"}))
        self.assertIn("The news pack could not be built", md)
        md = DI.summary(dict(base, news={"line": "3 names"}))
        self.assertIn("theses/director/inputs/news.md", md)

    def test_instructions(self):
        text = (H.REPO / "theses" / "DIRECTOR.md").read_text(encoding="utf-8")
        for needle in ("=== 2. LABEL THE HEADLINES ===", "Haiku", "never browse", "news_labels_check.py --merge",
                       "Headlines are leads, never facts", "tier and date", "news_pack.py --no-fetch --same-asof",
                       "=== 10. REPORT ==="):
            self.assertIn(needle, text)
        prompts = (H.REPO / "theses" / "PROMPTS.md").read_text(encoding="utf-8")
        self.assertIn("A headline in the director's plan is a lead to verify in filings, not a source.", prompts)
        for f in ("theses/bin/news_pack.py", "theses/bin/news_labels_check.py", "theses/news_sources.json",
                  "theses/DIRECTOR.md"):
            t = (H.REPO / f).read_text(encoding="utf-8")
            self.assertNotIn(EM, t, f)
            self.assertNotIn(EN, t, f)

    def test_no_model_is_called_from_code(self):
        for f in ("news_pack.py", "news_labels_check.py"):
            t = (H.REPO / "theses" / "bin" / f).read_text(encoding="utf-8").lower()
            for word in ("anthropic", "api_key", "openai", "import requests"):
                self.assertNotIn(word, t, f)


class ResearchPage(unittest.TestCase):
    def test_plan_news_for_the_plans_week_only(self):
        LF = H.LF
        with H.temp_dir() as d:
            inputs = d / "inputs"
            inputs.mkdir()
            pack = build()
            acme = next(n for n in pack["names"] if n["ticker"] == "ACME")
            acme["headlines"][0]["title"] = "<script>x</script> Acme"
            (inputs / "news.json").write_text(json.dumps(pack), encoding="utf-8")
            irrelevant = next(h["id"] for h in acme["headlines"] if h["title"].startswith("Acme names"))
            labels = {h["id"]: {"relevant_to_company": True, "event_type": "noise", "tone": -2,
                                "checkable_claim": None} for h in acme["headlines"] if h["tier"] <= 2}
            labels[irrelevant]["relevant_to_company"] = False
            (inputs / "news_labels.json").write_text(json.dumps({"week_of": "2026-09-28", "labels": labels}),
                                                    encoding="utf-8")
            with H.patched(LF, DIRECTOR_PLANS=d):
                news = LF._director_plan_news("2026-09-28")
                other = LF._director_plan_news("2026-10-05")
        self.assertEqual(other, {})
        items = news["ACME"]
        self.assertTrue(all(i["tier"] in (1, 2) for i in items))
        self.assertLessEqual(len(items), LF.DIRECTOR_NEWS_PER_NAME)
        self.assertNotIn("Acme names a new chief financial officer", [i["title"] for i in items])
        self.assertEqual(items[0]["tone"], "very negative")
        self.assertTrue(all(i["link"].startswith("https://") for i in items))

    def test_ledger_js_renders_and_escapes_the_news(self):
        js = (H.REPO / "web" / "ledger.js").read_text(encoding="utf-8")
        self.assertIn("News this week", js)
        self.assertIn("esc(n.title)", js)
        self.assertIn("newsBlock", js)


if __name__ == "__main__":
    unittest.main()
