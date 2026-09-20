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
key_claim: Apple makes the iPhone and other hardware, and it sells services. Its share price only makes sense if its sales growth, which is slowing, speeds back up, and Apple's reports do not show that yet. I am only watching Apple: the new foldable iPhone is the one possible reason I can see, and I have no figures to judge it.
falsifier: In either of Apple's next two quarterly reports, sales growth compared with the same quarter a year earlier is faster than in the quarter before. Or, in a report covering a full quarter of foldable iPhone sales, sales over the past 12 months are more than 18 percent above the 12 months before.
conditions:
  - Apple's sales growth, compared with the same quarter a year earlier, is no faster than in the quarter before. [check: revenue_acceleration <= 0]
  - In a report covering a full quarter of foldable iPhone sales, sales over the past 12 months are no more than 18 percent above the 12 months before.
data_caveats:
  - No analyst forecasts are available, so I cannot tell whether Apple's 14 percent sales growth beat or missed what analysts expected, and that is most of what moves the shares.
  - I have no figures on how many devices Apple sells or the average price it gets, and no split of its sales between products and services.
  - I have no text from Apple's own reports, so I have nothing from its managers and no forecast from the company.
  - What I know about the new foldable iPhone comes from news headlines that were checked to be about Apple but not checked for accuracy.
  - The stored share price was 3.6 percent out of date, so I use the latest closing price instead.
  - One growth measure, the change in what Apple keeps from each $1 of sales, has no comparison with similar companies, so I compare four measures instead of five.
---

Apple makes the iPhone and other technology hardware, and it also sells services. News reports say
Apple, under a new chief executive, has unveiled its first foldable iPhone, priced well above its
other iPhones.

## WHAT HAS TO BE TRUE FOR THE PRICE TO MAKE SENSE

Apple's shares closed at $326.57, about 37 times its profit per share over the past 12 months. The
shares rose about 38% over the past year and sit near the top of their range for the year.

At that price, Apple makes about $3 of spare cash a year for every $100 of stock. Buying at that price
pays off only if Apple's cash grows, and keeps growing for years.

Apple's sales are still growing, up 14% over the past 12 months. But the growth is slowing fast. In
the latest quarter, growth compared with a year earlier was more than 11 percentage points lower than
in the quarter before. Among 11 similar hardware companies, only one did worse on this measure.

If the slowdown continues, the years of growth a buyer is paying for will not arrive. So the price
only makes sense if sales growth speeds back up, and Apple's reports do not show that yet.

## WHERE I DISAGREE

I have no firm view that the price is wrong. The foldable iPhone could bring a real wave of sales,
and it is the one thing I can see that would justify the price. I cannot judge whether it will.

To judge it, I would need to know how many Apple sells, at what price, and whether buyers simply
switch from cheaper iPhones. I have none of those figures.

The 38 analysts who follow Apple study exactly this, and I know nothing they don't. So there may be a
good reason for the price, and I cannot test it.

## WHAT WOULD SETTLE IT

The first quarterly report that covers a full quarter of foldable iPhone sales will settle it. It
will show whether Apple's sales growth has turned back up. Apple stays on my list because the
question is clear and a scheduled report will answer it.

## WHAT THE SHARES COULD BE WORTH

I am not giving a bad, middle and good case. What the shares are worth depends almost entirely on
whether growth speeds back up, which I cannot judge.

My target price is $300, about 8% below the latest close. At $300, with profit unchanged, the shares
would cost about 34 times profit per share. I chose it because today's price seems a
little high to me. I am not forecasting that the shares will fall to it.

I keep a record of how my target prices turn out. Because I am only watching Apple, this one will not
go on that record.

## WHAT WOULD PROVE ME WRONG

I doubt the price because Apple's reports do not yet show sales growth speeding back up. Two results
would prove that doubt wrong.

The first: in either of Apple's next two quarterly reports, sales growth compared with a year
earlier is faster than in the quarter before. The second: a report covering a full quarter of
foldable iPhone sales shows sales over the past 12 months more than 18% above the 12 months before.
That figure is 14% now.

Either would mean buyers at this price were right, and the slowdown was a dip that ended. Neither can
be checked yet.

The strongest argument against my doubt is Apple's own history. Its sales growth has slowed many
times before a new model arrived, and paying a high price through those dips has been right for two
decades. I have no answer to that, except that it describes the past. Neither side can see how many
foldables Apple will sell, or what it will make on each.

## WHAT I DON'T KNOW

- No analyst forecasts are available, so I cannot tell whether results beat or missed what analysts
  expected. For Apple, that is most of what matters in the short run.
- I have no figures on how many devices Apple sells or at what average price.
- I cannot split Apple's sales between products and services. Services keep a different share of each
  sale as profit, and the shift between them is most of Apple's long-run story.
- I have no text from Apple's own reports, so no company forecast and nothing from its managers.
- What I know about the foldable comes from headlines checked to be about Apple but not checked for
  accuracy. I have not read an Apple report on it.
- One growth measure, the change in what Apple keeps from each $1 of sales after making its products,
  has no comparison with similar companies. So I compare Apple on four growth measures instead of five.

## WHERE THE NUMBERS COME FROM

Each row shows where a number in the note came from.

| In the note | What it means | Source | Exact value |
|---|---|---|---|
| $326.57 | latest closing share price | close 2026-09-10 | 326.57 |
| about 37 times | share price divided by profit per share over the past 12 months, at the latest close | calc: close $326.57 / `ttm_eps_diluted` $8.71 | 37.49 |
| about 38% | share price change over the past year | `return_52w` | +37.6% |
| near the top of their range for the year | where the latest close sits between the lowest and highest closes of the past year | calc: (close $326.57 - low $233.21) / (high $339.79 - low $233.21) | 88% |
| about $3 | spare cash a year for every $100 of stock | `fcf_yield` | +3.0% |
| 14%; 14 percent | sales over the past 12 months against the 12 months before | `revenue_growth_yoy` | +14.2% |
| more than 11 percentage points | latest quarter's sales growth against a year earlier, minus the same figure for the quarter before | `revenue_acceleration` | -11.5 percentage points |
| Among 11 similar hardware companies, only one did worse on this measure | Apple's rank on that measure among 11 hardware companies, where a higher rank means growth held up better | `revenue_acceleration`, rank against similar companies | 9th percentile; 1 of 11 lower |
| 38 | analysts who follow Apple | `analyst_count` | 38 |
| $300 | my target price | my choice | 300.00 |
| about 8% | how far the target is below the latest close | calc: $300 / close $326.57 - 1 | -8.1% |
| about 34 times | target price divided by profit per share over the past 12 months, with profit unchanged | calc: $300 / `ttm_eps_diluted` $8.71 | 34.44 |
| 18%; 18 percent | growth in sales over the past 12 months that would prove me wrong | my choice | 18% |
| 3.6 percent | how far the stored share price was from the latest close | calc: close $326.57 / stored `price` $315.34 - 1 | +3.6% |
| four measures instead of five | growth measures compared with similar companies; one has no comparison | `gross_margin_trend`, no rank against similar companies | 4 of 5 |
