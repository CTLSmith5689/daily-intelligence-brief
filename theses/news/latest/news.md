# News as of 2026-09-24, for the week of 2026-09-21

As of 2026-09-24T15:21-04:00 US Eastern, from git refs/remotes/origin/gh-pages at c2d46d183f. Headlines are leads, never facts: a plan may cite one (with its tier and date) as a reason to look at a name, and never as evidence of a business fact. Weigh tier 1 and 2 only. Sources and tiers: `theses/news_sources.json`. Method: `theses/bin/news_pack.py`.

- Names in scope: 45 (abnormal volume 25, covered 5, screen top 15)
- Headlines in the last 14 days, after removing 38 duplicates: tier 1 19, tier 2 84, tier 3 307
- Price claims: 26 found, 6 checked against stored closes, 0 off by more than 1.5 points, 5 unverifiable, 15 not checkable
- For labelling: 103 headlines in 3 batches

## Names

| Ticker | Why | T1 | T2 | T3 | 7d vs baseline | LM (T1-2) | VADER (T1-2) | Flags |
|---|---|---|---|---|---|---|---|---|
| AAPL | covered | 0 | 3 | 12 | 15 vs 15 (own) | n/a | -0.03 |  |
| AGBK | abnormal volume, rank 13 | 0 | 2 | 3 | 5 vs 0 (own) | n/a | +0.00 |  |
| AGX | screen top | 0 | 6 | 7 | 10 vs 11 (own) | +1.00 | +0.33 |  |
| BBT | abnormal volume, rank 3 | 0 | 5 | 9 | 15 vs 0.75 (own) | n/a | +0.00 |  |
| BE | abnormal volume, rank 1 | 2 | 5 | 6 | 12 vs 0 (own) | +0.00 | +0.53 |  |
| BLDP | abnormal volume, rank 8 | 0 | 1 | 6 | 8 vs 0.25 (own) | +1.00 | +0.84 |  |
| CART | abnormal volume, rank 21 | 1 | 2 | 5 | 9 vs 1 (own) | n/a | +0.00 |  |
| CF | covered | 1 | 0 | 10 | 14 vs 6.25 (own) | -1.00 | +0.00 |  |
| DCOY | abnormal volume, rank 2 | 1 | 0 | 11 | 13 vs 0.25 (own) | n/a | +0.00 |  |
| DELL | screen top | 1 | 6 | 3 | 10 vs 15 (own) | n/a | +0.32 |  |
| EHTH | abnormal volume, rank 24 | 0 | 1 | 3 | 5 vs 0.25 (own) | n/a | +0.00 |  |
| ESE | abnormal volume, rank 5 | 0 | 1 | 6 | 7 vs 0 (own) | n/a | +0.00 |  |
| ETRA | abnormal volume, rank 16 | 1 | 0 | 13 | 15 vs 2 (universe median) | n/a | +0.00 |  |
| GOLD | screen top | 1 | 1 | 7 | 9 vs 7.75 (own) | -1.00 | -0.11 |  |
| GSAT | abnormal volume, rank 20 | 0 | 1 | 9 | 11 vs 1.25 (own) | n/a | +0.00 |  |
| HIMS | covered | 5 | 2 | 6 | 13 vs 13.25 (own) | -0.67 | +0.05 |  |
| HTO | abnormal volume, rank 6 | 0 | 2 | 4 | 7 vs 0 (own) | n/a | +0.00 |  |
| IMVT | abnormal volume, rank 23 | 0 | 4 | 10 | 15 vs 2.25 (own) | -1.00 | -0.24 |  |
| JELD | abnormal volume, rank 10 | 1 | 0 | 8 | 9 vs 0.5 (own) | n/a | -0.36 |  |
| JOE | screen top | 0 | 1 | 3 | 4 vs 2.25 (own) | n/a | +0.00 |  |
| KALU | screen top | 0 | 1 | 9 | 9 vs 5.5 (own) | n/a | +0.00 |  |
| KIDZ | abnormal volume, rank 7 | 0 | 1 | 10 | 12 vs 0.75 (own) | n/a | +0.61 |  |
| KRT | screen top | 0 | 0 | 0 | 0 vs 1 (own) | n/a | n/a |  |
| LNAI | abnormal volume, rank 12 | 1 | 0 | 6 | 8 vs 0.5 (own) | +1.00 | +0.62 |  |
| LSTR | abnormal volume, rank 25 | 0 | 1 | 4 | 5 vs 0.25 (own) | n/a | +0.00 |  |
| MPC | covered | 0 | 4 | 11 | 15 vs 14.75 (own) | +1.00 | +0.21 |  |
| MRBK | abnormal volume, rank 17 | 0 | 1 | 4 | 15 vs 2 (own) | -1.00 | +0.62 |  |
| MU | screen top | 1 | 5 | 9 | 15 vs 14.5 (own) | -1.00 | -0.01 |  |
| NESR | screen top | 0 | 4 | 9 | 10 vs 8.5 (own) | +1.00 | +0.52 |  |
| NUTX | screen top | 0 | 0 | 6 | 4 vs 2.5 (own) | n/a | n/a | 6 |
| NVDA | covered | 0 | 5 | 10 | 15 vs 15 (own) | +0.00 | +0.32 |  |
| PAAI | abnormal volume, rank 15 | 1 | 1 | 11 | 12 vs 1.25 (own) | n/a | +0.25 |  |
| PGEN | abnormal volume, rank 9 | 1 | 1 | 5 | 8 vs 0.25 (own) | n/a | +0.25 |  |
| RNG | screen top | 0 | 4 | 8 | 14 vs 10.75 (own) | +1.00 | +0.18 |  |
| SFBS | abnormal volume, rank 11 | 1 | 1 | 11 | 14 vs 1.5 (own) | n/a | +0.00 |  |
| SFIX | abnormal volume, rank 18 | 0 | 3 | 12 | 15 vs 2 (own) | -1.00 | -0.16 |  |
| SLDE | screen top | 0 | 1 | 6 | 1 vs 9.5 (own) | n/a | +0.32 |  |
| SPB | screen top | 0 | 0 | 0 | 0 vs 0 (own) | n/a | n/a |  |
| STRR | abnormal volume, rank 4 | 0 | 1 | 6 | 11 vs 0.5 (own) | n/a | +0.00 |  |
| SVIA | abnormal volume, rank 19 | 0 | 1 | 12 | 15 vs 2 (universe median) | n/a | +0.00 |  |
| TNC | abnormal volume, rank 14 | 0 | 2 | 4 | 5 vs 0 (own) | n/a | +0.00 |  |
| UVE | screen top | 0 | 0 | 1 | 1 vs 1.25 (own) | n/a | n/a | 1 |
| VSXY | screen top | 0 | 3 | 4 | 5 vs 12 (own) | n/a | +0.61 |  |
| WBUY | abnormal volume, rank 22 | 0 | 1 | 3 | 4 vs 0 (own) | -1.00 | +0.00 |  |
| YOU | screen top | 0 | 0 | 5 | 4 vs 8.75 (own) | n/a | n/a | 5 |

## Headlines, tier 1 and 2

Newest first within each tier. A flag means: do not act on this headline alone.

### AAPL Apple Inc.

- `AAPL-648ad4ed06` 2026-09-22, tier 2, finance.yahoo.com: TSLA, SPCX, AAPL Could Be Next To ‘Rip’ After Meta’s Muse Rally, Analyst Says, Here Are The Catalysts
- `AAPL-f4cdc2a066` 2026-09-21, tier 2, finance.yahoo.com: Apple (AAPL) Eyes India Payments With Apple Pay Debut Next Month
- `AAPL-2e95e9106d` 2026-09-20, tier 2, finance.yahoo.com: Apple (AAPL)’s New CEO Bets Big on a $1,999 Foldable iPhone

### AGBK AGI Inc Class A Common Shares

- `AGBK-89616a6bc0` 2026-09-23, tier 2, Yahoo Finance UK: AGI Inc (AGBK) Analyst insights, Price targets and Recommendations
- `AGBK-9f6a11874c` 2026-09-21, tier 2, Yahoo Finance: AGBK S-8 & SEC Filings

### AGX Argan, Inc.

- `AGX-3e8652477d` 2026-09-23, tier 2, Yahoo Finance: Is Argan (AGX) a Solid Growth Stock? 3 Reasons to Think "Yes"
- `AGX-29fde2e0f9` 2026-09-23, tier 2, The Globe and Mail: ARGAN’s AutOnom® Warehouses Use Solar Power and Heat Pumps to Cool Logistics Sites
- `AGX-3bc1e76882` 2026-09-21, tier 2, The Globe and Mail: Argan vs. MYR Group: Which Power Infrastructure Stock Is a Better Buy?
- `AGX-d8263d3c09` 2026-09-16, tier 2, Yahoo Finance: Can Argan's Industrial Expansion Drive Growth Despite Margin Pressure?
- `AGX-5b53cc121b` 2026-09-15, tier 2, Yahoo Finance: Argan (AGX) Stock Looks Stretched After Record Earnings And Dividend Rise
- `AGX-aa746ae1b0` 2026-09-13, tier 2, Yahoo Finance: Argan’s (AGX) Power Boom Comes With A Margin Catch

### BBT Beacon Financial Corp.

- `BBT-126a2869e3` 2026-09-22, tier 2, The Globe and Mail: Beacon Financial names Sean Gray as new CEO
- `BBT-3090b3753b` 2026-09-21, tier 2, American Banker: One year after formation, Beacon Financial names new CEO
- `BBT-bf65530a30` 2026-09-21, tier 2, The Business Journals: Beacon Bank appoints new CEO, previous CEO to retire, consult
- `BBT-7cba3f0ee0` 2026-09-21, tier 2, Yahoo Finance: Beacon Financial Names Sean Gray as Chief Executive
- `BBT-7e2b5bd131` 2026-09-21, tier 2, Yahoo Finance: Beacon Financial Corporation Announces CEO Transition: Sean A. Gray Appointed Chief Executive Officer; Paul A. Perrault to Retire

### BE Bloom Energy

- `BE-c8e71eef73` 2026-09-23, tier 1, PR Newswire: Levi & Korsinsky Reminds Bloom Energy Investors of the Pending Class Action Lawsuit With a Lead Plaintiff Deadline of September 28, 2026 - BE
- `BE-3db80373a9` 2026-09-23, tier 1, TMX Newsfile: BE SHAREHOLDER ACTION REMINDER: Faruqi & Faruqi, LLP Reminds Bloom Energy (BE) Investors of Securities Class Action Lawsuit Deadline on September 28, 2026
- `BE-0e8ea25778` 2026-09-23, tier 2, finance.yahoo.com: Bloom Energy vs. Marathon Petroleum: Which Popular Energy Stock Is the Better Buy?
- `BE-a4f43fd35c` 2026-09-23, tier 2, finance.yahoo.com: Plug Power vs. Bloom Energy: Which Clean Energy Stock Has More Upside?
- `BE-8ddbe78c4f` 2026-09-22, tier 2, finance.yahoo.com: Bloom Energy vs. Eos Energy Enterprises: Which Energy Storage Stock Is a Better Buy in 2026?
- `BE-e54f361189` 2026-09-21, tier 2, finance.yahoo.com: BE Stock Jumps Premarket Ahead Of S&P 500 Inclusion: US Army Awards Power Leases To Indelible-Bloom Coalition
- and 1 more in news.json

### BLDP Ballard Power Systems, Inc. - Common Shares

- `BLDP-02989facf3` 2026-09-22, tier 2, Yahoo Finance: BE vs. BLDP: Which Clean Energy Stock Has Stronger Growth Potential?

### CART Maplebear Inc.

- `CART-781ee3a50c` 2026-09-23, tier 1, Barron's: Instacart Adds Another AI Partner to the List: Meta’s Muse. Maplebear Stock Falls.
- `CART-39c953135b` 2026-09-23, tier 2, Yahoo Finance: Why Instacart (CART) Shares Are Trading Lower Today
- `CART-595b149b70` 2026-09-23, tier 2, CNBC: Stocks making the biggest moves premarket: IonQ, Maplebear, Worthington & more

### CF CF Industries

- `CF-d61f7e0d74` 2026-09-21, tier 1, Barron's: Fertilizer Deal With Belarus Is In the Works, Says Trump. CF Industries and Nutrien Stocks Fall.

### DCOY Decoy Therapeutics Inc. - Common Stock

- `DCOY-64a05c276a` 2026-09-22, tier 1, PR Newswire: /C O R R E C T I O N -- Decoy Therapeutics, Inc/

### DELL Dell Technologies

- `DELL-95d2b8e56d` 2026-09-21, tier 1, Business Wire: Dell Technologies Introduces XPS Googlebook: The First XPS Laptop Built for Gemini Intelligence
- `DELL-d82dcbc2ad` 2026-09-23, tier 2, finance.yahoo.com: Is Dell Technologies (DELL) Reasonable on Cash Flow After Its Rally?
- `DELL-9a52cc62fb` 2026-09-23, tier 2, finance.yahoo.com: Are You Looking for a Top Momentum Pick? Why Dell Technologies (DELL) is a Great Choice
- `DELL-01b8cf40cc` 2026-09-22, tier 2, MarketWatch: Dell Technologies Inc. Cl C stock falls Tuesday, underperforms market
- `DELL-c60b05d832` 2026-09-21, tier 2, finance.yahoo.com: Dell Technologies Insider Sold Shares Worth $13,979,586, According to a Recent SEC Filing
- `DELL-a9d6ca2422` 2026-09-21, tier 2, finance.yahoo.com: DELL Rides on Growing AI Clientele: Can the Stock Outpace SMCI & CSCO?
- and 1 more in news.json

### EHTH eHealth, Inc. - Common Stock

- `EHTH-7a86c96024` 2026-09-18, tier 2, Yahoo Finance: Employer Health Costs Surge: What it Means for EHTH, CNC & UNH

### ESE ESCO Technologies Inc.

- `ESE-3fc30cdcce` 2026-09-22, tier 2, Yahoo Finance: Is ESCO Technologies (ESE) Stock Outpacing Its Business Services Peers This Year?

### ETRA Electra Therapeutics, Inc. - Common stock

- `ETRA-a0182bc623` 2026-09-17, tier 1, GlobeNewswire: Electra Therapeutics Announces Pricing of Upsized $350.0 Million Initial Public Offering

### GOLD Gold.com, Inc. Common Stock

- `GOLD-2fb4116c2f` 2026-09-23, tier 1, Bloomberg.com: Gold Steadies as Iran Talks Temper Concerns Over Fed Rate Path
- `GOLD-8d23e73b6a` 2026-09-23, tier 2, thestreet.com: Top Wall Street firm resets gold price target for 2030

### GSAT Globalstar, Inc. - Common Stock

- `GSAT-02d40331d9` 2026-09-22, tier 2, Yahoo Finance: Globalstar (GSAT) Stock Looks Fully Priced On Its 346% Run

### HIMS Hims & Hers Health

- `HIMS-003c38fbb3` 2026-09-23, tier 1, TMX Newsfile: ROSEN, LEADING INVESTOR COUNSEL, Encourages Hims & Hers Health, Inc. Investors to Secure Counsel Before Important Deadline in Securities Class Action - HIMS
- `HIMS-dfad1a0243` 2026-09-23, tier 1, GlobeNewswire: Bragar Eagel & Squire, P.C. Reminds Hims & Hers Health,
- `HIMS-abfef10536` 2026-09-23, tier 1, GlobeNewswire: HIMS Shareholder Alert: Investigation of Potential
- `HIMS-947a6f83f4` 2026-09-23, tier 1, TMX Newsfile: HIMS SHAREHOLDER ACTION REMINDER: Faruqi & Faruqi, LLP Reminds Hims Investors of Securities Class Action Lawsuit Deadline on November 2, 2026
- `HIMS-96cfd75cbf` 2026-09-21, tier 1, PR Newswire: Hims & Hers Health (HIMS) Investors: Securities Fraud Class Action Filed, Contact Hagens Berman Before November 2, 2026 Lead Plaintiff Deadline
- `HIMS-9661470f80` 2026-09-22, tier 2, Law360: Hims & Hers Suit Spotlights Health Data-Sharing Privacy Risks
- and 1 more in news.json

### HTO H2O America

- `HTO-5781321098` 2026-09-21, tier 2, Yahoo Finance: H2O America Gets Regulatory Clearance for Texas Unit's Cibolo Valley Acquisition
- `HTO-a4ca296dde` 2026-09-16, tier 2, Yahoo Finance: H2O America (HTO) Stock Forecasts

### IMVT Immunovant, Inc.  - Common Stock

- `IMVT-411cfb4761` 2026-09-23, tier 2, Fierce Biotech: Immunovant ends cutaneous lupus program after phase 2 flop
- `IMVT-acbfaa3f5f` 2026-09-23, tier 2, Yahoo Finance: Immunovant Pulls Plug On Lupus Program After Study Misses Primary Goal, IMVT Stock Heads For Over 3-Month Lows
- `IMVT-918eb8ecf2` 2026-09-23, tier 2, Yahoo Finance: Roivant’s Immunovant Stops IMVT-1402 Development in Cutaneous Lupus After Trial Miss
- `IMVT-b2b86404b3` 2026-09-23, tier 2, Yahoo Finance: Immunovant Shares Fall 6% After IMVT-1402 Lupus Study Misses Primary Endpoint

### JELD JELD-WEN Holding, Inc. Common Stock

- `JELD-89392b6c48` 2026-09-21, tier 1, Bloomberg.com: Window Maker Jeld-Wen Nears Deal for New Money, Debt Extension

### JOE St. Joe Company

- `JOE-4f98a471bc` 2026-09-17, tier 2, Yahoo Finance: The St. Joe Company Announces Plans for Additional 3,500 Homes in the Latitude Margaritaville Watersound® Community

### KALU Kaiser Aluminum Corporation

- `KALU-b30729d7ff` 2026-09-21, tier 2, Yahoo Finance: Are Investors Undervaluing Kaiser Aluminum (KALU) Right Now?

### KIDZ KIDZ AI Inc. - Class B Common Stock

- `KIDZ-fdb03493a9` 2026-09-23, tier 2, Yahoo Finance: KIDZ AI Secures Open Compute Project Membership to Support GPU Infrastructure Strategy

### LNAI Lunai Bioworks Inc. - Common Stock

- `LNAI-9c3bae6b9f` 2026-09-22, tier 1, PR Newswire: Lunai Bioworks (NASDAQ: LNAI) Expands Parkinson's Platform Through Exclusive Tanaist Agreement

### LSTR Landstar System

- `LSTR-fe4d9a3e62` 2026-09-23, tier 2, Yahoo! Finance Canada: Landstar System, Inc. (LSTR) Options Chain - Yahoo Finance

### MPC Marathon Petroleum

- `MPC-0e8ea25778` 2026-09-23, tier 2, finance.yahoo.com: Bloom Energy vs. Marathon Petroleum: Which Popular Energy Stock Is the Better Buy?
- `MPC-04d151e0b7` 2026-09-21, tier 2, CBS News: Contractor at St. Paul Park Marathon Petroleum site dies, police investigating
- `MPC-1032d062ee` 2026-09-20, tier 2, finance.yahoo.com: Morgan Stanley Sees Marathon Petroleum (MPC) Breaking into New Highs
- `MPC-dff8904322` 2026-09-20, tier 2, finance.yahoo.com: Marathon Petroleum (MPC) Could Smash its All-Time High, UBS Says

### MRBK Meridian Corporation - Common Stock

- `MRBK-144fca8817` 2026-09-23, tier 2, Morningstar: SHAREHOLDER ALERT: Levi & Korsinsky, LLP Notifies Shareholders of an Investigation Concerning Possible Breaches of Fiduciary Duties by Certain Officers and Directors of Meridian Corporation (NASDAQ: M

### MU Micron Technology

- `MU-cb6c08533f` 2026-09-23, tier 1, Barron's: Micron Faces Potential Strike at Major Chip Factory. The Stock Rises Anyway.
- `MU-10654f9318` 2026-09-23, tier 2, Yahoo! Finance Canada: Micron keeps analysts bullish as memory demand strengthens
- `MU-9915294889` 2026-09-23, tier 2, finance.yahoo.com: Micron Technology (MU) Draws Michael Burry Short As Memory Price Warning Grows
- `MU-5de2ee10c1` 2026-09-23, tier 2, Investor's Business Daily: Michael Burry Shorts Micron, Palantir; Famed Investor Expects Chip 'Down Cycle'
- `MU-29286e793c` 2026-09-23, tier 2, Forbes: Micron Technology Stock Is Up Over 260%. Where It May Be Heading Next
- `MU-2112d1c179` 2026-09-22, tier 2, finance.yahoo.com: Micron's Memory Boom Is Facing a New Test

### NESR National Energy Services Reunited Corp - Ordinary Shares

- `NESR-7b8bd8f237` 2026-09-22, tier 2, The Globe and Mail: NESR Leverages Technology Portfolio and Scale to Drive Growth
- `NESR-f4459875ed` 2026-09-18, tier 2, Yahoo Finance Singapore: Does Strong Earnings Beat And Kuwait Mega Contract Change The Bull Case For National Energy Services (NESR)?
- `NESR-f18ff011a3` 2026-09-16, tier 2, Yahoo Finance: NESR vs. FET: Which Stock Should Value Investors Buy Now?
- `NESR-462079c766` 2026-09-16, tier 2, Yahoo Finance: NESR Stock Trades at a Premium: Still an Attractive Opportunity?

### NUTX Nutex Health Inc. - Common Stock (tier 3 only)

- `NUTX-493ff99201` 2026-09-23, tier 3, TradingView: Nutex Health receives approval for dual-listing on Nasdaq Texas **tier 3 only**
- `NUTX-5ef4641fc5` 2026-09-19, tier 3, simplywall.st: Earnings Outlook Upgrade Might Change The Case For Investing In Nutex Health Stock (NUTX) **tier 3 only**
- `NUTX-7891f014f5` 2026-09-19, tier 3, Eastern Progress: Wall Street Analysts See a 47.12% Upside in Nutex Health (NUTX): Can the Stock Really Move This High? **tier 3 only**

### NVDA Nvidia

- `NVDA-f686d4065d` 2026-09-23, tier 2, The Globe and Mail: Forget Waiting for a Dip: Nvidia (NVDA) Is Worth Buying Today Based on The Motley Fool's Long-Term Conviction Ratings
- `NVDA-285509edc2` 2026-09-23, tier 2, Investor's Business Daily: Nvidia Scores Winning Streak: Trump-Xi Meeting, China Sales Outlook In Focus
- `NVDA-973a801302` 2026-09-23, tier 2, finance.yahoo.com: IonQ is in 'the beginning of an exciting era,' CEO says as Nvidia deal lifts stock
- `NVDA-d2bfb88cc6` 2026-09-23, tier 2, Morningstar: Nvidia’s Immense Dividend Hike Puts Dividend Growth Stocks in the Spotlight
- `NVDA-2b1cbbdfc2` 2026-09-22, tier 2, CNBC: Nvidia options are doing something unusual ahead of two catalysts. Here's how one trader is playing it

### PAAI Paradium.AI, Inc. Common Stock

- `PAAI-5aa11e9e8b` 2026-09-17, tier 1, Business Wire: Paradium.AI Signs 10-Year Strategic Platform Agreement with Roundtable (Nasdaq: RTB)
- `PAAI-659a7a8ac6` 2026-09-23, tier 2, Yahoo Finance: Paradium.AI, Inc. (PAAI) Q1 FY2026 earnings call transcript

### PGEN Precigen, Inc. - Common Stock

- `PGEN-241a12d6e9` 2026-09-21, tier 1, PR Newswire: Precigen Receives FDA Platform Technology Designation for AdenoVerse Platform
- `PGEN-f240d9c4c3` 2026-09-18, tier 2, Yahoo Finance: Precigen (PGEN) Stock May Be Fully Valued On Its Current Sales Base

### RNG RingCentral, Inc.

- `RNG-d6daea877a` 2026-09-23, tier 2, Yahoo Finance: Why Did RingCentral (RNG) Move Today?
- `RNG-3853db746e` 2026-09-22, tier 2, Yahoo Finance: RingCentral (RNG) Stock Looks Hard To Call Following New AI Plugin News
- `RNG-ebf617643c` 2026-09-22, tier 2, Yahoo Finance: Can RingCentral’s (RNG) New AI Plugins Quietly Reshape Its Competitive Moat In Cloud Communications?
- `RNG-82947a68bd` 2026-09-21, tier 2, Yahoo Finance: RingCentral Sees AI Driving Upsell, Growth and Stronger Customer Retention

### SFBS ServisFirst Bancshares, Inc.

- `SFBS-ff67fd3670` 2026-09-21, tier 1, GlobeNewswire: ServisFirst Bancshares, Inc. Declares Third Quarter Cash Dividend
- `SFBS-0658f47656` 2026-09-23, tier 2, Yahoo Finance: SFBS 8-K & SEC Filings

### SFIX Stitch Fix, Inc. - Class A Common Stock

- `SFIX-6b1670eda8` 2026-09-23, tier 2, Yahoo Finance: Stitch Fix, Inc. Q4 2026 Earnings: Live Updates of $SFIX Earnings Call, Stock Forecast
- `SFIX-f762bc85d6` 2026-09-23, tier 2, Yahoo Finance: Stitch Fix (SFIX) Reports Q4 Loss, Misses Revenue Estimates
- `SFIX-fab20a3ad6` 2026-09-21, tier 2, The Globe and Mail: Earnings To Watch: Stitch Fix (SFIX) Reports Q2 Results Tomorrow

### SLDE Slide Insurance Holdings, Inc. - Common Stock

- `SLDE-205aa7a702` 2026-09-15, tier 2, Yahoo Finance: Slide Insurance Holdings, Inc. (SLDE) Hits Fresh High: Is There Still Room to Run?

### STRR Star Equity Holdings, Inc. - Common Stock

- `STRR-9196c92eb7` 2026-09-23, tier 2, Yahoo Finance: STRR 8-K & SEC Filings

### SVIA Silvia, Inc. - Common Stock

- `SVIA-abf254092a` 2026-09-22, tier 2, Yahoo Finance UK: Silvia, Inc. Begins Trading on Nasdaq Under New Ticker "SVIA"

### TNC Tennant Company

- `TNC-b56225a295` 2026-09-22, tier 2, The Globe and Mail: Toro Company Adds Tennant CEO to Board of Directors
- `TNC-c78ea1744f` 2026-09-17, tier 2, Yahoo Finance: Tennant Company (TNC) Stock Forecasts

### UVE UNIVERSAL INSURANCE HOLDINGS INC Common Stock (tier 3 only)

- `UVE-7472994a56` 2026-09-18, tier 3, The Chronicle-Journal: UNIVERSAL INSURANCE HOLDINGS INC Common Stock (NY: UVE **tier 3 only**

### VSXY Victoria's Secret

- `VSXY-4dad21f691` 2026-09-22, tier 2, Yahoo Finance: Can Victoria's Secret's Path to Potential Sustain Broad-Based Growth?
- `VSXY-cce5854e08` 2026-09-16, tier 2, Yahoo Finance: Victoria's Secret (VSXY) CEO Hillary Super Draws Attention For A Rapid Turnaround
- `VSXY-280c5365ea` 2026-09-14, tier 2, Yahoo Finance: Is VSXY Worth Buying as Growth Improves but Valuation Stays Rich?

### WBUY WEBUY GLOBAL LTD. - Class A Ordinary Shares

- `WBUY-aa95632f2a` 2026-09-19, tier 2, The Globe and Mail: Webuy Global Receives Nasdaq Notice Over Minimum Bid Price Deficiency

### YOU Clear Secure, Inc. (tier 3 only)

- `YOU-05ae9e3401` 2026-09-21, tier 3, GuruFocus: Is Clear Secure Inc (YOU) Overvalued After 3.0% Rally? GF Value **tier 3 only**
- `YOU-66ff6a9e5b` 2026-09-21, tier 3, simplywall.st: How Leadership Changes Will Impact Clear Secure (YOU) Investors **tier 3 only**
- `YOU-81528d5b9f` 2026-09-18, tier 3, GuruFocus: Clear Secure Inc (YOU) Stock Down 5.0% but Still Overvalued -- G **tier 3 only**

## Price claims that do not match the stored closes

- None.
