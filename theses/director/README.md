# Research Director plans

One file per week, `{SUNDAY}.md`, written by the Research Director routine on the Sunday before the week it plans. The format and the process are in `theses/DIRECTOR.md`; `theses/bin/director_check.py` checks a plan.

`inputs/{WEEK_OF}.json` and `.md` are what `theses/bin/director_inputs.py` showed the director that week, committed with the plan so any plan can be read against its inputs. The headlines the director read are the News Desk's, in `theses/news/` (the dated copy for that Sunday).

On each weekday `theses/bin/prepare.py` reads the plan that covers the run date. If it passes `director_check.py`, that day's assignments take the first slots and the screen fills the rest. Otherwise the run is the screen's alone, and the run manifest's `director` key says why.

Plans are never deleted. A plan pushed here steers the next analyst run, so never commit a sample or test plan to this folder.
