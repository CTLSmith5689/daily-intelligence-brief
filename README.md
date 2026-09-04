# Daily Intelligence Brief

A data aggregation pipeline for US equities and finance/tech news. Runs on GitHub
Actions, writes append-only CSVs, and publishes a browsable site to GitHub Pages.

**No LLM, and no Anthropic API key.** This used to generate written analysis via
Claude. That was removed on 2026-09-04: the project is now purely a recorder, and
the analysis layer lives outside it (a Claude scheduled task reads the CSVs).

## The data

The CSVs under `data/` are the point of the project. Everything under `docs/` is a
view rebuilt from scratch each day and safe to regard as disposable.

| File | Cadence | Grain |
|---|---|---|
| `data/quotes.csv` | hourly | one row per quote per observation |
| `data/headlines/YYYY-MM.csv` | hourly | one row per article, deduped on link |
| `data/fundamentals/YYYY-MM.csv` | daily | one row per ticker per day, ~50 columns |

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

### Refresh cadence is deliberately split

Different sources move at different speeds, so they are gated separately:

- **yfinance** (price, market cap, P/E, momentum, quality factors): **daily**. This
  is what actually changes, and the panel needs real daily rows.
- **EDGAR filings** and **insider Form 4**: **weekly**. Filings are quarterly and
  Form 4s are sparse; pulling them daily would hammer SEC for no new information.
  Those columns therefore repeat within a week, which is why the `*_updated`
  columns exist.

### Growth

Roughly 240 MB/year, almost entirely `fundamentals`. Worth revisiting (compression,
or splitting static ticker metadata into its own reference table) before it becomes
a problem.

## Schedules (UTC)

| Cron | Mode | What it does |
|---|---|---|
| `0 11 * * *` | `daily` | Record, refresh fundamentals, rebuild the site |
| `0 0-10,12-23 * * *` | `record` | Quotes + headlines to CSV only |

The hourly and daily crons deliberately do not overlap at 11:00. The daily run
also records, so nothing is missed.

Actions cron is best-effort and can run 5 to 15 minutes late under load.

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

If you ignore the warning for 20 days, the schedule stops. That is the tradeoff
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
  keepalive.yml             # monthly empty commit, keeps the schedule alive
data/                       # the append-only record (the actual product)
docs/                       # GitHub Pages, rebuilt daily from data
  index.html today.html stories.html stocks.html
  briefs/                   # daily snapshot pages
  news/ prices/             # per-ticker caches
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
