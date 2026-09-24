"""The News Desk's pack and the headline-label check (theses/bin/news_pack.py,
theses/bin/news_labels_check.py, theses/bin/news_desk_check.py), and where they
meet director_inputs.py, director_check.py, the dossier and the research page.

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
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests import helpers as H  # noqa: E402

import queue  # noqa: E402,F401  (theses/bin/queue.py would shadow it)
_saved_path = list(sys.path)
sys.path.insert(0, str(H.REPO / "theses" / "bin"))
import news_pack as N  # noqa: E402
import news_labels_check as NL  # noqa: E402
import news_desk_check as ND  # noqa: E402
import common as C  # noqa: E402
import dossier as DOS  # noqa: E402
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

    def test_tier_3_is_never_labelled(self):
        pack = build(scope={"SPAM": ["covered"]})
        spam = next(n for n in pack["names"] if n["ticker"] == "SPAM")
        self.assertTrue(spam["headlines"])
        self.assertTrue(all("tier 3 only" in h["flags"] for h in spam["headlines"]))
        spam_ids = [i for b in pack["label_batches"] for i in b if i.startswith("SPAM-")]
        self.assertEqual(spam_ids, [])

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
    def test_the_director_may_not_write_news(self):
        ok = [("A", "theses/director/inputs/2026-09-28.json"), ("A", "theses/director/inputs/2026-09-28.md"),
              ("A", "theses/director/2026-09-27.md")]
        self.assertEqual(DC.check_changes(ok, {}.get, {}.get), [])
        for bad in (("A", "theses/director/inputs/news.json"), ("A", "theses/director/inputs/news.md"),
                    ("A", "theses/director/inputs/news_labels.json"), ("A", "theses/director/inputs/batch-01.json"),
                    ("M", "theses/news/latest/news.json"), ("A", "theses/news/2026-09-27/news.json")):
            self.assertTrue(DC.check_changes([bad], {}.get, {}.get), bad)

    def test_director_inputs_never_builds_the_news(self):
        src = (H.REPO / "theses" / "bin" / "director_inputs.py").read_text(encoding="utf-8")
        self.assertNotIn("import news_pack", src)
        self.assertNotIn("news_pack.run", src)
        with H.temp_dir() as d, mock.patch.object(N, "run", side_effect=AssertionError("must not run")):
            got = DI.news_step(latest=d)
        self.assertFalse(got["fresh"])
        self.assertIn("no News Desk pack", got["why"])

    def test_fresh_and_stale_news(self):
        now = datetime(2026, 9, 27, 16, 0, tzinfo=N.EASTERN)
        with H.temp_dir() as d:
            write_latest(d, asof=datetime(2026, 9, 27, 15, 0, tzinfo=N.EASTERN), labels=True)
            got = DI.news_step(now=now, latest=d)
            self.assertTrue(got["fresh"])
            self.assertGreater(got["labels"], 0)
            self.assertIn("ACME", got["names"])
            # Friday's 06:00 pack, read on Sunday at 16:00: 58 hours old.
            write_latest(d, asof=datetime(2026, 9, 25, 6, 0, tzinfo=N.EASTERN), labels=True)
            stale = DI.news_step(now=now, latest=d)
        self.assertFalse(stale["fresh"])
        self.assertIn("stale", stale["why"])
        self.assertIn("58 hours old", stale["why"])

    def test_labels_for_another_pack_are_not_read(self):
        now = datetime(2026, 9, 27, 16, 0, tzinfo=N.EASTERN)
        with H.temp_dir() as d:
            write_latest(d, asof=datetime(2026, 9, 27, 15, 0, tzinfo=N.EASTERN), labels=True)
            lab = json.loads((d / "news_labels.json").read_text(encoding="utf-8"))
            lab["news_asof"] = "2026-09-25T10:00:00+00:00"
            (d / "news_labels.json").write_text(json.dumps(lab), encoding="utf-8")
            pack, labels, _ = C.news_freshness(d, now)
        self.assertIsNotNone(pack)
        self.assertEqual(labels, {})

    def test_the_summary_points_at_the_news(self):
        base = {"generated_for": {"today": "2026-09-27", "week_of": "2026-09-28", "plan_file": "p",
                                  "last_week": ["a", "b"]},
                "slots": {"days": [], "per_day": 4, "max_per_sector_per_day": 1},
                "holdings_without_memo": [], "candidates_without_memo": [], "earnings_ahead": [],
                "earnings_window": ["a", "b"], "stale_views": [], "screen": {}, "last_week_memos": [],
                "last_plan": None, "desk_scorecard": [], "coverage_by_desk": [], "pm_open_questions": []}
        md = DI.summary(dict(base, news={"path": "theses/news/latest/", "fresh": False,
                                         "why": "the News Desk pack is stale: 58 hours old"}))
        self.assertIn("No news this week: the News Desk pack is stale", md)
        self.assertIn("say so in the plan", md)
        md = DI.summary(dict(base, news={"path": "theses/news/latest/", "fresh": True, "why": "fresh",
                                         "line": "3 names"}))
        self.assertIn("theses/news/latest/news.md", md)

    def test_instructions(self):
        text = (H.REPO / "theses" / "DIRECTOR.md").read_text(encoding="utf-8")
        for needle in ("theses/news/latest/", "36 hours", "No news this week", "Never build or label the news",
                       "Headlines are leads, never facts", "tier and date", "Weigh tier 1 and 2 headlines only",
                       "price claim mismatch", "never drive an assignment", "=== 9. REPORT ==="):
            self.assertIn(needle, text)
        for gone in ("LABEL THE HEADLINES", "spawn a helper", "news_labels_check.py --merge", "--same-asof",
                     "theses/director/inputs/news"):
            self.assertNotIn(gone, text)
        desk = (H.REPO / "theses" / "NEWS_DESK.md").read_text(encoding="utf-8")
        for needle in ("git pull --rebase", "python3 theses/bin/news_pack.py --quiet", "--show-batch N",
                       "title and source only", "no outside", "Never browse", "relevant_to_company",
                       "event_type", "tone: a whole number from -2 to 2", "checkable_claim",
                       "news_labels_check.py --merge /tmp/news_labels", "news_desk_check.py --archive",
                       "git add theses/news/", "news_desk_check.py --changes", "git push", "14 days",
                       "at most 40 headlines", "=== 8. REPORT ==="):
            self.assertIn(needle, desk)
        for t in NL.EVENT_TYPES:
            self.assertIn(t, desk)
        prompts = (H.REPO / "theses" / "PROMPTS.md").read_text(encoding="utf-8")
        self.assertIn("A headline in the director's plan is a lead to verify in filings, not a source.", prompts)
        self.assertIn('"### News this week"', prompts)
        runbook = (H.REPO / "theses" / "RUNBOOK.md").read_text(encoding="utf-8")
        self.assertIn("## News Desk", runbook)
        for needle in ("Haiku 4.5", "duplicate it", "6:00 AM ET", "3:00 PM ET", "Connectors: none",
                       "theses/routines/news-desk.md"):
            self.assertIn(needle, runbook)
        for f in ("theses/bin/news_pack.py", "theses/bin/news_labels_check.py", "theses/news_sources.json",
                  "theses/DIRECTOR.md", "theses/NEWS_DESK.md", "theses/routines/news-desk.md",
                  "theses/bin/news_desk_check.py", "theses/news/README.md"):
            t = (H.REPO / f).read_text(encoding="utf-8")
            self.assertNotIn(EM, t, f)
            self.assertNotIn(EN, t, f)

    def test_no_model_is_called_from_code(self):
        for f in ("news_pack.py", "news_labels_check.py", "news_desk_check.py"):
            t = (H.REPO / "theses" / "bin" / f).read_text(encoding="utf-8").lower()
            for word in ("anthropic", "api_key", "openai", "import requests"):
                self.assertNotIn(word, t, f)


class ResearchPage(unittest.TestCase):
    def test_plan_news_from_the_news_desk_while_fresh(self):
        LF = H.LF
        with H.temp_dir() as d:
            pack = build()
            acme = next(n for n in pack["names"] if n["ticker"] == "ACME")
            acme["headlines"][0]["title"] = "<script>x</script> Acme"
            (d / "news.json").write_text(json.dumps(pack), encoding="utf-8")
            irrelevant = next(h["id"] for h in acme["headlines"] if h["title"].startswith("Acme names"))
            labels = {h["id"]: {"relevant_to_company": True, "event_type": "noise", "tone": -2,
                                "checkable_claim": None} for h in acme["headlines"] if h["tier"] <= 2}
            labels[irrelevant]["relevant_to_company"] = False
            (d / "news_labels.json").write_text(json.dumps({"week_of": "2026-09-28", "news_asof":
                                                            pack["generated_for"]["asof"], "labels": labels}),
                                                encoding="utf-8")
            with H.patched(LF, NEWS_LATEST=d):
                news, asof = LF._director_plan_news(ASOF + timedelta(hours=10))
                other = LF._director_plan_news(ASOF + timedelta(hours=LF.SITE_NEWS_MAX_HOURS + 1))
            with H.patched(LF, NEWS_LATEST=d / "missing"):
                missing = LF._director_plan_news(ASOF)
        self.assertEqual(other, ({}, ""))
        self.assertEqual(missing, ({}, ""))
        self.assertEqual(asof, "2026-09-24")
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
        self.assertIn("How the news desk works", js)
        self.assertIn("newsDeskInstructionsHTML(CFG.howNewsDesk)", js)

    def test_news_desk_block(self):
        LF = H.LF
        how = LF._news_desk_instructions()
        self.assertEqual(how["routine"]["schedule"], "Weekdays 06:00 ET and Sundays 15:00 ET")
        self.assertEqual(how["routinePath"], "theses/routines/news-desk.md")
        self.assertEqual(how["promptPath"], "theses/NEWS_DESK.md")
        self.assertIn("news_pack.py", how["prompt"])
        self.assertIn("NEWS_DESK.md", how["routine"]["html"])

    def test_the_site_and_the_scripts_read_one_folder(self):
        self.assertEqual(H.LF.NEWS_LATEST.resolve(), C.NEWS_LATEST.resolve())
        self.assertEqual(N.OUT_DIR, C.NEWS_LATEST)
        self.assertEqual(NL.NEWS.parent, C.NEWS_LATEST)


def write_latest(d, asof, labels=False, pack=None):
    """A News Desk latest/ folder in d, built from the fixtures, as of asof."""
    pack = pack or build()
    pack = json.loads(json.dumps(pack))
    pack["generated_for"]["asof"] = asof.astimezone(timezone.utc).isoformat(timespec="seconds")
    pack["generated_for"]["asof_et"] = asof.astimezone(N.EASTERN).isoformat(timespec="minutes")
    (d / "news.json").write_text(json.dumps(pack), encoding="utf-8")
    (d / "news.md").write_text(N.summary(pack), encoding="utf-8")
    if labels:
        ids = [i for b in pack["label_batches"] for i in b]
        lab = {i: {"relevant_to_company": True, "event_type": "earnings", "tone": -1, "checkable_claim": None}
               for i in ids}
        (d / "news_labels.json").write_text(json.dumps({"week_of": pack["generated_for"]["week_of"],
                                                        "news_asof": pack["generated_for"]["asof"],
                                                        "labels": lab}), encoding="utf-8")
    return pack


class NewsDesk(unittest.TestCase):
    def test_the_week_it_gathers_for(self):
        self.assertEqual(N.desk_week_of(date(2026, 9, 30)), date(2026, 9, 28))   # a Wednesday
        self.assertEqual(N.desk_week_of(date(2026, 9, 28)), date(2026, 9, 28))   # a Monday
        self.assertEqual(N.desk_week_of(date(2026, 9, 27)), date(2026, 9, 28))   # a Sunday
        self.assertEqual(N.desk_week_of(date(2026, 9, 26)), date(2026, 9, 28))   # a Saturday

    def test_scope_takes_todays_assignments_and_the_plan(self):
        scope = {"covered": ["ACME"], "held": {}, "candidates": {}, "screen_top": ["NEWCO"],
                 "today_assigned": ["QUIET"]}
        got = N.scope_from_inputs({"news_scope": scope}, ["CON", "QUIET"])
        self.assertEqual(got["QUIET"], ["in this week's plan", "assigned today"])
        self.assertEqual(got["NEWCO"], ["screen top"])
        self.assertEqual(got["CON"], ["in this week's plan"])

    def test_today_assigned_reads_the_plan_that_covers_today(self):
        plan = ("---\nweek_of: 2026-09-28\nassignments:\n"
                "  - {date: 2026-09-28, ticker: JPM, kind: initiation, desk: financials-realestate, reason: \"a\"}\n"
                "  - {date: 2026-09-29, ticker: DELL, kind: initiation, desk: technology, reason: \"b\"}\n---\n")
        with H.temp_dir() as d:
            (d / "2026-09-27.md").write_text(plan, encoding="utf-8")
            with mock.patch.object(DC, "PLAN_DIR", d):
                self.assertEqual(N.today_assigned(date(2026, 9, 29)), ["DELL"])
                self.assertEqual(N.plan_tickers(date(2026, 9, 28)), ["JPM", "DELL"])
                self.assertEqual(N.today_assigned(date(2026, 10, 5)), [])

    def test_the_default_output_is_the_news_desk_folder(self):
        self.assertEqual(N.OUT_DIR, H.REPO / "theses" / "news" / "latest")

    def test_archive_copies_and_prunes(self):
        with H.temp_dir() as d:
            latest = d / "latest"
            latest.mkdir()
            write_latest(latest, datetime(2026, 9, 28, 6, 0, tzinfo=N.EASTERN), labels=True)
            for old in ("2026-09-14", "2026-09-15", "2026-09-10"):
                (d / old).mkdir()
                (d / old / "news.json").write_text("{}", encoding="utf-8")
            (d / "notes").mkdir()
            dest, pruned = ND.archive(latest, d, keep_days=14)
            self.assertEqual(dest, d / "2026-09-28")
            self.assertEqual(sorted(p.name for p in dest.iterdir()), sorted(C.NEWS_FILES))
            self.assertEqual(pruned, ["2026-09-10", "2026-09-14"])
            self.assertTrue((d / "2026-09-15").is_dir())
            self.assertTrue((d / "notes").is_dir())
            with self.assertRaises(FileNotFoundError):
                ND.archive(d / "empty", d)

    def test_change_check(self):
        ok = [("M", "theses/news/latest/news.json"), ("M", "theses/news/latest/news.md"),
              ("A", "theses/news/latest/news_labels.json"), ("A", "theses/news/2026-09-28/news.json"),
              ("A", "theses/news/2026-09-28/news_labels.json"), ("D", "theses/news/2026-09-14/news.md")]
        self.assertEqual(ND.check_changes(ok), [])
        for bad in (("M", "theses/ledger/events.csv"), ("A", "theses/director/2026-09-27.md"),
                    ("M", "theses/bin/news_pack.py"), ("A", "theses/news/latest/batch-01.json"),
                    ("A", "theses/news/tmp/news.json"), ("D", "theses/news/latest/news.json"),
                    ("M", "theses/news/README.md"), ("A", "theses/newsletter.md")):
            self.assertTrue(ND.check_changes([bad]), bad)
        fails = ND.check_changes([("M", "theses/ledger/events.csv")])
        self.assertIn("only theses/news/", fails[0])

    def test_change_check_reads_what_is_staged(self):
        with mock.patch.object(DC, "git_changes", return_value=([("M", "data/x.csv")], None, None)):
            with contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(ND.main(["--changes"]), 1)
        self.assertIn("[FAIL]", out.getvalue())
        with mock.patch.object(DC, "git_changes", return_value=([("M", "theses/news/latest/news.md")], None, None)):
            with contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(ND.main(["--changes"]), 0)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(ND.main([]), 2)

    def test_merge_keeps_labels_of_headlines_still_in_the_pack(self):
        news = build()
        batch = news["label_batches"][0]
        with H.temp_dir() as d:
            path = d / "news_labels.json"
            keep = {batch[0]: {"relevant_to_company": False, "event_type": "noise", "tone": 0,
                               "checkable_claim": None},
                    "GONE-0000000000": {"relevant_to_company": True, "event_type": "noise", "tone": 0,
                                        "checkable_claim": None}}
            path.write_text(json.dumps({"week_of": "2026-09-21", "labels": keep}), encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()):
                NL.merge(news, d / "none", path)
            saved = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(saved["news_asof"], news["generated_for"]["asof"])
        self.assertEqual(list(saved["labels"]), [batch[0]])


class DossierNews(unittest.TestCase):
    NOW = datetime(2026, 9, 29, 7, 0, tzinfo=N.EASTERN)

    def block(self, d, ticker="ACME"):
        lines, caveat = DOS.news_week_block(ticker, d, self.NOW)
        return "\n".join(lines), caveat

    def test_present(self):
        with H.temp_dir() as d:
            pack = build()
            # The fixture's one mismatch is tier 3; make a tier 2 claim one too.
            climbs = by_title(pack, "ACME", "Acme climbs 2%")
            climbs["price_claim"] = dict(climbs["price_claim"], status="mismatch", claimed=9.0)
            climbs["flags"] = ["price claim mismatch"]
            pack = write_latest(d, datetime(2026, 9, 29, 6, 0, tzinfo=N.EASTERN), labels=True, pack=pack)
            acme = next(n for n in pack["names"] if n["ticker"] == "ACME")
            text, caveat = self.block(d)
        self.assertIsNone(caveat)
        self.assertIn("### News this week", text)
        self.assertIn("Leads to verify in the filings, never sources", text)
        items = [l for l in text.splitlines() if l.startswith("- ")]
        t12 = [h for h in acme["headlines"] if h["tier"] <= 2]
        self.assertEqual(len(items), min(DOS.NEWS_WEEK_MAX, len(t12)))
        self.assertTrue(all(", tier 1," in l or ", tier 2," in l for l in items))
        self.assertIn("event earnings", text)
        self.assertIn("tone -1 (somewhat bad)", text)
        self.assertIn("price claim mismatch", text)
        self.assertNotIn(EM, text)
        self.assertNotIn(EN, text)

    def test_at_most_eight(self):
        with H.temp_dir() as d:
            pack = build()
            acme = next(n for n in pack["names"] if n["ticker"] == "ACME")
            first = next(h for h in acme["headlines"] if h["tier"] == 1)
            acme["headlines"] = [dict(first, id=f"ACME-{i:010d}", ts=first["ts"] + i) for i in range(11)]
            write_latest(d, datetime(2026, 9, 29, 6, 0, tzinfo=N.EASTERN), pack=pack)
            text, _ = self.block(d)
        self.assertEqual(len([l for l in text.splitlines() if l.startswith("- ")]), 8)
        self.assertIn("3 more in `theses/news/latest/news.json`", text)

    def test_absent(self):
        with H.temp_dir() as d:
            text, caveat = self.block(d)
        body = [l for l in text.splitlines() if l.strip() and not l.startswith("###")]
        self.assertEqual(len(body), 1)
        self.assertTrue(body[0].startswith("No fresh news: no News Desk pack"))
        self.assertTrue(caveat)

    def test_stale(self):
        with H.temp_dir() as d:
            write_latest(d, datetime(2026, 9, 25, 6, 0, tzinfo=N.EASTERN), labels=True)
            text, caveat = self.block(d)
        body = [l for l in text.splitlines() if l.strip() and not l.startswith("###")]
        self.assertEqual(len(body), 1)
        self.assertIn("No fresh news: the News Desk pack is stale", body[0])
        self.assertTrue(caveat)

    def test_name_not_in_scope(self):
        with H.temp_dir() as d:
            write_latest(d, datetime(2026, 9, 29, 6, 0, tzinfo=N.EASTERN))
            text, caveat = self.block(d, "ZZZZ")
        self.assertIn("has no tier 1 or 2 headline about ZZZZ (the name is not in its scope).", text)
        self.assertIsNone(caveat)

    def test_the_dossier_calls_the_block(self):
        src = (H.REPO / "theses" / "bin" / "dossier.py").read_text(encoding="utf-8")
        self.assertIn("news_week_block(ticker)", src)


if __name__ == "__main__":
    unittest.main()
