# portfolio/

## Model portfolios

Eight paper books, each started with $1,000,000: six style books (large, mid and
small by Russell rank; growth and value, split at the median of growth minus value)
measured against their Russell ETFs, and two books the PM builds, `hedge` (long and
short, against the S&P 500 and cash) and `neural` (a free hand, against the S&P 500).
Core, tax-managed and momentum books are reserved (`engine.PLANNED_BOOKS`), not built.
The code is `engine.py`; the method is printed on the site's Portfolios page and in
`FIELD_METHODS` (style_*, sales_ps_growth_3y, eps_growth_3y, book_*, cash_return).

| File | What it holds | Who writes it |
|---|---|---|
| `ledger/trades.csv` | every deposit and trade, with trade_id and lot_id | `bin/seed.py` at inception, then the PM through `bin/trade.py` |
| `ledger/decisions.csv` | every decision, with a short reason and its author | the same |
| `ledger/mandates.csv` | every mandate a book has had | `bin/seed.py` (the default), then `bin/trade.py --mandate` (the PM's own limits); a change is also a `mandate_change` decision |
| `books/<id>/mandate.json` | the current mandate (the last mandates.csv row) | the same |
| `orders/<date>/<book>.json` | the PM's order batches, as given to trade.py | the PM routine |
| `letters/<date>/<book>.md` | the PM's weekly letter per book | the PM routine |
| `../data/portfolio/nav.csv` | each book's value at each close | the daily run (`record_portfolio_nav`) |
| `../data/financials/style_history.csv` | three fiscal years per company, from 10-K facts | the daily run's weekly EDGAR pass |
| `PROMPTS.md` | the PM's process, which every PM routine follows, including how the PM sizes every position and which brief each book reads | the owner, by commit |
| `books/<style>.md` | the four book briefs: `growth.md`, `value.md`, `hedge.md`, `neural.md` | the owner, by commit |
| `routines/<pm>.md` | the two PM routines' claude.ai prompts, mirrored by hand: `style-pm.md` (six style books and hedge) and `neural-pm.md` (neural alone) | the owner, by commit (see `theses/RUNBOOK.md`) |

The Portfolios page prints `PROMPTS.md` and the routine files as they stand, and draws its
board (one column per book, one card per holding) from `ledger/trades.csv` and the stored
closes: no figure on it is typed in by a PM.

The CSVs are append-only with `merge=union`. `engine.append_rows` refuses a file whose
header differs; a new column goes through `engine.migrate_add_columns`, once, in
a quiet window. A short is a negative position whose sale proceeds are credited to
cash; gross and net exposure are computed from both sides. Named lots and FIFO are
both in the ledger arithmetic.

Prices are the stored close for the exact date (`docs/prices`). A missing close
leaves the day partial with its NAV blank; nothing is carried from another day.
Returns are price-only, and every trade pays 5 bps. The daily run only values the
books and republishes the rules candidate books; it never trades.

`bin/seed.py` is idempotent: a book with a deposit row is never seeded again, and
a style book whose size and style group is not yet full enough to fill its mandate waits.
`bin/trade.py` is dry-run by default, checks each batch against the mandate in
force on its date, and
is idempotent per book, date and order id. `bin/review.py` prints what the PM
reads. `bin/construct.py` is the earlier sizing rule; nothing runs it now, but
its rule weight, printed in each dossier, is one input to the PM's sizing
(`PROMPTS.md`, SIZE), for longs only.

## Agent 2 draft (earlier design)

Agent 2. Takes the theses agent 1 wrote and decides how much of the book is
willing to be wrong about each of them.

Like `theses/`, this is a new top-level directory that the pipeline's workflow
never stages, never regenerates and never force-pushes.

## The split

`construct.py` is arithmetic and constraints. It reads the current view per
ticker from `theses/ledger/events.csv`, sizes, and reports conflicts. It decides
nothing about whether a company is good, and it cannot read a note.

The PM **agent** reads the constructed book plus the flags, and writes the
judgment: whether to act on a flag, what to trade, what to leave. It cannot
change the arithmetic, and the flags it chooses not to act on stay in the file.

## Sizing

```
w = clamp(normalise(conviction / volatility_1y), 2%, 15%)   iterated to a fixed point
```

Conviction alone ignores risk. Inverse volatility alone ignores the analyst
entirely. This multiplies them and lets the cap bind.

It is not optimal under any model and is not meant to be. It is **legible**, and
a rule the PM can quietly abandon is worse than a blunt one it cannot.

Measured on nine real theses: book volatility 21.1% against 28.7% for equal
weight, and **book beta 1.24 against 1.72** &mdash; equal weight is not a neutral
default, it is an active decision to hold the most volatile names in the same
size as the calmest.

## What it flags rather than fixes

- **Sector cap breach** (hard). Blocks further adds in that sector.
- **Correlated pair** across sectors. IESC and TER correlate at 0.57 in
  different sectors: the same capex cycle, and no sector rule sees it.
- **Conviction comparability**. Two notes written in separate sessions with no
  memory of each other both say conviction 4. Nothing guarantees those mean the
  same thing. Bounded by the position cap, **not solved**.
- **No price series**. Excluded rather than sized blind.

A flag is recorded whether or not it is acted on. A PM that could silently
resolve its own conflicts would eventually resolve them all in the direction
that keeps the book intact.
