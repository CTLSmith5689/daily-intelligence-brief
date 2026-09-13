<!-- Written once, reviewed by hand, and versioned with the code that reads it.
     dossier.py injects this when a company's GICS sector matches the filename.
     Keyed on an exact column, so this is a lookup and not a retrieval problem:
     there is nothing to embed and nothing that can come back wrong. -->

# Consumer Discretionary

## What these businesses actually do

Consumer Discretionary sells what a household can postpone. Under that deferrable revenue sits a fixed cost block sized for demand chosen a year or two earlier: store leases and payroll, restaurant occupancy, a ship, a land bank. It does not move for several quarters after revenue does. Operating leverage in both directions is the sector's defining feature, and it is why a three percent revenue miss becomes a fifteen percent earnings miss.

Four models, four income statements. Capacity operators earn traffic times ticket times contribution margin against committed rent, labor and depreciation: company-operated Restaurants, Apparel Retail, Specialty Retail, Casinos & Gaming, and the owned half of Hotels, Resorts & Cruise Lines. Royalty collectors take a percentage of system sales with almost no capital employed. Durable-goods makers (Automobile Manufacturers, Auto Parts, Leisure Products, Footwear) book sell-in through a channel, so revenue can hold while sell-through has rolled over. Homebuilding is a debt-financed land bank converting inventory to revenue at closing, run on working capital.

sub_industry cannot separate the first two models, the distinction that matters most: MCD at operating_margin 0.4622 shares the Restaurants value with CMG at 0.1465, and HLT and MAR share Hotels, Resorts & Cruise Lines with RCL and CCL. Item 1 and the segment names separate them.

## What drives the P&L

Volume against the fixed cost base sets the amplitude of everything else. Second is gross margin, the markdown line in retail and durables: inventory bought for demand that did not arrive clears at a discount one to two quarters after the miss. Third is landed cost (freight, tariffs, commodity content, wages), which Item 1A names by exposure. Fourth is price against traffic, since price-led growth holds margin and discount-led growth does not. Fifth is the cost of capacity: new units, capex, and for Homebuilding land spend and mortgage rates. Sixth is interest expense, which turns a demand downturn into a solvency question at leveraged operators.

## Fields that mean something here

revenue_acceleration is the most useful field in the panel: this quarter's year-over-year growth minus last quarter's, seasonally clean by construction, at quarterly resolution, and it turns before revenue_growth_yoy does.

gross_margin_trend is this quarter's gross margin minus the same quarter a year ago, also seasonally clean, and the best available proxy for markdown pressure and inventory trouble. Check it exists: it is null for 100 of the 206 sector names above $1B, including AMZN, TJX, MCD and every homebuilder, because it needs the GrossProfit XBRL tag.

fcf_yield, ttm_fcf, fcf_growth_yoy and accruals_ratio are this sector's inventory instrumentation, since no inventory field exists in the data. A rising accruals_ratio, TTM net income less operating cash flow over average assets, means inventory and receivables outgrowing sales.

op_margin_history, the twelve-quarter series behind op_margin_stability, is worth more than the statistic derived from it: difference it year over year, then read the level against the decade of EDGAR annual history rather than against other sub_industry values. return_12_2, rel_strength_sp500, high52w_proximity and volatility_1y carry real signal: this sector re-rates ahead of its reported fundamentals.

## Fields that mislead here

price_book is useless across most of this sector. It is null for 24 of 206 names above $1B on negative equity from buybacks, and that list is the sector's best franchises: MCD, YUM, SBUX, AZO, ORLY, HLT, MAR, BKNG. roe_ttm fails on the same names, null for 27 of 206. Where equity is positive and small both read as buyback tallies: HD at price_book 18.64 and roe_ttm 1.0433. Homebuilding is the exception: price to tangible book against ROE is the convention there and the field works, at LEN 0.8955 and DHI 1.6314. Repair needs tangible book plus capitalized leases, neither carried here. Use operating_margin and fcf_yield instead.

op_margin_stability and earnings_consistency measure seasonality here: both are dispersion measures over eight raw quarterly values, unadjusted, in a sector that concentrates earnings into one or two quarters. Across the 156 sector names with twelve quarters of history, the raw standard deviation overstates instability against a year-over-year differenced one on 77, worst at MTN: 0.5705 against 0.0587.

net_debt_ebitda should not be read as printed, and leverage is the whole downside case here. Recomputed from total_debt and cash_and_investments over ttm_ebitda it carries a median relative error of 53.6 percent, and the sign flips: ROST prints 0.1233 against a balance sheet showing net cash. 41 of 206 have no total_debt at all, HLT prints $11 million and MAR $778 million, and total_debt excludes operating lease liabilities, the largest fixed obligation lease-heavy retail and restaurants carry.

gross_margin is not comparable across names, since filers choose whether occupancy and distribution sit in cost of sales or SG&A: TJX prints 0.1219 against ROST at 0.3338. Five names print a gross_margin below their own operating_margin, which is impossible: AMZN 0.0074 against 0.1208, DHI 0.0158 against 0.1299.

pe reconciles exactly to price over ttm_eps_diluted, and still needs care. It is null for 23 of 206 names, and the nulls cluster at cycle troughs. Where price and ttm_eps_diluted sit on different per-share bases the ratio is nonsense: BKNG prints pe 1.0326 against ttm_eps_diluted 167.96.

ev_ebitda does not reconcile to the panel's balance sheet anywhere in this universe, so recompute it. For Automobile Manufacturers with captive finance even that fails: F prints total_debt of $291 million against a finance book in the hundreds of billions. revenue_growth_yoy and eps_growth_yoy are TTM against prior TTM, lagging a turn by up to a year here.

## The valuation convention

Capacity operators go on EV/EBITDA, lease-heavy retail and restaurants on EV/EBITDAR or a lease-adjusted EV/EBITDA. Royalty collectors go on a DCF over system sales times royalty rate, since their book equity is negative by design. Homebuilders go on price to tangible book against ROE, automakers with captive finance on sum-of-parts. In all four the multiple is applied to a normalized mid-cycle margin.

This data reaches about half of that. A recomputed ev_ebitda and ev_revenue reach the operator convention without the lease adjustment, and EBITDAR is not constructible: rent expense is not a field and lease liabilities are not in total_debt. fcf_yield is the most robust valuation field here, surviving negative equity, seasonality and gross-profit tag inconsistency. Normalizing the margin by hand from op_margin_history and the EDGAR annual history is the highest-value step here.

## The cycle question

This sector leads the broad cycle down and lags it up, and the sequence inside the data runs: return_12_2 and rel_strength_sp500 move first, revenue_acceleration next, gross_margin_trend after that as markdowns clear, then operating_margin, then net_debt_ebitda.

The data can establish four things: whether revenue_acceleration has turned while revenue_growth_yoy is still positive, which is early deceleration; whether gross_margin_trend is negative against positive revenue_growth_yoy, which is discount-driven growth; whether accruals_ratio is rising and fcf_growth_yoy falling while eps_growth_yoy holds, which is an inventory build; and where operating_margin sits against the decade of EDGAR annual margins, the only trough-versus-peak anchor available.

It cannot establish where the consumer is: there are no rates beyond a risk-free proxy, no credit data and no employment figures, so that read comes from Item 1A and the 8-K EX-99.1 text.

## What this dossier cannot see

Same-store sales. Traffic against ticket. Unit counts, openings and closures. Inventory balance and turns, since no inventory line exists in the panel or in the decade of reported history. Average selling price and units. Segment revenue as numbers, since only segment names are available. Backlog and order books. The franchised against company-operated unit split, and the royalty rate. Discount depth. Rent expense and operating lease liabilities. Captive finance book size and credit quality. Mortgage rates, consumer credit metrics, tariff schedules, freight rates.

## Questions worth asking

1. Has revenue_acceleration turned negative while revenue_growth_yoy is still positive, and does the 8-K EX-99.1 text acknowledge it? Answerable from both.
2. Is fcf_growth_yoy falling while eps_growth_yoy holds, with accruals_ratio rising? Answerable, and the closest thing to an inventory read.
3. Where does operating_margin sit against this company's op_margin_history and its decade of EDGAR annual margins? Answerable, and the only mid-cycle anchor.
4. Is gross_margin_trend populated? If null, Item 1A and the earnings release are the only source on markdowns.
5. Is this a royalty collector or a capacity operator? Item 1 and the segment names answer it; sub_industry does not.
6. How large is the lease obligation relative to the fixed cost base? Unanswerable: total_debt excludes operating lease liabilities and rent expense is not carried.
7. Which Value and Quality fields are populated at all: pe, price_book, roe_ttm, net_debt_ebitda? Check before any enters a thesis.
