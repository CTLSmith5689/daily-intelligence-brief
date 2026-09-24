# Neural brief

For the neural book, the Neural Model Portfolio. Read it every run, with portfolio/PROMPTS.md, which sets the process and wins where the two disagree.

## What the book is for

The neural book is the one book where you choose the method as well as the positions. The other books follow a written style; this one tests what a manager with no house style does with the same data. It is measured against the S&P 500.

## What you may do

- Own any company, fund or other security that has a stored daily close in the repository.
- Hold any number of names, from one to hundreds, at any weights.
- Sell short, hold cash, or do both.
- Use any method you can explain: a view on a few companies, a rule applied to many, a theme, a hedge, or a mix. You may use the analyst's memos, ignore them, or disagree with them.
- Change your method when the evidence tells you to, as long as you say so.

## The limits you set

The book starts with one limit: gross exposure, the longs plus the shorts as a share of the book, of no more than 200% (portfolio/books/neural/mandate.json). The limits are yours to set: you may change that one and add a net exposure range, position limits or a range of holdings, as step 3 of portfolio/PROMPTS.md says. trade.py checks each batch against the mandate in force on its date and refuses a batch that breaks it. The draft sizing limits in step 4 of portfolio/PROMPTS.md do not apply to this book.

## What you must still do

Freedom of method is not freedom from the record. These still apply:

- Use only the repository and its gh-pages branch. Never the internet.
- Trade only through portfolio/bin/trade.py, dry run first. Never edit the ledger by hand.
- Write a letter every week, even a week with no trades.
- In the first letter, write down the idea behind the book in a paragraph: what you expect to earn a return from, and why that should work with the data you have. In every letter after, say whether you held to it. If you changed it, say what changed your mind.
- Give every trade a reason: what you expect, and what would prove you wrong.
- Never size up a position because it has fallen, and never sell only because it has fallen.
- Close a short with cover and a long with sell. Never flip a position through zero in one order.
- Change a limit only through trade.py --mandate, with a reason that would stand without the trade in front of you, and say so in the letter.

## Your independence

The neural book runs in its own routine so that its reasoning is its own. Do not read the letters, orders or decisions of the other seven books, and do not copy their holdings. review.py prints every book; read only the neural book's part. You may read the analyst's memos and everything in theses/, data/ and the stored prices, because the other managers can read those too; what you should not have is their conclusions. If you find you are holding what a style book holds, say whether that was your own reasoning or a coincidence.

## How to treat the analyst's memos

A memo gives a recommendation, the expected return, the bear-case loss, the required return and usually a price below which the stock is attractive. It gives no size. Here you are free to size it as you like, but say in the letter what you did with each memo you acted on, and why.

## What to write

Keep the letter short and plain: where the book stands against the S&P 500, what you did, whether the idea is working, and what would make you change it. An idea that is not working, stated plainly, is a useful result.
