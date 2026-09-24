# News Desk output

Written by the News Desk routine (`theses/NEWS_DESK.md`, prompt in `theses/routines/news-desk.md`) on weekdays at 06:00 and Sundays at 15:00 US Eastern.

- `latest/news.json`, `latest/news.md`: the latest pack from `theses/bin/news_pack.py`: headlines for the names in scope, graded by source tier, duplicates removed, price claims checked against the stored closes.
- `latest/news_labels.json`: the News Desk's labels for the tier 1 and 2 headlines, checked by `theses/bin/news_labels_check.py`.
- `YYYY-MM-DD/`: a dated copy of each run, made by `theses/bin/news_desk_check.py --archive`.

Everything here is derived from the published site and can be rebuilt. It is not ledger data. The routine keeps the last 14 days of dated copies and deletes older ones. The Research Director and the analyst's dossier read `latest/` only when its as-of time is at most 36 hours old, and go on without news otherwise. Paths, retention and freshness are set in `theses/bin/common.py`.

The News Desk's commit may touch only this folder (`news_desk_check.py --changes`). Nothing else writes here, and no sample or test data belongs here.
