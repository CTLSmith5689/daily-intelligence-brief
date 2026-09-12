---
thesis_id: HIMS-2026-09-12
ticker: HIMS
kind: initiation
written_on: 2026-09-12
panel_date: 2026-09-11
entry_price: 27.44
entry_source: close_series 2026-09-10
slot: contrarian
direction: avoid
conviction: 3
evidence_base: 1
falsifier_specific: 1
variant_perception: 0
disconfirmation: 1
horizon_days: 126
target_price: 22.00
review_by: 2026-12-12
key_claim: The growth line is the least reliable number in this dossier, because the revenue that is accelerating is the revenue that is being regulated away.
falsifier: Gross margin improves year over year in either of the next two quarterly releases, or revenue growth stays above 20 percent two quarters after the compounding restrictions bind.
data_caveats:
  - no consensus estimates exist anywhere in this pipeline, so nothing here is calibrated against what the market expects
  - no filing text collected for HIMS; there is no management commentary in this dossier and no segment split
  - the revenue mix between compounded GLP-1 and the rest of the business is the crux and is not in the dataset
  - shares_outstanding reads 0 against a market_cap of 6.6B, so per-share figures cannot be checked against the cap
  - insider data not collected below the top 600 by market cap
  - the panel price was 1.6 percent stale; the close series is used instead
---

## WHAT IS PRICED IN

At $27.44 the shares sit 55.5% below the 52-week high and at the **0th percentile of their
peer group on every one of the five momentum fields**: `return_12_2` &minus;38.6%,
`return_1m` &minus;12.2%, `high52w_proximity` &minus;55.5%, `rel_strength_sp500` &minus;62.0%.
`max_drawdown_1y` is &minus;76.9%. That is not a market that has overlooked something. It is
a market that has decided.

What it has decided is legible from the headlines the dossier kept: JPMorgan resumed
coverage at neutral on GLP-1 compounding headwinds, the FTC and two states are pursuing the
subscription and health-data practices, and there are securities class actions following the
FTC filing. The price is asserting that the regulatory picture takes both the revenue and
the margin.

## WHERE I DIFFER

I don't. This is the contrarian slot and the honest answer is that the bear case looks right.

What I can add is narrower and worth writing down anyway, because it is the thing a screen
would get wrong. **The Growth sleeve reads &minus;0.19, which looks unremarkable, and it is
hiding a 91-point split.** `revenue_growth_yoy` is +28.0% at the **91st percentile** and
`revenue_acceleration` is +14.1% at the **95th**. Meanwhile `gross_margin_trend` is
&minus;10.0pp, `eps_growth_yoy` &minus;179.7% and `fcf_growth_yoy` &minus;48.4%, all at the
**0th percentile**.

A factor screen sorting on revenue growth would surface this name as a fast grower trading
on 2.78x `ev_revenue`. It is not. Revenue is accelerating into a regulatory cutoff, and
accelerating revenue with collapsing gross margin is the signature of a business buying its
last quarters of growth.

That is not a variant perception and I have scored it 0. It is a warning about a column.

## WHAT CLOSES THE GAP

Nothing needs to close. The question is whether the revenue line breaks as well as the
margin, and that is answerable at the next release rather than argued now. The observable is
the segment split: what share of the $2.6B `ttm_revenue` is compounded GLP-1. **That number
is not in this dataset and the 8-K EX-99.1 is the first place it could appear.** Filing
collection began 2026-09-12 and runs forward, so the next quarter is the first time this is
checkable at all.

## VALUATION

No P/E: `ttm_eps_diluted` is &minus;$0.63, so the panel withholds it, correctly. `roe_ttm` is
&minus;32.0% and `price_book` 20.28 sits at the 5th percentile of the peer group, which is to
say expensive, on a book that losses are shrinking.

On `ev_revenue` 2.78x against $2.6B of revenue:

- **Bear $16.** 1.6x revenue, roughly where the group's weakest names trade, if the GLP-1
  revenue rolls off and growth goes negative.
- **Base $22.** 2.2x revenue. Revenue flattens rather than falls, margins stay impaired.
- **Bull $38.** 3.9x. The non-GLP-1 subscription base proves durable and margin recovers.

The range is wide because the input that would narrow it, the segment mix, is missing. I am
logging **$22** as the graded number. Direction is avoid rather than short: `volatility_1y`
is 93.2% and `beta_1y` 2.80, and a name that has already fallen 77% from its high can rally
40% on a single regulatory headline without the thesis being wrong.

## WHAT PROVES ME WRONG

Gross margin improving year over year in either of the next two quarterly releases, or
revenue growth holding above 20% two quarters after the compounding restrictions bind.

Either would mean the margin damage was transitional rather than structural, or that the
business underneath the GLP-1 revenue is larger than I am assuming. Checkable from the 8-K
EX-99.1 and the 10-Q income statement. **Not yet checkable.**

The strongest case against me: `accruals_ratio` is &minus;14.7% at the 86th percentile, so
what earnings there are, are cash-backed rather than accrual-inflated, and `net_debt_ebitda`
of 3.09 at the 32nd percentile is not a balance sheet in distress. This is a company with a
revenue problem ahead of it, not a solvency problem. That is why the direction is avoid and
the conviction is 3 rather than 5.

## WHAT I DON'T KNOW

- **The segment split.** What fraction of revenue is compounded GLP-1. This is the entire
  question and it is not in the dataset.
- **No consensus estimates**, so I cannot tell whether +28% revenue growth is a beat or a
  miss, which is most of what moves a name like this.
- **No filing text.** No management commentary on the transition, no guidance.
- `shares_outstanding` reads 0 against a `market_cap` of $6.6B, so I cannot reconcile
  per-share figures against the capitalisation.
- No insider data: Form 4 is fetched only for roughly the 600 largest by market cap.
- The legal exposure is known only from headlines, which the dossier filtered for relevance
  but did not verify. I have not read the FTC complaint.
