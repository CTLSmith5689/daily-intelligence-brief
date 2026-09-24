# Style PM routine

Schedule: Mondays 12:00 ET

The prompt of the claude.ai routine that runs the six style books, mirrored here so it is versioned and shown on the Portfolios page. The routine does not read this file: when either changes, change the other by hand (theses/RUNBOOK.md).

---

Apterreon weekly Style PM run. Unattended: do not ask questions.

1. A checkout of CTLSmith5689/daily-intelligence-brief is already here. Find it (it is usually at /home/claude/daily-intelligence-brief) and cd into it. If it is missing, stop and say so: without it this session cannot deliver its work.
2. Follow portfolio/PROMPTS.md exactly, with one change: this routine manages ONLY the six style books (lg-growth, lg-value, mid-growth, mid-value, sm-growth, sm-value). Do not trade, write letters for, or change anything in the hedge or neural books; they have their own routines.
3. Never use the internet. Everything you need is in the repository and its gh-pages branch.
4. Write every trade through portfolio/bin/trade.py (dry run first, then --write). Stage only portfolio/ (never git add -A), commit, and push to main.

If a style book has not started yet because its box lacks three years of history, that is expected this early. Run seed.py as the instructions say, and write that book's letter saying it is waiting.

Report back:
- for each style book: started or waiting, trades made (or "held"), and the one-line reason;
- the commit hash you pushed;
- anything you skipped, and why;
- and last, anything in portfolio/PROMPTS.md or the scripts that was unclear, contradictory or impossible to follow. Quote the wording.
