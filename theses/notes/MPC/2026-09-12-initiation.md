---
thesis_id: MPC-2026-09-12
ticker: MPC
kind: initiation
written_on: 2026-09-12
panel_date: 2026-09-11
entry_price: 392.42
entry_source: close_series 2026-09-10
slot: screen
direction: no view
conviction: 2
evidence_base: 1
falsifier_specific: 1
variant_perception: 0
disconfirmation: 0
horizon_days: 252
target_price: 392.00
review_by: 2026-12-12
falsifier: The Q3 8-K EX-99.1 shows Midstream contributing more than 40 percent of segment operating income, or throughput below 2.6 million barrels per day for two consecutive quarters.
data_caveats:
  - no consensus estimates exist anywhere in this pipeline, so nothing here is calibrated against what the market expects
  - no crack spread series; the central variable of any refining thesis is absent from this dataset
  - no filing text collected for MPC, so no segment split between Refining, Midstream and Retail
  - no tension fired on this name, so there is no question in the data to start from
  - insider data not collected for this name
  - the panel price was 1.8 percent stale; the close series is used instead
---

## WHAT IS PRICED IN

At $392.42 MPC trades on `pe` 13.79, a recomputed `ev_ebitda` of 6.17 and an `fcf_yield` of
**11.5%**, at the 78th percentile of its nine refining peers on that last measure. It sits at
`high52w_proximity` **+0.0%**, exactly on its 52-week high, after `return_52w` of **+122.8%** with a
`sharpe_1y` of 2.43.

The price is asserting that current refining margins persist for long enough to matter, while the
multiple is asserting they will not persist forever. Refining is the most reliably mean-reverting
margin pool in energy, so that tension is the normal state of a refiner at the top of a cycle rather
than a mispricing.

## WHERE I DIFFER

I don't, and I am not going to manufacture a view to fill the section.

The Growth sleeve reads +1.07 and it is measuring the crack spread, not anything management did.
Separating operator skill from cycle position is the entire thesis on any refiner, and there is no
crack spread series in this dataset. **The dossier also reports that no tension fired on this name**,
which is the screen saying honestly that its own factors agree with each other and offer no question
to start from.

It is worth saying why this reaches "no view" when CF, screened the same week on a similar setup,
did not. There I argued a `net_debt_ebitda` of 0.29x makes the cycle matter less than the multiple
implies, and named a mechanism that pays without calling the commodity. **Here I have no equivalent.**
MPC's earnings mix spans Refining, Midstream through MPLX, and Retail, and a midstream-weighted mix
deserves a materially higher multiple than a refining-weighted one. Without the segment split I
cannot tell which company I am looking at, and that is not a detail. It is the thesis.

## WHAT CLOSES THE GAP

Nothing needs to close. The question becomes answerable rather than arguable at the next release.
The observable is the segment table in the 8-K EX-99.1: operating income by segment, and refinery
throughput. Filing collection began **2026-09-12** and runs forward only, so **Q3 is the first
quarter at which this is answerable at all.**

## VALUATION

Declining to produce one. A bear, base and bull spanning a mean-reverting commodity spread I cannot
observe would be three numbers dressed as analysis. `target_price` is logged at **$392**, essentially
spot, which is the honest expression of no view: I am claiming no edge over the current price and the
ledger should grade me against exactly that.

Note this note will **not** appear in `predictions.csv`. A direction of "no view" is research rather
than a prediction, and putting it in the ledger would dilute the hit rate with a call nobody made.

## WHAT PROVES ME WRONG

Not wrong, exactly, since I am not claiming anything. But the condition that would make this
answerable: the Q3 EX-99.1 showing Midstream above **40%** of segment operating income, or
throughput below **2.6 million barrels per day** for two consecutive quarters. The first would mean
this is more of a fee-based business than the multiple assumes; the second that the refining half is
contracting rather than merely cyclical. Either turns this from a name I cannot assess into one I
can.

## WHAT I DON'T KNOW

- **No crack spread series.** The central variable of any refining thesis is not in this dataset and
  cannot be inferred from what is.
- **No segment split.** Refining versus Midstream versus Retail is the difference between two
  different companies trading under one ticker, and it is the crux.
- No consensus estimates, so I cannot tell whether the current quarter was a beat or a miss.
- No filing text collected yet, so no management commentary on capital allocation or on the MPLX
  relationship.
- No insider data for this name.
- `analyst_count` is 18, so this is a well-covered large cap. Whatever edge exists here is unlikely
  to be sitting in a factor panel.
