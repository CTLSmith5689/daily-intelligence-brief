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
conviction: 2
evidence_base: 1
falsifier_specific: 1
variant_perception: 0
disconfirmation: 0
horizon_days: 252
target_price: 152.00
review_by: 2027-01-12
key_claim: CF Industries makes nitrogen fertiliser, and its shares sit near a one-year high. At about 10 times last year's profit per share, the price only makes sense if those profits do not last. I would own it for a rise to $152 in about 8 months, because I think CF's almost-zero debt and spare cash leave its shares less room to fall.
falsifier: The view is wrong if, in either of CF's next two quarterly reports, CF would need more than 1.5 years of earnings to pay off its debt after using its cash. It is also wrong if, in those reports, CF's spare cash falls below $5 a year for every $100 of stock and its number of shares does not fall.
conditions:
  - CF would need no more than 1.5 years of earnings to pay off its debt after using its cash. [check: net_debt_ebitda <= 1.5]
  - CF's spare cash stays at $5 or more a year for every $100 of stock, or its number of shares falls.
add_if: I would be more confident if CF's next quarterly reports show its number of shares falling.
if_wrong_price: 47.00
next_check: 2026-11-04
data_caveats:
  - No analyst forecasts are available, so I cannot tell whether CF's results beat or missed what analysts expected.
  - CF is compared with a group of only five similar companies, which is too few for those comparisons to be reliable.
  - I have no figures on nitrogen fertiliser or natural gas prices, and gas is CF's largest cost.
  - I have no text from CF's reports, so I have nothing from management and no split of sales between its businesses.
  - I have no record of CF's executives or directors buying or selling its shares.
  - The stored share price was 2.2 percent out of date, so I use the latest closing price instead.
---

CF Industries makes nitrogen fertiliser for farming, and natural gas is its largest cost. CF earns the
gap between what its fertiliser sells for and what the gas costs.

## WHAT HAS TO BE TRUE FOR THE PRICE TO MAKE SENSE

CF's shares closed at $135.11, within a few percent of their highest price of the past year. That is
only about 10 times CF's profit per share over the past year.

Over the past year, CF's sales grew 20% and its profit per share rose 76%. Those figures point to the
gap between fertiliser and gas prices widening sharply.

At about 10 times last year's profit per share, the price only makes sense if those profits do not
last.

## WHERE I DISAGREE

I think CF's low debt means a fall in profit would hurt its shares less than their low price would
lead you to expect.

CF would need less than a third of a year's earnings to pay off its debt after using its cash. It also
makes about $9 of spare cash a year for every $100 of stock. So CF could get through a slump without
trouble paying its debts, and still have cash to buy back its own shares.

I have no evidence for why the price has not already risen to reflect CF's low debt. So I cannot
rule out that buyers have already weighed the debt and still expect profits to fall. That is part of
why my confidence score is low.

## WHAT WOULD SETTLE IT

CF makes roughly $1.9 billion of spare cash a year. If it spent even half of that buying back shares,
its number of shares would fall by about 4.5% a year, and faster when the share price is low.

CF's next two quarterly reports will show whether its number of shares is falling. I give the view
about 8 months, because I think a falling number of shares takes longer than two quarters to lift the
share price.

## WHAT THE SHARES COULD BE WORTH

The middle and good cases use CF's profit per share over the past year, $13.46.

- Bad case, about $47, or 7 times $6.73. The gap between fertiliser and gas prices narrows, and
  profit per share halves to $6.73.
- Middle case, $152, or 11.3 times $13.46. I picked this price by judgement: profit eases, and buying
  back shares does the rest.
- Good case, $215, or 16 times $13.46. The gap between fertiliser and gas prices stays wide long
  enough that CF keeps making this much spare cash for years.

The range is wide because I have no figures on fertiliser or gas prices. I will be scored on $152,
about 13% above the latest close, within about 8 months. The target is modest on purpose, because I
make no forecast of fertiliser prices.

## WHAT WOULD PROVE ME WRONG

Either of two results in CF's next two quarterly reports would prove me wrong. One is CF needing more
than 1.5 years of earnings to pay off its debt after using its cash. That would mean low debt is not
protecting CF the way I claim. The other is spare cash below $5 a year for every $100 of stock, with no
fall in the number of shares. That would mean the cash is not reaching shareholders, which removes the
only way this view pays off. Neither can be checked yet.

The strongest argument against me is that CF looks cheap only because its profits are at a high
point, and the shares could fall as those profits drop. My answer, that low debt lets CF get through
that fall, shows only that CF survives. Surviving does not make the shares a good buy at this price,
and I have no answer to that, so my confidence score is lower. I would still own the shares, because
the target is modest and rests on CF buying back shares.

## WHAT I DON'T KNOW

- I have no figures on nitrogen fertiliser or natural gas prices, and the gap between them drives the
  whole business.
- CF is compared with only five similar companies, so any reading of CF as cheap or well run next to
  them is rough.
- No analyst forecasts are available, so I cannot tell whether results beat or missed what analysts
  expected. That includes the 20% sales growth.
- I have nothing from CF's managers about what they plan to do with the cash. This view depends on CF
  buying back shares, and I infer that only from its spare cash.
- I have no record of CF's executives or directors buying or selling its shares.
- I don't know why the price has not already risen to reflect CF's low debt.

## WHERE THE NUMBERS COME FROM

| In the note | What it means | Source | Exact value |
|---|---|---|---|
| $135.11 | latest closing share price | close 2026-09-10 | 135.11 |
| within a few percent of their highest price | how far the latest close is below the highest close of the past year | calc: close $135.11 / highest close of the past year $139.27 - 1 | -3.0% |
| about 10 times | share price divided by profit per share over the past year | calc: close $135.11 / `ttm_eps_diluted` $13.46 | 10.04 |
| 20% | sales growth over the past year | `revenue_growth_yoy` | +20.0% |
| 76% | growth in profit per share over the past year | `eps_growth_yoy` | +76.4% |
| less than a third of a year's earnings | debt after using cash, in years of earnings | `net_debt_ebitda` | 0.29 |
| about $9 | spare cash a year for every $100 of stock | `fcf_yield` | +9.1% |
| $1.9 billion | spare cash a year | calc: `fcf_yield` 9.1% x `market_cap` $20.9B | $1.90B |
| half | share of spare cash spent buying back shares | my choice | 0.5 |
| 4.5% | yearly fall in the number of shares if half the spare cash buys back shares | calc: 0.5 x $1.90B / `market_cap` $20.9B | 4.55% |
| $13.46 | profit per share over the past year | `ttm_eps_diluted` | 13.46 |
| 7 times | bad-case price divided by last year's profit per share | my choice | 7 |
| about $47 | bad-case price, with profit per share halved | calc: $6.73 x 7 | 47.11 |
| $6.73 | half of last year's profit per share, the bad case | calc: `ttm_eps_diluted` $13.46 x 0.5 | 6.73 |
| halves | bad-case profit compared with last year's | my choice | 0.5 |
| $152 | middle-case price and my target | my choice | 152.00 |
| 11.3 times | middle-case price divided by last year's profit per share | calc: $152 / `ttm_eps_diluted` $13.46 | 11.29 |
| 16 times | good-case price divided by profit per share | my choice | 16 |
| $215 | good-case price | calc: `ttm_eps_diluted` $13.46 x 16 | 215.36 |
| about 13% | how far the target is above the latest close | calc: $152 / close $135.11 - 1 | +12.5% |
| more than 1.5 years | debt after using cash, in years of earnings, that would prove me wrong | my choice | 1.5 |
| $5 | spare cash a year for every $100 of stock that, with no fall in the number of shares, would prove me wrong | my choice | 5% |
| only five similar companies | size of the group CF is compared with | calc: group size printed with CF's figures, n=5 | 5 |
| 2.2 percent | how far the latest close was below the stored share price | calc: close $135.11 / stored `price` $138.11 - 1 | -2.17% |
