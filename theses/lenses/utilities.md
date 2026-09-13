<!-- Written once, reviewed by hand, and versioned with the code that reads it.
     dossier.py injects this when a company's GICS sector matches the filename.
     Keyed on an exact column, so this is a lookup and not a retrieval problem:
     there is nothing to embed and nothing that can come back wrong. -->

# Utilities

## What these businesses actually do

A regulated utility is a financing vehicle wrapped around an operating company. A commission sets a rate base, the depreciated cost of plant judged used and useful, and authorizes revenue that recovers operating costs and depreciation and earns a return on it: cost of debt near actual, plus an allowed ROE on an equity layer near half the capital structure.

Three consequences follow. Capital spending is the earnings engine, so a utility generating cash after capex is shrinking its earnings base. Debt is an input the commission prices into rates, so 50 to 60 percent debt is the authorized design and carries no distress information. And capital enters service before rates reflect it, which is regulatory lag, so earned ROE sits below allowed ROE until new rates take effect. That earned-to-allowed spread is the most important operating number here and it is not a field in this panel.

Electric Utilities, Gas Utilities, Water Utilities and Multi-Utilities follow that model. Independent Power Producers & Energy Traders and Renewable Electricity do not: IPPs sell energy and capacity into wholesale markets on hedged merchant plant with no allowed return, and Renewable Electricity operators run PPA-contracted projects funded with tax equity and non-recourse debt, where noncontrolling interests and HLBV allocations leave ttm_net_income and roe_ttm uninterpretable. Read sub_industry first.

## What drives the P&L

Rate base growth, capex placed in service earning the allowed ROE on its equity layer. Rate case outcomes: timing of new rates, disallowances, resets of the allowed ROE. Financing, since capex is funded with debt and new shares, and two to four percent annual share growth subtracts directly from eps_growth_yoy. Weather and load wherever revenue is not decoupled. O and M control, because between rate cases the company keeps what it saves. Catastrophe charges: wildfire, storms, coal ash, nuclear outages. Fuel last: it moves revenue substantially, earnings almost not at all, and operating cash flow hard through deferred fuel balances that unwind over one to two years.

## Fields that mean something here

price_book is the most useful Value field here: equity approximates the equity layer of rate base, and the premium to book expresses allowed ROE against cost of equity plus growth. Read it paired with roe_ttm, the earned return, against the nine to ten and a half percent band where recent US allowed ROEs sit. Seven percent indicates lag or a disallowance. Fourteen percent indicates non-regulated earnings or a depressed equity denominator.

ttm_dep_amort matters more here than anywhere else. Utility depreciation runs two and a half to three and a half percent of gross plant, so EDGAR annual capex divided by ttm_dep_amort is the best available proxy for rate base growth: above 1.5 times is a compounding rate base, near 1.0 is a flat one. In the decade history the equity series tracks rate base growth, since the equity layer is a fixed share of it, and shares_diluted shows how much of that growth was bought with dilution.

earnings_consistency is high across nearly every regulated name and does not discriminate; a low reading points to merchant exposure, an unrecovered storm, or a disallowance.

## Fields that mislead here

fcf_yield is useless in this sector. Regulated utilities run negative free cash flow by design for as long as the capital program runs, so a high fcf_yield flags a company under-investing against its own depreciation and shrinking future rate base, which the inverted Value sleeve ranks well. Fixing it would need capex split into maintenance and growth, which our data lacks, so treat fcf_yield as unscorable and use capex over ttm_dep_amort instead. fcf_growth_yoy and ttm_fcf add a second defect: they swing on deferred fuel recovery timing, which reverses.

net_debt_ebitda is inverted in the Quality sleeve and penalizes every healthy regulated utility, where five to six times is normal and consistent with solid investment grade. It is further inflated by securitization debt, the non-recourse storm-recovery bonds in total_debt serviced by a dedicated customer charge, so it overstates leverage at exactly the companies that already recovered a catastrophe cost.

revenue_growth_yoy is close to meaningless for regulated names: revenue can rise fifteen percent on gas costs with no change in earnings and fall back the next year. gross_margin_trend and operating_margin inherit that distortion through the denominator, so apparent margin compression at a gas distributor is usually a fuel price move. ev_revenue is useless and cannot be repaired: the pass-through share of revenue differs by more than two to one between a wires-only delivery utility, a vertically integrated electric and a gas distributor.

accruals_ratio reads backwards here. Regulatory assets and liabilities, deferred fuel, AFUDC equity, storm deferrals and deferred income taxes are large legitimate accruals the commission's accounting requires, so a high accruals_ratio describes the regulatory model. op_margin_stability carries the same fuel contamination: a utility with fuel riders looks unstable while its earned return is steady.

pe needs care because ttm_eps_diluted is GAAP, while utilities guide and are valued on ongoing EPS excluding hedge mark to market, storm charges and impairments. ev_ebitda needs care because utility EBITDA includes AFUDC, a non-cash accrued return on construction in progress, and because the panel's ev_ebitda does not reconcile to its own balance sheet; recompute it from market_cap, total_debt, cash_and_investments and ttm_ebitda.

return_12_2, rel_strength_sp500 and high52w_proximity here mostly track the long end of the yield curve, which the short-dated risk-free proxy will not explain. insider_net_buy_90d and insider_cluster_score rarely fire at utilities. neglect_score does not discriminate, because analyst_count and inst_ownership are high sector-wide.

## The valuation convention

Practitioners use three frames. A premium or discount to rate base, which price_book approximates if you accept equity as the proxy for the equity layer and adjust for acquisition goodwill that never entered rate base. A P/E on ongoing EPS against the group and the company's own history, which pe approximates with the GAAP caveat above. And a dividend discount model, the primary frame here, which this panel cannot reach at all: no dividend per share, no payout ratio, no dividend yield.

The construction available to us: equity from the decade history as the rate base proxy, capex over ttm_dep_amort as the growth rate, roe_ttm as the earned return, then ask whether price_book is consistent with that pair against an assumed cost of equity. For IPP names, use EV/EBITDA on mid-cycle ttm_ebitda.

## The cycle question

The cycle here is a capital and rate case cycle: a heavy capex program with earned ROE below allowed and eps_growth_yoy undershooting rate base growth, then rate case resolution with earned ROE closing toward allowed, then a maturing program with capex near depreciation.

EDGAR annual capex and ocf against ttm_dep_amort show the program's direction, shares_diluted shows the funding, and net_income over equity across the decade gives an earned ROE trend whose drift while capex runs high is the signature of lag. Our data cannot place the company in the rate cycle that sets the multiple, and it cannot separate a mild winter from a rate increase inside a quarterly revenue move.

## What this dossier cannot see

Allowed ROE, authorized equity ratio, test year convention and rate case calendar. Item 1 names jurisdictions and sometimes an allowed ROE, never as a series; the rate base figure and capital plan sit in Item 7 MD&A, outside our EDGAR set. Regulatory asset and liability balances, including the deferred fuel balance behind most OCF swings. Dividend per share, payout ratio and dividend yield. Credit ratings and downgrade headroom, which constrains the capital program. Wildfire and storm liability estimates. Degree days, customer counts, load growth and interconnection queues. Forward power curves and capacity auction prices, which set IPP earnings. Segment revenue as numbers, so a merchant arm can be named and never sized.

## Questions worth asking

1. What is annual capex divided by ttm_dep_amort over five years, and is equity compounding faster than shares_diluted? Both answerable from the EDGAR annual history; the equity-to-shares gap should reconcile to eps_growth_yoy.
2. Where does roe_ttm sit against the nine to ten and a half percent band, and is net_income over equity drifting down while capex runs high? The earned series is answerable; the allowed ROE is unanswerable unless Item 1 states it.
3. How much of revenue_growth_yoy is fuel? Not directly answerable. Test it against operating_income in the decade history: revenue up with operating_income flat is pass-through.
4. Does a merchant or non-regulated segment exist, and what does Item 1A name as the live catastrophe or disallowance exposure? Segment NAMES and Item 1A answer both. Segment size is unanswerable.
5. Is price_book consistent with roe_ttm and the capex-implied growth rate, and if not, does the premium sit in goodwill inside equity or in non-regulated earnings? Answerable arithmetically.
