# Running the analyst

Everything needed to make the scheduled run fire, and what it is fed when it
does. `PROMPTS.md` is the specification the agent follows; this is the operator's
half.

## Where it runs

| | Research Agent (claude.ai) | A session on the Mac |
|---|---|---|
| Runs on | Anthropic's cloud | This Mac |
| Fires when the Mac is asleep | Yes | No |
| Can push to GitHub | No. The cloud git proxy will not inject a credential for this repository | Yes, through `osxkeychain` |
| Delivers by | Uploading the run to Google Drive, which the hourly workflow ingests | `events.py`, then commit and push |

The Research Agent is the intended one, because a 7am slot is exactly when the Mac
is asleep. "Running in the cloud" below covers it, and "Running on the Mac
instead" covers the other.

### One-time setup

1. The workflow's Drive credential: "One-time setup: letting the workflow read
   the folder", below. Until it exists, deliveries wait in Drive.
2. The task, on the Scheduled tasks page: New task, named **Research Agent**,
   model Opus 5, Permissions "Skip all approvals", "Require this computer"
   unchecked. Frequency Manual until one run has been ingested end to end, then
   weekly.
3. Its instructions, below.

The instructions are deliberately short. They point at this file and `PROMPTS.md`
rather than restating them, so the analyst's instructions are versioned with the
code they invoke, and changing them is a commit rather than an edit in a web form.

```text
Apterreon weekly analyst run. Unattended: do not ask questions.

1. git clone https://github.com/CTLSmith5689/daily-intelligence-brief.git and cd into it.
2. Read theses/RUNBOOK.md, section "Running in the cloud", and follow "What the
   analyst does" exactly, step by step. Step 2 is a Google Drive preflight: if it
   fails, stop, do no analysis, and report the exact error.
3. Write the notes by following theses/PROMPTS.md, section "## Agent 1: the
   analyst". Do not follow the PM section. Do not commit or push anything: this
   session cannot push, and the pipeline ingests the Drive delivery.

Report back: tickers covered, each direction and conviction, the Drive run folder
link, and anything skipped and why.
```

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

### Why it delivers to Drive instead of pushing

A cloud session can clone this public repository but cannot push to it. The
first run's push preflight got this from the git proxy:

> access denied by the git proxy: CTLSmith5689/daily-intelligence-brief is not in
> this session's authorized repository set, so the proxy will not inject a
> credential for it. To fix, add the repository to the session's sources.

The scheduled-task form has no repository field. The routines API accepts a
`git_repository` source, but setting one requires a `session_request.worker`
object whose shape is not documented, and five probes did not find it. So a cloud
run writes its output to the owner's Google Drive, which its connector writes to
as the owner, and `theses/bin/ingest.py`, run by the hourly workflow, brings it
into the repository.

The delivery folder is **Investment Research**, id
`1J6Wk6dzAg92lgrbAnLqVbQLXXKy1qck6`. Every Drive call below was run against it
while building this, and an uploaded file came back byte-identical, trailing
newline included.

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

2. **Drive preflight, before any analysis.** Look for this run's folder:

   `search_files(query="parentId = '1J6Wk6dzAg92lgrbAnLqVbQLXXKy1qck6' and title = 'run-{RUN_DATE}'", excludeContentSnippets=true)`

   - If it exists and holds `ingest.json`, this run was already delivered. Stop
     and say so.
   - If it exists without `ingest.json`, an earlier attempt stopped part way.
     Trash every file in it with `trash_file` and reuse the folder.
   - Otherwise create it:
     `create_file(title="run-{RUN_DATE}", parentId="1J6Wk6dzAg92lgrbAnLqVbQLXXKy1qck6", contentMimeType="application/vnd.google-apps.folder")`

   Then upload a one-line `started.json` into it, using the call in step 5. If any
   of these calls fails, stop, do no analysis, and report the exact error. A run
   that writes four theses and only then finds it cannot deliver them has spent
   the work and lost it.

3. Write and validate the notes and update the manifest: `PROMPTS.md`, "Agent 1:
   the analyst", steps 2 to 7. Do not run `events.py`. Do not commit or push.

4. Hash what will be delivered:

   ```bash
   sha256sum theses/notes/*/{RUN_DATE}-*.md theses/runs/{RUN_DATE}/manifest.json
   ```

5. Upload each note and the manifest. The Drive name is the repository path with
   every `/` written as `__`:

   | Repository path | Drive name |
   |---|---|
   | `theses/notes/AAPL/2026-09-14-initiation.md` | `theses__notes__AAPL__2026-09-14-initiation.md` |
   | `theses/runs/2026-09-14/manifest.json` | `theses__runs__2026-09-14__manifest.json` |

   `create_file(title=<Drive name>, parentId=<run folder id>, textContent=<the file's exact contents>, contentMimeType="text/markdown", disableConversionToGoogleType=true)`

   Use `application/json` for the manifest and for `started.json`. Pass each file
   exactly as `cat` prints it. Do not reformat, rewrap or retype it: the pipeline
   compares sha256 and refuses any difference. `disableConversionToGoogleType` is
   required. Without it Drive converts the upload into a Google Doc, which the
   pipeline refuses.

6. Do not upload the dossiers. This step used to, for a person reading the
   folder. A dossier is now about 90,000 characters, and an upload through the
   connector means writing the whole file out as the call's content: four of
   them cost more than the four notes did. Anyone who wants one can rebuild it
   with `python3 theses/bin/dossier.py TICKER`.

7. Confirm the delivery:

   `search_files(query="parentId = '<run folder id>'", excludeContentSnippets=true)`

   Every note and the manifest must appear exactly once, as `text/markdown` or
   `application/json`. Upload anything missing and trash any duplicate.

   Do not download the files again to check their hashes. The pipeline recomputes
   every sha256 when it ingests and refuses a mismatch, and re-downloading through
   the connector took about four minutes a file on the first run.

8. Upload `ingest.json` **last**, as `application/json`. Until it exists the
   pipeline treats the run as still being delivered, so a half-finished upload is
   never picked up.

   ```json
   {
     "run_date": "2026-09-14",
     "notes": [
       {"path": "theses/notes/AAPL/2026-09-14-initiation.md",
        "trigger": "weekly screen, SCREEN slot",
        "rationale": "one line on why this name, now"}
     ],
     "sha256": {
       "theses/notes/AAPL/2026-09-14-initiation.md": "<64 hex characters>",
       "theses/runs/2026-09-14/manifest.json": "<64 hex characters>"
     }
   }
   ```

   `trigger` and `rationale` become the event row, exactly as if they had been
   passed to `events.py` on the command line.

9. Report: tickers, each one's direction and conviction, the run folder's link,
   and anything skipped and why.

If a script fails at any point, do not upload `ingest.json`. Upload the traceback
as `FAILED.md` into the run folder, report it, and stop.

### What the pipeline does with it

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
| Sector lens | `theses/lenses/{sector}.md` | How to read all of the above for this kind of business |
| Track record | `ledger/scores.csv` via `hit_rate()` | The agent cannot remember it otherwise |
| Data caveats | Assembled from what is stale or missing | |

### Where the filing text comes from

Filings are collected two ways, both inside the daily pipeline run.

1. **When a company reports.** The run reads the SEC's daily index for the last
   four days and, for every 8-K with results in it, keeps the announcement, the
   10-K items, the segment note and the reported history. This only ever looks
   forward from the day it was switched on, 2026-09-12.
2. **For the names the analyst is about to be handed.** `collect_reading_packs`
   tops up the watchlist, every company already written up, and the screen's top
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
this pipeline, no forward guidance as data, no price targets, and no commodity
price series. Unit volumes, selling prices and segment revenue exist only as
text, where a company's own filings print them, and never as data columns.

## Why a sector lens and not retrieval

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
- **A conflict under `theses/` aborts the push.** If something else pushes to a
  ledger file while a workflow run is ingesting, the run refuses to resolve the
  conflict rather than commit conflict markers into an append-only file. The job
  alerts, and the next run ingests the same delivery again from a clean checkout.
