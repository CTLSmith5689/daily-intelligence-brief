# theses/

Investment theses written by a scheduled Claude agent from the CSVs in `data/`.

This directory is the only safe place in the repo for an external agent to write.
`.github/workflows/brief.yml` stages exactly `git add data/ docs/ state/`, so nothing
here is touched, regenerated, or force-pushed by the pipeline.

**The agent runs on a personal Claude account as a scheduled task and pushes here with a
repo-scoped PAT. There is no Anthropic API key, in this repo or anywhere else.** That is
the arrangement the README's "no LLM, and no AI API key" note anticipated.

## Layout

| Path | What it is |
|---|---|
| `config.json` | slot counts, gates, cooldowns |
| `watchlist.txt` | hand-edited. One ticker per line, `#` comments. Skips the ranking, not the gate: a watchlist name is offered only if it passes the universe gate and is off cooldown |
| `bin/` | deterministic Python. The screen, the dossier builder, the scorer. Zero model tokens |
| `notes/{TICKER}/{DATE}-{kind}.md` | the thesis: YAML front-matter, Markdown body. From the run of 2026-09-28 a buy-side investment memo (`format: memo`): page one, twelve numbered sections and a glossary, as an initiation for a new name or a short revision for a covered one. Earlier notes are in the older plain format and are never edited |
| `GLOSSARY.md` | the one-sentence definition of every finance term a memo uses. Memos copy it word for word; the analyst does not edit it |
| `PROMPTS.md`, `RUNBOOK.md` | the analyst's instructions, and how the scheduled run is set up |
| `ledger/events.csv` | **append-only.** One row per note: initiate, revise, reaffirm or close, with the direction, conviction and target. Memo rows also carry `action`, `size_now`, `expected_return` and `bear_return`, the last four columns, blank for older rows |
| `ledger/predictions.csv` | **append-only, never rewritten** |
| `ledger/scores.csv` | **append-only.** Outcomes, joined on `prediction_id` |
| *(coverage)* | not a file. Folded from `events.csv` by `screen.coverage_from_events()`, so it cannot drift from the notes |
| `runs/{DATE}/` | one dossier per slot plus `manifest.json`: what the run planned, what it completed, what failed |
| `positions/{TICKER}.md` | the current view, folded from `events.csv`. Derived, never hand-edited |

## The ledger is the point

Each scheduled run is a fresh session with no memory of the last one, so every piece of
continuity lives in these files. More importantly: an open prediction is one that appears
in `predictions.csv` and not in `scores.csv`. No field is ever mutated. **The agent
structurally cannot go back and soften a call it got wrong.**

Scoring benchmarks twice: against SPY, and against the median return of up to twelve
sub-industry peers. The second is the one that tests stock picking rather than market
direction.

An analyst right 55% of the time on high-conviction calls and 50% on low-conviction ones
has a real skill. One whose conviction is uncorrelated with outcomes has none, however
well the notes read. Only this ledger can tell those apart.

## Coverage is deliberately partial

About 2,000 names pass the gates (2,026 on the 2026-09-21 panel), and about 1,200 of those
have enough data to score (1,189). At four slots a run that is a few hundred unique names a
year. **Most of the eligible universe is never written about.** That is the correct
behaviour for a screen, and it is stated here rather than implied.

## What the screen will not use

- Non-operating listings: exchange-traded notes and bonds, trust certificates, unit
  listings, closed-end funds and blank-check shells (`security_type` in
  `common.NON_OPERATING`). A note ticker resolves to its parent's CIK, so its row carries
  the parent's shares and EPS; `dossier.py` refuses one with a nonzero exit, and
  `score.py` leaves them out of peer benchmarks. Panel rows from before the column
  existed (blank `security_type`) are classified from their own name and data by the
  pipeline's classifier, `security_type.py`, rather than assumed to be companies.

- The seven dead panel columns (`g`, `v`, `m`, `q`, `pct`, `scorable`, `dims_present`),
  which are stripped from the CSV and permanently empty.
- The four news-derived columns, which are contaminated for every date before 2026-09-12
  by a company-news fetch that applied no relevance filter. The headlines that produced
  them were never archived, so those rows cannot be audited or repaired.
- The panel's own `ev_ebitda`, which does not reconcile to the panel's own balance sheet
  (median relative error 9.8%, p90 58%). It is recomputed from `price`, `shares_outstanding`,
  `total_debt`, `cash_and_investments` and `ttm_ebitda` instead.
