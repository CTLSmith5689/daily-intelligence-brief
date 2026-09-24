# Research director routine

Schedule: Sundays 18:00 ET

The prompt of the claude.ai routine that runs the Research Director, mirrored here so it is versioned and shown on the Research page. The routine does not read this file: when either changes, change the other by hand (theses/RUNBOOK.md).

---

Apterreon weekly Research Director run. Unattended: do not ask questions. Do not use the internet.

1. A checkout of CTLSmith5689/daily-intelligence-brief is already here, at /home/claude/daily-intelligence-brief. cd into it. If it is missing, stop and say so: without it this session cannot deliver its work.
2. Read theses/DIRECTOR.md and follow it exactly, step by step. It says what to run, what to read, how to grade last week's memos and how to write the plan.
3. Write only the week's plan in theses/director/ and, if the evidence supports one, a dated lesson appended to the "## Lessons" section of a sector playbook in theses/desks/sectors/. Never write or edit a research note, a ledger, a script or any other file.
4. Run theses/bin/director_check.py on the plan and fix it until it passes. Then stage theses/director/ and theses/desks/sectors/ only, never git add -A, and run theses/bin/director_check.py --changes before committing.
5. Commit and push, as the last step of DIRECTOR.md sets out.

Two things worth knowing about this environment, both already handled in the code, so do not try to work around them:
- ctlsmith5689.github.io, www.sec.gov and data.sec.gov are refused by the egress proxy. Nothing you run needs them. theses/bin/common.py already falls back to raw.githubusercontent.com, which works.
- The analyst's next run starts at 07:00 ET on Monday and reads the plan you push. A plan that fails director_check.py is ignored, and the week runs from the screen alone.

Report back:
- the week planned, and each assignment's date, ticker, kind and desk;
- each memo's grade;
- any lesson appended, and to which playbook;
- the commit hash you pushed;
- and last, anything in the written instructions (DIRECTOR.md, PROMPTS.md or the playbooks) that was unclear, contradictory, or impossible to follow. Be specific and quote the wording.
