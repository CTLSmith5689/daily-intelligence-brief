# Hedge PM routine

Schedule: not scheduled yet

The prompt for the claude.ai routine that will run the Hedge Fund Strategy Model. It is not scheduled yet: until it is, the book holds its starting cash. Once the routine exists, this file mirrors its prompt and the two are kept in sync by hand (theses/RUNBOOK.md).

---

Apterreon weekly Hedge PM run. Unattended: do not ask questions.

1. A checkout of CTLSmith5689/daily-intelligence-brief is already here. Find it (it is usually at /home/claude/daily-intelligence-brief) and cd into it. If it is missing, stop and say so: without it this session cannot deliver its work.
2. Follow portfolio/PROMPTS.md exactly, with one change: this routine manages ONLY the hedge book (the Hedge Fund Strategy Model). Do not trade, write letters for, or change anything in the six style books or the neural book; they have their own routines.
3. Never use the internet. Everything you need is in the repository and its gh-pages branch.
4. Write every trade through portfolio/bin/trade.py (dry run first, then --write). Stage only portfolio/ (never git add -A), commit, and push to main.

The book starts in cash. Build it over as many weeks as you think right; cash is a position. Every short needs a written reason: what you expect to go wrong and what would prove you wrong. Keep gross and net exposure inside the limits in portfolio/books/hedge/mandate.json, and let trade.py check them.

Report back:
- the hedge book's trades (or "held"), its gross and net exposure after them, and the one-line reason;
- the commit hash you pushed;
- anything you skipped, and why;
- and last, anything in portfolio/PROMPTS.md or the scripts that was unclear, contradictory or impossible to follow. Quote the wording.
