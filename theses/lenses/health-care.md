<!-- Written once, reviewed by hand, and versioned with the code that reads it.
     dossier.py injects this when a company's GICS sector matches the filename.
     Keyed on an exact column, so this is a lookup and not a retrieval problem:
     there is nothing to embed and nothing that can come back wrong. -->

# Health Care

> **Read this with suspicion.** The structured field claims that accompany this
> lens were checked against the panel by a second pass told to refute them. It
> refuted 31 of 158 outright and called 73 more overstated: a 34 percent survival
> rate. The refutations were specific, including wrong medians, backwards sign
> logic and a field key that does not exist. The prose below was written by the
> same pass and has **not** been checked that way. Treat it as a well-informed
> starting point, not as fact, and verify anything a thesis leans on.

## What these businesses actually do

Health Care holds four economic models. The first is the therapeutics developer: sub_industry Biotechnology, with small names in Medical Devices and Drug Manufacturers - Specialty & Generic. These are option portfolios financed by issuing stock: of 264 Health Care names above $1B market_cap, 38 report ttm_revenue at or below zero, and median gross_margin across the 99 billion-dollar Biotechnology names is 0.00. The asset is a clinical pipeline that never reaches the balance sheet.

The second is the commercial drug, device and tools maker: Pharmaceuticals, Health Care Equipment, Health Care Supplies, Life Sciences Tools & Services, Health Care Technology, at median gross_margin 0.52 to 0.69. Each product sits on its own exclusivity clock, and when that clock runs out revenue steps down toward a generic price over two to three years, on a date known years ahead and invisible in every trailing number.

Third are payors and providers. Managed Health Care and Healthcare Plans price premiums a year ahead and pay claims as they arrive, at median operating_margin 0.03. Health Care Facilities, Medical Care Facilities and Health Care Services carry fixed costs against a negotiated payor mix, levered at median net_debt_ebitda 3.20 to 4.49. Fourth, Health Care Distributors, Medical Distribution and Pharmaceutical Retailers work a penny spread on negative working capital, at median gross_margin 0.04. In all four, a third party pays: price is negotiated with insurers, pharmacy benefit managers and government programs.

## What drives the P&L

In rough order. Loss of exclusivity: one expiry can remove a fifth to a third of a commercial pharma's revenue over two to three years, on a date fixed long in advance. Binary clinical and regulatory events, which for a pre-revenue developer are the whole valuation. Net price, meaning list less rebates to pharmacy benefit managers, 340B and Medicaid; reported revenue is already net, and the gross-to-net gap moves independently of volume. Utilization and mix for payors and facilities. Reimbursement rulings on devices and diagnostics: CMS rates, DRG updates, CPT codes. R&D allocation, since R&D is expensed, so cutting it raises operating_margin now and removes revenue five years out.

## Fields that mean something here

gross_margin and gross_margin_trend carry the most signal for commercial-stage names. A gross_margin of 0.60 to 0.80 is the signature of patent-protected pricing, and a falling gross_margin_trend while revenue_growth_yoy is still positive is the earliest evidence of generic entry or net price erosion.

cash_and_investments with ttm_fcf is the only runway calculation available, and for a developer it is the most decision-relevant number here. Read it beside shares_diluted from the EDGAR decade, where steps mark equity raises. fcf_yield is the honest Value field for the commercial, payor and distribution models; 79 of 264 names carry negative ttm_fcf. net_debt_ebitda works for pharma, facilities and distributors once recomputed.

volatility_1y survives independent verification and splits the models: median 0.60 for billion-dollar Biotechnology against 0.37 for Pharmaceuticals, where above 0.6 usually marks a pending binary event. earnings_consistency and op_margin_stability measure underwriting discipline for payors and facilities. insider_cluster_score and insider_net_buy_90d matter more here than elsewhere, because insiders at a developer have seen unblinded data. Item 1A text names the products facing exclusivity loss.

## Fields that mislead here

pe is useless across most of this sector: it is present on 153 of 264 billion-dollar Health Care names, while 118 of the 264 report ttm_eps_diluted at or below zero. Within Biotechnology it appears on 31 of 99, at p90 108. Rows written before 2026-09-12 can carry a positive pe against negative ttm_eps_diluted. Repairing pe would take consensus forward EPS and a normalized post-cliff earnings base, neither of which exists here.

price_book is useless and often inverted; 36 of 264 report negative equity. Health Care Distributors show median price_book 20.22 and median roe_ttm 1.04 because negative working capital and buybacks drove book equity toward zero, which the Value sleeve scores as expensive. For a developer, book value is mostly cash, so price_book is a multiple of cash. roe_ttm is unusable wherever equity is negative or thin, and for Biotechnology, median -0.26.

ev_ebitda is usually unusable. It does not reconcile to the panel's balance sheet, median relative error near 10%, and 112 of 264 report negative ttm_ebitda, giving a cohort p10 of -7.08. Where ttm_ebitda is positive, R&D sits inside EBITDA, so a company harvesting a mature product and cutting research looks cheaper as it shrinks the pipeline.

ev_revenue spans the sector but is comparable only within sub_industry. Medians run 0.28 for Health Care Distributors, 0.41 for Managed Health Care, 3.42 for Pharmaceuticals, 11.35 for Biotechnology.

net_debt_ebitda misleads for payors: Managed Health Care's median -0.78 reads as net cash, while much of a plan's portfolio is statutory reserve locked at regulated subsidiaries, unavailable to the parent or to debt repayment. For developers it is negative because EBITDA is negative.

revenue_growth_yoy and revenue_acceleration are noise for developers: 38 of 264 have no revenue, and the rest often run on collaboration payments, where a 300% reading is one milestone.

return_12_2, rel_strength_sp500 and high52w_proximity encode binary events that already resolved. Billion-dollar Biotechnology shows return_12_2 p90 of 2.24, a past approval that says nothing about the next one, and a high52w_proximity near zero before a phase 3 result carries as much downside as upside.

accruals_ratio and benford_mad measure nothing without revenue and with few line items. inst_ownership reaches p90 of 1.15 here, so read it ordinally. analyst_count and neglect_score do not mark information advantage: Biotechnology's median analyst_count of 12 matches the sector, and analysts cannot see trial data either.

## The valuation convention

Developers are valued by risk-adjusted net present value: peak sales per program, times probability of regulatory success, discounted at 10% to 12%, less remaining development cost, plus net cash. None of those inputs exist here. The reachable approximation is market_cap less cash_and_investments as the price paid for the programs named in Item 1, against runway from ttm_fcf.

Commercial pharma and devices are valued as a sum of products on their own clocks plus a pipeline, or on normalized post-cliff earnings. pe and ev_ebitda reach the current-year multiple and cannot normalize it. The substitute is the decade of revenue and operating_income from EDGAR, which shows how this company absorbed its last cliff. Payors are valued on forward earnings with a loss ratio assumption, which only an 8-K EX-99.1 can supply. Distributors are valued on EV/EBIT and return on invested capital, where fcf_yield with operating_margin is the closest pair.

## The cycle question

Three clocks run at once. The exclusivity clock is company-specific, and only Item 1 and Item 1A speak to it. The biotech funding cycle is sector-wide, and the decade of shares_diluted from EDGAR reads it: open windows show as repeated issuance, closed windows as flat share counts with falling operating expense. The reimbursement and policy cycle, driven by CMS rates and drug pricing legislation, is absent from the panel and surfaces in Item 1A once a year. The payor utilization cycle runs two to three years and shows only after the fact, as compression in operating_margin and op_margin_stability.

## What this dossier cannot see

Clinical trial status, readout dates, endpoints, enrollment, data monitoring committee actions. Patent expiry dates per product and settlements with generic filers. Product-level revenue: the segment note gives NAMES only, so the largest drug cannot be sized. Gross-to-net detail: PBM rebates, 340B, Medicaid pricing. Formulary placement and coverage decisions. Medical loss ratio, membership, prior-period development, star ratings. CMS rates, DRG updates, CPT assignment. Litigation reserves, FDA inspection status. Unit volumes and selling prices. Consensus estimates and guidance, so the expected post-cliff earnings base is unknown.

## Questions worth asking

1. What share of current revenue sits on products losing exclusivity within five years? Not in the panel. Item 1 and Item 1A name the products, sometimes the year, never the revenue.
2. How many quarters does cash_and_investments buy at the burn in ttm_fcf, and does shares_diluted over the EDGAR decade show dilution at any price? Both answerable.
3. In the EDGAR decade, did revenue and operating_income fall through a prior cliff, and how long did revenue take to regain its peak? Answerable, and the only base rate here.
4. Do op_margin_stability and earnings_consistency show a payor underwriting the last two years correctly, and did the latest 8-K EX-99.1 state a medical loss ratio? The panel answers the first, the exhibit the second only if filed.
5. Recomputed from total_debt, cash_and_investments and ttm_ebitda, does net_debt_ebitda leave room for a reimbursement cut? The arithmetic is answerable, the size of the cut is not.
6. Do insider_cluster_score and insider_net_buy_90d show clustered buying near a step in shares_diluted? Answerable, and one of the few forward-looking signals for a developer.
