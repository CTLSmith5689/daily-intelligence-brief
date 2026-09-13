<!-- Written once, reviewed by hand, and versioned with the code that reads it.
     dossier.py injects this when a company's GICS sector matches the filename.
     Keyed on an exact column, so this is a lookup and not a retrieval problem:
     there is nothing to embed and nothing that can come back wrong. -->

# Communication Services

> **Read this with suspicion.** The structured field claims that accompany this
> lens were checked against the panel by a second pass told to refute them. It
> refuted 31 of 158 outright and called 73 more overstated: a 34 percent survival
> rate. The refutations were specific, including wrong medians, backwards sign
> logic and a field key that does not exist. The prose below was written by the
> same pass and has **not** been checked that way. Treat it as a well-informed
> starting point, not as fact, and verify anything a thesis leans on.

## What these businesses actually do

Communication Services holds four unrelated economic models under one GICS label, and the scoring cohort is the sector, so split it first. Model one is the attention auction: `sub_industry` Interactive Media & Services. Revenue is impressions times a clearing price set in an auction the company runs itself. Marginal cost per impression is near zero, so `gross_margin` runs high and operating leverage is violent both ways. Capex at the largest names went from trivial to a large share of operating cash flow, pulling `ttm_fcf` away from `ttm_net_income`.

Model two is content: Movies & Entertainment and Interactive Home Entertainment. Production spend is capitalized and amortized against the revenue it earns, so the income statement shows an amortization schedule instead of the year's cash, and revenue arrives in lumps set by a release calendar. In a streaming build, cash content spend runs years ahead of amortization, so `ttm_net_income` and reported ocf diverge by construction.

Model three is the network: Cable & Satellite, Integrated Telecommunication Services, Wireless Telecommunication Services, Alternative Carriers. Revenue is subscribers times ARPU, both moving slowly, `ttm_dep_amort` is enormous and non-cash, three to four turns of leverage is standing capital structure, and `equity` is often small after decades of buybacks and impairments. Model four is Advertising, Broadcasting and Publishing: agencies are labor businesses carrying large acquired goodwill in `equity`, broadcasters and publishers carry content cost against cyclical ad revenue plus contracted affiliate and retransmission fees.

## What drives the P&L

Interactive Media & Services: ad price times impression volume, then operating expense discipline, then depreciation from the data-center build, which moves `operating_margin` independently of demand, then legal accruals that hit one quarter and distort `ttm_eps_diluted` and `pe` for four.

Content: release timing, then the amortization schedule on capitalized content, then write-downs of weak titles.

Networks: subscriber net adds times ARPU, then interest expense, a first-order driver at three to four turns, then depreciation of the last build. Weak `ttm_net_income` beside strong `ttm_fcf` is normal.

Advertising, Broadcasting and Publishing: global ad spend, which tracks nominal GDP with amplification, then agency staff cost ratio, plus a political ad cycle lifting Broadcasting in even years.

## Fields that mean something here

`ttm_dep_amort` is the most useful line here: it sizes the wedge between `ttm_net_income` and `ttm_fcf` at networks and content, and says whether a low `pe` is cheapness or amortization.

`ev_ebitda` is the working multiple for Cable & Satellite, the telecom sub-industries, Broadcasting and Advertising. Rebuild it from `market_cap`, `total_debt`, `cash_and_investments` and `ttm_ebitda`; the supplied value does not reconcile to the panel's own balance sheet. `fcf_yield` is the other half. `net_debt_ebitda` informs as a direction: three turns held for a decade is structure, three turns that was one turn two years ago is a thesis.

`revenue_acceleration` is a demand signal at Interactive Media & Services and Advertising, where the auction reprices within weeks. `gross_margin_trend` reads well inside one company. `insider_ownership` flags dual-class founder control at the platforms; `neglect_score`, `analyst_count` and `inst_ownership` vary only in Publishing, Broadcasting and Alternative Carriers. `shares_outstanding` against decade shares_diluted shows how much of `eps_growth_yoy` is buyback. `volatility_1y`, `return_12_2`, `rel_strength_sp500`, `high52w_proximity` and `volume_trend` work within `sub_industry`.

## Fields that mislead here

`price_book` is useless across this sector, failing three ways at once. At Interactive Media & Services the index, social graph and ad stack were expensed as incurred, so `equity` is cash plus goodwill, cut further by buybacks. In telecom `equity` carries spectrum and plant at historical cost. In Broadcasting, Publishing and Advertising `equity` is acquired goodwill impaired repeatedly, so the denominator resets after every bad year. Fixing it would take capitalizing R&D and content and marking spectrum to auction value, which nothing in the panel does. Use rebuilt `ev_ebitda` and `fcf_yield` instead.

`roe_ttm` is misleading wherever `equity` is small, which covers most of the network group and much of legacy media, so a high `roe_ttm` there is a denominator artifact. When `equity` is a small share of `equity` plus `total_debt`, use `ttm_operating_income` over that sum instead.

`pe` needs care twice. Impairments in Broadcasting, Publishing and Movies & Entertainment put a one-year hole in `ttm_net_income`, leaving `pe` absurd for four quarters with no change in the business; check `ttm_operating_income` against the decade series first. Also confirm the sign of `ttm_eps_diluted`, since a positive `pe` has appeared on rows with negative trailing EPS.

`net_debt_ebitda` as a quality penalty inverts the sector, scoring cash-rich platforms as pristine and correctly financed Cable & Satellite and Wireless Telecommunication Services operators as low quality, when contracted subscriber cash flow supports that leverage. `earnings_consistency` and `op_margin_stability` penalize Movies & Entertainment and Interactive Home Entertainment for hit-driven revenue, which is how those businesses work. `accruals_ratio` runs high in content because capitalized content gaps `ttm_net_income` from operating cash flow, so only a move against the company's own history informs.

`revenue_acceleration` is close to meaningless in Movies & Entertainment, where it tracks the release calendar. `gross_margin` is not comparable across the sector: telecom and Cable & Satellite allocate network cost above or below the gross profit line inconsistently, and Advertising reports net of pass-through billings. `fcf_growth_yoy` misleads at the largest platforms, where falling `ttm_fcf` against rising `ttm_revenue` is a capex decision; check capex in the decade series first. `insider_seller_count_90d` and `insider_net_buy_90d` run structurally negative at platforms, where RSU vesting sells shares every quarter, leaving `insider_cluster_score` useful only at smaller Publishing and Broadcasting names. `ev_revenue` is comparable inside one `sub_industry` and meaningless across the sector.

## The valuation convention

Networks are valued on EV/EBITDA and levered free cash flow yield, which rebuilt `ev_ebitda`, `fcf_yield` and `net_debt_ebitda` approximate; the gap is the dividend, which has no field here and is the whole thesis at most telecom names. Interactive Media & Services are valued on cash-adjusted P/E and EV/EBIT with stock compensation charged as expense: `pe` alongside `cash_and_investments` over `market_cap` gets partway, and `ttm_operating_income` over rebuilt enterprise value beats `ev_ebitda`, which excludes stock compensation and the depreciation of the build. Content is valued on normalized slate earnings, which this data cannot produce; the substitute is ten-year mean `operating_margin` on `ttm_revenue`. Conglomerate media is valued by sum of the parts, which cannot be built here because the segment note gives segment names without segment revenue.

## The cycle question

Three cycles run here and they are not synchronized. The ad cycle tracks nominal GDP with amplification and shows first in `revenue_acceleration` at Interactive Media & Services and Advertising; the decade of quarterly revenue lets you test Broadcasting's even-year political pattern. The capex cycle covers fiber and spectrum at the networks and data centers at the platforms, visible as capex over revenue in the annual series. The content cycle shows as the gap between reported net income and ocf, widening in a build and closing in harvest. Rank current `operating_margin` inside the company's own ten-year distribution rather than against the sector cohort. The data cannot say how much of a build remains, or whether ad revenue moved on price or volume, since only the product is reported.

## What this dossier cannot see

Subscriber counts, churn and ARPU, which decide every Cable & Satellite, telecom and streaming outcome. Daily active users, engagement minutes, ad impressions and price per impression, the variables behind every platform revenue line. Segment revenue as numbers, so a studio, a network portfolio and a streaming service cannot be separated inside one filer. Cash content spend and the amortization schedule. Release slates and box office. Dividends and dividend coverage. Stock-based compensation as a line. Outcomes of antitrust proceedings, spectrum auctions and retransmission renewals, which appear in Item 1A as narrative and never as a number.

## Questions worth asking

1. Which of the four models is this, and does the segment note agree with `sub_industry`? Answerable from `sub_industry`, segment names and Item 1.
2. Is the gap between `revenue_growth_yoy` and `eps_growth_yoy` margin or share count? Answerable from the `operating_margin` path against decade shares_diluted.
3. If `fcf_growth_yoy` is negative while `revenue_growth_yoy` is positive, does capex explain it? Answerable from ocf and capex in the annual history.
4. Where does current `operating_margin` sit in this company's ten-year range, and was the low point an impairment? Answerable from the reported annual series.
5. Has `net_debt_ebitda` moved or held? Half answerable: the level is in the panel, `total_debt` history is not in the decade series, so the leverage path is unanswerable.
6. Which Item 1A risks are live proceedings rather than boilerplate? Existence is answerable as narrative, financial effect is unanswerable.
