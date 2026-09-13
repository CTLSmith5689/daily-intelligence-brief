<!-- Written once, reviewed by hand, and versioned with the code that reads it.
     dossier.py injects this when a company's GICS sector matches the filename.
     Keyed on an exact column, so this is a lookup and not a retrieval problem:
     there is nothing to embed and nothing that can come back wrong. -->

# Financials

> **Read this with suspicion.** The structured field claims that accompany this
> lens were checked against the panel by a second pass told to refute them. It
> refuted 31 of 158 outright and called 73 more overstated: a 34 percent survival
> rate. The refutations were specific, including wrong medians, backwards sign
> logic and a field key that does not exist. The prose below was written by the
> same pass and has **not** been checked that way. Treat it as a well-informed
> starting point, not as fact, and verify anything a thesis leans on.

## What these businesses actually do

Balance-sheet lenders buy funding and sell credit. The balance sheet is the production line, so its size, mix and duration are operating decisions. Economics are asset yield less funding cost, less credit losses, less expense, on a regulator-set capital base. Sub_industry: Diversified Banks, Regional Banks, Banks - Regional, Consumer Finance, Credit Services, Mortgage Finance, Mortgage REITs.

Risk underwriters take premium now and pay claims later, investing the float between, and reported earnings rest on loss reserves, which are management estimates revised for years. Sub_industry: Property & Casualty Insurance, Life & Health Insurance, Multi-line Insurance, Reinsurance, plus their Insurance - X duplicates.

Fee businesses earn a rate on other people's money with little credit risk: Asset Management, Asset Management & Custody Banks, Capital Markets, Investment Banking & Brokerage, Financial Exchanges & Data. Transaction & Payment Processing Services are toll roads with software economics (V: operating_margin 0.6112). 150 of 731 Financials rows are Shell Companies, and sub_industry is doubly spelled (Banks - Regional 117, Regional Banks 90), so string cohorts split peer groups.

## What drives the P&L

Lenders, in order: net interest margin and the repricing lag between assets and funding, loan growth, the provision for credit losses, fee income, expense. Reserve build and release move quarterly EPS more than anything operational, and none of those five is a field here.

Insurers: loss ratio, expense ratio, prior-year reserve development, investment income on float, and for life companies the mortality, lapse and discount-rate assumptions. Fee businesses: asset levels, which are market level times net flows, then fee rate, then compensation ratio, so revenue_growth_yoy partly measures the market.

## Fields that mean something here

price_book is the primary multiple. Here it is exactly market_cap over equity, verified at relative error 0.0000 across 205 gated Financials names, sector median 1.914 against 3.015 universe-wide. roe_ttm is the most informative field, median 0.139. Read them as a pair: price to book equals price to earnings times return on equity.

pe comes from price and ttm_eps_diluted, so it escapes the share-count problem below; sector median 14.36 against 22.75. eps_growth_yoy beats revenue_growth_yoy because ttm_eps_diluted is reported directly.

return_12_2, return_1m, high52w_proximity, rel_strength_sp500 and volume_trend are price-derived and work normally. volatility_1y survives independent checking, sector median 0.284 against 0.381; beta_1y is predictively weak and insurers print negatives (PGR -0.4965). insider_cluster_score and insider_buyer_count_90d cover 87 of 216 gated names at a median insider_net_buy_90d of -1.09M, so only clustered buying informs. Item 1A and the segment NAMES are the highest-value inputs here, disclosing concentration the aggregates hide.

## Fields that mislead here

gross_margin, gross_margin_trend and ttm_gross_profit are useless for lenders. 52 of 63 gated bank names carry gross_margin exactly 0.0, the exceptions scattering from -0.484 to 1.0 on filer tagging. gross_margin_trend fills for 1 of 63 banks. Nothing to fix: lending and underwriting have no cost of goods sold.

ev_ebitda, ttm_ebitda and net_debt_ebitda are meaningless here: enterprise value is undefined for a depository, since total_debt is funding raw material. Each fills for 1 of 63 gated banks. For insurers the failure is quieter: ev_ebitda fills for 54 of 62 while ttm_ebitda fills for 18, so the ratio has no stored denominator to audit, and panel-wide ev_ebitda carries median 9.8% and p90 58% error. net_debt_ebitda excludes policy reserves, the actual obligation, so AIG prints -0.1679 and reads as net cash. Repair needs reserves and regulatory capital, which no listed source carries.

fcf_yield, ttm_fcf and fcf_growth_yoy are noise for lenders. JPM's reported operating cash flow runs -79.9B in 2020, +107.1B in 2022 and -147.8B in 2025 while net income rose steadily from 21.7B to 57.0B, because loan origination, trading inventory and derivative collateral cross the operating line. COF prints fcf_yield 0.208 because the provision for credit losses is a non-cash addback, so fcf_yield improves mechanically as credit deteriorates. A fix needs distributable earnings, which needs capital ratios we do not have.

ev_revenue is useless. Rebuilt from market_cap, total_debt, cash_and_investments and ttm_revenue it misses by a median 13% across gated Financials and collapses for banks: LKFN reports 5.02 against 437.7 rebuilt.

revenue_growth_yoy and revenue_acceleration need checking before use: revenue for a bank is whichever XBRL tag got selected. JPM's ttm_revenue is 95,112,000,000: exactly its FY2014 reported revenue, 48% below FY2025's 182,447,000,000, while its ttm_net_income, ttm_eps_diluted and equity are current.

accruals_ratio inverts here. It is net income less operating cash flow over assets, so JPM's 0.0476 is just (65.0B plus 147.8B) over 4,424.9B of assets: earnings quality graded on trading flow.

earnings_consistency runs high by construction, sector median 0.770 against 0.642, because provisioning and reserve estimates smooth reported earnings. A high reading is weak evidence; a low one (COF 0.3245) is informative. op_margin_stability fills for 2 of 63 banks, and operating_margin divides a pre-tax line by that suspect revenue.

price_book carries a share-count risk. market_cap is price times shares_outstanding, which is missing or zero on 1,488 rows universe-wide and understated for multi-class registrants: CME prints price_book 0.6903 on 66.6M shares. Cross-check with pe times roe_ttm, built only from per-share and reported inputs. Against price_book that ratio has a median of 1.08 here and reaches 4.2x on CME, 6.1x on MA, 33.6x on IBKR. Where it departs far from 1, price_book is wrong and pe is right.

## The valuation convention

Banks and insurers trade on price to book against return on equity, plus price to earnings on normalized earnings, with excess-return models underneath. The practitioner plots price_book against roe_ttm inside a peer cohort and asks whether the spread is justified. Our fields reach that through price_book, roe_ttm, pe and eps_growth_yoy, plus reported net_income, equity and shares_diluted from EDGAR, which give a ROE history and a book value per share series. They cannot reach normalization, which needs a through-cycle loss rate. Fee businesses and networks value on earnings and free cash flow, and both work.

## The cycle question

Late in a credit cycle, reserve releases flatter EPS, roe_ttm sits near a decade high and price_book has expanded. After losses land, provisions spike, roe_ttm collapses and price_book falls under 1.0. The one genuine cycle read is positional: build a ten-year ROE series from reported net_income and equity in EDGAR, and locate roe_ttm in its own range. return_12_2 and rel_strength_sp500 then say whether a rerating happened.

The data cannot say whether current earnings are pre-loss or post-loss. No provision expense, allowance ratio, charge-off rate, loss ratio or reserve development exists in any listed source, and the only rate series is a risk-free proxy, so the curve behind net interest margin is invisible.

## What this dossier cannot see

For lenders: net interest margin and its direction, deposit beta and non-interest-bearing mix, loan concentration including commercial real estate, allowance for credit losses over loans, net charge-offs, non-performing assets, provision expense, CET1 ratios, AOCI and held-to-maturity marks. For insurers: combined ratio, loss ratio, expense ratio, reserve development, premium rate change, catastrophe exposure, reinsurance program. For fee businesses: AUM, net flows, fee rate. For networks: payment volume. Across all: consensus estimates, guidance as data, segment revenue as numbers, book value per share as a series.

Inside EDGAR, JPM's decade carries blank gross_profit, operating_income and capex every year, so a lender's usable history is net_income, eps_diluted, assets, equity and shares_diluted.

## Questions worth asking

1. Does ttm_revenue reconcile to the latest reported annual revenue in EDGAR? Answerable. If not, drop revenue_growth_yoy, revenue_acceleration and ev_revenue, and say so.

2. Does pe times roe_ttm land within roughly 20% of price_book? Answerable. If not, shares_outstanding is wrong and book value per share must come from EDGAR equity over shares_diluted.

3. Where does roe_ttm sit inside the ten-year ROE range built from reported net_income and equity? Answerable from EDGAR.

4. What cost of equity does price_book imply given roe_ttm? The arithmetic is answerable. Whether that gap is a credit view or a franchise view is not, and must come from Item 1A.

5. How much of eps_growth_yoy came from the share count? Compare eps_diluted growth to net_income growth in EDGAR. Material below a price_book of 1.0.

6. What concentration do the segment NAMES and Item 1A disclose that the aggregates hide: commercial real estate, one insurance line, one asset class? Answerable from filings.

7. Is there clustered insider buying while price_book sits below 1.0? Answerable for the 87 of 216 gated names carrying insider_cluster_score.

8. Is the reserve or provision line building or releasing? Unanswerable, and flag it, because accruals_ratio and fcf_yield will both supply a confident wrong answer.
