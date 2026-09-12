---
thesis_id: AAPL-2026-09-12
ticker: AAPL
kind: initiation
written_on: 2026-09-12
panel_date: 2026-09-11
entry_price: 326.57
entry_source: close_series 2026-09-10
slot: watchlist
direction: watch
conviction: 2
evidence_base: 1
falsifier_specific: 1
variant_perception: 0
disconfirmation: 0
horizon_days: 252
target_price: 300.00
review_by: 2026-12-12
key_claim: The price embeds a revenue reacceleration the filings do not yet show, and the foldable cycle is the only candidate explanation, which this dataset cannot assess.
falsifier: Revenue acceleration turns positive in either of the next two quarters, or revenue growth exceeds 18 percent in a quarter that includes a full foldable launch.
data_caveats:
  - no consensus estimates exist anywhere in this pipeline, so I cannot tell whether 14.2 percent revenue growth is a beat or a miss, which is most of what moves this stock
  - no unit volumes, no average selling prices, no segment split between Products and Services
  - no filing text collected for AAPL; there is no management commentary and no guidance
  - the product cycle claim rests on filtered headlines, which were checked for relevance but not verified
  - the panel price was 3.6 percent stale; the close series is used instead
  - gross_margin_trend has no peer percentile, so the Growth sleeve rests on four fields rather than five
---

## WHAT IS PRICED IN

At $326.57 Apple trades on `pe` 36.20, a recomputed `ev_ebitda` of 27.53 and an `fcf_yield` of
**3.0%**, sitting at 88% of its 52-week range after `return_52w` of +37.6%.

A 3.0% free cash flow yield is the clearest statement in the dossier. It prices cash flows that
**grow**, and grow for a long time. Against that, `revenue_acceleration` is **&minus;11.5%**, the 9th
percentile of its peer group: revenue is still growing at +14.2% but the rate is falling.

So the market is paying a growth multiple for a business whose reported growth rate is decelerating.
That is not incoherent. It means the price embeds a reacceleration that the filings do not yet show.

## WHERE I DIFFER

I don't, and the reason is specific rather than a shrug.

The screen surfaced this name on exactly that tension and asked the right question: anticipation, or
has momentum detached? **Name the inflection if you think there is one.** The filtered headlines name
a candidate: Apple has unveiled its first foldable, at a reported price point well above the current
iPhone range, with a new chief executive.

That is a genuine product cycle and it is a plausible reason for the market to look through a
decelerating trailing number. **I cannot assess it.** Judging a hardware cycle needs unit volumes,
average selling prices, and a view on whether a higher price point expands revenue or cannibalises
the existing range. None of those are in this dataset and none of them are inferable from what is.

So the honest position is that the tension has a candidate explanation, the explanation is
unfalsifiable with the data available, and I have no edge over a market of 38 covering analysts who
have models for exactly this. `variant_perception` scores **0**.

## WHAT CLOSES THE GAP

The first quarter that contains a full foldable launch. `revenue_acceleration` is the observable and
this pipeline already records it, so the claim becomes checkable without a new data source.

Direction is **watch** rather than a position. That is not the same as no view: it says this name
stays on the list, the question is well formed, and the answer arrives on a known date.

## VALUATION

Not building a range. At 36x trailing with a 3.0% FCF yield, the valuation is almost entirely a
function of the reacceleration I have just said I cannot assess, so a bear, base and bull would be
three restatements of that one unknown.

`target_price` is logged at **$300**, roughly 8% below spot, which is where the multiple lands near
33x on unchanged earnings. It expresses mild valuation discomfort rather than a forecast. Because
the direction is "watch", this note produces **no row in `predictions.csv`** and will not be graded.

## WHAT PROVES ME WRONG

`revenue_acceleration` turning positive in either of the next two quarters, or `revenue_growth_yoy`
exceeding **18%** in a quarter that includes a full foldable launch.

Either would mean the market was anticipating correctly and the deceleration was a trough rather
than a trend, and the multiple was the right one to pay. Checkable from the panel itself, quarter by
quarter. **Not yet checkable.**

`disconfirmation` scores 0. The strongest case against me is that Apple's trailing revenue growth
has decelerated before, repeatedly, ahead of a cycle, and that paying a premium through those
troughs has been correct for two decades. I have no answer to that beyond noting that it is a
statement about the past, and neither of us can see the unit economics of this particular cycle.

## WHAT I DON'T KNOW

- **No consensus estimates.** For a name with 38 analysts, the entire short-run question is whether
  a print beats or misses, and this pipeline cannot see the bar.
- **No unit volumes or average selling prices**, so the foldable's revenue arithmetic is unavailable.
- **No Products versus Services split.** Services carries a different margin and deserves a
  different multiple, and the mix shift is most of the long-run story.
- No filing text, so no guidance and no management framing of the cycle.
- The product cycle claim rests on headlines the dossier filtered for relevance but did not verify.
  **I have not read a filing on this.**
- `gross_margin_trend` has no peer percentile, so the Growth sleeve rests on four of five fields.
