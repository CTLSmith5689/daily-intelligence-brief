# News desk routine

Schedule: Weekdays 06:00 ET and Sundays 15:00 ET

The prompt of the claude.ai routine that runs the News Desk on the Haiku 4.5 model, mirrored here so it is versioned and shown on the Research page. The routine does not read this file: when either changes, change the other by hand (theses/RUNBOOK.md).

---

Apterreon News Desk run. Unattended: do not ask questions. Do not use the internet: no web search, no web fetch, never open a link.

1. A checkout of CTLSmith5689/daily-intelligence-brief is already here, at /home/claude/daily-intelligence-brief. cd into it. If it is missing, stop and say so.
2. Read theses/NEWS_DESK.md and follow it exactly, step by step. Run each command exactly as it is written there.
3. Label the headlines yourself, from each title and source only, with the four fields NEWS_DESK.md gives. Write each batch to /tmp/news_labels/batch-NN.json.
4. Run theses/bin/news_labels_check.py --merge until it passes, or until each failed batch has been labelled twice.
5. Stage theses/news/ only, never git add -A. Run theses/bin/news_desk_check.py --changes before committing. Then commit and push, as NEWS_DESK.md step 6 sets out.

Never write or edit a file outside theses/news/ and /tmp/news_labels/. If a script fails, do not commit; report the exact error.

Report back:
- the names in scope;
- how many headlines you labelled, and in how many batches;
- which batches failed twice, or "none";
- the commit hash you pushed;
- and last, anything in NEWS_DESK.md that was unclear or impossible to follow, quoting the wording.
