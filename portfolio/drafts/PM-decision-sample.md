# PM decision on the NVIDIA memo: sample

What the PM in `PM-agent-draft.md` would write after reading `NVDA-memo.md`, in the format that draft specifies. It has two parts: the book as it would be saved at `portfolio/books/2026-09-23.md`, and the rows `decide.py` would append to `portfolio/ledger/decisions.csv`.

The run is dated 2026-09-23 so that it reads the same data as the memo. Two things are assumed that do not yet exist: the memo recorded in the analyst's ledger, and the scripts `decide.py` and `score_pm.py`. Everything else, including the output of `construct.py`, is the repository as it stands.

---

# Book, 2026-09-23

## At the top

The book holds no positions and is 100% cash. Since the first notes on 12 September, no note has produced a name the sizing rule can hold. CF was a long view at conviction 2, below the gate of 3, and was then revised to avoid. HIMS is avoid, AAPL and NVIDIA are watch, and MPC is no view.

None of the four statements required at the top applies yet. No prediction has been scored, and I have made no earlier decisions.

## Positions

| Ticker | Size | Rule weight | Conviction | thesis_id | decision_id | Why it is in the book |
|---|---|---|---|---|---|---|
| None | | | | | | |

Cash: 100%.

## Decisions

| Ticker | Memo action | Decision | Proposed size | Rule weight | Bear-loss limit | Track-record limit | Size decided | Reason |
|---|---|---|---|---|---|---|---|---|
| NVDA | Avoid | confirm | 0% | 7.4% | 4.4% | 3.7% | 0% | The expected total return of 11.3% is below the memo's own 12.0% required return, so test T1 fails for a purchase today. |
| NVDA | Initiate, if both November tests pass | approve, as a plan | 3.7% | 7.4% | 4.4% | 3.7% | 3.7% when the condition is met | The plan passes all four tests on the memo's November figures, and 3.7% is the smallest of the four limits. |

**NVDA, confirm Avoid at 0%.** I checked page one. The probabilities add to 100%. The weighted value is 0.25 x $370 + 0.50 x $260 + 0.25 x $125 = $253.75, and ($253.75 + $1.00) / $228.87 - 1 = 11.3%. The bear case at the full rule weight would cost 7.4% x 45.4% = 3.4% of the portfolio. The arithmetic is right.

| Test | Result |
|---|---|
| T1: expected total return above the required return | Fails today: 11.3% against 12.0% |
| T2: dated falsifier, and a monitoring plan with thresholds and exit rules | Passes: January-quarter guidance and DSO on 2026-11-17; ten thresholds, five of them exits |
| T3: the variable the thesis turns on is not in "What I do not know" | Passes, with a reservation below |
| T4: conviction of 3 or more | Passes: 4 |

The reservation on T3. The thesis turns on growth and on cash conversion, and both are reported. The memo's third risk, customer financing, turns on something it lists as unknown: which customers owe the $63.1 billion of receivables. The DSO test is the memo's only early measure of it. I size the plan as though that gap matters, which the limits below already do.

**NVDA, approve the plan: buy 3.7% if both November tests pass.** Condition: guidance of $125 billion or more for the January 2027 quarter, and DSO at or below 55 days at the October quarter end, both in the report due 2026-11-17. Review by 2026-11-20.

On the memo's November scenario, T1 passes: an expected total return of 24.2% against 12.0%. That figure depends on the analyst's move from 25/50/25 to 35/50/15, which the memo calls a judgment. On unchanged probabilities the expected return would be 13.5%, which still passes, by 1.5 points.

The size is the smallest of four limits. The memo proposes 3.7%. The rule weight is 7.4%. The bear-loss limit is 2% divided by 45.4%, or 4.4%. The track-record limit is half the rule weight, 3.7%, because none of the analyst's predictions has been scored. The result, 3.7%, is above the 3% minimum. If the plan is carried out, the bear case would cost the portfolio 1.7%.

I do not approve the memo's second step, to 7.4% after the February annual report, as proposed. Two limits stop it. At today's price and bear value, the bear-loss limit caps the holding at 4.4%. The full 7.4% would need a bear-case loss of no more than 27.0%, a bear value of $167 or more at $228.87. And the track-record limit holds any new position at 3.7% until 10 predictions have been scored. Only four predictions exist, and the first matures on 2027-01-16. So on present evidence the most the plan can reach is 3.7%. The analyst should know that before writing the February update.

**Points for the November update memo.** These are not a send-back, because this memo asks for no trade today. They are what I will check when the update arrives.

1. Show the expected return on the November figures at 25/50/25 as well as 35/50/15, and name the evidence other than the guidance that supports moving 10 points from the bear case to the bull case.
2. Give the bear value on the November figures at the price of the day. It sets the bear-loss limit.
3. Show T1 at a beta of 1.46 and of 1.91 as well as 1.61, since the memo says a beta of 1.46 closes today's gap.
4. Record the update as a revision of NVDA-2026-09-22, quoting its key claim, because the ledger already holds an initiation for NVIDIA. The target moves from $240 to $255 and the horizon from 252 to 365 days. Recording this memo as a second initiation would give NVIDIA two initiate events.

**Falsifier checked:** not yet checkable. It is reported on 2026-11-17.

## What changed since the previous book

This is the first book. Nothing was left alone, because nothing was held.

## Flags

| Flag | Severity | My decision |
|---|---|---|
| Fewer than two holdable names. `construct.py` stopped with "0 name(s) are holdable, which is not a portfolio" and exit code 1. | hard, until `construct.py` is fixed | Recorded. I wrote this book by hand from `events.csv`. If NVIDIA's plan is carried out in November it will be the only holding, and `construct.py` will refuse a one-name book as it stands. That has to be fixed before 17 November. |
| Large cash residual: 100% uninvested | soft | Accepted. No note has produced a holdable name, and the rule forbids scaling up to fill cash. The honest answer is to hold cash until one does. |
| Position limits | hard | Nothing held. The planned 3.7% sits inside the 3% to 12% limits, below the rule weight, and within the 2% bear-loss limit. |
| Sector cap | hard | Nothing held. NVIDIA would put 3.7% in Information Technology, against a 25% cap. |
| Correlated pair | soft | No holdings to correlate with. `--candidate` does not exist yet, so NVIDIA's correlation with the other covered names was not computed. |
| Conviction comparability | soft | Not raised: no two holdings. |
| Beta | soft | Book beta 0 last week and this week. The plan would add 3.7% x 1.91 = 0.07. |

## Risk figures

| Measure | This week | Last week |
|---|---|---|
| Book volatility | 0% (all cash) | none (first book) |
| Book beta | 0 | none |
| Largest sector weight | 0% | none |
| Cash | 100% | none |

## Open plans

| decision_id | Ticker | Condition | Review by |
|---|---|---|---|
| NVDA-2026-09-23-2 | NVDA | January 2027 quarter guidance of $125 billion or more and DSO at or below 55 days, in the report due 2026-11-17 | 2026-11-20 |

## What I could not resolve

- `construct.py` cannot build a book of fewer than two names, and the NVIDIA plan would create exactly one.
- The memo's Avoid is recorded as watch, so the analyst's call on NVIDIA carries no prediction and will not be scored. My confirmation will be scored only against the rule weight.
- The analyst's record is empty. Every limit that depends on it is at its most cautious setting, and will stay there until at least 10 predictions have been scored.

---

# Rows appended to portfolio/ledger/decisions.csv

The commands:

    python3 portfolio/bin/decide.py NVDA confirm --size 0 \
        --reason "Expected total return of 11.3% is below the memo's 12.0% required return (T1 fails)." \
        --falsifier "not yet checkable"
    python3 portfolio/bin/decide.py NVDA approve --size 0.037 \
        --condition "January 2027 quarter guidance >= 125B and DSO <= 55 days, report due 2026-11-17" \
        --review-by 2026-11-20 --follows NVDA-2026-09-23-1 \
        --reason "Passes T1 to T4 on the memo's November figures; 3.7% is the smallest of the proposal, the rule weight, the bear-loss limit and the track-record limit." \
        --falsifier "not yet checkable"

The rows, in the proposed column order:

```csv
decision_id,date,ticker,thesis_id,note_path,memo_action,decision,size_proposed,size_rule,size_prior,size_decided,entry_price,entry_source,horizon_days,expected_return,required_return,bear_return,bear_loss,condition,review_by,follows,falsifier_checked,flags,questions,reason
NVDA-2026-09-23-1,2026-09-23,NVDA,NVDA-2026-09-23,theses/notes/NVDA/2026-09-23-initiation.md,avoid,confirm,0.000,0.074,0.000,0.000,228.87,close_series 2026-09-22,365,0.113,0.120,-0.454,0.000,,2026-11-20,,not yet checkable,fewer_than_two_holdable;large_cash_residual,,"Expected total return of 11.3% is below the memo's 12.0% required return (T1 fails)."
NVDA-2026-09-23-2,2026-09-23,NVDA,NVDA-2026-09-23,theses/notes/NVDA/2026-09-23-initiation.md,initiate,approve,0.037,0.074,0.000,0.037,228.87,close_series 2026-09-22,365,0.242,0.120,-0.454,-0.017,"January 2027 quarter guidance >= 125B and DSO <= 55 days, report due 2026-11-17",2026-11-20,NVDA-2026-09-23-1,not yet checkable,fewer_than_two_holdable;large_cash_residual,,"Passes T1 to T4 on the memo's November figures; 3.7% is the smallest of the proposal, the rule weight, the bear-loss limit and the track-record limit."
```

How these rows will be graded. The first is scored at the memo's 365-day horizon: `value_vs_proposed` is zero, because I agreed with the analyst, and `value_vs_rule` is (0 - 0.074) times NVIDIA's return against its peers. If NVIDIA beats its peers over the year, my confirmation shows as a cost. The second row is a plan and is not scored. If the condition is met, the row that buys NVIDIA will be scored; if it is not, a row recording that the plan lapsed will follow it.
