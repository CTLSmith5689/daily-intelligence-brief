# Running the analyst

Everything needed to make the scheduled run fire, and what it is fed when it
does. `PROMPTS.md` is the specification the agent follows; this is the operator's
half.

## Where it runs

Two mechanisms exist and they are not the same thing.

| | Cloud routine | Local scheduled task |
|---|---|---|
| Runs on | Anthropic's cloud, via claude.ai | The Mac, via `~/.claude/scheduled-tasks/` |
| Fires when the Mac is asleep | Yes | No. It waits for the next app launch |
| Repo | Needs the Claude GitHub App installed on this repository | Uses the local clone |
| Push credential | The GitHub App | `osxkeychain` |

The cloud routine is the intended one. A 7am slot is exactly the case the local
task fails, because the machine is usually asleep.

### One-time setup

1. Open claude.ai/code. Connect the GitHub account, then install the Claude
   GitHub App and grant it this repository. The repository picker at the bottom
   of that page should list `CTLSmith5689/daily-intelligence-brief` once it has
   worked.
2. Create the routine. Schedule: Monday 07:00 ET, weekly.
3. Paste the prompt below as the routine's instruction.

The routine's prompt is deliberately short. It points at `PROMPTS.md` in the
repo rather than restating it, so the analyst's instructions are versioned with
the code they invoke and changing them is a commit rather than an edit in a web
form.

```text
You are the analyst for Apterreon, a personal equity research archive.
Repo: github.com/CTLSmith5689/daily-intelligence-brief

This is a fresh session with no memory of previous runs. Everything you need is
in the repo.

1. Get a checkout. The environment may already provide one. If not:
     git clone https://github.com/CTLSmith5689/daily-intelligence-brief.git
     cd daily-intelligence-brief
   Then: git pull --rebase

2. Read theses/PROMPTS.md and follow the section headed
   "## Agent 1: the analyst" exactly, start to finish. That file is the
   specification. Do not improvise around it. Do not follow the
   "## Agent 2: the PM" section, which is a separate run.

3. You may not edit anything under theses/bin/. If a script fails, write the
   traceback into theses/runs/{run_date}/manifest.json, commit that alone, push,
   and stop. An agent that rewrites its own screen after a bad run is not a
   research process.

4. When you have pushed, report back in three lines: the tickers covered, each
   one's direction and conviction, and anything skipped and why.
```

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
| Reported history | `data/financials/reported.csv` | A decade, annual and quarterly |
| Peer revenue share | Computed across the gated universe | |
| Segment names | 10-Q or 10-K segment note | Names only, never revenue |
| 10-K Item 1 and Item 1A | `data/filings/text/` | The business and its stated risks |
| 8-K EX-99.1 | `data/filings/text/` | When one was filed |
| Filtered headlines | `docs/news/{TICKER}.json` | With a kept/total count |
| Sector lens | `theses/lenses/{sector}.md` | How to read all of the above for this kind of business |
| Track record | `ledger/scores.csv` via `hit_rate()` | The agent cannot remember it otherwise |
| Data caveats | Assembled from what is stale or missing | |

The dossier also states what is NOT available, because a model that is not told
what is missing will fill the gap. There are no consensus estimates anywhere in
this pipeline, no forward guidance as data, no price targets, no unit volumes,
no segment revenue, and no commodity price series.

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

The push is what publishes. `brief.yml` triggers on a push touching `theses/**`,
runs in `publish` mode, and rebuilds the site from committed data in under a
second, so a note reaches the Research page within a couple of minutes. Nothing
else needs triggering.

Check, in order:

1. `theses/runs/{RUN_DATE}/manifest.json` for what the run planned against what
   it completed, and for any dossier that failed to build.
2. `python3 theses/bin/validate.py theses/notes` passes.
3. The Research page shows the new names.
4. `ledger/predictions.csv` gained a row for each note whose direction was long,
   short or avoid, and none for a note that said watch or no view.

## Failure modes worth knowing

- **The daily cron gets dropped.** GitHub has dropped the 22:23 slot for days at
  a time. A record run promotes itself to daily when the panel row is missing and
  the session is over, but only on a weekday.
- **A cloud run is not this Mac.** Anything in a prompt that assumes a path here
  will fail there. `PROMPTS.md` clones rather than assuming.
- **The run date is Eastern.** `prepare.py` stamps `run_date` in US Eastern and
  writes it into the manifest. The agent's own clock may be on another day. The
  manifest is the authority.
