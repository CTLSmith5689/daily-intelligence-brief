<!-- Written once, reviewed by hand, and versioned with the code that reads it.
     dossier.py injects this when a company's GICS sector matches the filename.
     Keyed on an exact column, so this is a lookup and not a retrieval problem:
     there is nothing to embed and nothing that can come back wrong. -->

# Materials

## What these businesses actually do

Materials holds four economic models. Most of the sector is price-takers on a cost curve: Steel, Aluminum, Copper, Gold, Silver, Other Industrial Metals & Mining, Other Precious Metals & Mining, Diversified Metals & Mining, Commodity Chemicals, Paper Products and most of Fertilizers & Agricultural Chemicals. Output is fungible, so the company earns the gap between a price set outside it and its unit cash cost, and the durable advantage is cost-curve position: grade, asset vintage, energy contracts, freight. Plant is fixed and expensive, so a small move in realized price moves operating income several times as far.

The second group converts and passes through: Paper & Plastic Packaging Products & Materials, Metal, Glass & Plastic Containers, Building Materials, Construction Materials and the commoditized half of Specialty Chemicals. They buy an input and sell on contracts that reprice with a lag, so earnings are a conversion spread times volume tied to housing, autos and packaged goods, with aggregates and cement adding a freight-radius local monopoly.

Two smaller models break any sector average. Industrial Gases sells long-dated on-site contracts with take-or-pay terms, formulated Specialty Chemicals is specified into a customer's product (price_book 20.19 at SHW against a 1.95 median), and royalty companies inside Gold are contracts on other people's mines (OR at gross_margin 0.9687). Pre-revenue explorers sit in Gold and Silver with ttm_revenue zero and gross_margin 0.00 (NG, PPTA, HYMC, SA), where no earnings-based field applies.

## What drives the P&L

In order. The realized price of what the company sells, absent from this dossier entirely. The cost of the main input: natural gas for nitrogen and chlor-alkali, scrap and met coal for steel, power for aluminum. Earnings follow the spread between those two, and either leg alone explains nothing. Then volume and utilization; cost position from grade, strip ratio and plant vintage; the industry capex cycle, where supply approved at high prices takes three to seven years to arrive and lands into a weaker market; then trade policy, freight and FX. Last, net_debt_ebitda, which decides whether a trough costs two bad years or the equity.

## Fields that mean something here

fcf_yield is the most useful value field here, filled on 99% of Materials names above $1B, and counts cash after the capital these businesses consume: the sector median of ttm_fcf over ttm_ebitda is 0.38. net_debt_ebitda is the survival variable, filled at 90%, median 1.76, and belongs against a normalized EBITDA. price_book, filled at 96%, is the closest proxy for asset value and still computes where pe does not.

operating_margin and gross_margin matter as levels set against the decade of reported annual operating_income, gross_profit and revenue from EDGAR. That comparison is the normalization engine here, since every panel field is trailing-twelve-month. roe_ttm, earnings_consistency and op_margin_stability read as cycle amplitude, and a low earnings_consistency describes the business model.

return_12_2, rel_strength_sp500, return_52w and high52w_proximity carry information, because the underlying commodity trends and these equities track it, and volatility_1y survives recomputation from the close series. shares_outstanding against the reported shares_diluted history records capital allocation through the cycle.

## Fields that mislead here

pe is the worst field in this sector and fails at both ends of the cycle. At the peak it prints low while correctly reflecting earnings that will not repeat: CF at pe 10.26 with eps_growth_yoy +76.4%. At the trough it disappears: 24 of the 109 Materials names above $1B carry no pe on negative trailing EPS, including DOW, CE at -10.69, WLK at -9.58, FMC at -22.00 and LYB. Ranking on pe deletes the part of the cycle where entries are made. Repair takes a normalized earnings number: apply the decade-median operating margin from EDGAR history to ttm_revenue, tax it, and divide price into that. Rows written before 2026-09-12 also show a positive pe against negative ttm_eps_diluted.

ev_ebitda is unreliable twice over. It does not reconcile to the panel's own balance sheet, median relative error near 10% and p90 58%, so recompute it as market_cap plus total_debt minus cash_and_investments over ttm_ebitda, using ttm_operating_income plus ttm_dep_amort where ttm_ebitda is missing, as it is on a third of the sector. Even when correct it flatters capital-intensive producers, since it excludes sustaining capex.

ev_revenue is useless for price-takers, because revenue is a commodity price times a volume the company did not set, so one asset base prints a different ev_revenue in every year of the cycle. The Gold median of 5.76 against Steel at 1.41 describes commodity margin structure and carries nothing about relative value.

The whole Growth sleeve measures the commodity. revenue_growth_yoy, eps_growth_yoy, revenue_acceleration, gross_margin_trend and fcf_growth_yoy move with the spread rather than with anything management did, so a strong reading across all five locates the cycle and argues for less confidence in current earnings. gross_margin_trend is also only 47% filled here.

price_book needs care in three cases: impaired chemicals names where book is an accounting reset, the 25% of the sector above $1B with no equity figure, and explorers where book is capitalized exploration spend (CNL at 11.98, NG at 8.44). gross_margin is 100% filled and wrong at both extremes, 0.00 for pre-revenue explorers and 0.9687 for royalty companies. roe_ttm at a peak is a return on nothing repeatable (CF at 46.7%), and accruals_ratio moves on inventory write-downs with no earnings management involved. beta_1y is weak here: CF reads -0.96. benford_mad and earnings_date are empty across the sector, and the insider fields are filled on 29% of these names.

## The valuation convention

Practitioners value these on mid-cycle earnings power and on asset value, and treat the trailing multiple as an input to neither. A producer goes on normalized EBITDA or EPS at a mid-cycle multiple, on EV per ton or ounce of capacity, and for miners on P/NAV from a discounted mine plan at a price deck, usually 0.7x to 1.3x. Converters go on EV/EBITDA against a stable conversion spread, Industrial Gases on contracted cash flow.

Our fields reach the first of those and stop. A decade of reported revenue and operating_income gives a median-margin earnings number to set against market_cap, fcf_yield checks it against cash, and price_book stands in for asset value where equity is intact. P/NAV is unreachable without reserves, EV per ton without capacity, cost-curve percentile without unit cash cost.

## The cycle question

Set current operating_margin and gross_margin against the ten-year reported history, set ttm_fcf against ocf minus capex over the same decade, and read capex direction there. The peak signature is high52w_proximity near zero, a low pe, a high roe_ttm, and positive gross_margin_trend and eps_growth_yoy together. The trough signature is an absent pe, price_book near or below 1.0, a negative fcf_yield and a negative gross_margin_trend.

Where the commodity itself sits is not establishable here. No price series for nitrogen, copper, gold, containerboard or natural gas exists, so margin history is the only cycle instrument, it lags by a quarter or more, and it shows one company's capex, never the industry's.

## What this dossier cannot see

Realized price per unit and unit volumes. Input costs: natural gas, power, met coal, scrap, resin. Unit cash cost and AISC, and therefore cost-curve position, the central competitive fact here. Reserves, grades, mine life, mine plan. Capacity, utilization, and competitors' additions including Chinese export volumes. Segment revenue as numbers, since only segment NAMES exist, so a diversified chemicals company cannot be split into its exposures. Contract repricing lags, take-or-pay backlog, trade case outcomes, channel inventory. Forward curves, consensus estimates, guidance as data, price targets.

## Questions worth asking

1. Where does current operating_margin sit within the decade of reported operating_income over revenue? Answerable from EDGAR history.
2. What does the company earn with the decade-median operating margin applied to ttm_revenue, and how does that compare with the reported pe? Answerable.
3. Has reported capex run above or below ttm_dep_amort for five years, and which way is it moving? Answerable.
4. Does net_debt_ebitda hold if EBITDA reverts to its decade median, rebuilt from ttm_operating_income plus ttm_dep_amort? Answerable.
5. Did shares_diluted fall in the years when ocf minus capex was highest? Answerable.
6. Which commodities does the company sell, and what does Item 1A name as the largest input cost and trade exposure? Answerable from segment NAMES, Item 1 and Item 1A, unanswerable as a revenue mix, since segment revenue is absent.
7. Where is the realized price of the main product within its own five-year range? Unanswerable here, so a thesis resting on it is stating an assumption.
