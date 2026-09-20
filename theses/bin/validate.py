#!/usr/bin/env python3
"""Gate a written thesis before it is allowed into the archive.

These are not style preferences. Each check corresponds to a specific way that
AI-written research goes wrong, and each is mechanical so it cannot be argued
with at 3am by a model that would rather ship something.

    python3 theses/bin/validate.py theses/notes/YELP/2026-09-14-initiation.md
    python3 theses/bin/validate.py theses/runs/2026-09-14/          # whole run

Exit code 0 = all pass. 1 = at least one FAIL. Warnings never fail the run.
"""
import math, re, sys
from datetime import date
from pathlib import Path

from common import SLEEVES, DEAD, CONTAMINATED

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
    _plain_body(text, fm, body, (fm.get("kind") or "").strip().strip('"'), F, W)

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


def _style(text, where, F, W, body=False):
    """Plain-writing checks shared by the body and the front-matter the owner reads."""
    ticks = re.findall(r"`[^`\n]+`", text)
    if ticks:
        F(f"{where}: {len(ticks)} code-formatted name(s), e.g. {ticks[0]}. Data field names "
          f"belong only in the {NUMBERS_SECTION} table.")
    bare = re.findall(r"(?<![\w/.-])[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b", re.sub(r"`[^`\n]+`", " ", text))
    if bare:
        F(f"{where}: data field name(s) written into the prose: {', '.join(sorted(set(bare))[:5])}. "
          f"Say what the number means and put the field in {NUMBERS_SECTION}.")
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
            m = re.search(r"(?:\*\*|__)([^*_]+)(?:\*\*|__)[.!?]?$", " ".join(b.split()))
            if m and len(m.group(1).split()) >= 3:
                F(f"{where}: paragraph ends on a bolded line: {m.group(1)[:60]!r}. State it plainly "
                  f"without bold.")


def _length(sents, where, F, W, warn_at, fail_at=None):
    over_fail = [s for s in sents if fail_at and _words(s) > fail_at]
    over_warn = [s for s in sents if _words(s) > warn_at and s not in over_fail]
    if over_fail:
        F(f"{where}: {len(over_fail)} sentence(s) over {fail_at} words, e.g. "
          f"{' '.join(over_fail[0].split()[:10])!r}... Split them: one idea per sentence.")
    if over_warn:
        W(f"{where}: {len(over_warn)} sentence(s) over {warn_at} words, e.g. "
          f"{' '.join(over_warn[0].split()[:10])!r}... Split them: one idea per sentence.")


def _standalone(text, where, F, W):
    """key_claim, falsifier and each caveat. The website shows these with no body
    around them, so hard jargon fails here."""
    _style(text, where, F, W, body=False)
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


def _plain_body(text, fm, body, kind, F, W):
    # The website and events.py read front-matter line by line. A YAML block
    # scalar or a wrapped line would be silently cut to its first line.
    m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    for line in (m.group(1).splitlines() if m else []):
        if line.strip() and not re.match(r"^\s*-\s+", line) and not re.match(r"^[A-Za-z_][\w]*:", line):
            F(f"front-matter line is not read by the site and would be dropped: {line.strip()[:60]!r}. "
              f"Keep each value on one line.")
    for f in ("key_claim", "falsifier", "add_if", "if_wrong_price", "next_check"):
        if re.fullmatch(r"[>|][+-]?", _fm_text(fm.get(f))):
            F(f"{f} uses a YAML block ({_fm_text(fm.get(f))}). Write it on the same line as {f}:.")
    if (fm.get("direction") or "").strip().strip('"') == "no view" and not fm.get("key_claim"):
        W("no key_claim. The website shows nothing for this name. Write one for a 'no view' note too.")

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


def _new_fields(fm, worth, F, W):
    """conditions, add_if, if_wrong_price and next_check, the fields the popup reads.

    The popup shows the text fields with no body around them, so they get the
    key claim's word checks. Returns the text of those fields: their numbers need
    rows in the table like any other number the owner reads. `worth` is the body of
    WHAT THE SHARES COULD BE WORTH, where if_wrong_price must come from."""
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
            t = _condition(str(item), f"conditions item {i}", F, W)
            if t:
                texts.append(t)

    if "add_if" in fm:
        a = fm["add_if"]
        if isinstance(a, list):
            F("add_if must be one sentence on the same line as 'add_if:', not a list.")
        elif not _fm_text(a):
            F("add_if is blank. Write one sentence, or leave the field out.")
        else:
            _standalone(_fm_text(a), "add_if", F, W)
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
            _wrong_price(iwp, d, fm, worth, W)

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


def _condition(item, where, F, W):
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
    _standalone(text, where, F, W)
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


def _wrong_price(iwp, d, fm, worth, W):
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
        W(f"if_wrong_price {iwp:g} is not a dollar figure in {SECTIONS[3]}. It must repeat a case "
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
