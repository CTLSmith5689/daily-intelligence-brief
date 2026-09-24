# Research agent routine

Schedule: Weekdays 07:00 ET

The prompt of the claude.ai routine that runs the analyst, mirrored here so it is versioned and shown on the Research page. The routine does not read this file: when either changes, change the other by hand (theses/RUNBOOK.md).

---

Apterreon weekly analyst run. Unattended: do not ask questions.

1. A checkout of CTLSmith5689/daily-intelligence-brief is already here, at /home/claude/daily-intelligence-brief. cd into it. If it is missing, stop and say so: without it this session cannot deliver its work.
2. Read theses/RUNBOOK.md, section "Running in the cloud", and follow "What the analyst does" exactly, step by step.
3. Write the notes by following theses/PROMPTS.md, section "## Agent 1: the analyst". Notes use the buy-side memo format described there: a full memo for a new name, a short revision for a name the ledger already covers (the dossier's "### Current view" block tells you which).
4. Run theses/bin/validate.py on every note and fix each note until it passes.
5. Record each note and push, as the last step of that section sets out. Stage theses/ only, never git add -A.

Two things worth knowing about this environment, both already handled in the code, so do not try to work around them:
- ctlsmith5689.github.io, www.sec.gov and data.sec.gov are refused by the egress proxy. Nothing you run needs them. theses/bin/common.py already falls back to raw.githubusercontent.com, which works.
- The filings you read were collected by the hourly workflow and are in the checkout under data/filings/text/. Where a dossier shows an excerpt and names the whole file, read the file.

Report back:
- the tickers covered, each one's action, direction and conviction;
- the commit hash you pushed;
- roughly how long the run took, and which part took longest;
- anything you skipped, and why;
- and last, anything in the written instructions (RUNBOOK.md or PROMPTS.md) that was unclear, contradictory, or impossible to follow. That feedback is as valuable as the notes themselves, so be specific and quote the wording.
