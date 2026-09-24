# Hedge brief

For the hedge book, the Hedge Fund Strategy Model. Read it every run, with portfolio/PROMPTS.md, which wins where the two disagree.

## What the book is for

The hedge book buys companies it expects to do well and sells short companies it expects to do badly. To sell short is to borrow shares, sell them now, and buy them back later to return them; it makes money if the price falls. It aims to make money whether the market rises or falls, and is measured against the S&P 500 and against cash. Cash is a position.

## Gross and net exposure

Gross exposure is the longs plus the shorts, as a share of the book: 60% long and 40% short is 100% gross. Net exposure is the longs minus the shorts: the same book is 20% net long. Net says how much the book depends on the market rising. The mandate limits both, and trade.py checks them.

## Pairing

Pair a long with a short in the same industry: own the company you prefer and short the one you think weaker. If the whole industry falls, the short gains while the long loses, leaving the difference between the two companies, which is what you have a view on.

## Sizing

Size a long by step 4 of portfolio/PROMPTS.md. Never use the rule weight for a short; it was written for longs. Size a short from the risk that it goes against you. The two numbers below are drafts written with this brief, not approved by the owner:

- A new short starts at no more than half the mandate's short limit (draft).
- A long can lose at most what you paid; a short has no ceiling. So size a short from its bull case, the good outcome for the company. If the bull value is 60% above today's close, a 2% short loses 1.2% of the book in that case. Keep that loss at or below 1% of the book (draft).
- A short that rises grows as a share of the book without any trade. Cut it back each week if it has grown past its limit.

## Borrow and squeeze risk

A short needs shares to borrow, and the lender can ask for them back. This repository has no data on how hard a stock is to borrow or what it costs, so the book cannot see either. Say so when you open a short, and prefer large companies whose shares trade heavily.

A squeeze is a fast rise in price driven by short sellers all buying back at once, whatever the business is doing. It is most likely in small, heavily shorted companies after surprise good news. The book cannot see how much of a stock is shorted, so avoid shorting small companies, and do not hold a short through an earnings report without saying why.

## Short rules

1. Every short needs a written reason: what you expect to go wrong, and what would prove you wrong, with a number and a date.
2. A falling price alone is never a reason to short, and a rising price alone is never a reason to cover.
3. A memo that says Short is a reason to consider one, not an order. Avoid is not a reason to short: it means the return does not pay for the risk.
4. Cover when the reason has happened, when what would prove you wrong has happened, or when the short has grown past its limit.
5. Close a short with cover and a long with sell. Never flip a position through zero in one order.

## What to write

In each letter, give gross and net exposure, the pairs, each short's reason and its test, and the return against the S&P 500 and against cash.
