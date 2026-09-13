<!-- Written once, reviewed by hand, and versioned with the code that reads it.
     dossier.py injects this when a company's GICS sector matches the filename.
     Keyed on an exact column, so this is a lookup and not a retrieval problem:
     there is nothing to embed and nothing that can come back wrong. -->

# Information Technology

> **Read this with suspicion.** The structured field claims that accompany this
> lens were checked against the panel by a second pass told to refute them. It
> refuted 31 of 158 outright and called 73 more overstated: a 34 percent survival
> rate. The refutations were specific, including wrong medians, backwards sign
> logic and a field key that does not exist. The prose below was written by the
> same pass and has **not** been checked that way. Treat it as a well-informed
> starting point, not as fact, and verify anything a thesis leans on.

## What these businesses actually do

Software and services (Application Software, Systems Software, Internet Services & Infrastructure, IT Consulting & Other Services, Transaction & Payment Processing Services) sells code already written to customers already installed, so the next seat costs nothing. gross_margin runs 70 to 85 percent on subscription and licence revenue and toward 30 percent where the revenue is billed hours or passed-through payment volume. Acquisition cost is expensed up front against five years of payments, so operating_income understates a growing company. Cash precedes recognised revenue, so ocf sits above net_income.

Semiconductors (Semiconductors, Semiconductor Materials & Equipment) holds two models. A fabless designer has software-like gross_margin and an order book that turns inside a quarter. An integrated manufacturer owns a fab, so depreciation is fixed against a variable revenue line and gross_margin swings hundreds of basis points on utilisation alone. Semiconductor Materials & Equipment sells those two their equipment, so its revenue is a customer capital budget, 12 to 18 months out of phase with the chip cycle.

Hardware (Technology Hardware Storage & Peripherals, Communications Equipment, Electronic Equipment & Instruments, Electronic Components, Electronic Manufacturing Services, Technology Distributors) is units times price less a bill of materials, with gross_margin of 8 to 15 percent in Electronic Manufacturing Services and Technology Distributors and 30 to 45 percent behind a brand or a standard.

## What drives the P&L

Software, in order: retention of revenue already sold; gross_margin held while operating expense grows slower than revenue; stock-based compensation, which surfaces only as growth in shares_diluted.

Semiconductors: utilisation, then pricing, both landing in gross_margin before they reach revenue_growth_yoy. A ten percent volume shortfall at an integrated manufacturer takes two to three times that out of operating_income, since depreciation and fab overhead do not move.

Hardware: bill-of-materials cost and mix. With operating_margin of two to four percent in Electronic Manufacturing Services and Technology Distributors, one point of gross_margin is half the profit.

## Fields that mean something here

gross_margin_trend is the highest-value field here. In software it separates product revenue from resold hardware and services labour. In semiconductors it is the best proxy for utilisation and pricing, and it turns before revenue_growth_yoy.

revenue_acceleration means something only against gross_margin_trend: decelerating revenue_growth_yoy with gross_margin_trend flat is maturation, and the same deceleration with gross_margin_trend falling is price competition or an inventory correction.

ttm_fcf, ttm_dep_amort and capex from the reported decade. Capex persistently above ttm_dep_amort where Item 1 describes no fabs means capitalised software or owned data centres, and operating_income is flattered by cost moved to the balance sheet. accruals_ratio should agree; capitalised software and channel inventory are how IT revenue gets ahead of cash.

shares_diluted across the reported decade is the only measurement of stock-based compensation here. Two to four percent annual growth in it is a real claim on ttm_fcf that neither fcf_yield nor eps_growth_yoy charges for.

return_12_2, rel_strength_sp500, high52w_proximity and volume_trend carry more in semiconductors than elsewhere, because prices there turn ahead of reported revenue, and volatility_1y is the sizing input. insider_buyer_count_90d and insider_cluster_score survive; buying is voluntary.

## Fields that mislead here

pe. In software pe is useless wherever ttm_eps_diluted is near zero or negative, and rows written before 2026-09-12 carry a positive pe against a negative ttm_eps_diluted, so check the two together. In semiconductors pe is inverted, lowest at the cycle peak on peak earnings, so a cheap pe is usually a late-cycle one. Repair it by normalising: a mid-cycle operating margin from the decade of reported revenue and operating_income, applied to ttm_revenue.

price_book is useless here and cannot be repaired: equity is the residue of buybacks and acquisition goodwill, driven near zero or negative by years of repurchases, which leaves price_book enormous or undefined.

The panel's ev_ebitda does not reconcile to its own balance sheet, median relative error near ten percent, so recompute it from market_cap, total_debt, cash_and_investments and ttm_ebitda. It adds back ttm_dep_amort, which at a capitaliser is the cost of the product sold, so ev_ebitda flatters exactly the companies accruals_ratio flags.

ev_revenue is the most usable Value field here and is not comparable across the three models: 0.4x on an Electronic Manufacturing Services name and 11x on a Systems Software name say the same thing about different gross margins. Deflate it by gross_margin, or set market_cap plus total_debt less cash_and_investments against ttm_gross_profit.

fcf_yield is structurally high in software and low in semiconductors: ttm_fcf is lifted by deferred revenue growth and carries no charge for equity issued as pay, and at an integrated manufacturer mid-build it can go negative while the business is healthy. Correct with shares_diluted growth and the capex to ttm_dep_amort ratio.

roe_ttm is unusable where equity is small, negative or mostly goodwill, which is most of software. net_debt_ebitda just sorts by cash pile in software and fabless semiconductors, where cash_and_investments exceeds total_debt; it carries signal in Technology Distributors, Electronic Manufacturing Services and leveraged software roll-ups.

earnings_consistency and op_margin_stability penalise semiconductors for being semiconductors: a weak score there confirms the sub_industry and says nothing about quality. In software both are informative. eps_growth_yoy is contaminated by buybacks shrinking shares_diluted and by small-base arithmetic on a near-zero ttm_eps_diluted; prefer revenue_growth_yoy and fcf_growth_yoy.

insider_seller_count_90d and insider_net_buy_90d are noise here: pay is delivered in stock and disposals run on plans. beta_1y is predictively weak, R-squared near 0.13 on a large name. benford_mad tells you nothing.

## The valuation convention

Software is valued on EV to gross profit, or EV to revenue scaled by growth and cash margin. The approximation here is ev_revenue, gross_margin, revenue_growth_yoy and ttm_fcf over ttm_revenue, with shares_diluted growth subtracted from the cash margin.

Semiconductors are valued on mid-cycle earnings power, a multiple on normalised operating income, which the decade of reported revenue and operating_income supports. Semiconductor Materials & Equipment is valued off customer capital budgets, absent here.

Hardware is valued on EV to EBIT: a recomputed ev_ebitda plus operating_margin against its own decade.

## The cycle question

Semiconductors run a three to four year inventory cycle. Late cycle looks like gross_margin_trend rolling over while revenue_growth_yoy is still positive, return_12_2 and high52w_proximity near their highs, pe near its low. Early recovery looks like revenue_acceleration turning positive out of a deeply negative revenue_growth_yoy, with pe high or absent. The reported history has assets and no inventory line, so days of inventory is missing; Item 1A and EX-99.1 text usually describe a correction in words.

Software has a decay curve in place of a cycle: revenue_growth_yoy falls with scale, and the question is whether gross_margin holds. Halved revenue_growth_yoy over four years with flat gross_margin is normal maturation; the same path with gross_margin falling alongside is lost pricing power.

Hardware runs four to five year replacement cycles, visible only as waves in revenue_growth_yoy.

## What this dossier cannot see

Stock-based compensation as a line item, inferable only from shares_diluted growth, which understates it when buybacks offset issuance. R&D spend, absent from the panel and the reported history, so R&D intensity, the central capital allocation decision here, stays qualitative from Item 1. Net revenue retention, billings, remaining performance obligations, deferred revenue balances, seat and subscriber counts. Unit shipments, average selling prices, wafer pricing, node mix, foundry allocation, design wins. Segment revenue as numbers; segment NAMES and Item 1 describe a mixed company without measuring the mix. Hyperscaler capital budgets, which set Semiconductor Materials & Equipment revenue.

## Questions worth asking

1. What has shares_diluted done over the reported decade, and what does fcf_yield become once that dilution is charged against ttm_fcf? Answerable.
2. Is capex above ttm_dep_amort and growing faster than revenue, does the reported ocf to net_income gap widen faster than revenue, and does accruals_ratio agree? Answerable.
3. Where does operating_margin sit in the decade's range of operating_income over revenue, and does that put pe near a peak or a trough? Answerable, and the key calculation for a semiconductor name.
4. Does gross_margin_trend confirm or contradict revenue_acceleration, and does Item 1 point to pricing, mix or utilisation? Partly answerable: direction is data, attribution is text.
5. What share of revenue is recurring subscription, against licence, services or hardware? Unanswerable from segment NAMES and Item 1 alone; flag it.
6. Does Item 1A name dependence on one foundry, one customer or an export licence, and has that language changed since last year? Answerable from text.
7. Was an 8-K EX-99.1 filed last quarter, and what does management say about inventory, bookings or pricing? Conditionally answerable; about half of large filers carry no forward statement.
