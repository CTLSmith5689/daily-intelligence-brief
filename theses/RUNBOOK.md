# Running the analyst

Everything needed to make the scheduled run fire, and what it is fed when it
does. `PROMPTS.md` is the specification the agent follows; this is the operator's
half.

## Where it runs

| | Research Agent (claude.ai) | A session on the Mac |
|---|---|---|
| Runs on | Anthropic's cloud | This Mac |
| Fires when the Mac is asleep | Yes | No |
| Can push to GitHub | Yes, once the repository is attached to the routine as a source | Yes, through `osxkeychain` |
| Delivers by | `events.py`, then commit and push | `events.py`, then commit and push |

The Research Agent is the intended one, because a 7am slot is exactly when the Mac
is asleep. "Running in the cloud" below covers it, and "Running on the Mac
instead" covers the other.

### One-time setup

1. The routine, on the Scheduled tasks page or through the routines API, named
   **Research Agent**: model Opus 5, permissions "Skip all approvals", "Require
   this computer" unchecked. Frequency Manual until one run has gone end to end,
   then weekly.
2. **Attach the repository**, which is what lets the run deliver. In the routine's
   config set

   ```json
   "job_config": {"ccr": {"session_context": {"sources": [
     {"git_repository": {"url": "https://github.com/CTLSmith5689/daily-intelligence-brief"}}]}}}
   ```

   Without it the session can still clone the public repository but cannot push,
   and the run has nowhere to put its work.
3. Its instructions: paste the prompt from `theses/routines/research-agent.md`
   (everything after the "---" line).

The instructions are deliberately short. They point at this file and `PROMPTS.md`
rather than restating them, so the analyst's instructions are versioned with the
code they invoke, and changing them is a commit rather than an edit in a web form.
The routine's prompt, word for word, is in `theses/routines/research-agent.md`
(see "The routine prompts are mirrored in the repository, by hand", below). The
portfolio managers' instructions are in `portfolio/PROMPTS.md`, not here.

## Running in the cloud

The analyst runs as **Research Agent**, a scheduled task on claude.ai: Opus 5,
"Skip all approvals", and "Require this computer" unchecked, so it runs whether
or not the Mac is awake. Keep it manual until one run has gone end to end, then
set it weekly.

It has run once, on 2026-09-13, and delivered four notes to Drive in 39 minutes,
about 25 of them spent uploading files and downloading them again to compare.
That delivery was later removed from Drive and was never ingested: its dossiers
had no prices, because the cloud proxy refused the published site (fixed the
same day in `common.fetch_site`), and its notes were in the old style. To see
what a run did, use the routine's run log: routine
`trig_01FvCss6qQ6ZsAwwjeSisKeh` at claude.ai/code/routines. `trig_019GEQVFFZa8RbMuwNH8Tjyn` is a disabled test routine left
over from setting this up and can be deleted in the Scheduled tasks page.

**The routine prompts are mirrored in the repository, by hand.** The prompt saved
in each claude.ai routine is copied into a file here: the analyst's in
`theses/routines/research-agent.md`, the Research Director's in
`theses/routines/research-director.md`, the PMs' in `portfolio/routines/`
(`style-pm.md`, the Style PM, which runs the six style books and the hedge
book, and `neural-pm.md`, the Neural PM, which runs the neural book alone and
is not scheduled yet). The Research and Portfolios pages print these files, with the instructions
each one follows: `theses/PROMPTS.md` for the analyst, `portfolio/PROMPTS.md` for
the PMs. Nothing copies them automatically, and a routine never
reads its file: when you edit a prompt on claude.ai, make the same edit in its
file and commit it, and the other way round, or the site will show instructions
the routine is not running. The "Schedule:" line at the top of each file is what
the pages print as its schedule, so change it when the routine's schedule changes.

### How it delivers: it pushes

The first run could not push, and the error told us why: the repository was not
in that session's authorized set. It said what to do about it, which is to add
the repository to the session's sources, and that field turned out to exist.

A routine's config carries it at
`job_config.ccr.session_context.sources = [{"git_repository": {"url": ...}}]`.
With it set, the environment clones the repository before the session starts and
injects `GH_TOKEN` and `GITHUB_TOKEN`. Probed on 2026-09-21: the checkout arrives
at `/home/claude/daily-intelligence-brief` on main, and

    git push --dry-run origin HEAD:refs/heads/probe-push-access
    To https://github.com/CTLSmith5689/daily-intelligence-brief
     * [new branch]      HEAD -> probe-push-access

so the run records its notes with `events.py` and pushes them, exactly as a
session on the Mac does. Pushing to main under `theses/` makes the workflow
rebuild the site by itself.

Google Drive is no longer part of the path. The 2026-09-13 run delivered through
it successfully and spent about 25 of its 39 minutes uploading files and
downloading them again to compare, and a dossier has since grown to about 90,000
characters. `ingest.py` and the `GDRIVE_SA_KEY` secret still work and are left in
place: they are the way back if pushing is ever withdrawn.

**What the cloud session cannot reach.** Its egress proxy allows
raw.githubusercontent.com (200) and refuses `ctlsmith5689.github.io`,
`www.sec.gov` and `data.sec.gov` with a 403 on CONNECT. None of that stops a run.
`common.fetch_site` already falls back to the gh-pages copy on
raw.githubusercontent, and nothing in a run talks to the SEC: filings are
collected by the hourly workflow, which has no such restriction, and arrive in
the checkout. `screen.py` was run in the cloud on 2026-09-21 and returned its
usual JSON.

### What the analyst does

1. Clone, prepare, and read the run date.

   ```bash
   git clone https://github.com/CTLSmith5689/daily-intelligence-brief.git
   cd daily-intelligence-brief
   python3 theses/bin/score.py
   python3 theses/bin/prepare.py
   ```

   `run_date` in `theses/runs/*/manifest.json` is the run date. Use it wherever
   `{RUN_DATE}` appears below, not the session clock.

2. **Check you can deliver, before any analysis.** A run that writes its theses
   and only then finds it cannot deliver them has spent the work and lost it.

   ```bash
   git remote -v
   git push --dry-run origin HEAD:refs/heads/delivery-preflight
   ```

   The dry run creates nothing. It must report `[new branch]`. If it is refused,
   stop, do no analysis, and report the exact error: the repository is probably
   not attached to the routine as a source.

3. Write and validate the memos and update the manifest: `PROMPTS.md`, "Agent 1:
   the analyst", steps 2 to 7. From the run of 2026-09-28 each note is a buy-side
   investment memo (see "The memo format" below).

4. Record each note and push, as `PROMPTS.md` step 8 sets out:

   ```bash
   python3 theses/bin/events.py theses/notes/{TICKER}/{RUN_DATE}-{kind}.md "<trigger>" "<rationale>"
   git add theses/
   git commit -m "theses({RUN_DATE}): T1, T2"
   git pull --rebase && git push
   ```

   `events.py` runs the note checks again and refuses a note that fails one, so
   the ledger cannot take an unchecked note. `events.py --dry-run` shows the row
   it would write and writes nothing. Stage `theses/` only. Never
   `git add -A`: `data/`, `docs/` and `state/` belong to the workflow, and a run
   that commits its own scratch copies of them fights the hourly job.

5. Report: tickers, each one's action, direction and conviction, the
   commit hash you pushed, and anything skipped and why.

If a script fails at any point, do not push. Report the exact traceback and stop,
leaving the repository as you found it.

### The Google Drive path, kept as a fallback

Nothing below runs while the routine can push. It is documented because it
works, and it is the way back if push access is ever withdrawn: set the
analyst's instructions to deliver to Drive instead, and the hourly workflow
picks the run up as it did on 2026-09-13.

#### What the pipeline does with it

The hourly workflow runs `theses/bin/ingest.py` before Gather. It picks up any
`run-YYYY-MM-DD` folder that holds `ingest.json` and has not been ingested, and
checks it again against committed code:

- every Drive name starting `theses__` must decode to a note or the run manifest
  and be listed in `ingest.json`; anything else in the folder is ignored;
- every delivered file's sha256 must match `ingest.json`;
- a note that already exists must be byte-identical, because notes are never
  edited;
- then `validate.py` and `events.py` run.

If any of that fails, every file and ledger row from the run is rolled back and
the run is refused. The Research page picks up an ingested run within the hour,
because a record run that ingests something rebuilds the site. Ingested runs are
listed in `theses/ledger/ingested.csv`.

A refused run is written to `theses/ledger/ingest_refused.csv` with its reason and
a fingerprint of the folder. It alerts once and is skipped until its files change.
To retry one, fix the files in Drive and upload `ingest.json` again, and the
changed folder is checked from scratch. Or trash the folder and run again.

### One-time setup: letting the workflow read the folder

The workflow reads Drive as a service account that is shared on this one folder
as Viewer, so it can see nothing else in the Drive and cannot write. Until the
secret exists the ingest step logs that it has no credential and does nothing,
and the rest of the workflow runs as before.

1. [Create a project](https://console.cloud.google.com/projectcreate) named `apterreon-drive`.
2. [Enable the Drive API](https://console.cloud.google.com/apis/library/drive.googleapis.com) in it.
3. [Service accounts](https://console.cloud.google.com/iam-admin/serviceaccounts): Create service account, name it `github-actions-drive`, Create and continue, Done. Skip the roles step.
4. Open the account, Keys, Add key, Create new key, JSON. Copy the account's email.
5. Share **Investment Research** with that email as Viewer, with "Notify people" unticked.
6. Repository Settings, Secrets and variables, Actions, New repository secret: `GDRIVE_SA_KEY`, containing the whole JSON file.
7. Delete the downloaded JSON.

A service account cannot create files in a personal Drive (it has no storage
quota), which is why writing is left to the analyst's connector and the workflow
only reads.

### Running on the Mac instead

A session on the owner's Mac has push access and can skip Drive. After step 3,
record each note and push:

```bash
python3 theses/bin/events.py theses/notes/{TICKER}/{RUN_DATE}-{kind}.md "<trigger>" "<rationale>"
git add theses/
git commit -m "theses({RUN_DATE}): T1, T2, T3"
git pull && git push
```

`git pull && git push`, not a bare push: a bot commits to this repository every
hour at :23 and a bare push loses that race whenever one lands in the window.

Run one or the other for a given week, never both. Two runs on the same date
write the same note paths, and ingest refuses a note that already exists with
different content.

## The memo format

From the analyst run of Monday 2026-09-28 every note is a buy-side investment
memo: one analyst's argument to the portfolio manager, voice "I". `PROMPTS.md`,
"Agent 1", is the specification; the reference is the NVIDIA memo kept as
`tests/fixtures/memo/NVDA-2026-09-23-initiation.md`, with a revision beside it.
The format was redesigned on 2026-09-24, before any memo was written, because
the first design (twelve sections, 3,500 to 4,500 words) produced data sheets
that restated the dossier. The company page on the site shows the key data,
reported history and similar companies beside the memo, so the memo argues.

- **Page one** comes before the first heading, under a bold one-line headline
  naming the action (Initiate, Add, Hold, Trim, Exit, Avoid or Short): the
  recommendation and the price below which it is attractive, the expected
  return next to the bear-case loss, the thesis in
  one sentence, and why now in two or three. Under 250 words, no table.
- **Then six numbered sections, SOURCES and a glossary**: 1. THE DEBATE (the
  question the value turns on, and what the price requires, in a sentence or
  two), 2. MY VIEW (two or three arguments, each a claim, its evidence and why
  the price has not moved), 3. WHAT IT IS WORTH (the bull, base and bear table
  and a short paragraph on method), 4. WHAT WOULD PROVE ME WRONG (the monitoring
  table with at least one Exit or Cut row), 5. RISKS (at most three), 6. WHAT I
  DO NOT KNOW (short), SOURCES, GLOSSARY.
- **Argument, not data.** Every paragraph opens with a claim; at most four
  figures a paragraph (warn at five or six, fail above six); tables only in
  sections 3 and 4, SOURCES and GLOSSARY; no key data, peer or history tables.
- **Initiation or revision.** A ticker with no note gets the full memo (1,200 to
  2,000 words of prose; fails outside 900 to 2,400). A ticker that already has
  one, in either format, gets a revision: page one, WHAT CHANGED, sections 3 and
  4, the other sections that changed, SOURCES and GLOSSARY (300 to 800 words;
  fails outside 200 to 1,000). `validate.py` refuses an initiation for a ticker
  already in `theses/ledger/events.csv`.
- **Vocabulary.** Real finance terms, each defined once where it first appears
  in the wording of `theses/GLOSSARY.md`, and listed in the memo's glossary,
  which holds only the terms the memo uses.
- **Front-matter** adds `format: memo`, `action`, `expected_return`,
  `bear_return`, `required_return`, `scenarios` and, optionally,
  `entry_price_below`, and sets
  `horizon_days` to 365. `direction` stays, set from the action, because the
  ledger scores it.
- **Checks.** `validate.py` applies the memo checks only to a note that says
  `format: memo`, including the scenario arithmetic in section 3 and on page
  one (probabilities, weighted value, expected and bear returns, target, and
  the entry price within $1 of (weighted value + dividends) / (1 + required
  return)). A memo with `size_now` or `size_plan` fails. Every earlier note has no `format`
  field and is checked exactly as before.
- **Ledger.** `events.csv` gains four columns at the end: `action`, `size_now`,
  `expected_return`, `bear_return`, blank for older rows. `size_now` stays for
  the file's sake and is written blank, because the PM sizes. `events.py` adds them
  itself the first time it records a memo, once, and only if the file can be
  rewritten byte for byte apart from the new columns. To do it ahead of a run:
  `python3 theses/bin/events.py --migrate`.
- **Sizing belongs to the PM.** The memo gives no size. It gives the view and
  an entry price derived from its cases. The PM sizes every position, book by
  book, by `portfolio/PROMPTS.md`, step 4, SIZE, which also holds the two draft
  limits the owner has not approved (a 2% bear-case cost cap, and half size
  until 10 of the analyst's calls are scored). The dossier's "### Sizing
  inputs" block stays, as data for the required return and for the PM.

## What the run is fed

Nothing is passed in. The agent assembles its own inputs by running committed
Python, which is why the preparation is versioned alongside the notes it
produced and any past run can be re-derived exactly.

`python3 theses/bin/prepare.py` writes `theses/runs/{RUN_DATE}/` and takes about
eight seconds. Into each dossier it puts:

| Input | Source | Note |
|---|---|---|
| Factor panel, 20 fields in four sleeves | `data/fundamentals/{month}.csv` | With staleness ages |
| Peer percentiles | Computed per field, sub-industry where the cohort has 20+, else sector | |
| Tensions | `tensions.py`, 8 detectors | Questions, not rankings |
| A year of daily closes | `docs/prices/{TICKER}.json` | |
| Reported history | `data/financials/reported.csv` | A decade, annual and quarterly, with operating margin, the diluted share count, and the median year's profit per share |
| Peer revenue share | Computed across the gated universe | |
| Segment note | 10-Q or 10-K segment note | The note's own text and tables |
| 10-K Item 1 and Item 1A | `data/filings/text/` | The business and its stated risks |
| Management's discussion | `data/filings/text/`, `-mdna.txt` | 10-Q Item 2 or 10-K Item 7: what was sold, at what price, what it cost, and where the cash went. Two excerpts, with the path to the whole |
| 8-K EX-99.1 | `data/filings/text/` | The latest results announcement, however long ago it was filed |
| Filtered headlines | `docs/news/{TICKER}.json` | With a kept/total count |
| Sector playbook | `theses/desks/sectors/{sector}.md` | How to read all of the above for this kind of business: the deciding questions, the right valuation measures, the traps, where our data misleads, and the director's dated lessons. Replaced the unverified sector lenses in `theses/lenses/` on 2026-09-24 |
| Desk | `theses/desks/{desk}.md` | The desk that owns the name, from the `desks` map in `theses/config.json` |
| Memo blocks | `dossier.py`, for the memo format | "### Key data", "### Guidance", "### Peers", "### Balance sheet and cash flow", "### History", "### Calendar", "### Sizing inputs" and "### Current view", each named in `PROMPTS.md` where a memo section uses it |
| Glossary | `theses/GLOSSARY.md` | The one-sentence definitions every memo copies. Not in the dossier: the analyst reads the file |
| Track record | `ledger/scores.csv` via `hit_rate()` | The agent cannot remember it otherwise |
| Data caveats | Assembled from what is stale or missing | |

### Where the filing text comes from

Filings are collected two ways, both inside the daily pipeline run.

1. **When a company reports.** The run reads the SEC's daily index for the last
   four days and, for every 8-K with results in it, keeps the announcement, the
   10-K items, the segment note and the reported history. This only ever looks
   forward from the day it was switched on, 2026-09-12.
2. **For the names the analyst is about to be handed.** `collect_reading_packs`
   tops up the watchlist, the names in any Research Director plan whose week has
   not ended, every company already written up, and the screen's top
   twelve: the latest results announcement however old, management's discussion
   from the latest 10-Q or 10-K, and anything in (1) that a company is missing
   because it last reported before 2026-09-12. It was added on 2026-09-19, after
   the first CF note listed as unknowable three figures that CF's own 10-Q prints.

Measured on ten companies: ten of ten announcements, eight of ten discussions.
The misses are filings that keep the discussion under headings with no "Item 2"
(JPMorgan, Slide Insurance). A filing that yields nothing is remembered in
`state/reading_pack.json` and is not downloaded again, and the dossier says the
discussion is missing.

To read a change to the collection before it is pushed, point a dossier at a
local copy of the data: `APTERREON_RAW=file:///path/to/root python3
theses/bin/dossier.py CF`, where the root holds a `data/` directory.

The dossier also states what is NOT available, because a model that is not told
what is missing will fill the gap. There are no consensus estimates anywhere in
this pipeline, no forward guidance as data (the "### Guidance" block quotes it as
text from the latest results announcement), no price targets, and no commodity
price series. Unit volumes, selling prices and segment revenue exist only as
text, where a company's own filings print them, and never as data columns.

## The Research Director

A second routine, **Research Director**, runs on Sundays at 16:00 ET, before the
week's first analyst run. Set it up like the Research Agent (Opus 5, "Skip all
approvals", "Require this computer" unchecked, the repository attached as a
source), and paste the prompt from `theses/routines/research-director.md`
(everything after the "---" line). Its instructions are `theses/DIRECTOR.md`.

It runs `theses/bin/director_inputs.py` (a zero-cost input pack: holdings and
rules candidates without a memo, earnings in the next 10 trading days, stale
views, the screen, last week's memos with their `validate.py` results, and a
scorecard per desk), grades last week's memos, and writes
`theses/director/{SUNDAY}.md`: the week's assignments, a focus note per desk,
the grades with rewrite asks, the coverage gaps and any playbook proposals. It may
also append dated lessons to the "## Lessons" section of a sector playbook, and
nothing else: `director_check.py --changes` refuses any other change.

How the plan reaches the analyst: `prepare.py` looks for the plan that covers the
run date. If it exists and `theses/bin/director_check.py` passes, that day's
assignments take the first slots, in order, and the screen fills the rest. With
no plan, a plan that fails the check, or no assignment that day, the run is the
screen's alone, exactly as before. The manifest's `director` key records which
(`director`, `no_plan`, `invalid_plan` or `no_assignments_today`) and why.

The five desks (`theses/desks/`) own the names: every sector maps to one desk in
the `desks` key of `theses/config.json`, so `events.csv` needs no desk column.
The Research page shows the desk beside each note, the director's routine and
instructions, and the current week's plan.

## Why a sector playbook and not retrieval

The obvious version of this is RAG: embed a library of sector material, retrieve
the nearest neighbours to the company, paste them in. That is the wrong tool for
this job.

Retrieval earns its place when you do not know which document you need and have
to find it by similarity. Here the key is known exactly and is already a column
on every row: GICS `sector`, and `sub_industry` under it. A dictionary lookup on
a known key is exact, costs nothing, adds no dependency, and cannot return the
wrong document. Embedding search over eleven files would add a vector store, an
embedding step, and a failure mode, to answer a question a dictionary answers
correctly every time.

There is a case where retrieval will earn its place, and it is worth naming now
so the decision can be revisited on evidence rather than on taste: once the
archive holds several hundred notes, "what did I conclude last time about a
refiner at peak margins" is a real question with no known key, and similarity
search is the right way to answer it. That is a question about the archive's own
history, not about sector knowledge. It is not today's problem: there are four
notes.

## After a run

A delivered run reaches the archive at the next hourly workflow run, which
ingests it and, if that was a record run, rebuilds the site. Allow up to an hour.
Check, in order:

1. The run folder in Drive holds every note, the manifest and `ingest.json`, and
   no `FAILED.md`.
2. The workflow's "Ingest analyst runs from Drive" step logs `ingested
   run-{RUN_DATE}`. A refusal is logged there with its reason, and written to
   `theses/ledger/ingest_refused.csv`.
3. `theses/ledger/ingested.csv` lists the run.
4. The Research page shows the new names.
5. `theses/ledger/predictions.csv` gained a row for each note whose direction was
   long, short or avoid, and none for a note that said watch or no view.
6. `theses/ledger/events.csv` has a row for every memo, with its `action`,
   `expected_return` and `bear_return` filled in and `size_now` blank.

## Failure modes worth knowing

- **The daily cron gets dropped.** GitHub has dropped the 22:23 slot for days at
  a time. A record run promotes itself to daily when the panel row is missing and
  the session is over, but only on a weekday.
- **A cloud run is not this Mac.** Anything in a prompt that assumes a path here
  will fail there. `PROMPTS.md` clones rather than assuming.
- **The run date is Eastern.** `prepare.py` stamps `run_date` in US Eastern and
  writes it into the manifest. The agent's own clock may be on another day. The
  manifest is the authority.
- **No Drive credential yet.** The ingest step logs that `GDRIVE_SA_KEY` is not
  set and does nothing else. Deliveries wait in Drive and are ingested once the
  secret exists.
- **A refused delivery alerts once.** It is skipped until its files change, so an
  unchanged refusal does not alert again. Its reason is in
  `theses/ledger/ingest_refused.csv`.
- **A run folder without `ingest.json` is never ingested.** The step logs it as
  still being delivered, and flags it once it is two days old. Usually the
  analyst stopped after the preflight: look for `FAILED.md` in the folder.
- **A memo written as an initiation for a name already covered is refused.**
  `validate.py` reads `theses/ledger/events.csv`, and `events.py` will not record
  it. The analyst writes a revision instead.
- **A conflict under `theses/` aborts the push.** If something else pushes to a
  ledger file while a workflow run is ingesting, the run refuses to resolve the
  conflict rather than commit conflict markers into an append-only file. The job
  alerts, and the next run ingests the same delivery again from a clean checkout.
