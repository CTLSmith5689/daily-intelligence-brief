# Desks and sector playbooks

The research is organised in two layers.

- **Sector playbooks**, one per GICS sector, in `sectors/`. Each says which questions decide a stock in that sector, which valuation measures suit each kind of business in it, the common traps, and where our data misleads. The claims about our data were each checked against the panel of 2026-09-23 when the playbook was written. The dossier puts the name's playbook in front of the analyst.
- **Desks**, five of them, in this folder. A desk owns every name in its sectors, gets the Research Director's focus note each week, and is the unit the director's scorecards count. The dossier puts the owning desk's file after the playbook.

| Desk | File | Sectors |
|---|---|---|
| Technology and communications | `tech-comms.md` | Information Technology, Communication Services |
| Energy, materials and utilities | `energy-materials-utilities.md` | Energy, Materials, Utilities |
| Financials and real estate | `financials-realestate.md` | Financials, Real Estate |
| Health care | `health-care.md` | Health Care |
| Consumer and industrials | `consumer-industrials.md` | Consumer Discretionary, Consumer Staples, Industrials |

The table is for reading. The map the code uses is the `desks` key of `theses/config.json`, and `theses/bin/desks.py` is the only code that reads it. A test fails if the two disagree, if a sector on the panel has no desk, or if a desk or playbook file is missing.

Every covered name has exactly one owning desk, derived from its sector. `theses/ledger/events.csv` has no desk column and needs none. A name with no sector on the panel has no desk, and the director cannot assign it.

The older sector lenses in `theses/lenses/` are no longer put in the dossier. They were long, and only about a third of the claims checked in them survived. They stay in the repository for reference. `theses/lenses/_fields.json`, whose field warnings were each checked, is still read by the dossier.
