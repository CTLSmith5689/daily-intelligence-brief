"""Scoring the analyst's calls and the PM, and attributing each book's return.

Offline. Prices come from small in-memory series with hand-picked closes, so every
expected figure below is worked out by hand from those closes, not by the code
under test. Files are written only into temporary directories.
"""
import csv
import json
import math
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests import helpers as H  # noqa: E402

LF = H.LF
from portfolio import engine as E  # noqa: E402

sys.path.append(str(H.REPO / "theses" / "bin"))
import score as SC  # noqa: E402
from portfolio.bin import score_pm as PM  # noqa: E402

EN_DASH = chr(0x2013)


class FakePrices:
    """score.py's price interface over dicts: {ticker: {date: close}}."""

    def __init__(self, stocks, benches):
        self.s, self.b = stocks, benches

    def stock(self, t):
        return dict(self.s.get(t) or {})

    def bench(self, sym):
        return dict(self.b.get(sym) or {})


class FakePanel:
    def __init__(self, rows):
        self.rows = rows

    def asof(self, day):
        return self.rows


PANEL = {
    "AAA": {"ticker": "AAA", "sector": "Health Care", "sub_industry": "Services", "market_cap": 50,
            "security_type": "operating"},
    "P1": {"ticker": "P1", "sector": "Health Care", "sub_industry": "Services", "market_cap": 40,
           "security_type": "operating"},
    "P2": {"ticker": "P2", "sector": "Health Care", "sub_industry": "Services", "market_cap": 30,
           "security_type": "operating"},
    "P3": {"ticker": "P3", "sector": "Health Care", "sub_industry": "Services", "market_cap": 20,
           "security_type": "operating"},
}
START, END = "2026-01-05", "2027-01-05"


def call(stance_action=None, direction="", **kw):
    e = {"event_id": "AAA-2026-01-05-1", "date": START, "ticker": "AAA", "kind": "initiate",
         "thesis_id": "AAA-2026-01-05", "note_path": "", "direction": direction,
         "conviction": "4", "target_price": "120", "horizon_days": "365",
         "action": stance_action or ""}
    fm = {"entry_source": f"close_series {START}", "entry_price": "100"}
    fm.update(kw.pop("fm", {}))
    c = SC._call(e, fm, kw.pop("body", ""), None)
    c.update(kw)
    return c


def prices(stock_end, etf_end, spx_end=110.0, peers=(105.0, 110.0, 115.0)):
    stocks = {"AAA": {START: 100.0, "2026-06-01": 90.0, END: stock_end}}
    for t, x in zip(("P1", "P2", "P3"), peers):
        stocks[t] = {START: 100.0, END: x}
    return FakePrices(stocks, {"XLV": {START: 50.0, END: etf_end},
                               "^GSPC": {START: 1000.0, END: spx_end * 10}})


class AnalystCalls(unittest.TestCase):

    def test_stance_comes_from_the_action_then_the_direction(self):
        self.assertEqual(SC.stance_of("Initiate", "long"), ("long", "action"))
        self.assertEqual(SC.stance_of("Hold", "long")[0], "long")
        self.assertEqual(SC.stance_of("Short", "short")[0], "short")
        self.assertEqual(SC.stance_of("Avoid", "watch")[0], "avoid")
        self.assertEqual(SC.stance_of("Exit", "watch")[0], "avoid")
        self.assertEqual(SC.stance_of("", "avoid"), ("avoid", "direction"))
        self.assertEqual(SC.stance_of("", "watch")[0], "")
        self.assertEqual(SC.stance_of("", "no view")[0], "")

    def test_avoid_is_scored_against_the_sector_and_never_as_a_short(self):
        # The stock falls 5%, but its sector fund falls 10%: the stock did better than
        # its sector, so avoiding it was wrong, although a short would have made 5%.
        c = call("Avoid", "watch")
        row = SC.score_call(c, prices(95.0, 45.0), FakePanel(PANEL), "2027-02-01")
        self.assertEqual(row["stance"], "avoid")
        self.assertEqual(row["abs_return"], "", "an avoid has no P&L of its own")
        self.assertAlmostEqual(row["stock_return"], -0.05)
        self.assertAlmostEqual(row["sector_return"], -0.10)
        self.assertAlmostEqual(row["rel_sector"], -0.05)     # -1 * (-0.05 - -0.10)
        self.assertEqual((row["hit"], row["outcome"]), ("no", "wrong"))
        # The stock rises 2% while the sector rises 10%: it lagged, so the avoid was right.
        row = SC.score_call(c, prices(102.0, 55.0), FakePanel(PANEL), "2027-02-01")
        self.assertAlmostEqual(row["rel_sector"], 0.08)
        self.assertEqual((row["hit"], row["outcome"]), ("yes", "right"))

    def test_exit_is_scored_like_an_avoid_from_the_exit_date(self):
        c = call("Exit", "watch", fm={"entry_source": "close_series 2026-06-01", "entry_price": "90"})
        self.assertEqual((c["stance"], c["start_date"]), ("avoid", "2026-06-01"))
        p = prices(99.0, 55.0)
        p.b["XLV"]["2026-06-01"] = 50.0
        p.b["^GSPC"]["2026-06-01"] = 1000.0
        row = SC.score_call(c, p, FakePanel(PANEL), "2027-02-01")
        # From the exit date's close of 90: the stock +10%, the fund +10%.
        self.assertAlmostEqual(row["stock_return"], 0.10)
        self.assertAlmostEqual(row["rel_sector"], 0.0)
        self.assertEqual(row["abs_return"], "")

    def test_short_is_scored_as_a_short(self):
        c = call("Short", "short")
        row = SC.score_call(c, prices(80.0, 50.0), FakePanel(PANEL), "2027-02-01")
        self.assertAlmostEqual(row["abs_return"], 0.20, msg="a short makes what the stock loses")
        self.assertAlmostEqual(row["rel_sector"], 0.20)      # -1 * (-0.20 - 0)
        self.assertEqual(row["hit"], "yes")
        # max favourable for a short is the lowest close seen: 80 at the end, 90 in June.
        self.assertAlmostEqual(row["max_favourable"], 0.20)

    def test_long_excess_against_sector_spx_and_peers(self):
        c = call("Initiate", "long")
        row = SC.score_call(c, prices(118.0, 55.0, spx_end=112.0), FakePanel(PANEL), "2027-02-01")
        self.assertAlmostEqual(row["stock_return"], 0.18)
        self.assertAlmostEqual(row["abs_return"], 0.18)
        self.assertAlmostEqual(row["rel_sector"], 0.08)       # 0.18 - 0.10
        self.assertAlmostEqual(row["rel_spy"], 0.06)          # 0.18 - 0.12
        self.assertAlmostEqual(row["peer_median_return"], 0.10)
        self.assertAlmostEqual(row["rel_peer"], 0.08)
        self.assertEqual(row["peers_used"], 3)
        self.assertEqual((row["sector_etf"], row["desk"]), ("XLV", "Health care"))
        self.assertEqual(row["target_hit"], "no")             # 120 never reached
        self.assertEqual(row["exit_date"], END)

    def test_watch_is_not_scored_and_nothing_scores_before_the_horizon(self):
        self.assertIsNone(SC.score_call(call("", "watch"), prices(118, 55), FakePanel(PANEL), "2027-02-01"))
        self.assertIsNone(SC.score_call(call("Initiate", "long"), prices(118, 55), FakePanel(PANEL), "2026-12-31"))

    def test_a_missing_fund_close_leaves_the_call_unbenchmarked(self):
        p = prices(118.0, 55.0)
        del p.b["XLV"][END]
        row = SC.score_call(call("Initiate", "long"), p, FakePanel(PANEL), "2027-02-01")
        self.assertEqual(row["rel_sector"], "")
        self.assertEqual(row["hit"], "")
        self.assertEqual(row["outcome"], "unbenchmarked")
        self.assertIn(f"no stored XLV close on {END}", row["note"])

    def test_brier_and_the_nearest_case(self):
        sc = [("bull", 150.0, 0.25), ("base", 110.0, 0.5), ("bear", 70.0, 0.25)]
        landed, b = SC.brier(sc, 100.0, 0.12)
        self.assertEqual(landed, "base")
        self.assertAlmostEqual(b, 0.25 ** 2 + 0.5 ** 2 + 0.25 ** 2)
        landed, b = SC.brier(sc, 100.0, -0.4)
        self.assertEqual(landed, "bear")
        self.assertAlmostEqual(b, 0.25 ** 2 + 0.5 ** 2 + 0.75 ** 2)
        self.assertEqual(SC.brier(sc[:2], 100.0, 0.1), ("", None))

    def test_price_thresholds_are_read_from_the_monitoring_table(self):
        text = (H.REPO / "tests" / "fixtures" / "memo" / "NVDA-2026-09-23-initiation.md").read_text(encoding="utf-8")
        fm, body = SC.validate.parse(text)
        th = SC.price_thresholds(body, 228.87)
        self.assertEqual(len(th), 1, "only the share price row is checkable against a close")
        self.assertEqual((th[0]["level"], th[0]["op"], th[0]["action"]), (370.0, ">=", "If owned, Trim"))
        self.assertEqual(len(SC.parse_scenarios(fm["scenarios"])), 3)

    def test_load_calls_reads_the_notes_and_attaches_predictions(self):
        with H.temp_dir() as tmp:
            note = tmp / "theses" / "notes" / "AAA" / "2026-09-01-initiation.md"
            note.parent.mkdir(parents=True)
            note.write_text("---\nticker: AAA\nformat: memo\naction: Exit\nentry_price: 50\n"
                            "entry_source: close_series 2026-08-31\n---\nbody\n", encoding="utf-8")
            events = [{"event_id": "AAA-2026-09-01-1", "date": "2026-09-01", "ticker": "AAA",
                       "kind": "revise", "thesis_id": "AAA-2026-09-01",
                       "note_path": "theses/notes/AAA/2026-09-01-initiation.md",
                       "direction": "watch", "conviction": "3", "target_price": "40",
                       "horizon_days": "365"}]
            preds = [{"prediction_id": "ZZZ-2026-01-01-1", "thesis_id": "ZZZ-2026-01-01", "ticker": "ZZZ",
                      "written_on": "2026-01-01", "direction": "long", "horizon_days": "30",
                      "entry_price": "10"}]
            calls = SC.load_calls(events, preds, root=tmp)
        self.assertEqual([c["call_id"] for c in calls], ["AAA-2026-09-01-1", "ZZZ-2026-01-01-1"])
        self.assertEqual((calls[0]["stance"], calls[0]["start_date"], calls[0]["stage"]),
                         ("avoid", "2026-08-31", "revision"))
        self.assertEqual(calls[1]["prediction_id"], "ZZZ-2026-01-01-1")


class Marks(unittest.TestCase):

    def test_missing_close_is_skipped_flagged_and_later_superseded(self):
        days = ["2026-01-05", "2026-01-06", "2026-01-07"]
        stocks = {"AAA": {"2026-01-05": 100.0, "2026-01-07": 104.0}}   # no close on the 6th
        benches = {"XLV": {d: 50.0 for d in days}, "^GSPC": {"2026-01-05": 1000.0, "2026-01-07": 1010.0}}
        p = FakePrices(stocks, benches)
        c = call("Initiate", "long", body="## 4. WHAT WOULD PROVE ME WRONG\n\n| What I check | Latest | Threshold | Action | Next |\n|---|---|---|---|---|\n| Share price | $100 | Above $103 | If owned, Trim | Daily |\n")
        with H.temp_dir() as tmp:
            path = tmp / "marks.csv"
            n = SC.record_marks([c], days, p, FakePanel(PANEL), path, "2026-01-07", now="t1")
            rows = E.read_rows(path)
            self.assertEqual(n, 3)
            by = {r["date"]: r for r in rows}
            self.assertEqual(by["2026-01-06"]["status"], "skipped")
            self.assertEqual(by["2026-01-06"]["close"], "", "never carried from another day")
            self.assertEqual(by["2026-01-06"]["stock_return"], "")
            self.assertIn("no stored close for AAA on 2026-01-06", by["2026-01-06"]["flags"])
            self.assertIn("no stored ^GSPC close on 2026-01-06", by["2026-01-06"]["flags"])
            self.assertEqual(by["2026-01-07"]["status"], "ok")
            self.assertAlmostEqual(float(by["2026-01-07"]["excess_sector"]), 0.04)
            self.assertAlmostEqual(float(by["2026-01-07"]["excess_spy"]), 0.03)
            self.assertAlmostEqual(float(by["2026-01-07"]["progress_to_target"]), 0.2)  # 4 of 20
            self.assertIn("at or above $103.00", by["2026-01-07"]["thresholds_crossed"])
            self.assertEqual(SC.record_marks([c], days, p, FakePanel(PANEL), path, "2026-01-07", "t2"), 0,
                             "nothing changed, so nothing is appended")
            stocks["AAA"]["2026-01-06"] = 101.0
            benches["^GSPC"]["2026-01-06"] = 1005.0
            self.assertEqual(SC.record_marks([c], days, p, FakePanel(PANEL), path, "2026-01-07", "t3"), 1)
            rows = E.read_rows(path)
            self.assertEqual(len(rows), 4, "the skipped row stays; a new one supersedes it")
            latest = SC.latest_rows(rows, lambda r: (r["call_id"], r["date"]))
            self.assertEqual(latest[(c["call_id"], "2026-01-06")]["status"], "ok")

    def test_archived_start_close_outlives_the_price_file(self):
        rows = [{"call_id": "X", "start_close": "", "sector_start": "", "spy_start": ""},
                {"call_id": "X", "start_close": "100", "sector_start": "50", "spy_start": "1000"}]
        self.assertEqual(SC.archived_starts(rows)["X"],
                         {"start_close": 100.0, "sector_start": 50.0, "spy_start": 1000.0})


class Aggregates(unittest.TestCase):

    def scored(self, n, conviction, hit_every=2):
        out = []
        for i in range(n):
            ex = 0.04 if i % hit_every == 0 else -0.02
            out.append({"call_id": f"C{conviction}-{i}", "conviction": conviction, "desk": "Health care",
                        "action": "Initiate", "kind": "initiate", "rel_sector": ex,
                        "hit": "yes" if ex > 0 else "no"})
        return out

    def test_counts_and_the_small_sample_label(self):
        scores = self.scored(9, "5") + self.scored(10, "3")
        calls = [{"call_id": s["call_id"], "stance": "long", "conviction": s["conviction"],
                  "action": "Initiate", "direction": "long", "stage": "initiation"} for s in scores]
        calls.append({"call_id": "OPEN", "stance": "avoid", "conviction": "5", "action": "Avoid",
                      "direction": "watch", "stage": "revision"})
        agg = SC.aggregates(scores, calls)
        self.assertEqual(agg["overall"]["n"], 19)
        self.assertFalse(agg["overall"]["tooFew"])
        conv = {g["name"]: g for g in SC.group_table(agg, "conviction", {})}
        self.assertEqual((conv["5"]["n"], conv["5"]["open"], conv["5"]["tooFew"]), (9, 1, True))
        self.assertEqual((conv["3"]["n"], conv["3"]["tooFew"]), (10, False))
        self.assertAlmostEqual(conv["3"]["hitRate"], 0.5)
        self.assertAlmostEqual(conv["3"]["meanExcess"], 0.01)
        self.assertAlmostEqual(conv["3"]["medianExcess"], 0.01)
        stage = {g["name"]: g for g in SC.group_table(agg, "stage", {}, ["Initiation", "Revision"])}
        self.assertEqual((stage["Revision"]["n"], stage["Revision"]["open"]), (0, 1))
        self.assertTrue(stage["Revision"]["tooFew"])
        desk = SC.group_table(agg, "desk", {}, [d for d, _ in SC.desk_list()])
        self.assertEqual(len(desk), 6, "five desks, always shown, plus the open call's unknown sector")

    def test_desks_come_from_the_shared_map(self):
        self.assertEqual(SC.desk_for_sector("Communication Services"), "Technology and communications")
        self.assertEqual(SC.desk_for_sector("Real Estate"), "Financials and real estate")
        self.assertEqual(SC.desk_for_sector(""), "")
        self.assertEqual(len(SC.desk_list()), 5)
        self.assertEqual(len(E.SECTOR_ETFS), 11)
        self.assertTrue(set(E.SECTOR_ETFS.values()) <= set(E.BENCHMARK_SYMBOLS))


class ScoresMigration(unittest.TestCase):

    def test_migration_is_byte_preserving_and_runs_once(self):
        with H.temp_dir() as tmp:
            path = tmp / "scores.csv"
            head = ",".join(SC.OLD_SCORE_COLUMNS) + "\n"
            row = "P-1,2026-01-01,2026-01-01,1,0.1,0.05,0.05,0.02,0.08,3,yes,0.1,-0.1,right,\"a, note\"\r\n"
            path.write_text(head + row, encoding="utf-8", newline="")
            self.assertEqual(SC.migrate_scores(path, write=False), "would migrate")
            self.assertEqual(SC.migrate_scores(path), "migrated")
            text = path.read_bytes().decode("utf-8")
            self.assertTrue(text.startswith(",".join(SC.SCORE_COLUMNS) + "\n"))
            pad = "," * len(SC.NEW_SCORE_COLUMNS)
            self.assertTrue(text.endswith(row[:-2] + pad + "\r\n"))
            self.assertEqual(SC.migrate_scores(path), "current")
            SC.append_csv(path, SC.SCORE_COLUMNS, [{"call_id": "X", "prediction_id": ""}])
            self.assertEqual(len(E.read_rows(path)), 2)


# ---------------------------------------------------------------------------
# The PM


D1, D2, D3, D4 = "2026-09-01", "2026-09-02", "2026-09-03", "2026-10-05"
CLOSES = {"A": {D1: 10.0, D2: 10.0, D3: 11.0, D4: 12.0},
          "B": {D1: 10.0, D2: 10.0, D3: 9.0, D4: 8.0},
          "C": {D1: 20.0, D2: 20.0, D3: 24.0, D4: 30.0}}


class Store:
    def __init__(self, data):
        self.d = data

    def close(self, t, day):
        return (self.d.get(t) or {}).get(day)


CLASSES = {
    "A": {"ticker": "A", "name": "A Co", "sector": "Tech", "market_cap": 100.0, "size": "large",
          "style": "growth", "box": "large-growth", "growth": 3.0, "value": 0.0, "quality": 0.0},
    "B": {"ticker": "B", "name": "B Co", "sector": "Tech", "market_cap": 100.0, "size": "large",
          "style": "growth", "box": "large-growth", "growth": 2.0, "value": 0.0, "quality": 0.0},
    "C": {"ticker": "C", "name": "C Co", "sector": "Energy", "market_cap": 200.0, "size": "large",
          "style": "growth", "box": "large-growth", "growth": 1.0, "value": 0.0, "quality": 0.0},
}


def trade(tid, day, book, tk, side, sh, px, cost, reason, dec):
    return {"trade_id": tid, "date": day, "book": book, "ticker": tk, "side": side, "shares": sh,
            "price": px, "cost": cost, "lot_id": tid if side in ("buy", "short") else "",
            "reason_code": reason, "decision_id": dec}


def pm_fixture(tmp, trades, decisions):
    led = tmp / "ledger"
    E.append_rows(led / "trades.csv", E.TRADE_COLUMNS, trades)
    E.append_rows(led / "decisions.csv", E.DECISION_COLUMNS, decisions)
    E.append_rows(led / "mandates.csv", E.MANDATE_COLUMNS, [
        {"date": D1, "book": "lg-growth", "decision_id": "lg-growth-m",
         "mandate": json.dumps(dict(E.DEFAULT_STYLE_MANDATE, holdings_range=[2, 2], sector_cap=1.0,
                                    max_position=0.6))}])
    panel = tmp / "panel"
    panel.mkdir()
    for month in ("2026-09", "2026-10"):
        with (panel / f"{month}.csv").open("w", encoding="utf-8", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["date", "ticker", "sector", "security_type", "market_cap"])
            for d in (D1, D2, D3, D4):
                if d.startswith(month):
                    for t, i in CLASSES.items():
                        w.writerow([d, t, i["sector"], "operating", i["market_cap"]])
    bench = Store({"IWY": {D1: 100.0, D2: 100.0, D3: 109.0, D4: 110.0},
                   "XLE": {D1: 50.0, D2: 50.0, D3: 51.0, D4: 55.0},
                   "^GSPC": {D1: 1000.0, D2: 1000.0, D3: 1100.0, D4: 1100.0}})
    ctx = PM.Ctx(ledger_dir=led, prices=Store(CLOSES), benchmarks=bench, panel_dir=panel,
                 history={}, events=tmp / "no-events.csv", books_dir=tmp / "books")
    ctx.classes = lambda day: CLASSES
    return ctx


STYLE_TRADES = [
    trade("g-1", D1, "lg-growth", "CASH", "deposit", 1000, 1, 0, "inception_capital", "g-d1"),
    trade("g-2", D1, "lg-growth", "A", "buy", 49, 10.0, 0.25, "rules_inception", "g-d1"),
    trade("g-3", D1, "lg-growth", "B", "buy", 49, 10.0, 0.25, "rules_inception", "g-d1"),
    trade("g-4", D2, "lg-growth", "B", "sell", 49, 10.0, 0.25, "pm_order", "g-pm"),
    trade("g-5", D2, "lg-growth", "C", "buy", 25, 20.0, 0.25, "pm_order", "g-pm"),
]
STYLE_DECISIONS = [
    {"date": D1, "book": "lg-growth", "decision_id": "g-d1", "action": "inception", "reason": "x", "author": "rules"},
    {"date": D2, "book": "lg-growth", "decision_id": "g-pm", "action": "trade", "reason": "C over B", "author": "pm"},
]


class ShadowBook(unittest.TestCase):

    def test_shadow_value_added_and_the_decision_mark(self):
        with H.temp_dir() as tmp:
            ctx = pm_fixture(tmp, STYLE_TRADES, STYLE_DECISIONS)
            with H.quiet():
                counts = PM.run_daily(ctx, today=D4, data_dir=tmp / "out", now="t")
                again = PM.run_daily(ctx, today=D4, data_dir=tmp / "out", now="t2")
            shadow = E.read_rows(tmp / "out" / "shadow_trades.csv")
            nav = {r["date"]: r for r in E.read_rows(tmp / "out" / "shadow_nav.csv")
                   if r["book"] == "lg-growth"}
            marks = E.read_rows(tmp / "out" / "decision_marks.csv")
        self.assertEqual(again["shadow_trades"], 0, "a rebalance is written once")
        self.assertEqual(again["decision_marks"], 0)
        # The inception is copied from the real book: the deposit and the two rules buys.
        first = [t for t in shadow if t["date"] == D1 and t["reason_code"] != "shadow_rebalance"]
        self.assertEqual([(t["ticker"], t["side"], t["shares"]) for t in first],
                         [("CASH", "deposit", "1000"), ("A", "buy", "49"), ("B", "buy", "49")])
        # On D2 the rules still want A and B at 49 shares each (floor(0.5 * 999.5 / 10.005)),
        # so the shadow only records that it rebalanced.
        d2 = [t for t in shadow if t["date"] == D2]
        self.assertEqual([t["reason_code"] for t in d2], ["shadow_rebalance"])
        # D3 by hand. Book: A 49 x 11 + C 25 x 24 + cash 9.00 = 1,148.00.
        # Shadow: A 49 x 11 + B 49 x 9 + cash 19.50 = 999.50.
        self.assertEqual(float(nav[D3]["book_nav"]), 1148.0)
        self.assertEqual(float(nav[D3]["shadow_nav"]), 999.5)
        self.assertAlmostEqual(float(nav[D3]["value_added_since"]), 0.148 - (-0.0005), places=8)
        self.assertAlmostEqual(float(nav[D3]["value_added_day"]), 1148.0 / 999.0 - 1, places=8)
        # The decision swapped B for C. At D4, 30 days on: B -20%, C +50% from D2.
        m1 = [m for m in marks if m["horizon"] == "1m"]
        self.assertEqual(len(m1), 1)
        self.assertEqual(len(marks), 1, "3 and 6 months are not due")
        m = m1[0]
        self.assertEqual((m["mark_date"], m["names"], m["basis"]), (D4, "B;C", "rules"))
        did = 500.0 / 999.0 * 0.5
        rules = 490.0 / 999.5 * -0.2
        self.assertAlmostEqual(float(m["did_return"]), did, places=6)
        self.assertAlmostEqual(float(m["rules_return"]), rules, places=6)
        self.assertAlmostEqual(float(m["value_added"]), did - rules, places=6)
        self.assertEqual(counts["shadow_nav"], 4)

    def test_style_attribution_by_hand(self):
        with H.temp_dir() as tmp:
            ctx = pm_fixture(tmp, STYLE_TRADES, STYLE_DECISIONS)
            row = PM.attribution_row(ctx, E.BOOKS["lg-growth"], D2, D3, "t")
        # Held over D2 to D3: A 490 (Tech, +10%), C 500 (Energy, +20%), cash 9; value 999.
        # Proxy (the box by market value): Tech 0.5 (A +10%, B -10%: 0%), Energy 0.5 (+20%).
        w_t, w_e, cw = 490 / 999, 500 / 999, 9 / 999
        rb = 0.1
        alloc = (w_t - 0.5) * (0.0 - rb) + (w_e - 0.5) * (0.2 - rb)
        sel = 0.5 * (0.1 - 0.0) + 0.5 * (0.2 - 0.2)
        inter = (w_t - 0.5) * (0.1 - 0.0) + (w_e - 0.5) * 0.0
        self.assertAlmostEqual(float(row["book_return"]), 149 / 999, places=8)
        self.assertAlmostEqual(float(row["proxy_return"]), rb, places=8)
        self.assertAlmostEqual(float(row["allocation"]), alloc, places=8)
        self.assertAlmostEqual(float(row["selection"]), sel, places=8)
        self.assertAlmostEqual(float(row["interaction"]), inter, places=8)
        self.assertAlmostEqual(float(row["cash_drag"]), cw * -rb, places=8)
        self.assertAlmostEqual(float(row["proxy_error"]), rb - 0.09, places=8)
        self.assertEqual(row["interaction_folded"], "")
        # Sizing: equal weights of A and C would be (490 + 500) / 2 each.
        eq = (990 / 2) / 999
        self.assertAlmostEqual(float(row["sizing"]), (w_t - eq) * 0.1 + (w_e - eq) * 0.2, places=8)

    def test_missing_close_skips_the_day(self):
        closes = json.loads(json.dumps(CLOSES))
        del closes["C"][D3]
        with H.temp_dir() as tmp:
            ctx = pm_fixture(tmp, STYLE_TRADES, STYLE_DECISIONS)
            ctx.prices = Store(closes)
            row = PM.attribution_row(ctx, E.BOOKS["lg-growth"], D2, D3, "t")
        self.assertEqual(row["status"], "skipped")
        self.assertIn(f"no stored close for C on {D3}", row["flags"])
        self.assertNotIn("book_return", row)


class BrinsonFachler(unittest.TestCase):

    def test_hand_worked_example_sums_to_the_active_return_exactly(self):
        # Book: 60% in A returning 10%, 40% in B returning 2%: 6.8%.
        # Benchmark: 50% A returning 8%, 50% B returning 4%: 6.0%. Active 0.8%.
        # Allocation: A (0.6-0.5)(0.08-0.06) = 0.002, B (0.4-0.5)(0.04-0.06) = 0.002.
        # Selection: A 0.5(0.10-0.08) = 0.010, B 0.5(0.02-0.04) = -0.010.
        # Interaction: A 0.1 x 0.02 = 0.002, B -0.1 x -0.02 = 0.002.
        bf = PM.brinson_fachler({"A": 0.6, "B": 0.4}, {"A": 0.10, "B": 0.02},
                                {"A": 0.5, "B": 0.5}, {"A": 0.08, "B": 0.04})
        self.assertAlmostEqual(bf["book"], 0.068, places=12)
        self.assertAlmostEqual(bf["bench"], 0.060, places=12)
        self.assertAlmostEqual(bf["allocation"], 0.004, places=12)
        self.assertAlmostEqual(bf["selection"], 0.000, places=12)
        self.assertAlmostEqual(bf["interaction"], 0.004, places=12)
        self.assertAlmostEqual(bf["sectors"]["A"][1], 0.010, places=12)
        self.assertAlmostEqual(bf["sectors"]["B"][1], -0.010, places=12)
        self.assertAlmostEqual(bf["allocation"] + bf["selection"] + bf["interaction"] + bf["cash"] + bf["costs"],
                               bf["active"], places=15)
        self.assertAlmostEqual(bf["active"], 0.008, places=12)

    def test_cash_costs_and_a_sector_the_benchmark_lacks_still_sum(self):
        bf = PM.brinson_fachler({"A": 0.5, "X": 0.3}, {"A": 0.05, "X": -0.1},
                                {"A": 0.7, "B": 0.3}, {"A": 0.02, "B": 0.1},
                                cash_weight=0.2, costs=0.001)
        parts = bf["allocation"] + bf["selection"] + bf["interaction"] + bf["cash"] + bf["costs"]
        self.assertAlmostEqual(parts, bf["active"], places=14)
        self.assertAlmostEqual(bf["book"], 0.5 * 0.05 - 0.03 - 0.001, places=14)
        self.assertAlmostEqual(bf["cash"], 0.2 * -(0.7 * 0.02 + 0.3 * 0.1), places=14)

    def test_carino_links_to_the_compounded_difference(self):
        rows = [{"book_return": 0.02, "proxy_return": 0.01, "a": 0.004, "s": 0.006},
                {"book_return": -0.01, "proxy_return": 0.005, "a": -0.01, "s": -0.005},
                {"book_return": 0.03, "proxy_return": 0.03, "a": 0.001, "s": -0.001}]
        R, B, linked = PM.carino(rows, ["a", "s"])
        self.assertAlmostEqual(R, 1.02 * 0.99 * 1.03 - 1, places=14)
        self.assertAlmostEqual(B, 1.01 * 1.005 * 1.03 - 1, places=14)
        self.assertAlmostEqual(linked["a"] + linked["s"], R - B, places=14)


HEDGE_TRADES = [
    trade("h-1", D1, "hedge", "CASH", "deposit", 1000, 1, 0, "inception_capital", "h-d1"),
    trade("h-2", D1, "hedge", "A", "buy", 30, 10.0, 0.15, "pm_order", "h-pm"),
    trade("h-3", D1, "hedge", "B", "short", 20, 10.0, 0.10, "pm_order", "h-pm"),
]
HEDGE_DECISIONS = [
    {"date": D1, "book": "hedge", "decision_id": "h-d1", "action": "inception", "reason": "x", "author": "rules"},
    {"date": D1, "book": "hedge", "decision_id": "h-pm", "action": "trade", "reason": "A over B", "author": "pm"},
]


class HedgeLegs(unittest.TestCase):

    def test_long_and_short_legs_and_the_folded_interaction(self):
        with H.temp_dir() as tmp:
            ctx = pm_fixture(tmp, HEDGE_TRADES, HEDGE_DECISIONS)
            row = PM.attribution_row(ctx, E.BOOKS["hedge"], D2, D3, "t")
        # Value at D2: cash 1000 - 300.15 + 199.90 = 899.75, A +300, B -200: 999.75.
        nav = 999.75
        self.assertAlmostEqual(float(row["long_contrib"]), 300 / nav * 0.10, places=8)
        self.assertAlmostEqual(float(row["short_contrib"]), -200 / nav * -0.10, places=8)
        self.assertAlmostEqual(float(row["book_return"]), 50 / nav, places=8)
        self.assertAlmostEqual(float(row["gross"]), 500 / nav, places=6)
        self.assertAlmostEqual(float(row["net"]), 100 / nav, places=6)
        self.assertEqual(row["interaction_folded"], "1")
        self.assertEqual(float(row["interaction"]), 0.0)
        self.assertAlmostEqual(float(row["spx_return"]), 0.1, places=8)
        # Proxy: every operating company by market value: A 0.25, B 0.25, C 0.5.
        rb = 0.25 * 0.1 + 0.25 * -0.1 + 0.5 * 0.2
        parts = sum(float(row[k]) for k in ("allocation", "selection", "interaction", "cash_drag", "costs"))
        self.assertAlmostEqual(parts, 50 / nav - rb, places=7)
        self.assertAlmostEqual(float(row["cash_drag"]), 899.75 / nav * -rb, places=8)

    def test_summary_links_legs_and_needs_sixty_sessions_for_beta(self):
        rows = []
        day = 0
        for i in range(61):
            m = ((i * 37) % 11 - 5) / 1000.0
            rows.append({"date": f"2026-{1 + i // 28:02d}-{1 + i % 28:02d}", "book": "hedge", "prev_date": "p",
                         "status": "ok", "book_return": 2 * m, "proxy_return": m, "spx_return": m,
                         "allocation": 0.0, "selection": m, "interaction": 0.0, "cash_drag": 0.0,
                         "costs": 0.0, "long_contrib": 3 * m, "short_contrib": -m, "sizing": 0.0,
                         "gross": 1.0, "net": 0.5, "fund_return": m, "proxy_error": 0.0,
                         "interaction_folded": "1", "by_sector": "{}"})
        s = PM.book_summary(E.BOOKS["hedge"], [{k: str(v) for k, v in r.items()} for r in rows], [], [])
        self.assertEqual(s["days"], 61)
        self.assertAlmostEqual(s["beta"], 2.0, places=9)
        self.assertAlmostEqual(s["longLeg"] + s["shortLeg"] + s["legCosts"], s["bookReturn"], places=12)
        self.assertAlmostEqual(s["allocation"] + s["selection"] + s["interaction"] + s["cash_drag"] + s["costs"],
                               s["active"], places=12)
        few = PM.book_summary(E.BOOKS["hedge"], [{k: str(v) for k, v in r.items()} for r in rows[:59]], [], [])
        self.assertNotIn("beta", few)
        self.assertEqual(few["betaObs"], 59)


class Pages(unittest.TestCase):

    def test_scorecard_page_and_nav(self):
        with H.temp_dir() as tmp:
            docs = tmp / "docs"
            docs.mkdir()
            theses = tmp / "theses"
            (theses / "ledger").mkdir(parents=True)
            E.append_rows(theses / "ledger" / "events.csv", ["event_id", "date", "ticker", "kind", "thesis_id",
                                                             "note_path", "direction", "conviction",
                                                             "target_price", "horizon_days"],
                          [{"event_id": "AAA-1", "date": "2026-09-01", "ticker": "AAA", "kind": "initiate",
                            "thesis_id": "t", "note_path": "", "direction": "avoid", "conviction": "3",
                            "target_price": "10", "horizon_days": "365"}])
            with H.patched(LF, DOCS_DIR=docs, ASSETS_DIR=docs / "assets", THESES_DIR=theses,
                           FUNDAMENTALS_CSV_DIR=tmp / "panel", SCORING_SCORES_CSV=tmp / "s.csv",
                           SCORING_MARKS_CSV=tmp / "m.csv"), H.quiet():
                v = LF._write_ledger_assets()
                LF.generate_scorecard({"date": "2026-09-23", "stocks": []}, v)
            html = (docs / "scorecard.html").read_text(encoding="utf-8")
        self.assertIn('<a href="scorecard.html" aria-current="page">Scorecard</a>', html)
        self.assertNotIn(H.EM_DASH, html)
        self.assertNotIn(EN_DASH, html)
        cfg = json.loads(re.search(r"window\.APT_PAGE = (\{.*?\});\n", html).group(1))
        sc = cfg["scorecard"]
        self.assertEqual(sc["openCount"], 1)
        self.assertEqual(sc["open"][0]["action"], "Avoid (older note)")
        self.assertEqual(sc["minGroup"], 10)
        self.assertEqual(len(sc["byDesk"]), 6)

    def test_the_page_script_draws_the_scorecard_and_the_panels(self):
        js = (H.REPO / "web" / "ledger.js").read_text(encoding="utf-8")
        for needle in ("function renderScorecard(main)", 'page === "scorecard"', "Too few to read",
                       "bookAttributionHTML(b)", "bookPmHTML(b)", "<b>Excess return</b>",
                       "<b>Allocation</b>", "<b>Selection</b>", "<b>shadow portfolio</b>", "Proxy error",
                       ": \"No limit\", \"turnover_budget\"]", "Active share vs rules", "pmVsAnalystHTML(SC.pmVsAnalyst)"):
            self.assertIn(needle, js)
        self.assertNotIn(EN_DASH, js)
        self.assertNotIn(H.EM_DASH, js)

    def test_new_outputs_live_under_data_and_merge_by_union(self):
        attrs = (H.REPO / ".gitattributes").read_text(encoding="utf-8")
        self.assertIn("data/**/*.csv merge=union", attrs)
        self.assertTrue(str(LF.SCORING_MARKS_CSV).startswith(str(H.REPO / "data")))
        self.assertTrue(str(PM.ATTRIBUTION_CSV).startswith(str(H.REPO / "data")))


STYLE_MANDATE = dict(E.DEFAULT_STYLE_MANDATE, holdings_range=[2, 2], sector_cap=1.0, max_position=0.6)
PANEL_ROWS = [{"ticker": t, "sector": i["sector"], "security_type": "operating"} for t, i in CLASSES.items()]


def batch(day, orders, batch_id="w1"):
    return {"book": "lg-growth", "date": day, "batch_id": batch_id, "reason": "Swap B for C.",
            "orders": orders}


def rows_of(trades):
    return [{k: str(v) for k, v in t.items()} for t in trades]


def plan_batch(trades, b, views=None, mandate=STYLE_MANDATE, rules=None):
    return E.plan_orders(b, rows_of(trades), Store(CLOSES), PANEL_ROWS, CLASSES, mandate,
                         views=views, rules=rules)


class PmAgainstAnalyst(unittest.TestCase):

    def test_a_buy_against_a_negative_rating_needs_a_reason_and_is_tagged(self):
        orders = [{"id": "1", "ticker": "B", "side": "sell", "shares": 49},
                  {"id": "2", "ticker": "C", "side": "buy", "shares": 24}]
        views = {"C": {"exclude": True, "overweight": False, "why": "analyst: avoid"}}
        with self.assertRaises(E.OrderError) as cm:
            plan_batch(STYLE_TRADES[:3], batch(D2, orders), views)
        self.assertIn("order 2 (buy C): the analyst rates it avoid; a buy against that needs an "
                      "override_reason", str(cm.exception))
        orders[1]["override_reason"] = "The fertiliser price has already turned."
        plan = plan_batch(STYLE_TRADES[:3], batch(D2, orders), views)
        codes = {f["ticker"]: f["reason_code"] for f in plan["fills"]}
        self.assertEqual(codes, {"B": "pm_order", "C": "override_analyst"})
        self.assertEqual(plan["decision"]["reason"],
                         "Swap B for C. Against the analyst: buy C, rated avoid: "
                         "The fertiliser price has already turned..")
        # Selling a name the analyst rates Initiate or Add is tagged too; no reason is required.
        views = {"B": {"exclude": False, "overweight": True, "why": "analyst: initiate"}}
        plan = plan_batch(STYLE_TRADES[:3], batch(D2, orders[:1]), views)
        self.assertEqual(plan["fills"][0]["reason_code"], "override_analyst")

    def test_disagreements_are_marked_against_the_sector_fund(self):
        with H.temp_dir() as tmp:
            ctx = pm_fixture(tmp, STYLE_TRADES, STYLE_DECISIONS)
            E.append_rows(tmp / "no-events.csv", ["event_id", "date", "ticker", "kind", "direction",
                                                  "horizon_days", "action"],
                          [{"event_id": "C-1", "date": D1, "ticker": "C", "kind": "initiate",
                            "direction": "watch", "horizon_days": "60", "action": "Avoid"}])
            with H.quiet():
                PM.run_daily(ctx, today=D4, data_dir=tmp / "out", now="t")
                again = PM.run_daily(ctx, today=D4, data_dir=tmp / "out", now="t")
            rows = E.read_rows(tmp / "out" / "disagreements.csv")
        self.assertEqual(again["disagreements"], 0)
        self.assertEqual(len(rows), 1, "1 month is due; 3, 6 months and the analyst's 60 days are not")
        r = rows[0]
        self.assertEqual((r["ticker"], r["pm_side"], r["analyst_rating"], r["horizon"], r["mark_date"],
                          r["sector_etf"]), ("C", "buy", "Avoid", "1m", D4, "XLE"))
        # C 20 -> 30 (+50%), the energy fund 50 -> 55 (+10%): the PM's buy beat it by 40%.
        self.assertAlmostEqual(float(r["pm_excess"]), 0.4, places=9)
        self.assertEqual(r["pm_right"], "yes")
        summary = PM.disagreement_summary(rows)
        self.assertEqual((summary[0]["n"], summary[0]["winRate"], summary[0]["tooFew"]), (1, 1.0, True))
        self.assertEqual([g["n"] for g in summary[1:]], [0, 0, 0])


class TradeScript(unittest.TestCase):

    def test_trade_py_reads_the_analysts_view_as_of_the_trade_date(self):
        from tests import test_model_portfolio as TMP
        from portfolio.bin import trade as TRADE
        with H.temp_dir() as tmp:
            TMP.ledger_fixture(tmp)
            events = tmp / "events.csv"
            E.append_rows(events, ["event_id", "date", "ticker", "kind", "direction", "action"],
                          [{"event_id": "T100-1", "date": TMP.DAY, "ticker": "T100", "kind": "initiate",
                            "direction": "watch", "action": "Avoid"},
                           {"event_id": "T101-1", "date": "2026-09-25", "ticker": "T101",
                            "kind": "initiate", "direction": "short", "action": "Short"}])
            with H.quiet():
                E.seed(ledger_dir=tmp / "ledger", books_dir=tmp / "books", panel_dir=tmp / "panel",
                       prices=E.PriceStore(tmp / "prices"), events=events,
                       history=tmp / "history.csv", day=TMP.DAY)

            def run(orders):
                return TRADE.run([{"book": "hedge", "date": "2026-09-24", "batch_id": "w1",
                                   "reason": "Test.", "orders": orders}],
                                 prices=E.PriceStore(tmp / "prices"), ledger_dir=tmp / "ledger",
                                 books_dir=tmp / "books", panel_dir=tmp / "panel",
                                 history=tmp / "history.csv", events=events, out=lambda *a: None)
            with self.assertRaises(E.OrderError):
                run([{"id": "1", "ticker": "T100", "side": "buy", "weight": 0.02}])
            plan = run([{"id": "1", "ticker": "T100", "side": "buy", "weight": 0.02,
                         "override_reason": "Cheaper than the memo allows for."},
                        # rated Short only on the 25th, so on the 24th there is no view
                        {"id": "2", "ticker": "T101", "side": "buy", "weight": 0.02}])[0]
        codes = {f["ticker"]: f["reason_code"] for f in plan["fills"]}
        self.assertEqual(codes, {"T100": "override_analyst", "T101": "pm_order"})
        self.assertIn("buy T100, rated avoid: Cheaper than the memo allows for.", plan["decision"]["reason"])


class ActiveShare(unittest.TestCase):

    def test_formula_by_hand(self):
        self.assertAlmostEqual(E.active_share_vs_rules({"A": 0.5, "B": 0.5}, 0.0, {"A": 0.5, "B": 0.5}), 0.0)
        self.assertAlmostEqual(E.active_share_vs_rules({"C": 0.9}, 0.1, {"A": 0.5, "B": 0.5}), 1.0)
        # 0.5 * (|0.6-0.5| + |0-0.5| + |0.3-0| + |0.1-0|) = 0.5
        self.assertAlmostEqual(E.active_share_vs_rules({"A": 0.6, "C": 0.3}, 0.1, {"A": 0.5, "B": 0.5}), 0.5)

    def test_shadow_nav_records_active_share(self):
        with H.temp_dir() as tmp:
            ctx = pm_fixture(tmp, STYLE_TRADES, STYLE_DECISIONS)
            with H.quiet():
                PM.run_daily(ctx, today=D3, data_dir=tmp / "out", now="t")
            nav = {r["date"]: r for r in E.read_rows(tmp / "out" / "shadow_nav.csv")}
        self.assertAlmostEqual(float(nav[D1]["active_share"]), 0.0, places=12)
        # D3: book A 539, C 600, cash 9 (1,148); shadow A 539, B 441, cash 19.50 (999.50).
        want = 0.5 * (abs(539 / 1148 - 539 / 999.5) + 441 / 999.5 + 600 / 1148 + abs(9 / 1148 - 19.5 / 999.5))
        self.assertAlmostEqual(float(nav[D3]["active_share"]), want, places=9)

    def test_cap_is_enforced_only_when_set_and_only_against_increases(self):
        self.assertIsNone(E.DEFAULT_STYLE_MANDATE["max_active_share_vs_rules"])
        self.assertIsNone(E.DEFAULT_STYLE_MANDATE["turnover_budget"], "only tax books will carry one")
        rules = {"A": 0.5, "B": 0.5}
        swap = [{"id": "1", "ticker": "B", "side": "sell", "shares": 49},
                {"id": "2", "ticker": "C", "side": "buy", "shares": 24}]
        self.assertTrue(plan_batch(STYLE_TRADES[:3], batch(D2, swap))["fills"])
        capped = dict(STYLE_MANDATE, max_active_share_vs_rules=0.10)
        with self.assertRaises(E.OrderError) as cm:
            plan_batch(STYLE_TRADES[:3], batch(D2, swap), mandate=capped, rules=rules)
        self.assertIn("active share against the rules", str(cm.exception))
        with self.assertRaises(E.OrderError) as cm:
            plan_batch(STYLE_TRADES[:3], batch(D2, swap), mandate=capped)
        self.assertIn("no rules book was given", str(cm.exception))
        # Already far from the rules after the swap; undoing it brings the book closer
        # and is allowed.
        back = [{"id": "1", "ticker": "C", "side": "sell", "shares": 25},
                {"id": "2", "ticker": "B", "side": "buy", "shares": 49}]
        plan = plan_batch(STYLE_TRADES, batch(D3, back, "w2"), mandate=capped, rules=rules)
        self.assertEqual(len(plan["fills"]), 2)


if __name__ == "__main__":
    unittest.main()
