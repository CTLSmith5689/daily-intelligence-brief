<!-- Written once, reviewed by hand, and versioned with the code that reads it.
     dossier.py injects this when a company's GICS sector matches the filename.
     Keyed on an exact column, so this is a lookup and not a retrieval problem:
     there is nothing to embed and nothing that can come back wrong. -->

# Energy

## What these businesses actually do

Upstream producers sell an undifferentiated barrel or mcf at a price set elsewhere: revenue is volume times realized price, and that price is exogenous. Cash costs are near fixed short term while depletion is a large non-cash charge, so operating leverage is extreme in both directions and the same company prints a record year and a loss two years apart. Reserves deplete, so output falls without continuous spending, which makes capex both maintenance and growth. The `sub_industry` values here are `Oil & Gas Exploration & Production` and `Oil & Gas E&P`, one cohort under two labels, plus `Oil & Gas Integrated` and `Integrated Oil & Gas`, which add downstream as a partial hedge.

The second group earns a spread or a toll. `Oil & Gas Refining & Marketing` buys crude and sells products, so revenue is mostly a pass-through of the crude price and the business is the few points in between: cohort median `gross_margin` above $1B is 0.13 for refiners against 0.71 for E&P, and a 200 basis point move is the whole earnings year. `Oil & Gas Midstream` and `Oil & Gas Storage & Transportation` are again one business under two labels: fee-based transport and storage, debt financed, several of them partnerships, earnings suppressed by heavy depreciation.

The third group sells activity. `Oil & Gas Equipment & Services` and `Oil & Gas Drilling` draw revenue from upstream capital budgets with a two to four quarter lag. `Coal & Consumable Fuels`, `Thermal Coal` and `Uranium` sell on term contracts that decouple revenue from spot; several `Uranium` names are development stage, at a cohort median `gross_margin` of -0.45, leaving the Value sleeve undefined.

## What drives the P&L

The realized commodity price, which for an E&P explains most of the variance in `ttm_operating_income`. Then volumes: production and decline upstream, throughput at a refinery, rig activity in services. Then spread capture at refiners. Then operating leverage against the fixed cost base, which moves `operating_margin` by thousands of basis points across a cycle. Then capital allocation, the share of operating cash flow returned rather than reinvested, visible in the EDGAR capex history. Last, non-cash items: impairments, hedge marks and retirement obligation revisions move `ttm_net_income` and `ttm_eps_diluted` with no cash consequence, corrupting every field built on reported earnings. The first three are absent from this dossier entirely.

## Fields that mean something here

`ttm_fcf` and `fcf_yield` are the most honest valuation inputs here, because free cash flow nets the capital intensity depletion accounting obscures. Read them against the company's own decade of ocf and capex.

`market_cap`, `total_debt`, `cash_and_investments` and `ttm_ebitda` matter because they let you rebuild enterprise value yourself. `operating_margin` and `ttm_operating_income` are the cleanest cycle locators, sitting above the impairment and hedge noise; read them positionally, as where today's figure falls in the ten-year reported range. `net_debt_ebitda` is the survival test, read against the worst EBITDA year in the reported decade rather than against `ttm_ebitda`, which flatters leverage at the top. `volatility_1y` is reliable here and is the right sizing input.

`return_12_2` and `rel_strength_sp500` carry real signal, because commodity prices trend and the equities track them; pair them with `volume_trend`. `insider_buyer_count_90d`, `insider_net_buy_90d` and `insider_cluster_score` are unusually informative, since insiders see realized prices and the hedge book long before outside data. `neglect_score` and `analyst_count` deteriorate into a trough.

## Fields that mislead here

`pe` is useless in this sector. It divides today's price by peak or trough earnings, so it reads lowest at the top and highest or undefined at the bottom. Fourteen of 94 Energy names above $1B have no `pe` at all, and the panel has carried a positive `pe` against a negative `ttm_eps_diluted`, so check the two together before quoting either. In partnerships, depreciation alone makes `pe` meaningless.

`price_book` is actively misleading. Book equity is historical property cost net of depletion and past impairments, which land at the trough. A writedown cuts `equity`, raising `price_book`, so the stock looks more expensive at maximum pessimism. Full cost and successful efforts accounting give different book values for identical assets, and partnership `equity` can be negative.

`ev_revenue` is useless here. Refiners show a cohort median of 0.73 because revenue carries the pass-through cost of crude, while `Oil & Gas Storage & Transportation` shows 5.76 on fee revenue. That 8x spread is business model alone.

`ev_ebitda` is the right concept attached to an unreliable number: it does not reconcile to the panel's own balance sheet, median relative error near 10% and p90 above 50%. Recompute it as (`market_cap` + `total_debt` - `cash_and_investments`) / `ttm_ebitda`. Recomputed, it is still a peak-cycle multiple with the same trap as `pe`.

`roe_ttm` compounds two errors in one direction, peak earnings over impaired book. Fourteen of 94 names show a negative `roe_ttm`, a reading on the commodity.

`earnings_consistency` and `op_margin_stability` are cyclicality detectors mislabelled as quality. Energy median `earnings_consistency` is 0.49, and a name scoring well on both is almost certainly a fee-based midstream: these fields sort by contract structure.

`accruals_ratio` carries no information inside this sector. Depletion pushes reported net income far below operating cash flow for nearly every name; cohort p90 is -0.01, so everyone scores clean by construction.

`eps_growth_yoy` and `revenue_acceleration` are base effects off the commodity; the cohort spans -0.69 at p10 to +2.98 at p90 on `eps_growth_yoy`, and growth of 200% beside a `pe` of 7 is the signature of a cycle peak. `gross_margin_trend` is not comparable across these sub-industries given the 0.13 to 0.71 spread in `gross_margin`, and `revenue_growth_yoy` at a refiner reads as a crude price index.

`beta_1y` is useless here; the cohort median is -0.11, because these equities price off their commodity. `high52w_proximity` needs care, since the whole cohort moves toward and away from its highs together. `sub_industry` needs care as a key: given the four duplicate label pairs above, a peer screen on one spelling drops half the cohort.

## The valuation convention

E&P is valued on net asset value, the present value of proved and risked unproved reserves at a stated price deck, cross-checked against EV/EBITDAX. Refiners go on mid-cycle EV/EBITDA and replacement cost per complexity-adjusted barrel, midstream on EV/EBITDA and distributable cash flow yield, services and drillers on peak and trough EV/EBITDA.

Our data reaches one of these. Build enterprise value from `market_cap`, `total_debt` and `cash_and_investments`, divide by `ttm_ebitda`, then repeat on the worst EBITDA year in the EDGAR history. Then normalise: take the mid-cycle operating margin from a decade of reported revenue and operating income, apply it to `ttm_revenue`, compare that to `market_cap`. Reserves, PV-10 and price decks are unreachable, so no NAV is possible.

## The cycle question

The panel locates the company inside its own history. Put `operating_margin`, `ttm_operating_income`, `ttm_fcf` and `revenue_growth_yoy` into percentiles of the decade of reported annual figures. A top-decile margin beside a bottom-decile recomputed EV/EBITDA is a peak signature. Capex direction over the last eight reported quarters says what management believes.

The panel cannot locate the commodity: no price series, no forward curve, no inventories, no production data. A cycle call here is conditional by construction. State the commodity assumption explicitly, give the margin it implies, and anchor the falsifier to something the company reports, such as a capex line in the next 8-K EX-99.1.

## What this dossier cannot see

Proved reserves and PV-10, finding and development cost, decline rates, realized price per boe, differentials to benchmark, the hedge book and its strikes, crack spreads and capture rate, turnaround schedules, utilization, rig counts and day rates, take-or-pay coverage, distribution coverage, uranium contract prices, OPEC+ policy, storage inventories, segment revenue as numbers, any commodity price series, covenant headroom, consensus estimates and guidance as data.

## Questions worth asking

1. Where does today's `operating_margin` sit in the ten-year range of reported annual operating margins, and what is the decade median? Answerable.
2. Over the last eight reported quarters, what share of operating cash flow went to capex? Answerable.
3. How far does a recomputed EV/EBITDA sit from the panel's `ev_ebitda`, and what is it on the worst EBITDA year in the decade? Answerable; the trough version decides solvency.
4. Does the reported equity series show a step down consistent with an impairment, and when? Answerable, and it says whether `price_book` measures written-down assets.
5. Does the 8-K EX-99.1, where filed, state realized prices, volumes or hedge positions? Answerable only when the exhibit exists.
6. Which Item 1A risk is new against the prior 10-K: price, capital access, or regulation? Answerable from the filing text.
7. What realized commodity price is embedded in the current `ttm_ebitda`, and what would EBITDA be 30% below it? Unanswerable here; a thesis needing it must carry the assumption on its face.
