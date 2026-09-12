# Daily Intelligence Brief

A data aggregation pipeline for US equities and finance/tech news. Runs on GitHub
Actions, writes append-only CSVs, and publishes a browsable site to GitHub Pages.

**No LLM, and no AI API key.** That was removed on 2026-09-04 with the logic that 
the data can be gathered here, and a scheduled agent on a personal plan with claude
can pull the data to run whatever analysis you want from there.

## Where the idea came from

I have always wanted a window into the investment research analyst world; It is an area
of finance that I have become more and more interested in, but I only have a basic 
understanding of it. I built this tool primarily as a way to expose myself to the data and 
information research analysts look at, with the ultimate goal of being able to understand more
about the profession. 

## The data

The CSVs under `data/` are the point of the project. Everything under `docs/` is a
view rebuilt from scratch each day and safe to regard as disposable.

| File | Cadence | Grain |
|---|---|---|
| `data/quotes.csv` | hourly | one row per quote per observation |
| `data/headlines/YYYY-MM.csv` | hourly | one row per article, deduped on link |
| `data/fundamentals/YYYY-MM.csv` | trading days | one row per ticker per day, ~50 columns |
| `data/tickers.csv` | daily | every ticker ever seen, with first/last seen and status |
| `data/filings/YYYY-MM.csv` | daily | one row per earnings release, deduped on accession |
| `data/filings/text/{TICKER}/` | daily | the release text, one file per filing |

`filings` is the only qualitative series here. When a company reports, it files
an 8-K carrying item 2.02, Results of Operations and Financial Condition, with
the press release attached as exhibit EX-99.1. That exhibit is what the panel
cannot express: the quarter in management's own words, and sometimes next
quarter's guidance.

Measured on 20 large caps before this was built, the exhibit was retrievable for
20 of 20 and 9 of 20 carried a real guidance section. So it is dependable for
reported results and framing, and a coin flip for guidance. Nothing reading this
series should assume guidance is present. Apple, for one, never puts it there.

Discovery runs off EDGAR's daily index rather than per-issuer submissions. One
request lists every filing EDGAR received that day, so a run costs one index
fetch plus two requests per new earnings filing, instead of one request per
ticker. Across ~5,300 tickers that is the difference between about forty seconds
and half an hour. Two details are load-bearing and easy to get wrong: form.idx
is fixed-width, not pipe-delimited like master.idx, and the filing header page
names items by description ("Results of Operations and Financial Condition")
and never by number, so matching on the string "2.02" finds nothing at all.

The window is four days, so a weekend, a holiday or a dropped cron slot is
caught up rather than lost. Accessions already recorded are skipped before any
request is made, which is what makes re-running cheap.

`headlines` dedupes on link across the month, so `first_seen` is genuinely the
first time the pipeline saw an article, not the most recent hour it was still on
the feed.

`fundamentals` is a panel: `date` + `ticker` is the key. It carries
`edgar_updated` and `insider_updated` so you can tell how stale the slow-moving
columns are (see cadence note below). Float columns are rounded to 6 decimals,
which removes binary-float noise like `0.22199999999999998`.

```python
import pandas as pd, glob
df = pd.concat(pd.read_csv(f) for f in glob.glob("data/fundamentals/*.csv"))
```

### Survivorship bias

Index membership churns: names get acquired, delisted, or demoted out of the S&P
indices. The universe is rebuilt from Wikipedia each day, so a removed ticker would
simply stop appearing, and a panel containing only the survivors overstates returns.

`data/tickers.csv` is therefore append-only. Nothing is ever deleted from it. A
ticker that leaves is marked `status=dropped` with the `dropped_on` date, and is
*still collected* for `RETAIN_DROPPED_DAYS` (400) afterwards so its post-removal
history exists. Those rows carry `in_index=0`; current members carry `in_index=1`.

```python
panel = pd.read_csv("data/fundamentals/2026-09.csv")
indexed_only = panel[panel.in_index == 1]   # opt IN to survivorship bias, knowingly
```

Rows are only written on days the market traded. The daily run fires after the
close, so without that guard a weekend run would stamp Friday's closing prices with
Saturday's date. Exchange holidays are not detected and will repeat the prior
close; `last_updated` identifies them.

### Schema changes

Adding a field appends a column to the current month's CSV and back-fills existing
rows as empty, rather than silently dropping it (`csv.DictWriter` is configured with
`extrasaction="ignore"`, so a new field would otherwise vanish with no error).

### Universe

~5,300 US-listed operating companies, from two sources merged in this order:

1. **Wikipedia** S&P 500/400/600 (~1,500). First, because their GICS sector
   classification is cleaner and should win on any overlapping ticker.
2. **NASDAQ Trader symbol directory** (~3,800 more). Plain pipe-delimited files,
   no API key, no quota, regenerated each business day. ETFs, warrants, units,
   rights, preferreds, test issues and financially deficient listings are filtered
   out; only operating-company common stock is kept.

These files carry no sector, so `enrich_with_yfinance` supplies it for the non-S&P
names. This replaced an iShares Russell holdings feed that died: it began answering
HTTP 200 with `Content-Type: text/csv` and an HTML body, which went unnoticed for
two months. Both sources now assert the body is not HTML before parsing.

A partial scrape is more dangerous than a total one, because it looks fine. If one
source fails, the universe shrinks and every missing name would be marked as having
left the index. The build therefore aborts to the previous day's cache if the scrape
returns under 90% of the previously-active count.

Rows with neither a price nor a market cap are not written. Broadening to every US
listing brings in many names yfinance has nothing for, and a line of commas is noise.

### What is archived, and what deliberately is not

Two different things travel under the name "news" here.

`data/headlines/` is the general feed archive: section-level stories from the RSS
set (Finance & Markets, AI & Technology, International, and so on). It carries no
ticker column because these stories are not about particular companies, and a
column that would be empty on every row is worse than no column.

Per-ticker headlines are a different thing. They are fetched per company, scored
for sentiment, and cached in `docs/news/{TICKER}.json` on a 12-hour refresh. What
is archived from them is the **derived** values, in the panel, per ticker per day:
`news_count_7d`, `news_lm_avg`, `news_vader_avg`, `neglect_score`. The panel is
self-contained for those columns.

The headlines themselves are not archived, on purpose. The current cache is 81,385
headlines, about 34 MB as CSV, and it turns over every twelve hours; keeping the
raw text would add well over 100 MB a month to a repository whose entire product
is presently under 5 MB. That is the same trade that made `docs/` 161 MB, and it
would buy auditability of a sentiment score rather than any new measurement.

The consequence is worth being explicit about: you can see what a company's tone
score was on a given day, and you cannot go back and see which headlines produced
it.

### Refresh cadence is deliberately split

Different sources move at different speeds, so they are gated separately:

- **yfinance** (price, market cap, P/E, momentum, quality factors): **daily**. This
  is what actually changes, and the panel needs real daily rows.
- **EDGAR filings** and **insider Form 4**: **weekly**. Filings are quarterly and
  Form 4s are sparse; pulling them daily would hammer SEC for no new information.
  Those columns therefore repeat within a week, which is why the `*_updated`
  columns exist.

### Growth, and where the site lives

`docs/` used to be versioned in `main`: 10,900 per-ticker JSON files rewritten in
full on every daily run, which made it 161 MB of a 197 MB repository and cost
12-20 MB a night. `data/`, which is the actual product, is 3.9 MB of it. On that
trajectory the repo reached GitHub's 1 GB warning in about five weeks.

The rendered site now publishes to a `gh-pages` branch as a single orphan commit,
force-pushed each run, so it never accumulates. `main` keeps only `docs/briefs`,
the archive, which is written once and cannot be regenerated.

The obvious alternative, `actions/upload-pages-artifact`, does not work here.
`docs/news` and `docs/prices` are not only output: `enrich_with_news` and
`enrich_with_prices` decide what to skip by reading those files off disk. A deploy
that does not restore them first would refetch 5,400 news feeds and 5,447 price
histories on every run. So the workflow checks the branch out before the run and
publishes it after, with two guards, since a force-push writes whatever is on
disk: it refuses a tree with no `index.html`, and one whose brief count went
backwards.

### Risk metrics come from history already on disk

`enrich_with_prices` keeps a year of daily closes per ticker so the expanded row
can draw a chart. `volatility_1y`, `beta_1y`, `sharpe_1y` and `max_drawdown_1y`
are computed from those same files, so they cost no extra fetching and reach 95%
of the universe, better than any other factor here.

Beta and Sharpe need a benchmark and a risk-free rate. Both are keyless: `^IRX`
(13-week Treasury bill) and `^GSPC`, fetched through yfinance, which is already a
dependency, and cached in `docs/prices/_MARKET.json` beside the ticker histories.
The Treasury publishes the same series as CSV at home.treasury.gov if Yahoo stops
carrying it.

Volatility and drawdown need only a ticker's own closes and are produced whether
or not that file exists. Beta and Sharpe are withheld when it is missing rather
than falling back to a zero rate, which would inflate every Sharpe in the universe
by roughly the level of short rates.

Checked against the case that is forced regardless of market conditions: SPY
against the index it tracks comes out at beta 0.9964, correlation 0.9968.

## Data provenance

Every derived number, what produces it, and how often its input can actually
change. Generated from `FIELD_METHODS` in `lambda_function.py`, which is the
single place methodology is written down, so this table, the screener's
methodology panel and the code cannot disagree.

| Field | Units | Formula | Source | Changes |
|---|---|---|---|---|
| `beta_1y` | ratio | `cov(r_stock, r_index) / var(r_index)` | market_series | changes every trading day |
| `change_pct` | percent | `(closes[-1] / closes[-2] - 1) * 100` | price_history | changes every trading day |
| `eps_growth_yoy` | fraction | `ttm_diluted_eps / prior_ttm_diluted_eps - 1` | edgar | changes only when the company files |
| `fcf_yield` | fraction | `(ttm_operating_cash_flow - ttm_capex) / market_cap` | edgar | changes only when the company files |
| `gross_margin` | fraction | `ttm_gross_profit / ttm_revenue` | edgar | changes only when the company files |
| `high52w_proximity` | fraction | `closes[-1] / max(closes) - 1` | price_history | changes every trading day |
| `market_cap` | USD | `price * shares_outstanding` | edgar | changes every trading day |
| `max_drawdown_1y` | fraction | `min(close / running_max(close) - 1)` | price_history | changes every trading day |
| `operating_margin` | fraction | `ttm_operating_income / ttm_revenue` | edgar | changes only when the company files |
| `pe` | ratio | `price / sum(last 4 quarters of diluted EPS)` | edgar | changes only when the company files |
| `price` | USD | `closes[-1]` | price_history | changes every trading day |
| `price_book` | ratio | `market_cap / stockholders_equity` | edgar | changes only when the company files |
| `rel_strength_sp500` | fraction | `return_52w(stock) - return_52w(^GSPC)` | price_history | changes every trading day |
| `return_12_2` | fraction | `closes[-22] / closes[-253] - 1` | price_history | changes every trading day |
| `return_1m` | fraction | `closes[-1] / closes[-22] - 1` | price_history | changes every trading day |
| `return_52w` | fraction | `closes[-1] / closes[-253] - 1` | price_history | changes every trading day |
| `revenue_growth_yoy` | fraction | `ttm_revenue / prior_ttm_revenue - 1` | edgar | changes only when the company files |
| `roe_ttm` | fraction | `ttm_net_income / mean(equity_now, equity_a_year_ago)` | edgar | changes only when the company files |
| `sector` | text | `normalize_sector(yahoo.sector)` | yfinance | rarely changes; carried forward until it does |
| `sharpe_1y` | ratio | `mean(r - rf) / stdev(r - rf) * sqrt(252)` | market_series | changes every trading day |
| `volatility_1y` | fraction | `stdev(daily returns) * sqrt(252)` | price_history | changes every trading day |
| `volume` | shares | `volumes[-1]` | price_history | changes every trading day |
| `volume_trend` | fraction | `mean(volumes[-10:]) / mean(volumes[-63:]) - 1` | price_history | changes every trading day |

Notes where the choice matters:

- **`market_cap`** — Cover-page shares outstanding from the latest filing, times the latest close. Not the weighted-average count, which describes a period rather than a moment and understates a company mid-buyback. Matches the vendor to 0.0% across the filers checked.
- **`pe`** — Diluted, not basic, because that is the share count an outside holder is actually diluted by. Undefined and withheld when trailing EPS is zero or negative.
- **`roe_ttm`** — Average equity over the same window as the earnings, not the closing balance, because the denominator moves through the year.
- **`fcf_yield`** — Capital expenditure is a positive outflow in the cash-flow statement, so it is subtracted by magnitude. This deliberately does not match the vendor's freeCashflow, which implies about $16bn for Microsoft against roughly $70bn of actual free cash flow; ours reconstructs from the filed statements.
- **`revenue_growth_yoy`** — Trailing twelve months against the twelve before it, which is the smoother and more usual construction for a screen. The vendor's revenueGrowth compares a single quarter with the year-ago quarter, so the two agree only when growth is steady.
- **`change_pct`** — Close-to-close, one session. Stored in percent, not as a fraction, which is why it is the one percentage field not scaled by 100 for display.
- **`return_12_2`** — Jegadeesh-Titman momentum: twelve months of return ending one month ago. Skipping the most recent month is the point, because that is where short-term reversal lives. Needs 200 sessions.
- **`rel_strength_sp500`** — Difference of the two 52-week returns over the same trading days, which is the usual construction. Not a ratio and not a regression; beta_1y is the regression.
- **`sharpe_1y`** — Daily excess return over the 13-week Treasury bill, annualized. Withheld rather than assuming a zero rate when the rate series is unavailable, since that would inflate every Sharpe by roughly the level of short rates.
- **`sector`** — Yahoo's own eleven-sector taxonomy, mapped onto the GICS sector NAMES. It is not licensed GICS, which is a commercial product of S&P Dow Jones Indices and MSCI and is not publicly available. The names match; the classifications are Yahoo's.

### What is deliberately not derived

`ev_ebitda`, `ev_revenue` and `net_debt_ebitda` still come from the vendor.
All three need total debt, and total debt cannot be composed from XBRL
reliably: filers split short-term borrowing across `ShortTermBorrowings`,
`CommercialPaper`, `DebtCurrent` and the current portion of long-term debt in
combinations that overlap. Coca-Cola tags both `ShortTermBorrowings` and
`CommercialPaper`, so picking one understates and summing both risks counting
the same paper twice; the attempt put its total debt at $36.8bn against
roughly $45bn. A leverage figure wrong by that much is worse than none, and
unlike free cash flow there is no independent check saying ours is better.
The `ttm_ebitda`, `total_debt` and `cash_and_investments` aggregates are
published anyway, for anyone who wants to build their own.

### Institutional ownership is not computed from 13F, and here is the measurement

Worth writing down, because the idea is obvious enough to be tried again.

SEC publishes quarterly Form 13F structured data sets, about 95 MB a quarter,
covering every manager over $100M. For the quarter ending 31-MAR-2026 that is
8,741 managers and 3.1 million holdings across 22,840 issuers. Aggregating
shares by issuer is straightforward: drop `13F-NT` notices, keep the latest
amendment per manager, exclude option positions and `PRN` principal rows, and
sum `SSHPRNAMT`.

The problem is identity. 13F reports holdings by CUSIP, and CUSIP-to-ticker
crosswalks are licensed data, so issuers have to be matched by name. Matching
against SEC's own company names, which is the best available key since both
sides then come from SEC filings, resolves 3,301 of our 5,339 tickers, 61.8%.

That number flatters it. Of those 3,301, **1,311 had more than one CUSIP
mapping to the same ticker**, which is share classes and similarly named
companies being summed into one position. Against the vendor's figure for the
2,269 tickers carrying both, only **25% land within 10%**, the median gap is
16.2%, and **195 imply ownership above 100%**, which is impossible.

So the arithmetic is right and the identity resolution is not, and a wrong
match does not fail visibly: it produces a plausible percentage for the wrong
company. `inst_ownership` therefore keeps coming from the vendor, where it
covers 65% of the universe and is correct, rather than 62% and wrong in three
cases out of four.

What would change this is a CUSIP-to-ticker crosswalk. There is no free one.

### Why a number is missing or old

A quarterly figure that is seventy days old is exactly as current as the
filings allow. Rather than let that look like staleness, fields carry a status
when there is something to say:

| Status | Meaning |
|---|---|
| `awaiting_filing` | Waiting on the next quarterly report. This is as current as the filings allow. |
| `no_coverage` | The source has nothing for this company. |
| `insufficient_history` | Not enough observations to compute this honestly. |
| `cohort_too_small` | Too few sector peers to rank against. |
| `deferred_budget` | The fetch pass ran out of time this run and will reach it next run. |
| `not_meaningful` | The inputs make this arithmetic meaningless, such as a multiple on negative earnings. |
| `source_error` | The source was reachable but the fetch or parse failed. |

## Schedules (UTC)

| Cron | Mode | What it does |
|---|---|---|
| `23 22 * * *` | `daily` | Record, refresh fundamentals, rebuild the site |
| `23 0-21,23 * * *` | `record` | Quotes + headlines to CSV only |

Both run at `:23`, never on the hour. GitHub's docs warn that scheduled runs are
delayed under load and that high load includes the start of every hour; the first
`:00` run this schedule had did not fire at all.

The daily run is at 22:23 UTC deliberately: that is after the US close in both EDT
(18:23 ET) and EST (17:23 ET), so a row dated today contains today's close. It
previously ran pre-open, which stamped today's date on yesterday's prices.

Actions cron is best-effort and can still run late.

## Required GitHub config

**Secrets:**

- `ALPHAVANTAGE_API_KEY` (index quotes and the fed funds fallback)
- `APTERREON_ICLOUD_APP_PASSWORD` (used *only* to email failure alerts)

**Variables:**

- `RECIPIENTS` (where failure alerts go)

Nothing emails on success. The failure alert is the only notification, which is
deliberate: this pipeline previously failed 78 times in a row without anyone
noticing, because it emailed *before* the step that crashed.

## Keepalive

GitHub disables scheduled workflows after 60 days without repository activity,
and commits pushed by the workflow's own `GITHUB_TOKEN` do **not** reset that
timer. This is what silently killed the project on 2026-07-07.

`keepalive-check.yml` runs on the 1st of each month, finds the newest commit that
is not the bot's, and emails a warning once that is over 40 days old. It needs no
new secret and holds no write credential: a long-lived PAT existing purely to
fake activity is a standing liability, and one real commit does the same job.

Any commit resets the clock:

```sh
git commit --allow-empty -m keepalive && git push
```

If you ignore the warning for 14 days or more, the schedule stops. That is the tradeoff
for not keeping a write token around.

## Manual run

Actions tab, "apterreon-brief", Run workflow, pick `daily` or `record`.

## Local dev

```sh
python lambda_function.py record    # fast: quotes + headlines to CSV
python lambda_function.py daily     # full: adds fundamentals + site rebuild
```

Needs `ALPHAVANTAGE_API_KEY` for quotes. A `daily` run takes 20+ minutes and hits
Yahoo and SEC hard, so prefer `record` for iteration.

## Layout

```
lambda_function.py          # the whole pipeline
.github/workflows/
  brief.yml                 # hourly record + daily full run
  keepalive.yml             # weekly check; emails a warning, makes no commit
data/                       # the append-only record (the actual product)
docs/                       # the site. Only briefs/ is tracked in main; the
                            # rest lives on gh-pages, restored before each run
  index.html today.html stories.html stocks.html
  briefs/                   # daily snapshot pages, tracked in main, not regenerable
  news/ prices/             # per-ticker caches, and the fetch state itself
  prices/_MARKET.json       # risk-free rate (^IRX) and benchmark (^GSPC)
state/                      # caches committed back by the workflow
  stocks_universe.json      # the daily fundamentals snapshot
  news_fetch_log.json       # per-ticker news freshness; see note below
```

### A note on freshness and mtime

Nothing in this repo may judge cache freshness by file mtime. `actions/checkout`
stamps every file with the checkout time, so under CI an mtime-based cache looks
permanently fresh and never refreshes. That bug silently froze `docs/news` and
`docs/prices` for two months. Freshness comes from data written into the file
(`prices` uses its inline `updated` field) or from a sidecar manifest
(`state/news_fetch_log.json`).
