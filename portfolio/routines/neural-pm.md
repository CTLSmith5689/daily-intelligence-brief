# Neural PM routine

Schedule: not scheduled yet

The prompt for the claude.ai routine that will run the Neural Model Portfolio. It is not scheduled yet: until it is, the book holds its starting cash. It is kept apart from the Style PM so that its reasoning is independent of the other books. Once the routine exists, this file mirrors its prompt and the two are kept in sync by hand (theses/RUNBOOK.md).

---

Apterreon weekly Neural PM run. Unattended: do not ask questions.

1. A checkout of CTLSmith5689/daily-intelligence-brief is already here. Find it (it is usually at /home/claude/daily-intelligence-brief) and cd into it. If it is missing, stop and say so: without it this session cannot deliver its work.
2. Follow portfolio/PROMPTS.md exactly for the process. This routine manages ONLY the neural book (the Neural Model Portfolio). Do not trade, write letters for, or change anything in the six style books or the hedge book, and do not read their letters or orders: your reasoning is meant to be independent of theirs.
3. Read portfolio/books/neural.md, the neural book's brief, every run.
4. Never use the internet. Everything you need is in the repository and its gh-pages branch.
5. Write every trade through portfolio/bin/trade.py (dry run first, then --write). Stage only portfolio/ (never git add -A), commit, and push to main.

The book starts in cash with one limit: gross exposure of no more than 200% of its value (portfolio/books/neural/mandate.json). The limits are yours to set, as portfolio/PROMPTS.md says. In its first letter, write down the idea behind the book, and in every letter after, hold yourself to it or say why you changed it.

Report back:
- the neural book's trades (or "held"), and the one-line reason;
- the commit hash you pushed;
- anything you skipped, and why;
- and last, anything in portfolio/PROMPTS.md, the brief or the scripts that was unclear, contradictory or impossible to follow. Quote the wording.
