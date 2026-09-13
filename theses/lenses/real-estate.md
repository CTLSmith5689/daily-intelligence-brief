<!-- Written once, reviewed by hand, and versioned with the code that reads it.
     dossier.py injects this when a company's GICS sector matches the filename.
     Keyed on an exact column, so this is a lookup and not a retrieval problem:
     there is nothing to embed and nothing that can come back wrong. -->

# Real Estate

> **Read this with suspicion.** The structured field claims that accompany this
> lens were checked against the panel by a second pass told to refute them. It
> refuted 31 of 158 outright and called 73 more overstated: a 34 percent survival
> rate. The refutations were specific, including wrong medians, backwards sign
> logic and a field key that does not exist. The prose below was written by the
> same pass and has **not** been checked that way. Treat it as a well-informed
> starting point, not as fact, and verify anything a thesis leans on.

## What these businesses actually do

Most of this sector is equity REITs: long-lived buildings, financed roughly half with debt, collecting contractual rent, exempt from corporate tax provided they distribute at least 90% of taxable income. Growth cannot come from retained earnings, so it requires issuing equity or debt, and the spread between that cost of capital and acquisition yields is the engine. And GAAP depreciates buildings over 27.5 to 40 years while they often gain value, so ttm_net_income and ttm_eps_diluted run structurally low, then spike whenever a property sells at a gain.

Lease duration splits REIT economics more than property type. Long-lease landlords (Telecom Tower REITs, Data Center REITs, Industrial REITs, Health Care REITs, Other Specialized REITs, net-lease Retail REITs) collect contractual escalators and roll a small share of the portfolio each year. Short-lease operators reprice everything within a year or two and carry a real cost base: Hotel & Resort REITs on nightly rates, Self-Storage REITs on monthly, Multi-Family Residential REITs and Single-Family Residential REITs on annual leases. Office REITs sit apart: renewal rents can reset downward, and re-tenanting eats capital.

Real Estate Management & Development is a different business: Real Estate Services firms earn fees, Real Estate Development firms recognize revenue at completion and hold inventory at cost, and Real Estate Operating Companies and Diversified Real Estate Activities sit between. They pay full tax and carry small ttm_dep_amort against ttm_ebitda, so the corrections below do not apply; read sub_industry first. Mortgage REITs are in Financials, so nothing here is a lender.

## What drives the P&L

In order. Occupancy and in-place rent on the portfolio, which sets nearly all of revenue. The spread between expiring and new rent on leases rolling this year. Interest expense, since debt is half the capital stack and a refinancing moves cash earnings more than any operating decision. Acquisition and development volume, which buys revenue with issued shares. Property taxes, insurance and payroll, visible in operating_margin. Last, disposition gains, which move ttm_net_income and ttm_eps_diluted violently and change nothing.

## Fields that mean something here

net_debt_ebitda is the most informative single field here: ttm_ebitda approximates cash property income less overhead, and total_debt is the binding constraint.

ev_ebitda is the closest thing here to a cap rate: its reciprocal approximates the yield on portfolio income, understated because ttm_ebitda is after G&A. It is the one Value sleeve field worth using here. ev_revenue works the same way. Compare both inside sub_industry, since property types trade at persistently different yields.

revenue_growth_yoy and revenue_acceleration are real, since revenue is the rent roll, but check them against shares_diluted and assets from the decade history. Revenue up 30% on share count up 28% adds nothing per share.

op_margin_stability carries signal, since depreciation is steady inside operating_income, so variation in operating_margin reflects occupancy and expense control. Read ttm_dep_amort as an input: its size against ttm_ebitda says how distorted the earnings fields are here.

return_12_2, rel_strength_sp500, high52w_proximity and volume_trend are the most honest numbers on the page, and mostly register the direction of long rates, the dominant driver of REIT prices.

## Fields that mislead here

pe is useless for an equity REIT. Its denominator is depreciation-crushed and disposition-inflated, so two portfolios with the same cash yield can show 60 and 9. The fix is FFO, net income plus real estate depreciation less gains on sale: market_cap over (ttm_net_income + ttm_dep_amort) is a usable P/FFO proxy, wrong in years with big disposition gains.

price_book is useless for the matching reason on the balance sheet. equity is historical cost less accumulated depreciation, so an old portfolio carries near-zero book against buildings worth billions. The fix is price to NAV, which needs market cap rates applied to property-level NOI. Neither exists here. Ignore it.

eps_growth_yoy is the worst field in the Growth sleeve here: disposition timing swings it by hundreds of percent with no operational change. Replace it with growth in ocf divided by shares_diluted from the decade history.

roe_ttm is useless and points the wrong way. Depreciation suppresses the numerator while accumulated depreciation shrinks equity, so the oldest portfolios post the highest ROE.

accruals_ratio flatters every REIT mechanically. Depreciation drives net income far below operating cash flow, producing large negative accruals the inverted scoring rewards, so it separates nothing here. earnings_consistency measures the wrong series: it penalizes gain-on-sale lumpiness in ttm_net_income and rates a stable net-lease portfolio as inconsistent. Use op_margin_stability instead.

gross_margin_trend and gross_margin need care. Classification of property operating expense varies, so a triple-net landlord shows 95% gross margin against a hotel owner's 30%, from lease structure alone. Read the trend within one name; levels are not comparable across names.

fcf_yield, ttm_fcf and fcf_growth_yoy turn on what sits inside capex. Maintenance-only capex makes ttm_fcf a good AFFO proxy; development spend makes a REIT funding a pipeline look cash-destroying. Check capex to revenue across the decade: stable and low means maintenance, lumpy means development.

net_debt_ebitda needs care in one direction: five to seven times is ordinary for a REIT and would signal distress elsewhere. Whether a level is dangerous depends on the maturity ladder, floating rate share and covenant headroom, absent here.

return_12_2 and return_52w are price returns, so for an asset class required to distribute most of its income they understate total return by several points a year. insider_net_buy_90d and insider_cluster_score are noisier here, since UPREIT insiders hold partnership units and Form 4 picks up conversions.

## The valuation convention

Practitioners use four measures; this panel reaches two. Price to FFO, approximated by market_cap over (ttm_net_income + ttm_dep_amort); AFFO is out of reach, since maintenance capex is not separable. Implied cap rate, approximated by the reciprocal of ev_ebitda. Price to NAV, which most desks lead with, unreachable without market cap rates. Dividend yield and payout coverage, absent, since the panel has no dividend field. For Real Estate Services and Development names, pe and eps_growth_yoy are legitimate.

## The cycle question

Two cycles run at once. The capital markets cycle sets cap rates and the cost of debt, tracking long rates. The space market cycle sets occupancy and rent growth through deliveries against absorption, lagging starts by two to four years.

Our data shows whether the company has been acquiring, from assets and shares_diluted growing faster than revenue; whether it is developing, from the capex to revenue path; whether costs are outrunning rents, from the ten-year operating margin trend; and whether it is issuing equity into weakness, from shares_diluted rising while high52w_proximity is low. Cap rates, new supply and the cost of the next refinancing are absent.

## What this dossier cannot see

Occupancy and leased percentage. Same-store NOI. Lease expiration schedule, weighted average lease term, and releasing spreads on renewal. Tenant credit quality and concentration. The debt maturity ladder, floating rate share, and covenant headroom. The dividend, payout ratio, and AFFO coverage. Cap rates, and therefore NAV. Development pipeline yield on cost. Tenant improvement and leasing commission spend, the true maintenance capex for Office REITs and Retail REITs. The interest rate path. The company's own reported FFO and AFFO, which are non-GAAP and appear only in release text.

## Questions worth asking

1. Across the decade history, has ocf divided by shares_diluted risen, and at what rate? If revenue grew while per-share cash flow stalled, issuance funded it. Answerable.
2. What is market_cap over (ttm_net_income + ttm_dep_amort) against the sector cohort? Answerable. Whether a disposition gain inflates ttm_net_income is inferable from the decade history and ttm_operating_income, not measurable.
3. Does capex run at a stable low share of revenue across the decade, or does it spike? That decides whether ttm_fcf and fcf_yield measure distributable cash or a build program. Answerable.
4. What cap rate does the reciprocal of ev_ebitda imply, and is the gap to peers explained by sub_industry alone? Answerable cross-sectionally; the absolute level is untrustworthy with no cap rates here.
5. Has total_debt grown faster than ttm_ebitda, and where does net_debt_ebitda sit against the five to seven times normal here? Answerable. Whether that leverage is safe is not: no maturity ladder, no rate mix, no covenants.
6. Are shares_outstanding and shares_diluted rising while high52w_proximity is low? Answerable, and the clearest dilution warning here.
7. What do Item 1, the segment names, Item 1A and any EX-99.1 text say about property mix, tenant concentration, expirations, occupancy or same-store NOI? Answerable qualitatively. None of it arrives as numbers, and nothing arrives at all when no release was filed.
