# portfolio/

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
