# Style PM routine

Schedule: Mondays 12:00 ET

The prompt of the claude.ai routine that runs the six style books and the hedge book, mirrored here so it is versioned and shown on the Portfolios page. The routine does not read this file: when either changes, change the other by hand (theses/RUNBOOK.md).

---

Apterreon weekly Style PM run. Unattended: do not ask questions.

1. A checkout of CTLSmith5689/daily-intelligence-brief is already here. Find it (it is usually at /home/claude/daily-intelligence-brief) and cd into it. If it is missing, stop and say so: without it this session cannot deliver its work.
2. Follow portfolio/PROMPTS.md exactly for the process. This routine manages ONLY the six style books (lg-growth, lg-value, mid-growth, mid-value, sm-growth, sm-value) and the hedge book (the Hedge Fund Strategy Model). Do not trade, write letters for, or change anything in the neural book; it has its own routine.
3. Read the brief for each book you run, every run: portfolio/books/growth.md for the three growth books, portfolio/books/value.md for the three value books, and portfolio/books/hedge.md for the hedge book.
4. You size every position, by the SIZE step of portfolio/PROMPTS.md. The analyst's memos give no size.
5. Never use the internet. Everything you need is in the repository and its gh-pages branch.
6. Write every trade through portfolio/bin/trade.py (dry run first, then --write). Stage only portfolio/ (never git add -A), commit, and push to main.

If a style book has not started yet because its size and style group lacks three years of history, that is expected this early. Run seed.py as the instructions say, and write that book's letter saying it is waiting. The hedge book starts in cash; build it over as many weeks as you think right.

Report back:
- for each style book and the hedge book: started or waiting, trades made (or "held"), and the one-line reason; for hedge, its gross and net exposure after the trades;
- the limits you set or changed for any book (trade.py --mandate), and why;
- any position where a draft sizing limit bound, and by how much;
- the commit hash you pushed;
- anything you skipped, and why;
- and last, anything in portfolio/PROMPTS.md, the briefs or the scripts that was unclear, contradictory or impossible to follow. Quote the wording.
