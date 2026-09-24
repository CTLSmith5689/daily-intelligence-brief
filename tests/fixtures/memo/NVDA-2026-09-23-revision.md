---
thesis_id: NVDA-2026-09-23
ticker: NVDA
kind: revision
format: memo
written_on: 2026-09-23
panel_date: 2026-09-22
entry_price: 228.87
entry_source: close_series 2026-09-22
slot: watchlist
action: Avoid
expected_return: 0.113
bear_return: -0.454
required_return: 0.120
entry_price_below: 227.46
scenarios:
  - {case: bull, value: 370.00, probability: 0.25}
  - {case: base, value: 260.00, probability: 0.50}
  - {case: bear, value: 125.00, probability: 0.25}
direction: watch
conviction: 4
evidence_base: 2
falsifier_specific: 1
variant_perception: 0
disconfirmation: 1
horizon_days: 365
target_price: 255.00
review_by: 2026-11-20
key_claim: NVIDIA designs the chips, networking and software that train and run artificial intelligence programs. At $228.87 I would own none, because the return I expect over 12 months is slightly below what the risk requires. I would buy if its November report passes two tests.
falsifier: I would recommend buying if NVIDIA forecasts $125 billion or more of sales for the quarter to January 2027 in its report due around 17 November 2026, with customer balances owed at or below 55 days of sales.
data_caveats:
  - No analyst forecasts are available, so I cannot say whether my figures sit above or below what other analysts expect.
  - The stored sales figure for the past 12 months is correct, although an automatic check flagged it as 40 percent away from the last fiscal year.
  - The filed history has no annual spending on equipment, so free cash flow cannot be shown for past years.
conditions:
  - NVIDIA's quarterly sales keep rising from one quarter to the next through the report due in February 2027.
  - Cash from operations stays above 70 percent of NVIDIA's adjusted profit.
add_if: I would buy if NVIDIA's forecast for the quarter to January 2027 is $125 billion or more and customer balances owed fall to 55 days of sales or fewer.
next_check: 2026-11-17
---

# NVIDIA Corporation (NVDA): investment memo, revision

**Avoid for now: $228.87 already pays for my base case.**

From the analyst to the portfolio manager (PM), the person who decides what the portfolio owns and in what size. Written from prices and filings as of the close on 2026-09-22.

**Recommendation.** I recommend Avoid, which means owning none at $228.87. The price becomes attractive below $227.46, where the return I expect equals the 12.0% its risk requires. I would also buy if the 17 November report passes two tests.

**Return and risk.** I expect a return of 11.3% over 12 months, including $1.00 of dividends, against the 12.0% its risk requires. The bear case, the bad outcome I think plausible, loses 45.4%.

**Thesis.** NVIDIA's next year of earnings is well supported, but the price needs about as much growth as I forecast, so I would wait for two tests before buying.

**Why now.** The October-quarter report on 17 November will show both tests. The annual report in late February will show whether cash has caught up with profit.

## WHAT CHANGED

This memo replaces the note of 22 September. That note said:

> NVIDIA designs and sells the computers that train and run artificial intelligence programs. It reported profit of $118 billion for the six months to July 2026, and its operations produced $74 billion of cash. I am watching rather than owning it, until two more reports show whether that gap was timing.

I am REPLACING that key claim. The earlier note asked the same question about cash, but it gave no action, no entry price and no expected return. This memo gives all three, and its price target runs 12 months instead of about 8.

The earlier note was wrong about one figure and inconsistent about another. It said the stored sales figure was wrong, but the last four quarters add up to exactly that figure. It also mixed profit under US accounting rules with the company's adjusted profit, and I now use the adjusted figures throughout. The target moves from $240 to $255, because the model now runs to the year to January 2029.

## 2. MY VIEW

### The next year is well supported, and the price already needs it.

My forecast and the price agree on the next 12 months. The price requires Data Center growth of 30.2% in the year to January 2028, and I forecast 32%. Variant perception, where my forecast differs from what the price requires with a checkable reason the price has not yet moved, is therefore absent. The expected return sits 0.7 percentage points below the required return, a gap inside the error of my own inputs.

### The January-quarter forecast is the one number that could change that.

The company's forecast for the January quarter, due on 17 November, could separate my view from the price. At $125 billion it would exceed the rise in my bull case, the good outcome I think plausible. The price cannot yet reflect it, because it does not exist until the report.

## 3. WHAT IT IS WORTH

| Case | Probability | Value in 12 months | Return from $228.87 | The one driver |
|---|---|---|---|---|
| Bull | 25% | $370 | +61.7% | Revenue grows 43% in the year to January 2028, at 25 times earnings |
| Base | 50% | $260 | +13.6% | Revenue grows 30% in the year to January 2028, at today's 21 times |
| Bear | 25% | $125 | -45.4% | A repeat of the year to January 2023, a year later, at 18 times |
| Probability-weighted | | $253.75 | +10.9%; +11.3% with $1.00 of dividends | |
| Required return | | | +12.0% | |

P/E, price divided by earnings per share, suits NVIDIA because its earnings are large and positive. The probability-weighted value, each case's value multiplied by its probability with the results added, is $253.75. My price target is that value rounded to the nearest $5.

## 4. WHAT WOULD PROVE ME WRONG

| What I check | Latest | Threshold | Action | Next reading |
|---|---|---|---|---|
| Guidance for the next quarter | $108.0B for October | $125B or more, with DSO at or below 55 days | Initiate | 2026-11-17 |
| Guidance for the next quarter | $108.0B for October | Below $108.0B, a fall from October | Stay out. If owned, Exit | 2026-11-17 |
| Revenue against guidance | $96.2B for July | Below $105.8B, the bottom of the range | Stay out. If owned, Exit | 2026-11-17 |
| DSO | 60 days | Above 70 days at the FY2027 year end | Stay out. If owned, Exit | Late February 2027 |
| Share price | $228.87 | $370, the bull value | If owned, Trim | Daily |

My view is wrong if the company grows faster than I assume while its customers pay on time. Guidance is management's own published forecast. Days sales outstanding (DSO) is receivables divided by a quarter's revenue, times 91 days. A falsifier, a dated observation that would show a view is wrong, is set by the first row.

A fall in the price alone is never a reason to sell. It starts a review of the case values.

## SOURCES

| Figure | Value | Source |
|---|---|---|
| Last close, date | $228.87, 2026-09-22 | Published `prices/NVDA.json`, last of `closes` |
| Entry price | $227.46 | calc: (253.75 + 1.00) / 1.12, the price at which the expected return equals the required return |
| Expected return | $253.75; +10.9%, +11.3% with dividends | calc: 0.25 x 125 + 0.50 x 260 + 0.25 x 370; (253.75 + 1.00) / 228.87 - 1 |
| Market capitalisation | $5,516B | `market_cap` |
| Guidance, October 2026 quarter | $108.0B | Earnings release, `data/filings/text/NVDA/0001045810-26-000073.txt`, Outlook |
| DSO | 51 and 60 days | calc: receivables / quarter revenue x 91, from the release balance sheet |
| What the price needs | Data Center growth 30.2% | calc: base model solved for NTM EPS of $12.16 at 21 times |

## GLOSSARY

| Term | Definition |
|---|---|
| Base case | The middle outcome the analyst thinks plausible, built from stated assumptions. |
| Bear case | The bad outcome the analyst thinks plausible, built from stated assumptions. |
| Bull case | The good outcome the analyst thinks plausible, built from stated assumptions. |
| Days sales outstanding (DSO) | Receivables divided by a quarter's revenue, times 91 days. |
| Earnings per share (EPS) | The profit attributable to each share. |
| Expected return | The percentage gain from the last close to the probability-weighted value, plus any dividends expected over the same 12 months. |
| Falsifier | A specific, dated observation that would show a view is wrong. |
| Guidance | Management's own published forecast. |
| P/E | Price divided by earnings per share. |
| Percentage point | The unit of difference between two percentages. |
| Portfolio manager (PM) | The person who decides what the portfolio owns and in what size. |
| Price target | The analyst's estimate of what the shares will be worth in 12 months. |
| Probability-weighted value | Each case's value multiplied by its probability, with the results added. |
| Required return | The annual return a stock must offer to pay for its risk. |
| Variant perception | Where the analyst's forecast differs from what the price requires, with a checkable reason the price has not yet moved. |
