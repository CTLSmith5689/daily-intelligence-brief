# Daily Intelligence Brief

Daily intelligence brief: 3x/day finance + tech + politics + culture roundup, generated via Claude, emailed via iCloud SMTP, archived as static HTML on GitHub Pages.

Migrated from AWS Lambda + EventBridge + S3 to GitHub Actions + GitHub Pages so it runs while the laptop is off and nothing-but-API-calls cost money.

## Schedules (UTC)

| Time | Edition |
|---|---|
| 11:00 | morning brief (~7am ET) |
| 16:15 | midday update (~12:15pm ET) |
| 20:45 | evening wrap (~4:45pm ET) |

Weekends: only the morning edition is sent (handler skips weekend midday/evening).

GitHub Actions cron is best-effort and can run 5–15 min late under load.

## Required GitHub config

**Secrets** (Settings → Secrets and variables → Actions → Secrets):

- `ANTHROPIC_API_KEY`
- `ALPHAVANTAGE_API_KEY`
- `APTERREON_ICLOUD_APP_PASSWORD` — app-specific password from appleid.apple.com

**Variables** (same screen, Variables tab):

- `APTERREON_READER_CONTEXT` — the persona/style instructions appended to the analysis prompt
- `RECIPIENTS` — comma-separated email addresses
- `APTERREON_MODEL` — optional override; defaults to `claude-haiku-4-5-20251001`

## Manual run

Actions tab → "apterreon-brief" → Run workflow → pick edition.

## Archive viewer

GitHub Pages serves `docs/` at `https://ctlsmith5689.github.io/daily-intelligence-brief/` with Today / Archive / Pinned tabs. Pins are device-local (localStorage) since the site is static.

## Local dev (optional)

```sh
python lambda_function.py morning
```

Reads the same env vars. Writes to `docs/briefs/<date>-<edition>.html` and updates `docs/index.html`. Doesn't push.

## Layout

```
lambda_function.py        # the brief generator (ported from AWS Lambda)
.github/workflows/brief.yml
docs/                     # GitHub Pages source
  index.html              # auto-generated index
  manifest.json           # PWA manifest
  briefs/                 # the actual briefs
    2026-04-25-morning.html
state/                    # state committed back by the workflow
  last_headlines.json     # legacy; no longer used
  pins.json               # legacy; pins now live in browser localStorage
```
