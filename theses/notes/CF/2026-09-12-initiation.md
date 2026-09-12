---
thesis_id: CF-2026-09-12
ticker: CF
kind: initiation
written_on: 2026-09-12
panel_date: 2026-09-11
entry_price: 135.11
entry_source: close_series 2026-09-10
slot: screen
direction: long
conviction: 3
evidence_base: 1
falsifier_specific: 1
variant_perception: 1
disconfirmation: 0
horizon_days: 252
target_price: 152.00
review_by: 2027-01-12
key_claim: A commodity producer with almost no debt and a 9 percent free cash flow yield is less exposed to where the cycle goes than the multiple implies, and cyclicals are priced by people who do not look at the balance sheet.
falsifier: Net debt to EBITDA rises above 1.5x in either of the next two quarters, or free cash flow yield falls below 5 percent while the share count fails to decline.
data_caveats:
  - no consensus estimates exist anywhere in this pipeline, so nothing here is calibrated against what the market expects
  - the peer group has only 5 members, so field-level percentiles are unavailable and the sleeve z-scores rest on a cohort too small to be stable
  - no nitrogen or natural gas price series exists in this dataset, and gas is the single largest input cost
  - no filing text collected for CF; there is no management commentary and no segment split
  - insider data not collected for this name
  - the panel price was 2.2 percent stale; the close series is used instead
---

## WHAT IS PRICED IN

At $135.11 CF trades on `pe` 10.26, a recomputed `ev_ebitda` of 6.10 and an `fcf_yield` of
**9.1%**, while sitting at `high52w_proximity` &minus;0.8%, effectively on its 52-week high after
`return_52w` of +63.7%.

That combination is the whole question. A stock does not usually reach its high on ten times
earnings unless the market believes those earnings are temporary. The price is not saying CF is
cheap. It is saying **these are peak-cycle earnings and the multiple should be low against them.**

Nitrogen fertiliser is a spread business between ammonia prices and natural gas. The panel shows
the spread widening hard: `revenue_growth_yoy` +20.0%, `eps_growth_yoy` +76.4%,
`revenue_acceleration` +28.9% and `gross_margin_trend` **+13.4pp**. Those are cycle numbers, not
company numbers, in exactly the way MPC's are.

## WHERE I DIFFER

The market is pricing where the cycle goes. I think the balance sheet means that matters less here
than the multiple implies.

`net_debt_ebitda` is **0.29x** and `roe_ttm` is 46.7%. A commodity producer carrying almost no debt
at a 9.1% free cash flow yield does not need the cycle to cooperate in order to survive it, and it
converts a downcycle into share count rather than distress. `earnings_consistency` of 71.4% is high
for a cyclical, which says the spread has not been as violent as the category's reputation.

**Why the error persists:** cyclicals are screened out by quality investors on principle, and the
people who do trade them are calling the commodity, not reading the balance sheet. Nobody whose
process would reward a 0.29x leverage ratio is looking at this name. That is a structural reason for
the gap to stay open rather than a claim that the market has simply not noticed.

`beta_1y` is **&minus;0.96**, so this has been moving against the market. Whatever is driving it is
not the same thing driving the index.

## WHAT CLOSES THE GAP

Capital returned. At a 9.1% FCF yield against a $20.9B market cap, free cash flow is roughly $1.9B a
year. Retiring even half of that shrinks `shares_outstanding` from 151,338,130 by around 4.5%
annually, and does so faster when the price is low. The observable is the share count on the next
two 10-Q cover pages. **That is a number this pipeline already records**, so the claim is checkable
without a new data source.

Horizon is 252 days rather than 126, because a capital-allocation thesis needs more than two
quarters to show up.

## VALUATION

On `ttm_eps_diluted` of $13.46:

- **Bear $94.** 7x. The spread reverts, earnings halve toward mid-cycle, and the multiple does not
  expand to compensate.
- **Base $152.** 11.3x. Earnings ease from here and the multiple holds, with buybacks doing the
  rest.
- **Bull $215.** 16x. The spread holds long enough that the market re-rates this as a cash
  generator rather than a commodity.

The range is enormous because the input that would narrow it, a nitrogen or natural gas price
series, is not in this dataset. I am logging **$152** as the graded number. Note it implies only
+12.5% from spot over a year, which is deliberately modest: I am not forecasting the cycle, I am
arguing the downside is smaller than the multiple suggests.

## WHAT PROVES ME WRONG

`net_debt_ebitda` rising above **1.5x** in either of the next two quarters, or `fcf_yield` falling
below **5%** while `shares_outstanding` fails to decline.

The first would mean the balance sheet is not the shock absorber I am claiming. The second would
mean the cash flow is going somewhere other than shareholders, which removes the only mechanism by
which the thesis pays. Checkable from the 10-Q cover page and cash flow statement. **Not yet
checkable.**

I have scored `disconfirmation` **0** and should be explicit about why. The strongest case against
me is that peak-cycle earnings on a ten multiple at the 52-week high is the single most reliable
value trap in commodities, and my answer, that the balance sheet absorbs it, only establishes that
the company survives. Surviving is not the same as being a good investment at this price. **I have
not answered that objection, only acknowledged it**, and the conviction reflects that rather than
papering over it.

## WHAT I DON'T KNOW

- **No nitrogen or natural gas price series.** Gas is the largest input cost and the spread is the
  entire business. The central variable is absent.
- **The peer group has five members**, so every field-level percentile in the dossier is blank and
  the sleeve z-scores rest on a cohort too small to be stable. The Value and Quality readings should
  be treated as directional, not precise.
- No consensus estimates, so I cannot tell whether +20% revenue growth is a beat or a miss.
- No filing text, so no management commentary on capital allocation intent. **The buyback is the
  mechanism of this thesis and I am inferring it from free cash flow rather than from anything
  management has said.**
- No insider data at this market cap tier.
