#!/usr/bin/env python3
"""Gate a written thesis before it is allowed into the archive.

These are not style preferences. Each check corresponds to a specific way that
AI-written research goes wrong, and each is mechanical so it cannot be argued
with at 3am by a model that would rather ship something.

    python3 theses/bin/validate.py theses/notes/YELP/2026-09-14-initiation.md
    python3 theses/bin/validate.py theses/runs/2026-09-14/          # whole run

Exit code 0 = all pass. 1 = at least one FAIL. Warnings never fail the run.

Two formats. A note whose front-matter says `format: memo` is the buy-side
investment memo (page one, twelve numbered sections, a glossary) and gets the
memo checks in _memo(). A note with no `format`, or `format: note`, is the
older plain note and gets exactly the checks it always had.
"""
import math, re, sys
from datetime import date
from pathlib import Path

from common import SLEEVES, DEAD, CONTAMINATED, THESES, LEDGER, read_csv_rows

REQUIRED_FM = ["thesis_id", "ticker", "kind", "written_on", "panel_date", "entry_price",
               "direction", "conviction", "horizon_days", "target_price", "review_by",
               "falsifier", "data_caveats",
               # Conviction is DERIVED, not asserted. A number the analyst simply
               # feels is not comparable across notes written in separate sessions
               # with no memory of each other, and a model asked to rate its own
               # confidence clusters at 3 to 4 no matter how the prompt is worded.
               # Each component is a property of the note a reader can check, so a
               # disagreement about conviction becomes a disagreement about
               # something specific.
               "evidence_base", "falsifier_specific", "variant_perception",
               "disconfirmation"]
CONVICTION_PARTS = {"evidence_base": (0, 2), "falsifier_specific": (0, 1),
                    "variant_perception": (0, 1), "disconfirmation": (0, 1)}
DIRECTIONS = {"long", "short", "avoid", "watch", "no view"}
KINDS = {"initiation", "update", "revision", "close"}

# The six plain-English sections, in the order a note uses them. Each must be a
# heading line of its own. The sources table is the seventh and last, and is
# checked separately because it has a format.
SECTIONS = ["WHAT HAS TO BE TRUE FOR THE PRICE TO MAKE SENSE", "WHERE I DISAGREE",
            "WHAT WOULD SETTLE IT", "WHAT THE SHARES COULD BE WORTH",
            "WHAT WOULD PROVE ME WRONG", "WHAT I DON'T KNOW"]
NUMBERS_SECTION = "WHERE THE NUMBERS COME FROM"

# A note written on or after this date has two parts. The business comes first:
# four sections that say how the company makes money, before the six above ask
# the reader to trust a view on its price. The first notes had only the view, and
# the owner, who reads them to learn how a business works, was given a price
# target for a fertiliser maker without being told what decides its profit.
# The four 2026-09-12 notes keep the one-part shape they were written in.
TWO_PART_FROM = "2026-09-19"
BUSINESS_SECTIONS = ["WHAT THE COMPANY DOES", "HOW IT MAKES MONEY", "THE LAST TEN YEARS",
                     "WHAT MANAGEMENT DOES WITH THE CASH"]
BUSINESS_MIN_WORDS = 300

# Prose length, not counting WHERE THE NUMBERS COME FROM: (shortest, warn, fail,
# target). A note that runs long is almost always explaining terms it should drop.
PROSE_ONE_PART = (220, 800, 1100, 700)
PROSE_TWO_PART = (700, 1800, 2300, 1500)

# ---------------------------------------------------------------- memo format
# The buy-side investment memo: one analyst's note to the portfolio manager,
# switched on for the analyst run of 2026-09-28. Page one comes before the first
# heading, under a bold one-line headline. Then these sections, in this order.
# An initiation carries all of them; a revision carries WHAT CHANGED, section 10,
# any other numbered section that changed, then SOURCES and GLOSSARY.
MEMO_SECTIONS = ["1. WHAT IS PRICED IN", "2. WHERE I DISAGREE", "3. THE BUSINESS",
                 "4. INDUSTRY AND PEERS", "5. FINANCIAL HISTORY", "6. FORECAST", "7. VALUATION",
                 "8. CATALYSTS", "9. RISKS AND PRE-MORTEM", "10. MONITORING AND EXIT RULES",
                 "11. WHAT I DON'T KNOW", "12. SOURCES", "GLOSSARY"]
MEMO_CHANGED = "WHAT CHANGED"
MEMO_MONITOR = "10. MONITORING AND EXIT RULES"
MEMO_SOURCES = "12. SOURCES"
MEMO_GLOSSARY = "GLOSSARY"
# Words of prose, not counting tables, headings, SOURCES or GLOSSARY.
MEMO_LENGTH = {"initiation": (2000, 5000), "revision": (300, 1500)}
MEMO_HORIZON = 365
MEMO_FM = ["action", "size_now", "size_plan", "expected_return", "bear_return",
           "required_return", "scenarios"]
MEMO_BLANK_OK = {"size_plan"}
# The seven actions, and the direction each one is scored as. Avoid is "watch",
# or "avoid" only when the memo expects the stock to do worse than its peers.
MEMO_ACTIONS = {"Initiate": {"long"}, "Add": {"long"}, "Hold": {"long"}, "Trim": {"long"},
                "Exit": {"watch"}, "Avoid": {"watch", "avoid"}, "Short": {"short"}}
MEMO_HOLDING = {"Initiate", "Add", "Hold", "Trim"}
SCENARIO_CASES = ("bull", "base", "bear")
# Page-one arithmetic tolerances.
PROB_TOL, VALUE_TOL, RETURN_TOL, TARGET_TOL = 0.005, 0.50, 0.005, 5.0
# The draft bear-loss limit in PROMPTS.md step 5, SIZE: size x |bear_return|.
BEAR_COST_CAP = 0.02
GLOSSARY_FILE = THESES / "GLOSSARY.md"
_DATE_WORDS = re.compile(r"\b\d{4}-\d{2}-\d{2}\b"
                         r"|\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.?\s+\d{4}\b")
# The en dash, written as a code point so this file never contains one.
EN_DASH = chr(0x2013)

# Front-matter the website's thesis popup reads besides key_claim and falsifier
# (lambda_function.py, _thesis_note_record). None is required; each is checked
# when present. A condition may end in "[check: FIELD OP NUMBER]", which the page
# evaluates against docs/stocks-data.json. This is the page's own _CONDITION_CHECK
# pattern, so a check that parses here is one the page can compute.
CONDITION_CHECK = re.compile(
    r"\s*\[check:\s*([A-Za-z_][\w.]*)\s*(>=|<=|>|<)\s*"
    r"([-+−]?(?:\d+(?:\.\d*)?|\.\d+))\s*\]\s*$")
CHECK_OPS = (">=", "<=", ">", "<")
# stocks-data.json stores these as decimals (fcf_yield 0.0948 is 9.48 percent) and
# every other checkable field raw (net_debt_ebitda 0.29, pe 9.89). A check number
# written in percent is the likeliest mistake. The BOUNDED ones stay within 1 for
# nearly every stock (p95 at most 0.56 on 2026-09-13); growth and returns can pass
# 1 legitimately, so they only warn at 10.
DECIMAL_FIELDS = {"revenue_growth_yoy", "eps_growth_yoy", "revenue_acceleration", "gross_margin_trend",
                  "fcf_growth_yoy", "fcf_yield", "return_12_2", "return_1m", "high52w_proximity",
                  "rel_strength_sp500", "volume_trend", "roe_ttm", "earnings_consistency",
                  "op_margin_stability", "accruals_ratio", "volatility_1y", "max_drawdown_1y",
                  "return_52w"}
BOUNDED_DECIMAL = {"fcf_yield", "high52w_proximity", "max_drawdown_1y", "earnings_consistency",
                   "gross_margin_trend", "op_margin_stability", "accruals_ratio", "return_1m"}

# Fields a dossier prints. A cited name outside this set is either a typo or an
# invented source. Warn rather than fail: dossier.py can add a field before this
# list is updated.
KNOWN_FIELDS = {f for s in SLEEVES.values() for f in s["fields"]} | {
    "price", "market_cap", "shares_outstanding", "analyst_count", "beta_1y",
    "volatility_1y", "sharpe_1y", "max_drawdown_1y", "return_52w", "ttm_revenue",
    "ttm_eps_diluted", "prior_ttm_eps_diluted", "ttm_ebitda", "ttm_net_income", "ttm_fcf",
    "ttm_gross_profit", "ttm_operating_income", "total_debt", "cash_and_investments",
    "equity", "insider_net_buy_90d", "insider_buyer_count_90d", "insider_seller_count_90d",
    "insider_cluster_max_30d", "insider_tx_count_90d"}

# ---------------------------------------------------------------- plain writing
# The owner reads these notes and has no finance background. Every pattern below
# is narrow on purpose: each one names the legitimate writing it could catch, and
# is shaped to leave that writing alone.

# A section heading has exactly two # signs. The website (lambda_function.py
# _note_sections) splits a note into sections only on "## ", so a "###" section
# would show as part of the one before it. ANY_HEADING is every heading level: it
# keeps heading lines out of word counts and spots a section written at the wrong level.
HEADING = re.compile(r"^##[ \t]+(.+?)[ \t]*#*[ \t]*$", re.M)
ANY_HEADING = re.compile(r"^#{1,6}[ \t]+(.+?)[ \t]*#*[ \t]*$", re.M)

# Words about the research tools rather than the company. FAIL in the body, the
# key claim, the falsifier and the caveats, all of which the owner reads.
PIPELINE_WORDS = [
    ("dossier", r"\bdossiers?\b"),
    ("dataset", r"\b(?:this|the|that|our|my|any) data ?sets?\b"),
    # The research panel. A solar or display panel business is fine: "panel makers",
    # "panel prices fell", "the panel price per watt". "The panel price was 2.2
    # percent stale" is the caveat every seed note carries, so the singular
    # "panel price" is not excused.
    ("panel", r"\b(?:the|this|our|factor|data) panel(?:'s|’s)?\b"
              r"(?!\s+(?:makers?|manufactur\w*|suppliers?|shipments?|prices\b|price\s+(?:per|of|for|index)\b"
              r"|installations?|business))"
              r"|\bpanel (?:date|factors?|fields?|says|reads)\b"),
    # A factor sleeve, never a shirt sleeve.
    ("sleeve", r"\b(?:growth|value|momentum|quality|factor) sleeves?\b|\bsleeves?\s+(?:reads?|z\b|scores?)"),
    ("percentile", r"\bpercentiles?\b|\b\d{1,3}(?:st|nd|rd|th)\s+pctl?\b|\bpct\b"),
    ("z-score", r"\bz[- ]?scores?\b|\bcomposite z\b"),
    # Only the peer-group sense. Trial cohorts and customer cohorts are fine.
    ("cohort", r"\bpeer cohort\b|\bcohort (?:is |was )?too small\b"),
    # Only the data sense. Oil fields and field sales are fine.
    ("field", r"\bfield-level\b|\b(?:data|factor|panel|growth|value|momentum|quality) fields?\b"
              r"|\b(?:this|that|the|each|every) field reads\b"),
    # Only the tooling sense. Oil pipelines and drug pipelines are fine.
    ("pipeline", r"\b(?:this|the|our) pipeline (?:collects?|records?|fetche?s|cannot|can't|already records"
                 r"|does not (?:collect|record|carry|have))\b"
                 r"|\b(?:exists?|available|collected) (?:anywhere )?in (?:this|the) pipeline\b"),
    # Only the tooling sense. A phone screen or a cancer screening is fine.
    ("screen", r"\bscreener\b|\bfactor screen\b|\bthe screen (?:surfaced|flagged|picked|put|puts|says|said)\b"
               r"|\btension fired\b"),
    ("slot", r"\b(?:contrarian|screen|watchlist) slot\b"),
    ("ledger", r"\bgraded number\b|\bthe ledger (?:should|will|would|grades?|records?)\b"),
    ("file name", r"\b[\w-]+\.(?:csv|json|py)\b"),
]
PIPELINE_WORDS = [(label, re.compile(p, re.I)) for label, p in PIPELINE_WORDS]

# A price, a stock or a market written as if it had a mind. FAIL. The subject must
# be one of these nouns and the verb must be a verb of thought, speech or
# judgement, so "the stock fell" and "the market for fertiliser grew" pass.
# Participles followed by "by" describe arithmetic ("the price implied by 10 times
# profit") and pass.
_SUBJ = (r"(?:\b(?:the|this|its|that|a|today's|today’s)|\b[A-Z][A-Za-z&.]*(?:'s|’s)) "
         r"(?:stock market|stock price|share price|stock|shares|price|market|multiple|valuation)")
_THINK = (r"embeds|embedding|embedded(?!\s+(?:by|in))|asserts|asserting|asserted|believes|believing"
          r"|believed|thinks|thinking|thought|decides|deciding|decided|says|saying|knows|knowing|knew"
          r"|fears|fearing|feared|worries|worrying|worried|expects|expecting|wants|wanting|tells"
          r"|telling|told|bets|betting|forgets|forgot|forgotten|ignores|ignoring|ignored|agrees"
          r"|disagrees|punishes|punishing|punished|rewards|rewarding|rewarded|doubts|hopes|hoping"
          r"|looks through|looking through|prices in|price in|priced in|pricing in|pays for|paying for"
          r"|assumes|assuming|assumed(?!\s+(?:by|in))|implies|implying|implied(?!\s+by)"
          r"|suggests|suggesting|suggested(?!\s+by)|anticipates|anticipating|anticipated"
          r"|discounts|discounting|underestimates|underestimating|underestimated"
          r"|overestimates|overestimating|overestimated|overlooks|overlooking|overlooked"
          r"|(?:gets|getting|got)(?:\s+\S+){0,4}?\s+wrong")
PERSONIFY = re.compile(
    _SUBJ + r"(?:\s+(?:that|which))?"
    r"(?:\s+(?:is|are|was|were|has|have|had|does|do|did|seems to|appears to|is not|isn't|has not"
    r"|hasn't|does not|doesn't|do not|don't|has been|have been))?"
    r"(?:\s+(?:already|clearly|still|now|simply|really|only))?"
    r"\s+(?:" + _THINK + r")(?!\w)"
    r"|" + _SUBJ + r"\s+(?:is|are|was|were|has been|have been)\s+pricing\b", re.I)

# Numbers in analyst shorthand. FAIL. "36x" is "36 times", "13.4pp" is "13.4
# percentage points". The lookbehind leaves product names such as "S4x" alone, the
# lookahead leaves "4x4" alone, and 10x Genomics is a company name.
SHORTHAND = re.compile(r"(?<![\w.])\d+(?:\.\d+)?[x×](?!\w)(?!\s+Genomics\b)"
                       r"|(?<![\w.])[+\-−]?\d+(?:\.\d+)?\s?(?:pp|bps|pts)\b"
                       r"|\b(?:pe|p/e)\s+\d", re.I)

# FAIL: filler phrases with no literal use in a note.
FILLER_HARD = re.compile(
    r"\bwhich is the (?:whole )?point\b|\bthat is the (?:whole|entire) (?:point|question|thesis|story)\b"
    r"|\bthe (?:whole|entire|single most important) (?:question|point|thesis)\b"
    r"|\bthat is not a detail\b|\bmake no mistake\b|\bworth writing down\b", re.I)

# Everything below is WARN: useful to see, but each has real legitimate uses.
# PROMPTS.md step 7 makes the analyst fix each warning or record why it stays.
GENERALIZE = re.compile(
    r"\b(?:investors|traders|fund managers|the street|wall street|everyone|nobody)\b"
    r"|\b(?:cyclicals|growth stocks|value stocks|commodity stocks) (?:are|trade|get|tend)\b"
    r"|\bpeople who (?:trade|buy|own|sell|screen|follow|price)\b"
    r"|\b(?:stocks|shares|markets) (?:usually|typically|always|never|rarely|tend to)\b"
    r"|\b(?:a|the) (?:stock|share price) does not usually\b"
    r"|\bthe (?:single )?most reliabl\w*\b", re.I)
_NEG = r"(?:(?:is|are|was|were|does|do|did) not|isn't|aren't|wasn't|weren't|doesn't|don't|didn't)"
ANTITHESIS = re.compile(
    r"\b" + _NEG + r"\b[^.!?\n]{1,120}[.!?]\s+(?:it|this|that|they|these|those) (?:is|are|was|were)\b"
    r"|\b" + _NEG + r" (?:just |only |merely |simply )?[^.,;:!?\n]{1,50},? but\b"
    r"|\bnot (?:just|only|merely|simply)\b[^.;:!?\n]{1,60}\bbut\b"
    r"|\bnot because\b[^.!?\n]{1,80}\bbut because\b"
    r"|\w, not (?:a |an |the )?[\w-]+(?: [\w-]+)?[.;]", re.I)
FILLER_SOFT = re.compile(
    r"\bto be clear\b|\bit is worth (?:saying|noting)\b|\bthe whole story\b"
    r"|\bthat is the (?:point|question)\b|\bthe honest answer is\b|\bin other words\b", re.I)

# Figures of speech. The owner's main complaint about the first notes was their
# tone: a picture where the fact should be ("the balance sheet is the shock
# absorber") and a hint that something is hidden where a source should be. A
# picture makes the reader translate it back, and he has no finance background to
# translate with. Say what literally happens: "low debt means CF can keep paying
# its bills if profit falls".
#
# FAIL: phrases with no literal use in a note about a company.
FIGURE_HARD = re.compile(
    r"\bvalue traps?\b|\bfalling kni(?:fe|ves)\b|\bdead money\b|\bcoiled spring\b"
    r"|\bmelting ice cubes?\b|\bcash cows?\b|\bcrown jewels?\b|\bwar chests?\b|\bdry powder\b"
    r"|\bfortress balance sheet\b|\bkitchen[- ]sink\w*\b|\bpriced (?:for|to) perfection\b"
    r"|\bcanary in the coal ?mine\b|\belephant in the room\b"
    r"|\bdouble-edged sword\b|\bperfect storm\b|\bgreen shoots\b|\bsea change\b"
    r"|\bat the end of the day\b|\bpaper(?:s|ed|ing)? over\b"
    r"|\bjury is (?:still )?out\b|\blow-hanging fruit\b|\bmov(?:e|es|ed|ing) the needle\b"
    r"|\bsecret sauce\b|\brocket ?ship\b|\bhouse of cards\b|\btip of the iceberg\b"
    r"|\bsilver (?:bullet|lining)\b|\bgame[- ]changers?\b|\bnorth star\b|\bholy grail\b"
    r"|\b(?:economic |competitive |wide |narrow |deep )?moats?\b|\bskin in the game\b"
    r"|\bhead ?fake\b|\bsmoking gun\b", re.I)
# WARN: usually a figure of speech, but a wind farm has tailwinds and a parts maker
# sells shock absorbers.
FIGURE_SOFT = re.compile(
    r"\b(?:tail|head)winds?\b|\bshock absorbers?\b|\bflywheels?\b|\brunway\b|\blevers?\b to pull"
    r"|\b(?:growth|turnaround|recovery|equity|bull|bear|long-run|real) story\b|\bthe narrative\b"
    r"|\bstory stock\b|\bre-?rat(?:e|es|ed|ing)\b|\bunlock(?:s|ed|ing)? value\b|\bhidden gems?\b"
    r"|\bbleed(?:s|ing)? cash\b|\bhaemorrhag\w+|\bhemorrhag\w+|\bfiring on all cylinders\b"
    r"|\bin the driver'?s seat\b|\bback of the envelope\b|\bcushion\b|\bbackstop\b"
    r"|\bcooperat(?:e|es|ed|ing)\b|\bred flags?\b"
    # Figures of speech nearly every time, but a bakery's bread is baked in stores
    # and a defence supplier sells radar. A FAIL refuses a whole delivery, so a
    # phrase with an honest literal use only warns.
    r"|\bbaked in(?:to)?\b|\bunder the hood\b|\bthin ice\b|\b(?:below|under) the radar\b", re.I)

# Hinting that something is hidden, overlooked or about to be revealed. FAIL. If
# there is a fact, state it and say where it comes from.
MYSTIQUE = re.compile(
    r"\bwhat (?:nobody|no one|everyone|the market|the street) (?:is )?(?:asking|watching|missing|misses"
    r"|missed|has missed|talking about|sees|see)\b"
    r"|\b(?:beneath|below|under) the surface\b|\bthe (?:real|deeper|bigger|actual) (?:story|question|issue)\b"
    r"|\btells? a different story\b|\bhiding in plain sight\b|\bin plain sight\b|\bit turns out\b"
    r"|\bhere(?: is|'s|’s) the thing\b|\blurk(?:s|ing)?\b|\bthe truth is\b|\blook closer\b"
    r"|\bon closer inspection\b|\bscratch the surface\b|\bthe dirty secret\b|\bopen secret\b", re.I)
# Adverbs that tell the reader how to feel about a fact. WARN.
DRAMA = re.compile(
    r"\b(?:crucially|strikingly|remarkably|tellingly|notably|importantly|interestingly|curiously"
    r"|surprisingly|famously|quietly|dramatically|ominously|critically|fundamentally|ultimately)\b",
    re.I)

# Finance and industry terms the owner will not know: (label, pattern, plain
# wording, hard). The plain wording matches the glossary in the writing rules.
# In key_claim, falsifier and data_caveats a HARD term FAILS: the website shows
# those with no body around them, so there is nowhere to explain the term. In the
# body every term only warns, because a term explained where it first appears is
# allowed. Acronyms match case-sensitively so ordinary words do not trip them.
JARGON = [
    ("EPS", r"\bEPS\b", "profit per share", True),
    ("EBITDA", r"\bEBITDA\b", "earnings before interest, tax and wear-and-tear, spelled out", True),
    ("free cash flow yield", r"\bFCF\b|(?i:\bfree[- ]cash[- ]flow yield\b|\bcash flow yield\b)",
     "$N of spare cash a year for every $100 of stock", True),
    ("TTM", r"\bTTM\b|\bLTM\b", "over the past 12 months", True),
    ("YoY", r"\bYoY\b|\bQoQ\b", "compared with a year earlier", True),
    ("P/E", r"\bP/E\b|(?i:\bprice[- ]to[- ]earnings\b|\bearnings multiple\b)",
     "costs N times its profit per share over the past year", True),
    ("EV", r"\bEV\b|(?i:\benterprise value\b)",
     "what the whole company would cost, counting its debt minus its cash", True),
    ("ROE", r"\bROE\b|(?i:\breturn on equity\b)", "profit compared with the money shareholders have put in", True),
    ("net debt / leverage", r"(?i:\bnet debt\b|\bnet cash\b|\bleverage ratio\b|\boperating leverage\b)",
     "debt minus cash", True),
    ("the multiple", r"(?i:\b(?:the|a|its|this|that|higher|lower|growth|low|high|premium) multiples?\b"
                     r"(?!\s+(?:sclerosis|myeloma)))", "N times profit per share", True),
    ("re-rate", r"(?i:\bre-?rat(?:e|es|ed|ing)\b)", "the stock starts to cost more for each $1 of profit", True),
    ("reacceleration / deceleration", r"(?i:\bre-?accelerat\w*|\bdecelerat\w*)",
     "growth speeding back up / slowing down", True),
    ("the cycle", r"(?i:\bcyclicals?\b|\b(?:peak|mid|up|down|trough)[- ]?cycle\b|\bthe cycle\b)",
     "profits that rise and fall with prices the company does not control, and where they are now", True),
    ("priced in", r"(?i:\b(?:already |fully |largely |partly )?priced in(?=\s*[.,;:)]|\s*$|\s+(?:by|already|now)\b))",
     "the price only makes sense if", True),
    ("variant perception", r"(?i:\bvariant perception\b)", "where I disagree", True),
    ("value trap", r"(?i:\bvalue traps?\b)", "a stock that looks cheap on today's profit, which then falls", True),
    ("mean reversion", r"(?i:\bmean[- ]revert\w*|\bmean reversion\b)", "tends to return to its usual level", True),
    ("accruals", r"(?i:\baccruals?\b)", "profit booked but not yet received as cash", True),
    ("drawdown", r"(?i:\bdrawdowns?\b)", "the biggest fall from a high point", True),
    ("Sharpe", r"\bSharpe\b", "leave it out", True),
    ("basis points", r"(?i:\bbasis points?\b)|\bbps\b", "hundredths of a percentage point", True),
    ("bear / base / bull case", r"(?i:\b(?:bear|bull|base) case\b)", "bad / middle / good case", True),
    ("headwinds / tailwinds", r"(?i:\b(?:head|tail)winds?\b)", "say what is hurting or helping", True),
    ("margin pool", r"(?i:\bmargin pools?\b)", "the profit the whole industry makes", True),
    ("price to book", r"(?i:\bprice[- /]to[- ]book\b|\bbook value\b)|\bP/B\b",
     "the price compared with what the company owns minus what it owes", True),
    ("capex", r"(?i:\bcapex\b|\bcapital expenditures?\b)", "spending on equipment and buildings", True),
    ("long / short", r"(?i:\b(?:go|going|went|rather than|not) short\b(?!\s+of\b)|\bshort(?:ing)? (?:it|the stock|the shares)\b"
                     r"|\b(?:long|short) positions?\b)", "own it / bet against it / stay away", True),
    ("market cap", r"(?i:\b(?:market|large|mid|small|micro|mega)[- ]caps?\b|\bmarket capitali[sz]ation\b)",
     "all its shares together are worth $N", True),

    ("trailing", r"(?i:\btrailing\b)", "over the past 12 months", False),
    ("beta", r"(?i:\bbeta\b)", "how much the stock tends to move when the whole market moves", False),
    ("momentum", r"(?i:\bmomentum\b)", "the price has been rising (or falling) for months", False),
    ("consensus", r"(?i:\bconsensus\b)", "the average forecast of the analysts who follow it", False),
    ("catalyst", r"(?i:\bcatalysts?\b)", "the dated event that will show who is right", False),
    ("guidance", r"(?i:\bguidance\b)", "the company's own forecast", False),
    ("margin", r"(?i:\b(?:gross|operating|net|profit) margins?\b)",
     "how much of each $1 of sales is left after the costs you name", False),
    ("SEC form codes", r"\bEX-99\.1\b|\b8-K\b|\b10-Q\b|\b10-K\b|\bForm 4\b",
     "the quarterly results announcement / the quarterly report / the annual report", False),
    ("buyback", r"(?i:\bbuybacks?\b|\brepurchases?\b)", "the company buying back its own shares", False),
    ("spread", r"(?i:\b(?:crack|nitrogen|fertili[sz]er|refining|the) spreads?\b)",
     "the gap between what it sells for and what its main input costs", False),
    ("accelerating", r"(?i:\baccelerat\w*)", "growth is speeding up", False),
    ("throughput", r"(?i:\bthroughput\b)", "how much crude oil its refineries process each day", False),
    ("midstream / upstream / downstream", r"(?i:\b(?:mid|up|down)stream\b)",
     "say what that part does: pipelines and storage, drilling, or refining and selling", False),
    ("compounding", r"(?i:\bcompound(?:ed|ing)\b)",
     "for drugs, copies mixed by pharmacies; for money, growth on top of growth", False),
    ("GLP-1", r"\bGLP-1s?\b", "the newer weight-loss and diabetes drugs", False),
    ("telehealth", r"(?i:\btele(?:health|medicine)\b)", "seeing a doctor online", False),
    ("ASP", r"\bASPs?\b|(?i:\baverage selling prices?\b)", "the average price it gets for each one sold", False),
    ("unit volumes", r"(?i:\bunit (?:volumes?|sales)\b)", "how many it sells", False),
    ("cannibalise", r"(?i:\bcannibali[sz]\w*)", "people buy the new product in place of an existing one", False),
    ("product cycle", r"(?i:\b(?:product|upgrade|replacement|launch|foldable|iphone) cycles?\b)",
     "the wave of sales that follows a new model", False),
    ("fee-based", r"(?i:\bfee-based\b)", "earns a set charge for each barrel it moves or stores", False),
    ("balance sheet", r"(?i:\bbalance sheets?\b)", "what the company owns and what it owes", False),
    ("upside / downside", r"(?i:\b(?:up|down)side\b)", "how far the price could rise / fall", False),
    ("exposure", r"(?i:\bexpos(?:ed|ure)\b)", "say what could hurt it and how", False),
    ("fundamentals", r"(?i:\bfundamentals\b)", "sales, profit and cash", False),
    ("inflection", r"(?i:\binflection\b)", "the point where growth turns", False),
    ("trough", r"(?i:\btroughs?\b)", "low point", False),
    ("solvency / distress", r"(?i:\b(?:in)?solven(?:t|cy)\b|\bdistress(?:ed)?\b)",
     "able (or unable) to pay its debts", False),
    ("class action", r"(?i:\bclass actions?\b)", "a lawsuit brought for a whole group of shareholders or customers", False),
    ("agency initials", r"\bFTC\b|\bFDA\b|\bSEC\b", "name the agency in full once and say what it does", False),
    ("analyst rating", r"(?i:\b(?:downgrade[sd]?|overweight|underweight|outperform|underperform|price targets?"
                       r"|at neutral|neutral rating|resumed coverage|initiat(?:es|ed|ing) coverage)\b)",
     "say in plain words whether the analyst thinks the shares will do better or worse", False),
    ("peer group", r"(?i:\bpeer groups?\b|\bpeers\b)", "similar companies", False),
    ("dilution", r"(?i:\bdilut(?:ion|ive)\b)", "new shares that shrink each existing share's slice", False),
    ("revenue mix", r"(?i:\b(?:revenue|segment|product) mix\b|\bmix shift\b)",
     "how sales split between the parts of the business", False),
    ("dividend yield", r"(?i:\bdividend yield\b)", "$N of dividends a year for every $100 of stock", False),
]
JARGON = [(label, re.compile(p), plain, hard) for label, p, plain, hard in JARGON]

# In a memo a finance term is allowed once the note's GLOSSARY defines it. A
# JARGON label counts as defined when its own pattern matches a term in the
# GLOSSARY's first column, or when one of these does: the glossary names the
# term in its dictionary form ("Valuation multiple", "Short"), which the body
# pattern, written to catch the term in running prose, would miss.
GLOSSARY_ALIASES = {
    "the multiple": r"(?i:multiple)",
    "the cycle": r"(?i:cycl)",
    "bear / base / bull case": r"(?i:\b(?:bear|base|bull)\b)",
    "long / short": r"(?i:^(?:long|short)\b)",
    "peer group": r"(?i:\bpeers?\b|\bcomparable compan)",
    "priced in": r"(?i:^priced in\b)",
    "analyst rating": r"(?i:price target|\brating)",
    "agency initials": r"(?i:securities and exchange commission|food and drug administration"
                       r"|federal trade commission)",
}
GLOSSARY_ALIASES = {k: re.compile(v) for k, v in GLOSSARY_ALIASES.items()}

# "history" is the dossier's reported history table: a decade of the company's own
# filed figures, and the source the business sections lean on most. It had no
# name here, so the first two-part note labelled fifteen rows "dossier reported
# history", failed on all of them, and settled on "filing reported history in the
# checkout" as a guess at what was wanted.
SOURCE_KIND = re.compile(r"^(?:`[a-z][a-z0-9_]*`|close\b|filing\b|history\b|headline\b|calc\b|my choice\b)", re.I)
_MONTHS = r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.?"
# Units whose numbers are always claims: dollars, percent, "times", percentage
# points, cents. A missing row for one of these FAILS. A bare count only warns,
# because product names, rule numbers and the like cannot all be listed.
FAIL_UNITS = {"$", "%", "x", "pts", "¢"}
_ABBREV = re.compile(r"\b(?:U\.S|U\.K|Inc|Corp|Co|Ltd|St|Dr|Mr|Mrs|Ms|No|vs|e\.g|i\.e|approx)\.")


def parse(text):
    m = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.S)
    if not m:
        return None, text
    fm, body = {}, m.group(2)
    key, buf = None, []
    for line in m.group(1).splitlines():
        if re.match(r"^\s*-\s+", line) and key:
            fm.setdefault(key + "__list", []).append(line.strip()[1:].strip())
            continue
        mm = re.match(r"^([A-Za-z_][\w]*):\s*(.*)$", line)
        if mm:
            key, val = mm.group(1), mm.group(2).strip()
            # Inline flow lists: "[]" and "[a, b]". Without this an empty inline
            # list reads as the string "[]" and the caller reports the wrong
            # defect, which is worse than reporting none.
            if val.startswith("[") and val.endswith("]"):
                inner = val[1:-1].strip()
                fm[key] = [x.strip() for x in inner.split(",") if x.strip()] if inner else []
            else:
                fm[key] = val
    for k in list(fm):
        if k.endswith("__list"):
            fm[k[:-6]] = fm.pop(k)
    return fm, body


def check(path):
    text = Path(path).read_text(encoding="utf-8")
    fm, body = parse(text)
    fails, warns = [], []
    F, W = fails.append, warns.append

    if fm is None:
        return ["no YAML front-matter: the PM agent reads front-matter, not prose"], []

    for k in REQUIRED_FM:
        if k not in fm:
            F(f"missing required field: {k}")
        elif fm[k] in ("", None) and not isinstance(fm[k], list):
            # An empty LIST is a real value with its own diagnostic below.
            # Reporting it twice buries the useful message under the generic one.
            F(f"required field is blank: {k}")

    # The defence against a note that asserts more than it can know.
    cav = fm.get("data_caveats")
    if isinstance(cav, list):
        if len(cav) == 0:
            F("data_caveats is empty. No thesis built on this pipeline knows everything: "
              "there are no consensus estimates anywhere in it, which alone is a caveat.")
        elif len(cav) == 1:
            W("only one data caveat. Check the dossier's own caveat list was carried over.")
    elif cav:
        F("data_caveats must be a list, not a string")

    fal = fm.get("falsifier", "")
    if fal and len(fal) < 40:
        F(f"falsifier is {len(fal)} chars and is almost certainly not checkable. It needs a "
          f"condition, a threshold and a source.")
    if fal and not re.search(r"\d", fal):
        F("falsifier contains no number. 'the thesis deteriorates' is not falsifiable.")

    d = (fm.get("direction") or "").strip().strip('"')
    if d and d not in DIRECTIONS:
        F(f"direction {d!r} not one of {sorted(DIRECTIONS)}")
    k = (fm.get("kind") or "").strip().strip('"')
    if k and k not in KINDS:
        F(f"kind {k!r} not one of {sorted(KINDS)}")

    try:
        c = int(str(fm.get("conviction", "")).strip())
        if not 0 <= c <= 5:
            F(f"conviction {c} outside 0-5")
    except (TypeError, ValueError):
        c = None
        F("conviction is not an integer")

    total, ok_parts = 0, True
    for k, (lo, hi) in CONVICTION_PARTS.items():
        try:
            v = int(str(fm.get(k, "")).strip())
        except (TypeError, ValueError):
            # Must fail, not just skip. Both checks below are guarded on
            # ok_parts, so a component that is present but unparseable used to
            # disable the sum check and the evidence floor at once: a note could
            # claim conviction 5 with evidence_base "two" and pass clean.
            F(f"{k} is not an integer: {fm.get(k)!r}")
            ok_parts = False
            continue
        if not lo <= v <= hi:
            F(f"{k} is {v}, outside {lo}-{hi}")
            ok_parts = False
        else:
            total += v
    if ok_parts and c is not None and c != total:
        F(f"conviction is {c} but its components sum to {total} "
          f"(evidence_base + falsifier_specific + variant_perception + disconfirmation). "
          f"Conviction is derived, not asserted: change a component or change the note.")
    if ok_parts and c is not None and c >= 4 and int(str(fm.get("evidence_base", 0)).strip() or 0) < 1:
        F("conviction 4 or above with evidence_base 0. A high-conviction call resting on "
          "neither filings nor factor data is resting on inference.")

    for f in ("entry_price", "target_price"):
        try:
            float(str(fm.get(f, "")).strip())
        except (TypeError, ValueError):
            F(f"{f} is not numeric")

    # A directional call with nothing said about why is the failure this whole
    # design exists to prevent.
    if d and d != "no view" and not fm.get("key_claim"):
        F(f"direction is {d!r} but there is no key_claim. A direction without a claim is a "
          f"guess wearing a number.")

    # An HTML entity renders as the same dash, so it is the same violation.
    n = text.count("—") + len(re.findall(r"&mdash;|&#8212;|&#x2014;", text, re.I))
    if n:
        F(f"{n} em dash{'es' if n > 1 else ''}. Repo brand voice forbids them.")

    # Every note gets the plain-writing checks, whatever its written_on date.
    # Not `k`: that name is reused by the conviction loop above.
    fmt = _fm_text(fm.get("format")).lower()
    if fmt in ("", "note"):
        _plain_body(text, fm, body, (fm.get("kind") or "").strip().strip('"'), F, W)
    elif fmt == "memo":
        _memo(path, text, fm, body, (fm.get("kind") or "").strip().strip('"'), F, W)
    else:
        F(f"format {fmt!r} is not 'memo'. Write format: memo for an investment memo, or leave the "
          f"field out for the older plain note.")

    return fails, warns


def _fm_text(v):
    return str(v or "").strip().strip('"').strip("'")


def _words(s):
    return len(re.findall(r"[A-Za-z0-9$][\w$%.,'’/&-]*", s))


def _head_name(s):
    return re.sub(r"[*_`]", "", s).replace("’", "'").strip().rstrip(":").strip().upper()


def _sentences(text):
    """Sentences of running prose. A list item or table row is its own unit, so a
    bullet with no full stop is not glued to the next one. Common abbreviations
    lose their full stop first, so "U.S. Steel" is not two sentences."""
    out = []
    for block in re.split(r"\n\s*\n", text):
        for unit in re.split(r"\n(?=\s*(?:[-*+]\s|\d+\.\s|\|))", block):
            unit = unit.strip()
            if not unit or unit.startswith(("|", ">", "#")):
                continue
            unit = " ".join(re.sub(r"^(?:[-*+]|\d+\.)\s+", "", unit).split())
            unit = _ABBREV.sub(lambda m: m.group(0).replace(".", ""), unit)
            out.extend(s for s in re.split(r"(?<=[.!?])(?:\*\*|[\"”’)])?\s+(?=[\"“(*]*[A-Z0-9$])", unit) if s)
    return out


def _numbers_in(text):
    """Numeric values a reader would want sourced, each as ((value, unit), shown).
    Dates, years, form codes, product and rule names, plain durations ("126
    trading days") and the fixed phrases "each $1" and "every $100" are left out."""
    # A wrapped line is one sentence. Without this, "39\npercent" reads as a bare
    # count of 39 with no unit, which no row for "39 percent" can satisfy, and the
    # writer is told a sourced number is unsourced.
    t = re.sub(r"[ \t]*\n[ \t]*", " ", text)
    t = re.sub(r"\b\d{4}-\d{2}-\d{2}\b", " ", t)
    t = re.sub(rf"\b{_MONTHS}\s+\d{{1,2}}(?:,\s*\d{{4}})?\b|\b\d{{1,2}}\s+{_MONTHS}(?:\s+\d{{4}})?", " ", t)
    t = re.sub(r"\b(?:Q[1-4]|\d+-[KQ]|EX-\d+(?:\.\d+)?|Form \d+|S&P \d+|Russell \d+|iPhone \d+|Phase \d"
               r"|Chapter \d+|Section \d+)\b", " ", t)
    t = re.sub(r"\b[A-Za-z]+-\d+\b", " ", t)  # GLP-1, COVID-19, F-150
    t = re.sub(r"\b(?:each|every|per|for each|for every) \$1(?:00)?\b(?![.,]\d)"
               r"(?!\s*(?:million|billion|trillion|[MBKT]\b))", " ", t)
    t = re.sub(r"\b\d+ times a (?:year|quarter|month|week|day)\b", " ", t)
    vals = []
    for m in re.finditer(r"(?<![\w.])(\$?)(\d[\d,]*(?:\.\d+)?)"
                         r"(%| ?percentage points?\b| ?percent\b| ?per cent\b| ?cents?\b| times\b"
                         r"|[BMKT]\b| (?:billion|million|trillion)\b)?(?![\w-]|\.\d)"
                         r"(?!\s+(?:(?:trading |business |calendar )?days?|weeks?|months?|quarters?|years?)\b)", t):
        raw = m.group(2).rstrip(",")
        try:
            v = float(raw.replace(",", ""))
        except ValueError:
            continue
        u = (m.group(3) or "").strip().lower()
        if not m.group(1) and not u and "." not in raw and "," not in raw and 1900 <= v <= 2099:
            continue  # a year
        # The unit is part of the match, so "38%" is not satisfied by a row for
        # "38 analysts". Scale words are not: "$4.77T" and "$4.77 trillion" agree.
        unit = ("$" if m.group(1) else "%" if u in ("%", "percent", "per cent")
                else "pts" if u.startswith("percentage") else "¢" if u.startswith("cent")
                else "x" if u == "times" else "")
        vals.append(((round(v, 4), unit), m.group(0).rstrip(",")))
    return vals


def _style(text, where, F, W, body=False, memo=None):
    """Plain-writing checks shared by the body and the front-matter the owner reads.

    `memo` is None for the older plain note, whose checks are unchanged. For a
    memo it is {"defined": JARGON labels its GLOSSARY defines, "headline": the
    page-one headline}: a defined term is allowed, and the headline and short
    bold labels may stand as bold lines."""
    home = f"the {NUMBERS_SECTION} table" if memo is None else MEMO_SOURCES
    ticks = re.findall(r"`[^`\n]+`", text)
    if ticks:
        F(f"{where}: {len(ticks)} code-formatted name(s), e.g. {ticks[0]}. Data field names "
          f"belong only in {home}.")
    bare = re.findall(r"(?<![\w/.-])[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b", re.sub(r"`[^`\n]+`", " ", text))
    if bare:
        F(f"{where}: data field name(s) written into the prose: {', '.join(sorted(set(bare))[:5])}. "
          f"Say what the number means and put the field in {NUMBERS_SECTION if memo is None else MEMO_SOURCES}.")
    hits = [(label, m.group(0)) for label, rx in PIPELINE_WORDS for m in [rx.search(text)] if m]
    if hits:
        F(f"{where}: talks about the research tools instead of the company: "
          + "; ".join(f"{label} ({t!r})" for label, t in hits))
    m = PERSONIFY.search(text)
    if m:
        F(f"{where}: writes a price or market as if it thinks: {m.group(0)!r}. Say what would "
          f"have to be true for the price to make sense, or name the people involved.")
    m = SHORTHAND.search(text)
    if m:
        F(f"{where}: number in analyst shorthand: {m.group(0)!r}. Write '36 times its profit "
          f"per share', '13 percentage points'.")
    m = FILLER_HARD.search(text)
    if m:
        F(f"{where}: filler emphasis: {m.group(0)!r}. Delete it and let the fact stand.")
    # Every distinct hit, not the first: a note in the wrong tone has a dozen of
    # these, and naming one per run would take a dozen runs to clear.
    found = lambda rx: sorted({m.group(0).lower() for m in rx.finditer(text)})[:8]
    if found(FIGURE_HARD):
        F(f"{where}: figure(s) of speech: {', '.join(map(repr, found(FIGURE_HARD)))}. Say what "
          f"literally happens instead of giving the reader a picture to translate.")
    if found(MYSTIQUE):
        F(f"{where}: hints at something hidden instead of stating it: "
          f"{', '.join(map(repr, found(MYSTIQUE)))}. State the fact and where it comes from.")
    if found(FIGURE_SOFT):
        W(f"{where}: possible figure(s) of speech: {', '.join(map(repr, found(FIGURE_SOFT)))}. "
          f"Keep one only where it is literally what the company makes or does.")
    if found(DRAMA):
        W(f"{where}: adverb(s) telling the reader how to feel: "
          f"{', '.join(map(repr, found(DRAMA)))}. Delete them and let the fact stand.")

    for rx, what in ((GENERALIZE, "a general claim about investors or stocks"),
                     (ANTITHESIS, "a 'not X, it is Y' construction"), (FILLER_SOFT, "filler")):
        m = rx.search(text)
        if m:
            W(f"{where}: possible {what}: {m.group(0)[:80]!r}")
    if memo is not None:
        _memo_jargon(text, where, F, W, body, memo["defined"])
    else:
        hard = [f"{label} (say: {plain})" for label, rx, plain, h in JARGON if h and rx.search(text)]
        soft = [f"{label} (say: {plain})" for label, rx, plain, h in JARGON if not h and rx.search(text)]
        if body:
            soft = hard + soft
        elif hard:
            F(f"{where}: finance terms the owner will not know, with no room here to explain them: "
              + "; ".join(hard))
        if soft:
            W(f"{where}: terms to check are explained in plain words where they first appear: "
              + "; ".join(soft))

    if body:
        _length(_sentences(text), where, F, W, warn_at=30, fail_at=40)
        for block in re.split(r"\n\s*\n", text):
            b = block.strip()
            if not b or re.match(r"(?:[-+]|\*(?!\*)|\d+\.)\s", b) or b.startswith(("|", ">", "#")):
                continue
            joined = " ".join(b.split())
            if memo is not None and (joined == memo.get("headline")
                                     or (re.fullmatch(r"\*\*[^*]+\*\*", joined) and _words(joined) <= 8)):
                # The page-one headline, and a short bold label standing over a
                # list or table ("**Key data.**"), are a memo's layout, not emphasis.
                continue
            m = re.search(r"(?:\*\*|__)([^*_]+)(?:\*\*|__)[.!?]?$", joined)
            if m and len(m.group(1).split()) >= 3:
                F(f"{where}: paragraph ends on a bolded line: {m.group(1)[:60]!r}. State it plainly "
                  f"without bold.")


def _memo_jargon(text, where, F, W, body, defined):
    """A memo uses the profession's vocabulary, each term defined once where it
    first appears and listed in the GLOSSARY. So a term is judged by whether the
    GLOSSARY defines it. The fields the website shows alone (key_claim, falsifier,
    caveats, conditions, add_if) get no exemption: their terms must be in the
    GLOSSARY too, because the page can link a reader there and nowhere else."""
    hard = [label for label, rx, plain, h in JARGON if h and label not in defined and rx.search(text)]
    soft = [label for label, rx, plain, h in JARGON if not h and label not in defined and rx.search(text)]
    if hard:
        F(f"{where}: finance term(s) not defined in the note's {MEMO_GLOSSARY}: {'; '.join(hard)}. "
          f"Define each in one plain sentence where it first appears, using the wording in "
          f"theses/GLOSSARY.md, and add it to the {MEMO_GLOSSARY}; or say the plain thing instead.")
    if soft:
        W(f"{where}: term(s) to define where they first appear and list in the {MEMO_GLOSSARY}, or "
          f"replace with plain words: {'; '.join(soft)}")


def _length(sents, where, F, W, warn_at, fail_at=None):
    over_fail = [s for s in sents if fail_at and _words(s) > fail_at]
    over_warn = [s for s in sents if _words(s) > warn_at and s not in over_fail]
    if over_fail:
        F(f"{where}: {len(over_fail)} sentence(s) over {fail_at} words, e.g. "
          f"{' '.join(over_fail[0].split()[:10])!r}... Split them: one idea per sentence.")
    if over_warn:
        W(f"{where}: {len(over_warn)} sentence(s) over {warn_at} words, e.g. "
          f"{' '.join(over_warn[0].split()[:10])!r}... Split them: one idea per sentence.")


def _standalone(text, where, F, W, memo=None):
    """key_claim, falsifier and each caveat. The website shows these with no body
    around them, so hard jargon fails here."""
    _style(text, where, F, W, body=False, memo=memo)
    sents = _sentences(text)
    if where == "key_claim":
        if not 2 <= len(sents) <= 3:
            W(f"key_claim is {len(sents)} sentence(s). Use two or three: what the company does, "
              f"what I think and why, and what I am doing about it.")
        _length(sents, where, F, W, warn_at=30, fail_at=40)
    elif where == "falsifier":
        _length(sents, where, F, W, warn_at=40)
    elif where == "add_if" or where.startswith("conditions"):
        if len(sents) > 1:
            W(f"{where} is {len(sents)} sentences. Use one plain sentence.")
        _length(sents, where, F, W, warn_at=30, fail_at=40)


def _fm_lines(text, fm, F, W, extra=()):
    # The website and events.py read front-matter line by line. A YAML block
    # scalar or a wrapped line would be silently cut to its first line.
    m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    for line in (m.group(1).splitlines() if m else []):
        if line.strip() and not re.match(r"^\s*-\s+", line) and not re.match(r"^[A-Za-z_][\w]*:", line):
            F(f"front-matter line is not read by the site and would be dropped: {line.strip()[:60]!r}. "
              f"Keep each value on one line.")
    for f in ("key_claim", "falsifier", "add_if", "if_wrong_price", "next_check") + tuple(extra):
        if re.fullmatch(r"[>|][+-]?", _fm_text(fm.get(f))):
            F(f"{f} uses a YAML block ({_fm_text(fm.get(f))}). Write it on the same line as {f}:.")
    if (fm.get("direction") or "").strip().strip('"') == "no view" and not fm.get("key_claim"):
        W("no key_claim. The website shows nothing for this name. Write one for a 'no view' note too.")


def _plain_body(text, fm, body, kind, F, W):
    _fm_lines(text, fm, F, W)

    heads = [(hm.start(), hm.end(), _head_name(hm.group(1))) for hm in HEADING.finditer(body)]
    names = [h[2] for h in heads]
    two_part = _fm_text(fm.get("written_on")) >= TWO_PART_FROM
    wanted = (BUSINESS_SECTIONS if two_part else []) + SECTIONS

    # Required headings written with the wrong number of # signs, by name.
    wrong_level = {_head_name(hm.group(1)): len(hm.group(0)) - len(hm.group(0).lstrip("#"))
                   for hm in ANY_HEADING.finditer(body)}
    for s in wanted + [NUMBERS_SECTION]:
        if s not in names and s in wrong_level:
            F(f"the heading {s} starts with {wrong_level[s]} # signs. Start it with exactly two, "
              f"as in '## {s}'. The website only starts a new section at two # signs.")
        elif s not in names:
            F(f"body is missing the heading: {s} (it must be a heading line of its own)")
    present = [n for n in names if n in wanted]
    if present != [s for s in wanted if s in present]:
        W("sections are out of order. Use: " + " / ".join(wanted) + " / " + NUMBERS_SECTION)
    known = [h for h in heads if h[2] in wanted or h[2] == NUMBERS_SECTION]
    if NUMBERS_SECTION in names and known[-1][2] != NUMBERS_SECTION:
        F(f"{NUMBERS_SECTION} must be the last section of the body.")

    if two_part:
        # The business part, up to the first section of the view. It is what the
        # owner reads to learn how the company works, so a token paragraph under
        # each heading does not count as having written it.
        first_view = next((h[0] for h in heads if h[2] in SECTIONS), len(body))
        start = next((h[0] for h in heads if h[2] in BUSINESS_SECTIONS), first_view)
        business = _words(ANY_HEADING.sub("", body[start:first_view]))
        if business < BUSINESS_MIN_WORDS:
            F(f"the four business sections come to {business} words. Write at least "
              f"{BUSINESS_MIN_WORDS}: what the company sells and to whom, what decides its "
              f"profit, its last ten years, and what management does with the cash.")
    else:
        # A title line above the opening paragraph is not counted against it.
        opening = ANY_HEADING.sub("", body[:known[0][0]]) if known else body
        if _words(opening) < 20:
            F(f"the note opens with {_words(opening)} words before its first section. Open with "
              f"a plain paragraph of at least 20 words saying what the company does.")

    numbers, prose = "", body
    for i, (start, end, name) in enumerate(heads):
        if name == NUMBERS_SECTION:
            stop = heads[i + 1][0] if i + 1 < len(heads) else len(body)
            numbers, prose = body[end:stop], body[:start] + body[stop:]
            break
    prose = ANY_HEADING.sub("", prose)

    # A revision must quote the prior key_claim verbatim, and that quote may be in
    # the old style. Exempt one short blockquote, on a note that is not an initiation.
    quotes = list(re.finditer(r"(?:^>.*(?:\n|$))+", prose, re.M))
    if quotes and kind != "initiation":
        q = quotes[0]
        if _words(q.group(0)) <= 80:
            prose = prose[:q.start()] + prose[q.end():]
        else:
            W("the quoted block is over 80 words, so it is checked like the rest of the note. "
              "Quote only the prior key claim.")
        if len(quotes) > 1:
            W(f"{len(quotes)} quoted blocks. Only the first, the prior key claim, is exempt "
              f"from the plain-writing checks.")

    words = _words(prose)
    shortest, warn_at, fail_at, target = PROSE_TWO_PART if two_part else PROSE_ONE_PART
    if words < shortest:
        F(f"body is {words} words, not counting {NUMBERS_SECTION}. Too short to have shown any reasoning.")
    elif words > fail_at:
        F(f"body is {words} words, not counting {NUMBERS_SECTION}, over the {fail_at:,}-word limit. "
          f"The target is about {target:,}. Cut ideas the argument does not need rather than explaining them.")
    elif words > warn_at:
        W(f"body is {words} words, not counting {NUMBERS_SECTION}, over {warn_at:,}. The target is "
          f"about {target:,}. Cut ideas the argument does not need rather than explaining them.")

    _style(prose, "body", F, W, body=True)
    kc = _fm_text(fm.get("key_claim"))
    if kc:
        _standalone(kc, "key_claim", F, W)
    if fm.get("falsifier"):
        _standalone(_fm_text(fm["falsifier"]), "falsifier", F, W)
    cavs = [_fm_text(cv) for cv in fm.get("data_caveats")] if isinstance(fm.get("data_caveats"), list) else []
    for cv in cavs:
        _standalone(cv, "data_caveats", F, W)

    worth = ""
    for i, (start, end, name) in enumerate(heads):
        if name == SECTIONS[3]:
            worth = body[end:heads[i + 1][0] if i + 1 < len(heads) else len(body)]
            break
    extra = _new_fields(fm, worth, F, W)

    writing = "\n".join([prose, kc, _fm_text(fm.get("falsifier"))] + cavs + extra)
    _numbers_table(numbers, writing, words, F, W)


def _new_fields(fm, worth, F, W, memo=None):
    """conditions, add_if, if_wrong_price and next_check, the fields the popup reads.

    The popup shows the text fields with no body around them, so they get the
    key claim's word checks. Returns the text of those fields: their numbers need
    rows in the table like any other number the owner reads. `worth` is the body of
    WHAT THE SHARES COULD BE WORTH, where if_wrong_price must come from; in a memo
    it is page one, where the three cases are."""
    texts = []
    d = _fm_text(fm.get("direction"))

    if "conditions" not in fm:
        if d in ("long", "short", "avoid", "watch"):
            W(f"no conditions. The website shows what has to stay true for a {d!r} view: list two to four.")
    elif fm["conditions"] == "":
        F("conditions is blank. List two to four items under it, or leave the field out.")
    elif not isinstance(fm["conditions"], list):
        F(f"conditions must be a block list: 'conditions:' on its own line, then one '  - ' item per "
          f"line. Found {_fm_text(fm['conditions'])[:40]!r}.")
    else:
        items = fm["conditions"]
        if not 2 <= len(items) <= 4:
            W(f"conditions has {len(items)} item(s). List the two to four things that have to stay true.")
        for i, item in enumerate(items, 1):
            t = _condition(str(item), f"conditions item {i}", F, W, memo)
            if t:
                texts.append(t)

    if "add_if" in fm:
        a = fm["add_if"]
        if isinstance(a, list):
            F("add_if must be one sentence on the same line as 'add_if:', not a list.")
        elif not _fm_text(a):
            F("add_if is blank. Write one sentence, or leave the field out.")
        else:
            _standalone(_fm_text(a), "add_if", F, W, memo)
            texts.append(_fm_text(a))

    if "if_wrong_price" in fm:
        v = fm["if_wrong_price"]
        shown = v if isinstance(v, list) else _fm_text(v)
        try:
            iwp = None if isinstance(v, list) else float(shown)
        except ValueError:
            iwp = None
        if iwp is None or not math.isfinite(iwp) or iwp <= 0:
            F(f"if_wrong_price {shown!r} is not a price. Write a plain positive number such as 94.00, "
              f"or leave the field out.")
        else:
            _wrong_price(iwp, d, fm, worth, W, "page one" if memo is not None else SECTIONS[3])

    if "next_check" in fm:
        v = fm["next_check"]
        s = "" if isinstance(v, list) else _fm_text(v)
        try:
            nd = date.fromisoformat(s) if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s) else None
        except ValueError:
            nd = None
        if nd is None:
            F(f"next_check {(s or v)!r} is not a date written YYYY-MM-DD. Give the next quarterly "
              f"report date, or leave the field out.")
        else:
            written = _fm_text(fm.get("written_on"))
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", written) and s <= written:
                W(f"next_check {s} is not after written_on {written}. The stored earnings date is often "
                  f"the last report, not the next one. Use the next one, or leave the field out.")
    return texts


def _condition(item, where, F, W, memo=None):
    """One conditions item. Returns its sentence without the check, or '' if unusable."""
    s = item.strip()
    # The website strips double quotes from a list item and nothing else.
    if len(s) >= 2 and s[0] == s[-1] == '"':
        s = s[1:-1].strip()
    elif len(s) >= 2 and s[0] == s[-1] == "'":
        W(f"{where} is in single quotes, which the website shows as typed. Drop them.")
    if not s:
        F(f"{where} is empty.")
        return ""
    # A nested list, a mapping or a flow collection: the website would show it as
    # typed, and anything reading the front-matter as YAML would not get a string.
    # An item that is only a check is caught below, with a better message.
    if (s[0] in "[{" and not re.match(r"\[\s*check\b", s, re.I)) or re.match(r"(?:-\s|[A-Za-z_]\w*:\s)", s):
        F(f"{where} is not a plain sentence: {s[:60]!r}. Write the condition as a sentence, with any "
          f"check at the end as [check: FIELD OP NUMBER].")
        return ""
    text = s
    tag = re.search(r"\[\s*check\b", s, re.I)
    if tag:
        m = CONDITION_CHECK.search(s)
        if m:
            text = s[:m.start()].strip()
            _check_target(where, m.group(1), float(m.group(3).replace("−", "-")), text, F, W)
        else:
            F(f"{where}: {_bad_check(s[tag.start():])} The website would show the condition but "
              f"could not check it.")
            text = s[:tag.start()].strip()
        if not text:
            F(f"{where} is only a check. Put a plain sentence before it.")
            return ""
    _standalone(text, where, F, W, memo)
    return text


def _bad_check(frag):
    """Why a [check: ...] tag does not parse, in words."""
    show = frag.strip()[:50]
    m = re.match(r"\[\s*(check)\s*(:?)([^\]]*)(\]?)(.*)$", frag, re.I | re.S)
    word, colon, inner, close, after = m.groups()
    inner = inner.strip()
    if word != "check" or not colon or not close:
        return (f"{show!r} does not parse. Write it exactly as [check: FIELD OP NUMBER], lower case, "
                f"with the colon and the closing bracket.")
    if after.strip():
        return f"{show!r} must be the very end of the item. Move the rest of the sentence before it."
    p = re.match(r"([A-Za-z_][\w.]*)?\s*([=!<>]+)?\s*(.*)$", inner)
    field, op, num = p.group(1), p.group(2), p.group(3).strip()
    if not field:
        return f"{show!r} names no field. Write [check: FIELD OP NUMBER]."
    if op not in CHECK_OPS:
        return f"{show!r} uses {(op or 'no operator')!r}. OP must be one of {' '.join(CHECK_OPS)}."
    if "%" in num or "percent" in num.lower():
        return f"{show!r} gives a percentage. NUMBER is in stored units, so 5 percent is 0.05."
    return f"{show!r}: {num!r} is not a plain number. Write it like 0.05 or 1.5, with no $, commas or units."


def _check_target(where, field, num, text, F, W):
    """The field and number of a check that parsed."""
    if field in DEAD or field in CONTAMINATED:
        F(f"{where}: the check uses {field}, which must never be used.")
        return
    if field not in KNOWN_FIELDS:
        F(f"{where}: the check names {field!r}, which is not a stored field the website can check. "
          f"Fix the spelling, or leave the check off.")
        return
    if field not in DECIMAL_FIELDS:
        return
    if abs(num) > (1 if field in BOUNDED_DECIMAL else 10):
        W(f"{where}: {field} is stored as a decimal, so {num:g} means {num * 100:g} percent. "
          f"5 percent is written 0.05.")
    pcts = {abs(v) for (v, u), _ in _numbers_in(text) if u in ("%", "pts")}
    if len(pcts) == 1 and abs(abs(num) * 100 - next(iter(pcts))) > 0.51:
        W(f"{where}: the sentence says {next(iter(pcts)):g} percent, but the check compares {field} "
          f"with {num:g}, which is {num * 100:g} percent. 5 percent is written 0.05.")


def _wrong_price(iwp, d, fm, worth, W, worth_name=SECTIONS[3]):
    """if_wrong_price is the bad case of a long, or the good case of an avoid or short."""
    try:
        entry = float(_fm_text(fm.get("entry_price")))
    except ValueError:
        entry = None
    if d == "long" and entry and iwp >= entry:
        W(f"if_wrong_price {iwp:g} is not below entry_price {entry:g}. On a long note it is the "
          f"bad-case price.")
    elif d in ("short", "avoid") and entry and iwp <= entry:
        W(f"if_wrong_price {iwp:g} is not above entry_price {entry:g}. On a {d!r} note it is the "
          f"good-case price.")
    elif d in ("watch", "no view"):
        W(f"if_wrong_price is set on a {d!r} note. It is defined only for long, short and avoid notes.")
    dollars = [v for (v, u), _ in _numbers_in(worth) if u == "$"]
    if not any(abs(x - iwp) < 0.005 or abs(x - round(iwp)) < 0.005 for x in dollars):
        W(f"if_wrong_price {iwp:g} is not a dollar figure in {worth_name}. It must repeat a case "
          f"price from that section, not add a new one.")


def _numbers_table(numbers, writing, words, F, W):
    rows = []
    for line in numbers.splitlines():
        s = line.strip()
        if not s.startswith("|"):
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        if all(re.fullmatch(r":?-{3,}:?", c) for c in cells if c):
            continue
        rows.append(cells)
    if not rows:
        F(f"{NUMBERS_SECTION} has no table. Every number in the note needs a row saying where it came from.")
        return
    header, data = rows[0], rows[1:]
    if len(header) != 4:
        F(f"{NUMBERS_SECTION} table has {len(header)} columns. It needs four: "
          f"In the note | What it means | Source | Exact value")
    if not data:
        F(f"{NUMBERS_SECTION} table has a header and no rows.")
        return
    for r in data:
        if len(r) < 4 or not all(r[:4]):
            F(f"{NUMBERS_SECTION} row is incomplete: {' | '.join(r)[:80]!r}")
            continue
        if not SOURCE_KIND.match(r[2]):
            F(f"{NUMBERS_SECTION} row {r[0]!r}: source {r[2][:40]!r} must start with a `field_name`, "
              f"or with close, filing, history, headline, calc or my choice.")
        if re.match(r"`price`", r[2]):
            W(f"{NUMBERS_SECTION} row {r[0]!r} cites the stored `price`, which can be days old. "
              f"Use close and its date.")
        cells = r[2] + " " + r[3]
        ticked = re.findall(r"`([^`]+)`", cells)
        bare = re.findall(r"(?<![\w`])[a-z][a-z0-9]*(?:_[a-z0-9]+)+(?![\w`])", cells)
        for name in sorted(set(ticked) | set(bare)):
            if name in CONTAMINATED or name in DEAD:
                F(f"{NUMBERS_SECTION} cites {name}, which must never be used.")
            elif name in ticked and name not in KNOWN_FIELDS:
                W(f"{NUMBERS_SECTION} cites `{name}`, which is not a field the dossier prints. "
                  f"Check the spelling or that it exists.")

    listed = {v for v, _ in _numbers_in(" ".join(r[0] for r in data))}
    hard, soft, seen = [], [], set()
    for v, shown in _numbers_in(writing):
        if v in listed or shown in seen:
            continue
        seen.add(shown)
        (hard if v[1] in FAIL_UNITS else soft).append(shown)
    if hard:
        F(f"{len(hard)} number(s) in the writing have no row in {NUMBERS_SECTION}: "
          f"{', '.join(hard[:8])}. Add a row whose first column shows the number as the sentence does.")
    if soft:
        W(f"{len(soft)} count(s) in the writing are not in the first column of {NUMBERS_SECTION}: "
          f"{', '.join(soft[:8])}")
    if len(data) > max(12, words / 20):
        W(f"{len(data)} numbers in {words} words of writing: dense enough that this may be "
          f"reciting data rather than explaining it.")


# ------------------------------------------------------------------ memo checks

def _num_fm(fm, key, F, what):
    """A front-matter number, or None after reporting why it is not one."""
    v = fm.get(key)
    s = "" if isinstance(v, list) else _fm_text(v)
    try:
        x = float(s)
    except ValueError:
        F(f"{key} {s or v!r} is not a number. Write {what}.")
        return None
    if not math.isfinite(x):
        F(f"{key} {s!r} is not a finite number. Write {what}.")
        return None
    return x


def _scenarios(fm, F):
    """The three cases from front-matter, as {case: (value, probability)}.

    Written one per line, as a block list of flow mappings:
        scenarios:
          - {case: bull, value: 370.00, probability: 0.25}
    """
    items = fm.get("scenarios")
    if not isinstance(items, list):
        F("scenarios must be a block list of three items, one per line, each written "
          "{case: bull, value: 370.00, probability: 0.25}.")
        return None
    out = {}
    for i, item in enumerate(items, 1):
        s = str(item).strip()
        m = re.fullmatch(r"\{(.*)\}", s)
        pairs = dict((k.strip().lower(), v.strip().strip('"').strip("'"))
                     for k, _, v in (p.partition(":") for p in (m.group(1).split(",") if m else [])))
        case = pairs.get("case", "").lower()
        try:
            value, prob = float(pairs.get("value", "")), float(pairs.get("probability", ""))
        except ValueError:
            value = prob = None
        if not m or case not in SCENARIO_CASES or value is None:
            F(f"scenarios item {i} is {s[:60]!r}. Write {{case: bull|base|bear, value: PRICE, "
              f"probability: FRACTION}}, with plain numbers.")
            return None
        if case in out:
            F(f"scenarios lists the {case} case twice.")
            return None
        if value <= 0 or not 0 < prob <= 1:
            F(f"scenarios {case}: value must be a positive price and probability a fraction "
              f"between 0 and 1, such as 0.25. Found value {value:g}, probability {prob:g}.")
            return None
        out[case] = (value, prob)
    if set(out) != set(SCENARIO_CASES):
        F(f"scenarios must list exactly three cases, bull, base and bear. Found: "
          f"{', '.join(out) or 'none'}.")
        return None
    return out


def _table_rows(text):
    """Every table in `text`, as a list of tables, each a list of rows of cells.
    The separator row is dropped; the first row of each table is its header."""
    tables, cur = [], []
    for line in text.splitlines() + [""]:
        s = line.strip()
        if s.startswith("|"):
            cells = [c.strip() for c in s.strip("|").split("|")]
            if not all(re.fullmatch(r":?-{3,}:?", c) for c in cells if c):
                cur.append(cells)
        elif cur:
            tables.append(cur)
            cur = []
    return tables


def _plain_cell(c):
    return re.sub(r"[*_`]", "", c).strip()


def _col(header, *words):
    for i, h in enumerate(header):
        if any(w in h.lower() for w in words):
            return i
    return None


def _dollars(s):
    return [float(x.replace(",", "")) for x in re.findall(r"\$\s?(\d[\d,]*(?:\.\d+)?)", s)]


def _load_glossary(text):
    """{term.lower(): (term, definition)} from the first table in `text`."""
    out = {}
    for table in _table_rows(text):
        for row in table[1:]:
            if len(row) >= 2 and _plain_cell(row[0]):
                out[_plain_cell(row[0]).lower()] = (_plain_cell(row[0]), row[1].strip())
        break
    return out


_CANON = {}


def canonical_glossary():
    """theses/GLOSSARY.md, the wording every memo copies. Read once per process."""
    if "g" not in _CANON:
        try:
            _CANON["g"] = _load_glossary(GLOSSARY_FILE.read_text(encoding="utf-8"))
        except OSError:
            _CANON["g"] = {}
    return _CANON["g"]


def _defined_labels(terms):
    """The JARGON labels a glossary with these terms defines."""
    out = set()
    for label, rx, _plain, _hard in JARGON:
        alias = GLOSSARY_ALIASES.get(label)
        if any(rx.search(t) or (alias and alias.search(t)) for t in terms):
            out.add(label)
    return out


def _repo_rel(path):
    p = Path(path).resolve()
    try:
        return str(p.relative_to(THESES.parent))
    except ValueError:
        return str(path)


def covered_elsewhere(path, fm):
    """Ledger events for this ticker that come from a different note.

    The note's own event is left out, so a memo recorded as an initiation still
    validates afterwards. LEDGER is read at call time so tests can point it at
    a temporary copy."""
    t, tid = _fm_text(fm.get("ticker")), _fm_text(fm.get("thesis_id"))
    rel = _repo_rel(path)
    return [e for e in read_csv_rows(LEDGER / "events.csv")
            if e.get("ticker") == t and e.get("note_path") != rel and e.get("thesis_id") != tid]


def _memo(path, text, fm, body, kind, F, W):
    """The buy-side investment memo (format: memo)."""
    _fm_lines(text, fm, F, W, extra=("size_plan",))
    shape = "initiation" if kind == "initiation" else "revision"

    # ---- front-matter
    for k in MEMO_FM:
        if k not in fm:
            F(f"missing memo field: {k}")
        elif k not in MEMO_BLANK_OK and fm[k] in ("", None) and not isinstance(fm[k], list):
            F(f"memo field is blank: {k}")

    raw_action = _fm_text(fm.get("action"))
    action = next((a for a in MEMO_ACTIONS if a.lower() == raw_action.lower()), None)
    d = _fm_text(fm.get("direction"))
    if raw_action and action is None:
        F(f"action {raw_action!r} is not one of {', '.join(MEMO_ACTIONS)}.")
    elif action and d and d not in MEMO_ACTIONS[action]:
        want = " or ".join(repr(x) for x in sorted(MEMO_ACTIONS[action]))
        F(f"action {action} is scored as direction {want}, but direction is {d!r}. Initiate, Add, "
          f"Hold and Trim are long; Short is short; Exit is watch; Avoid is watch, or avoid only "
          f"when the memo expects the stock to do worse than its peers.")

    size_now = _num_fm(fm, "size_now", F, "the position size today as a fraction of the portfolio, "
                       "such as 0.037 for 3.7 percent, or 0") if "size_now" in fm else None
    if size_now is not None:
        if not 0 <= size_now <= 1:
            F(f"size_now {size_now:g} is not a fraction of the portfolio between 0 and 1. 3.7 percent "
              f"is written 0.037.")
        elif action and action not in MEMO_HOLDING and size_now != 0:
            F(f"size_now is {size_now:g} but the action is {action}. Only Initiate, Add, Hold and Trim "
              f"carry a size; for {action} it is 0.")
        elif action in ("Initiate", "Add") and size_now == 0:
            W(f"action is {action} with size_now 0. Give the size you recommend buying to.")

    er = _num_fm(fm, "expected_return", F, "a decimal fraction, such as 0.113 for 11.3 percent") \
        if "expected_return" in fm else None
    br = _num_fm(fm, "bear_return", F, "a decimal fraction, such as -0.454 for a 45.4 percent loss") \
        if "bear_return" in fm else None
    rr = _num_fm(fm, "required_return", F, "a decimal fraction, such as 0.12 for 12 percent") \
        if "required_return" in fm else None
    for key, x, lo, hi in (("expected_return", er, -1, 5), ("bear_return", br, -1, 5),
                           ("required_return", rr, 0, 1)):
        if x is not None and not lo <= x <= hi:
            F(f"{key} {x:g} is outside {lo} to {hi}. It is a decimal fraction: 12 percent is 0.12.")

    try:
        horizon = int(_fm_text(fm.get("horizon_days")))
    except ValueError:
        horizon = None
    if horizon is not None and horizon != MEMO_HORIZON:
        F(f"horizon_days is {horizon}. A memo's price target is for 12 months: set it to {MEMO_HORIZON}.")

    n = text.count(EN_DASH) + len(re.findall(r"&ndash;|&#8211;|&#x2013;", text, re.I))
    if n:
        F(f"{n} en dash{'es' if n > 1 else ''}. Write 'to' for a range and a comma or full stop elsewhere.")

    if kind == "initiation":
        prior = covered_elsewhere(path, fm)
        if prior:
            last = prior[-1]
            F(f"kind is initiation, but the ledger already has {len(prior)} event(s) for "
              f"{_fm_text(fm.get('ticker'))}, the latest {last.get('date', '?')} from "
              f"{last.get('note_path', '?')}. Write a revision: page one, WHAT CHANGED, section 10 "
              f"and the sections that changed.")

    scen = _scenarios(fm, F) if "scenarios" in fm else None

    # ---- the action follows from the numbers (PROMPTS.md step 5, ACT, and rule e)
    if action in ("Initiate", "Add") and er is not None and rr is not None and er <= rr:
        F(f"action is {action}, but expected_return {er:g} is not above required_return {rr:g}. "
          f"Buy only when the expected return pays for the risk; otherwise the action is Avoid.")
    if scen and d == "long" and scen["bear"][1] > scen["bull"][1] + PROB_TOL:
        F(f"direction is long, but the bear case ({scen['bear'][1]:g}) is likelier than the bull case "
          f"({scen['bull'][1]:g}). With the bear case likelier the action is not Initiate or Add.")
    if size_now and br is not None and size_now * abs(br) > BEAR_COST_CAP + 1e-9:
        W(f"size_now {size_now:g} x bear_return {br:g} costs {size_now * abs(br):.1%} of the portfolio "
          f"in the bear case, above the draft {BEAR_COST_CAP:.0%} limit. Say by how much and why in "
          f"'Why this size', or size down to {BEAR_COST_CAP / abs(br):.3f}.")

    # ---- structure
    heads = [(hm.start(), hm.end(), _head_name(hm.group(1))) for hm in HEADING.finditer(body)]
    names = [h[2] for h in heads]
    sections = {}
    for i, (start, end, name) in enumerate(heads):
        sections.setdefault(name, body[end:heads[i + 1][0] if i + 1 < len(heads) else len(body)])
    _memo_headings(names, shape, body, F)
    page1 = body[:heads[0][0]] if heads else body

    # ---- page one
    p1 = re.sub(r"\A\s*#[ \t]+[^\n]*\n", "", page1)
    paras = [" ".join(b.split()) for b in re.split(r"\n\s*\n", p1) if b.strip()]
    headline = paras[0] if paras and re.fullmatch(r"\*\*[^*]+\*\*", paras[0]) else None
    if not paras:
        F("page one is missing. Before the first ## heading, write the headline, the action and "
          "size, the expected return next to the bear loss, the thesis, why now, the three things "
          "that matter most and the key data table.")
    elif headline is None:
        F(f"page one must open with a bold one-line headline giving the action and size, such as "
          f"'**Recommendation: Avoid for now. Size today: 0% of the portfolio.**'. Found "
          f"{paras[0][:60]!r}.")
    elif action and action.lower() not in headline.lower():
        W(f"the page-one headline does not name the action, {action}.")
    for label, rx in (("Expected return and bear loss", r"\*\*expected return and bear loss\b"),
                      ("Why this size", r"\*\*why this size\b"),
                      ("Thesis", r"\*\*(?:the )?(?:investment )?thesis\b"), ("Why now", r"\*\*why now\b"),
                      ("The three things that matter most", r"\*\*the three things\b"),
                      ("Key data", r"\*\*key data\b")):
        if paras and not re.search(rx, p1, re.I):
            W(f"page one has no bold '{label}.' paragraph. Page one must stand alone: action and size, "
              f"expected return and bear loss, thesis, why now, the three things that matter most, "
              f"key data.")
    if scen:
        _memo_arithmetic(fm, scen, er, br, p1, F, W)

    # ---- section 10
    if MEMO_MONITOR in sections:
        _memo_monitor(sections[MEMO_MONITOR], F)
    unknown = "11. WHAT I DON'T KNOW"
    if unknown in sections and not re.search(r"no analyst forecasts", sections[unknown], re.I):
        W(f"{unknown} does not say that no analyst forecasts are available. PROMPTS.md asks for that "
          f"sentence in every memo.")

    # ---- SOURCES and GLOSSARY
    if MEMO_SOURCES in sections:
        src = sections[MEMO_SOURCES]
        if not src.strip():
            F(f"{MEMO_SOURCES} is empty. Say where every figure comes from: file and field, or filing.")
        elif not any(len(t[0]) >= 3 for t in _table_rows(src)):
            W(f"{MEMO_SOURCES} has no table of three columns or more (figure, value or file, source "
              f"or field).")
    gloss = {}
    if MEMO_GLOSSARY in sections:
        gloss = _load_glossary(sections[MEMO_GLOSSARY])
        if not gloss:
            F(f"{MEMO_GLOSSARY} has no table. List every term the memo defines: | Term | Definition |.")
        empty = [t for t, dfn in gloss.values() if not _plain_cell(dfn)]
        if empty:
            F(f"{MEMO_GLOSSARY}: no definition for {', '.join(empty[:5])}.")
        canon = canonical_glossary()
        new = [t for k, (t, _) in gloss.items() if k not in canon]
        changed = [t for k, (t, dfn) in gloss.items()
                   if k in canon and " ".join(dfn.split()).rstrip(".") != " ".join(canon[k][1].split()).rstrip(".")]
        if changed:
            W(f"{MEMO_GLOSSARY}: definition differs from theses/GLOSSARY.md for {', '.join(changed[:8])}. "
              f"Copy the canonical wording so a term means the same in every memo.")
        if new:
            W(f"{MEMO_GLOSSARY}: not in theses/GLOSSARY.md: {', '.join(new[:8])}. Use the canonical term "
              f"if there is one; otherwise list it in the run manifest as a proposed addition.")
    ctx = {"defined": _defined_labels([t for t, _ in gloss.values()]), "headline": headline}

    # ---- prose: everything but SOURCES and GLOSSARY
    prose = body
    for name in (MEMO_GLOSSARY, MEMO_SOURCES):
        for i, (start, end, hname) in enumerate(heads):
            if hname == name:
                stop = heads[i + 1][0] if i + 1 < len(heads) else len(body)
                prose = prose.replace(body[start:stop], "\n")
                break
    prose = ANY_HEADING.sub("", prose)
    quotes = list(re.finditer(r"(?:^>.*(?:\n|$))+", prose, re.M))
    if quotes and kind != "initiation":
        q = quotes[0]
        if _words(q.group(0)) <= 80:
            prose = prose[:q.start()] + prose[q.end():]
        else:
            W("the quoted block is over 80 words, so it is checked like the rest of the note. "
              "Quote only the prior key claim.")

    words = _words("\n".join(l for l in prose.splitlines() if not l.strip().startswith("|")))
    lo, hi = MEMO_LENGTH[shape]
    if not lo <= words <= hi:
        F(f"the {shape} is {words:,} words of prose, not counting tables, {MEMO_SOURCES} or "
          f"{MEMO_GLOSSARY}. A memo {shape} runs {lo:,} to {hi:,}.")

    _style(prose, "body", F, W, body=True, memo=ctx)
    kc = _fm_text(fm.get("key_claim"))
    if kc:
        _standalone(kc, "key_claim", F, W, ctx)
    if fm.get("falsifier"):
        _standalone(_fm_text(fm["falsifier"]), "falsifier", F, W, ctx)
    for cv in (fm.get("data_caveats") if isinstance(fm.get("data_caveats"), list) else []):
        _standalone(_fm_text(cv), "data_caveats", F, W, ctx)
    _new_fields(fm, page1, F, W, ctx)


def _memo_headings(names, shape, body, F):
    numbered = [s for s in MEMO_SECTIONS if s[0].isdigit()]
    allowed = set(MEMO_SECTIONS) | ({MEMO_CHANGED} if shape == "revision" else set())
    wrong_level = {_head_name(hm.group(1)): len(hm.group(0)) - len(hm.group(0).lstrip("#"))
                   for hm in ANY_HEADING.finditer(body)}
    if shape == "initiation":
        required = MEMO_SECTIONS
    else:
        required = [MEMO_CHANGED, MEMO_MONITOR, MEMO_SOURCES, MEMO_GLOSSARY]
    for s in required:
        if s not in names and wrong_level.get(s):
            F(f"the heading {s} starts with {wrong_level[s]} # signs. Start it with exactly two, "
              f"as in '## {s}'.")
        elif s not in names:
            F(f"body is missing the heading: ## {s}")
    stray = [n for n in names if n not in allowed]
    if stray:
        F(f"heading(s) not in the memo format: {', '.join(stray[:4])}. Page one has no heading; the "
          f"sections are {' / '.join(MEMO_SECTIONS)}"
          + (f", with {MEMO_CHANGED} first in a revision." if shape == "revision" else "."))
    dup = sorted({n for n in names if names.count(n) > 1})
    if dup:
        F(f"heading(s) used twice: {', '.join(dup)}")
    known = [n for n in names if n in allowed]
    if shape == "initiation":
        if [n for n in known if n in MEMO_SECTIONS] != [s for s in MEMO_SECTIONS if s in known]:
            F("sections are out of order. Use: " + " / ".join(MEMO_SECTIONS))
        return
    # A revision: WHAT CHANGED first; SOURCES and GLOSSARY last; the numbered
    # sections between them in number order, except that section 10 may come
    # straight after WHAT CHANGED.
    if known and known[0] != MEMO_CHANGED and MEMO_CHANGED in known:
        F(f"{MEMO_CHANGED} must be the first section of a revision.")
    if known[-2:] != [MEMO_SOURCES, MEMO_GLOSSARY] and MEMO_SOURCES in known and MEMO_GLOSSARY in known:
        F(f"a revision ends with {MEMO_SOURCES} and then {MEMO_GLOSSARY}.")
    middle = [numbered.index(n) for n in known if n in numbered and n != MEMO_SOURCES]
    rest = middle[1:] if middle and numbered[middle[0]] == MEMO_MONITOR else middle
    if rest != sorted(rest):
        F("a revision's numbered sections are out of order. After WHAT CHANGED and section 10, put "
          "the other sections that changed in number order.")


def _memo_arithmetic(fm, scen, er, br, p1, F, W):
    """Page one's numbers must agree with each other and with the front-matter."""
    total = sum(p for _, p in scen.values())
    if abs(total - 1) > PROB_TOL:
        F(f"scenario probabilities sum to {total:.3f}, not 1.")
    weighted = sum(v * p for v, p in scen.values())
    bull, base, bear = (scen[c][0] for c in SCENARIO_CASES)
    if not bear <= base <= bull:
        W(f"scenario values are not in order: bear {bear:g}, base {base:g}, bull {bull:g}.")

    # The page-one table: a row per case and a probability-weighted row.
    rows, stated = {}, None
    for table in _table_rows(p1):
        header = [h.lower() for h in table[0]]
        vi, pi = _col(header, "value"), _col(header, "probab")
        for row in table[1:]:
            first = _plain_cell(row[0]).lower()
            rest = row[1:]
            vcell = row[vi] if vi is not None and vi < len(row) and vi > 0 else " ".join(rest)
            pcell = row[pi] if pi is not None and pi < len(row) and pi > 0 else " ".join(rest)
            dollars = _dollars(vcell) or _dollars(" ".join(rest))
            if re.match(r"(?:probability[- ])?weighted", first) and dollars:
                stated = dollars[0]
            else:
                case = next((c for c in SCENARIO_CASES if first.startswith(c)), None)
                pct = re.search(r"(\d+(?:\.\d+)?)\s?%", pcell)
                if case and dollars:
                    rows[case] = (dollars[0], float(pct.group(1)) / 100 if pct else None)
    if stated is None:
        F("page one does not show the probability-weighted value. Give a table with a row for each "
          "case (value and probability) and a 'Probability-weighted' row.")
    elif abs(stated - weighted) > VALUE_TOL:
        F(f"page one gives a probability-weighted value of ${stated:,.2f}, but the three cases give "
          f"${weighted:,.2f} (the sum of probability times value).")
    for c in SCENARIO_CASES:
        if c not in rows:
            if stated is not None:
                F(f"page one's scenario table has no {c.title()} row with a dollar value.")
            continue
        v, p = rows[c]
        if abs(v - scen[c][0]) > VALUE_TOL:
            F(f"page one gives the {c} case ${v:,.2f}; scenarios in the front-matter say "
              f"${scen[c][0]:,.2f}.")
        if p is not None and abs(p - scen[c][1]) > PROB_TOL:
            F(f"page one gives the {c} case a probability of {p:.0%}; scenarios in the front-matter "
              f"say {scen[c][1]:g}.")

    try:
        entry = float(_fm_text(fm.get("entry_price")))
    except ValueError:
        entry = None
    if entry and entry > 0:
        divs = [0.0] + [float(x) for x in re.findall(
            r"\$(\d+(?:\.\d+)?)(?: a share)? (?:of|in) dividends?\b", p1, re.I)]
        if er is not None:
            implied = [(weighted + dv) / entry - 1 for dv in divs]
            if all(abs(er - x) > RETURN_TOL for x in implied):
                show = " or ".join(f"{x:.3f}" for x in implied)
                F(f"expected_return {er:g} does not follow from the weighted value ${weighted:,.2f}, "
                  f"entry_price {entry:g} and any dividend stated on page one ({show}).")
        if br is not None and abs(br - (bear / entry - 1)) > RETURN_TOL:
            F(f"bear_return {br:g} does not follow from the bear value ${bear:,.2f} and entry_price "
              f"{entry:g} ({bear / entry - 1:.3f}).")
    try:
        target = float(_fm_text(fm.get("target_price")))
    except ValueError:
        target = None
    if target is not None and abs(target - weighted) > TARGET_TOL:
        F(f"target_price {target:g} is more than ${TARGET_TOL:g} from the probability-weighted value "
          f"${weighted:,.2f}. The target is that value, rounded.")

    # The expected return and the bear loss sit side by side on page one.
    pcts = {round(abs(v), 1) for (v, u), _ in _numbers_in(p1) if u in ("%",)}
    for key, x in (("expected return", er), ("bear loss", br)):
        if x is not None and not any(abs(abs(x) * 100 - p) <= 0.051 for p in pcts):
            W(f"page one does not show the {key} as a percentage ({abs(x) * 100:.1f}%).")


def _memo_monitor(sec, F):
    """Section 10 needs a rule that ends the position: an Exit or Cut row with a
    number to watch and the date it will be known."""
    for table in _table_rows(sec):
        header = [h.lower() for h in table[0]]
        ai, ti = _col(header, "action"), _col(header, "threshold", "trigger")
        if ai is None or ti is None:
            continue
        for row in table[1:]:
            if max(ai, ti) >= len(row):
                continue
            if (re.search(r"\b(?:exit|cut)\b", row[ai], re.I) and re.search(r"\d", row[ti])
                    and any(_DATE_WORDS.search(c) for c in row)):
                return
    F(f"{MEMO_MONITOR} needs a table with Threshold and Action columns, and at least one row whose "
      f"action is Exit or Cut, with a numeric threshold and a date (YYYY-MM-DD, or a month and year).")


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    # Every argument, not just the first. A shell glob like notes/*/*.md expands
    # to many paths, and reading only argv[1] checked one note while printing
    # "1/1 passed", which reads as a clean run over the whole set.
    targets = [Path(a) for a in sys.argv[1:]]
    if len(targets) > 1:
        files = []
        for t in targets:
            files.extend(sorted(t.rglob("*.md")) if t.is_dir() else [t])
        return _report(files)
    target = targets[0]
    if target.is_dir():
        # A run directory holds dossiers as well as notes. A dossier has no
        # front-matter and is not meant to: it is the input. Scanning a
        # directory checks what claims to be a note; naming a file explicitly
        # checks that file, front-matter or not.
        files = [f for f in sorted(target.rglob("*.md"))
                 if f.read_text(encoding="utf-8").lstrip().startswith("---")]
        if not files:
            print(f"validate: no notes with front-matter under {target} "
                  f"({len(list(target.glob('*.md')))} markdown files there are dossiers or "
                  f"other input).")
            return 0
    else:
        files = [target]
    if not files:
        print(f"validate: nothing to check in {target}")
        return 0

    return _report(files)


def _report(files):
    bad = 0
    for f in files:
        fails, warns = check(f)
        status = "FAIL" if fails else ("warn" if warns else "pass")
        print(f"\n[{status}] {f.name}")
        for m in fails:
            print(f"   FAIL  {m}")
        for m in warns:
            print(f"   warn  {m}")
        bad += bool(fails)
    print(f"\n{len(files) - bad}/{len(files)} passed")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
