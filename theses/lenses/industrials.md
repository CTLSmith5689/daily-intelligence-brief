<!-- Written once, reviewed by hand, and versioned with the code that reads it.
     dossier.py injects this when a company's GICS sector matches the filename.
     Keyed on an exact column, so this is a lookup and not a retrieval problem:
     there is nothing to embed and nothing that can come back wrong. -->

# Industrials

> **Read this with suspicion.** The structured field claims that accompany this
> lens were checked against the panel by a second pass told to refute them. It
> refuted 31 of 158 outright and called 73 more overstated: a 34 percent survival
> rate. The refutations were specific, including wrong medians, backwards sign
> logic and a field key that does not exist. The prose below was written by the
> same pass and has **not** been checked that way. Treat it as a well-informed
> starting point, not as fact, and verify anything a thesis leans on.

## What these businesses actually do

Industrials holds five unrelated economic models, so `sub_industry` gates everything below.

Aerospace & Defense, Industrial Machinery & Supplies & Components, Construction Machinery & Heavy Transportation Equipment, Agricultural & Farm Machinery, Electrical Components & Equipment, Building Products and Industrial Conglomerates sell durable equipment at thin margin, then earn for twenty to forty years on parts, overhauls and service at two to three times the OE margin. The economic asset is the installed base, which appears in no field we hold, and revenue blends a violent OE line with a steady aftermarket one.

Rail Transportation, Passenger Airlines, Marine Transportation, Cargo Ground Transportation, Air Freight & Logistics and Marine Ports & Services run physical networks with no backlog: volume times price against a fixed cost base, decrementals as extreme as incrementals, and `equity` that approximates a depreciating asset base. Human Resource & Employment Services, Research & Consulting Services, Environmental & Facilities Services, Security & Alarm Services and Professional Services sell hours, with near-zero capital intensity, headcount as cost of sales, and in staffing a top line that is mostly pass-through wages. Trading Companies & Distributors turns on inventory position and price-cost timing. Construction & Engineering recognises revenue on percentage of completion, so profit stays an estimate until a project closes.

## What drives the P&L

Volume against a fixed cost base comes first: incremental margins of 30 to 40 percent up, similar decrementals down, explain more of `eps_growth_yoy` than anything management chose. Mix is second. `revenue_growth_yoy` falling while `gross_margin_trend` rises usually means OE deferral with the service line intact; the reverse means an OE ramp at low initial margin, or price lagging input cost. Price against input cost is third, with a two to four quarter lag in both directions. Fourth is contract execution in Aerospace & Defense and Construction & Engineering, where one estimate-at-completion revision can consume a year of segment profit with no warning in any ratio. Fifth is capital allocation: share count, and at the large machinery names a captive finance arm inside `total_debt`. In transport, fuel and labour rank second.

## Fields that mean something here

`gross_margin_trend` is the most useful field here, the only proxy for aftermarket versus OE mix, and must be read against `revenue_growth_yoy`. `revenue_acceleration` is the nearest thing to book-to-bill, one delivery cycle downstream: prompt for staffing and distributors, while for Aerospace & Defense it reflects orders taken one to three years ago.

`net_debt_ebitda` has a cyclical denominator: 2.5x on peak EBITDA is 5x on trough EBITDA, and covenants bind at the trough. `ttm_fcf` against `ttm_net_income` is the working capital tell: upturns absorb cash into inventory and receivables, downturns release it, so `fcf_growth_yoy` looks strongest near the bottom. In Construction & Engineering, poor `accruals_ratio` with rising `revenue_growth_yoy` is the standard precursor to a contract charge.

`insider_cluster_score` and `insider_buyer_count_90d` carry unusual weight because management sees the order book first, and cluster buying with `operating_margin` near its ten-year low end is one of the few forward-looking inputs here. `return_12_2` and `rel_strength_sp500` describe macro exposure more than company performance. `price_book` is meaningful in the network group, where `equity` tracks a depreciated asset base.

## Fields that mislead here

`pe` is the most dangerous field here. Earnings peak before prices do, so `pe` prints lowest at the top of the cycle and highest at the bottom. A machinery or transport name at `pe` near 8 with `high52w_proximity` near zero is a peak-earnings configuration. The repair: from the decade of reported revenue and `operating_income` in EDGAR, form a mid-cycle operating margin, apply it to `ttm_revenue`, and divide `market_cap` by the result.

`fcf_yield` inverts: it peaks when working capital liquidates, which is the trough, so a high `fcf_yield` with negative `revenue_growth_yoy` is evidence of contraction.

`roe_ttm` is useless across most of this sector: buybacks have left `equity` small or negative at several large industrials, and at the services acquirers it is mostly purchase accounting. Use `ttm_operating_income` over (`total_debt` plus `equity` minus `cash_and_investments`).

`price_book` is useless for the services group: no asset base, and the reading is a goodwill and buyback artifact that nothing we hold replaces. `ev_revenue` is meaningless for Human Resource & Employment Services, where revenue is largely pass-through wages billed at a spread. Use enterprise value over `ttm_gross_profit`.

`ev_ebitda` needs two repairs. It does not reconcile to this panel's own balance sheet, so recompute it from `market_cap`, `total_debt`, `cash_and_investments` and `ttm_ebitda`. At Construction & Engineering names much of `cash_and_investments` is customer money under billings in excess of costs, so netting it against debt flatters enterprise value. `net_debt_ebitda` is not peer-comparable where the segment names include a financing segment: that captive book funds customer receivables and inflates `total_debt`.

`op_margin_stability` penalises the operating leverage that defines these businesses, so a manufacturer scoring badly on it is behaving normally. Keep it out of any quality ranking. `earnings_consistency` reflects which ten years the window covered. `gross_margin` as a level is not comparable across this sector: service and distribution costs land in cost of sales at some filers and in SG&A at others, though `gross_margin_trend` within one company survives that.

## The valuation convention

Practitioners apply EV/EBITDA to mid-cycle EBITDA, using trailing EBITDA only as an input, and check free cash flow conversion across a cycle against a 100 percent standard. Aerospace & Defense adds backlog coverage in years of revenue. Rails are judged on operating ratio, airlines on EV/EBITDAR and `price_book`, staffing on enterprise value over gross profit. Our fields reach most of that: a recomputed `ev_ebitda`, a normalised `pe` from reported `operating_income`, enterprise value over `ttm_gross_profit`, `price_book` for transports, and `ttm_fcf` against `ttm_net_income` for conversion. The mid-cycle margin takes a judgment call: the ten-year window holds the 2020 collapse and the 2021 to 2022 price surge, and a mechanical average of `operating_margin` inherits both. State that assumption in the note.

## The cycle question

The sector runs two clocks. Short-cycle businesses turn first: Human Resource & Employment Services, Trading Companies & Distributors, Cargo Ground Transportation, smaller machinery. Long-cycle businesses lag them by four to eight quarters: Aerospace & Defense, Construction & Engineering, Heavy Electrical Equipment. One cycle call across both groups will be wrong for one of them.

The decade of reported history answers this best: place current `operating_margin` inside its own ten-year range. Top decile with a low `pe` and `high52w_proximity` near zero is late-cycle. Bottom decile with a high `pe`, weak `earnings_consistency` and rising `insider_cluster_score` is the trough. `revenue_acceleration` turning negative while `gross_margin_trend` holds positive is early rollover: OE fading, aftermarket carrying. The data cannot date the macro cycle: no ISM, no order index, no rates beyond a risk-free proxy, no commodity series.

## What this dossier cannot see

Backlog as a number or series, book-to-bill, orders, cancellation rates. Item 1 of the 10-K often states a backlog figure with the prior year beside it, giving one change and no trend, and many filers state none.

The aftermarket and original equipment split, since only segment names are collected. Installed base size and fleet age. Fixed-price versus cost-plus mix and estimate-at-completion revisions. Dealer and channel inventory, which separates shipments from retail demand. Freight rates, load factors, operating ratio. Steel, copper and fuel costs. Union contract dates, headcount, and the bill-to-pay spread that is the economics of staffing. Tariff exposure by origin. Pension and lease obligations sitting outside `total_debt`.

## Questions worth asking

1. Where does current `operating_margin` sit inside its own ten-year reported range, and is `pe` low because the numerator is high? Answerable from EDGAR history and `market_cap`.
2. Is `gross_margin_trend` rising while `revenue_growth_yoy` falls? Answerable. The OE and service split itself is not.
3. Does Item 1 state a backlog figure with the prior year beside it? Answerable from filing text only, and if none is disclosed, flag book-to-bill unanswerable.
4. Do the segment names include a financing segment? Answerable from the segment note, and it decides whether `net_debt_ebitda` is peer-comparable.
5. Is `ttm_fcf` below `ttm_net_income` with `revenue_growth_yoy` positive, or above it with `revenue_growth_yoy` negative? Answerable, and it says which side of the working capital cycle `fcf_yield` sits on.
6. Has capex run below `ttm_dep_amort` for three or more consecutive years, and how much of `eps_growth_yoy` came from `shares_diluted` decline? Both answerable from the reported decade.
7. Do Item 1A risk factors name a fixed-price programme or contract loss provision while `accruals_ratio` sits at the poor end of its range? Answerable as a flag; the size of any revision is not.
