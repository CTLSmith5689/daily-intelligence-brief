<!-- Written once, reviewed by hand, and versioned with the code that reads it.
     dossier.py injects this when a company's GICS sector matches the filename.
     Keyed on an exact column, so this is a lookup and not a retrieval problem:
     there is nothing to embed and nothing that can come back wrong. -->

# Consumer Staples

## What these businesses actually do

Consumer staples sell cheap, frequently repurchased items through retail channels they do not own. Unit demand tracks population and category penetration, zero to two percent a year in developed markets. Everything above that is price and mix. The durable asset is shelf position and purchase habit, which let a branded manufacturer price above the retailer's own-label version of a near-identical product. That premium is the business. The retailer attacks it continuously, since own label earns the retailer more.

Three models sit inside the sector. Branded manufacturers are the sub_industry values Packaged Foods & Meats, Household Products, Personal Care Products, Soft Drinks & Non-alcoholic Beverages, Brewers, Distillers & Vintners, and Tobacco: gross_margin of 35 to 60 percent, earnings set by price realization against input cost. Tobacco loses volume by design and prices over it, so revenue_growth_yoy of two to four percent sits on a shrinking physical business. Retail and distribution is Food Retail, Consumer Staples Merchandise Retail, and Food Distributors: gross_margin of 12 to 28 percent, operating_margin of one to four percent, earnings set by traffic, ticket, shrink, and fixed-cost leverage over a store base. Agricultural Products & Services, and the protein processors inside Packaged Foods & Meats, are commodity processors whose spreads follow crop and livestock cycles: cyclicals shelved under staples.

## What drives the P&L

For branded manufacturers, in order: price and mix net of input cost, seen in gross_margin and gross_margin_trend; the volume response to that price, which this data cannot see; the input cost cycle, lagging price by two to four quarters; advertising and promotion, discretionary inside a quarter and visible when operating_margin rises while gross_margin falls; currency, since large branded names earn 40 to 60 percent of revenue abroad and translation sits inside revenue_growth_yoy; and buybacks, which hold eps_growth_yoy three to five points above revenue_growth_yoy for years, checkable against shares_diluted.

For retailers and distributors: comparable store or case growth first, absent here; then gross margin mix across fuel, pharmacy, fresh, and own brand; then labor cost per store. operating_margin works off a base of one to four percent, so 30 basis points is a large earnings event.

## Fields that mean something here

gross_margin_trend is the most informative panel field here, the closest read on whether pricing is outrunning input cost, and it should be read against the gross_margin level. op_margin_stability and earnings_consistency matter more here than in most sectors, since stability is what a staples multiple pays for. fcf_yield, ttm_fcf, and fcf_growth_yoy carry the return story: ttm_fcf at or above ttm_net_income is normal, and a sustained shortfall points at working capital or restructuring cash. Read net_debt_ebitda as a level: 2.5x to 4.0x is deliberate at a post-acquisition branded name. Pair it with fcf_yield. accruals_ratio has content because revenue recognition is simple: spikes trace to trade promotion accruals or channel loading. revenue_acceleration is mostly a pricing signal: deceleration two or three quarters after a price increase is elasticity arriving. From EDGAR, segment NAMES show whether the company is run by geography or by category, and Item 1A is where retailer concentration appears, commonly one customer at 15 to 25 percent of revenue.

## Fields that mislead here

price_book is useless for branded staples. Buybacks and acquisition goodwill leave equity as a residue, sometimes negative, so it returns an absurd or negative number. Nothing in the Value sleeve repairs it, and a fix would need brands carried at replacement value, which no filing reports. Drop it across the branded sub_industry values.

roe_ttm fails on the same denominator, in the flattering direction: a 150 percent roe_ttm reports the size of past buybacks. Repair it as ttm_operating_income after an assumed tax rate over (total_debt + equity - cash_and_investments).

ev_ebitda is the right convention here, and the panel's stored value does not reconcile to its own balance sheet, so recompute it from market_cap, total_debt, cash_and_investments, and ttm_ebitda. It also adds back ttm_dep_amort, the recurring cost of stores and fleets at Food Retail, Consumer Staples Merchandise Retail, and Food Distributors, so ev_ebitda flatters those three against branded peers.

ev_revenue is not comparable across this sector. gross_margin runs from 12 percent at Food Distributors to above 60 percent at Tobacco, so identical readings mean opposite things. Use it inside one sub_industry only.

revenue_growth_yoy is the field most likely to produce a wrong thesis here. It sums price, volume, currency, and acquisitions, and this data cannot decompose it. Six percent can be nine points of price against three of volume decline, a business losing its customer, or one point of price against five of volume, a business winning shelf. Pair it with gross_margin_trend: price-led growth with falling gross_margin is cost pass-through, price-led growth with rising gross_margin is pricing power. Volume itself stays unobservable.

pe needs care: restructuring and impairment charges run through ttm_net_income and ttm_eps_diluted regularly here, so check ttm_eps_diluted against the reported eps_diluted series before calling a low pe cheap.

high52w_proximity, return_1m, and much of return_12_2 track rate expectations and defensive rotation sector-wide, so they carry regime information and little about the company. volume_trend is weak: turnover in mega-cap staples is dominated by index and income funds. sharpe_1y and max_drawdown_1y flatter this sector by construction given low beta_1y, while volatility_1y recomputes cleanly. neglect_score and analyst_count are usable only in the Agricultural Products & Services and Food Distributors tail.

## The valuation convention

Practitioners value branded staples on EV/EBITDA and P/E against the company's own ten-year range and a sub_industry peer set, then cross-check free cash flow yield against the dividend and the risk-free rate. Discounted cash flow exists here, and at two to four percent growth terminal value carries 85 percent or more of the answer, so the model is an exit multiple in disguise. Our fields reach part of the way: a recomputed ev_ebitda and a checked pe give today's multiple, fcf_yield the cash return. Multiple history is the gap: one year of daily closes with reported quarterly eps_diluted yields one year of trailing P/E, so calling a name cheap against a ten-year range is unsupported. The dividend is absent from the panel, which matters where yield sets the valuation floor. Use fcf_yield as the substitute and label it as one.

## The cycle question

Three cycles overlap. The input cost and pricing cycle runs two to three years: costs rise, price follows with a lag, gross_margin troughs and recovers, volume weakens once the increases stick. Recovering gross_margin_trend with decelerating revenue_growth_yoy and negative revenue_acceleration places a name late in that cycle, elasticity bill arriving. Compressing gross_margin_trend with accelerating revenue_growth_yoy places it early. The category cycle is longer. It appears as years of sub-inflation revenue growth in the reported decade alongside stable gross_margin, which describes a franchise being harvested. The relative valuation cycle shows in rel_strength_sp500 and return_12_2, which move together across the sector and say nothing about the rates driving them.

## What this dossier cannot see

Unit volumes and average selling prices, so the price and volume split behind revenue_growth_yoy is hidden permanently. Private label share by category. Same-store sales and traffic, the number Food Retail and Consumer Staples Merchandise Retail are judged on. Segment revenue as numbers, so mix shift shows only through segment NAMES and narrative text. Commodity series for grains, resins, pulp, freight. Dividend per share and dividend yield. Promotional calendars and elasticity coefficients. Consensus estimates and guidance, so nothing says what is priced in.

## Questions worth asking

1. Over the last four quarters, do gross_margin_trend and revenue_growth_yoy move together or apart, and where does gross_margin sit against its ten-year median? Answerable as a direction. The price and volume split behind it is unanswerable.
2. Does the gap between eps_growth_yoy and revenue_growth_yoy match the change in reported shares_diluted, or is it coming from operating_margin? Answerable.
3. Is ttm_fcf converting at or above ttm_net_income, and has that held across the reported ocf and capex history? Answerable.
4. Recomputed from market_cap, total_debt, cash_and_investments, and ttm_ebitda, where does ev_ebitda sit against sub_industry peers on the same panel date? Answerable within the cohort, unanswerable against the company's own history.
5. Does Item 1A name a customer above 15 percent of revenue, and do Item 1 or the risk factors name private label? Answerable from filing text.
6. Have op_margin_stability or earnings_consistency degraded while net_debt_ebitda rose, which would describe a levered acquirer failing to integrate? Answerable.
7. If accruals_ratio has worsened, does the segment note or an EX-99.1 release discuss trade spending or inventory at retail? Answerable only when an EX-99.1 was filed.
