"""
Daily Intelligence Brief. AWS Lambda Handler.
Full-spectrum newsfeed with real market data via Alpha Vantage.
Fetches news via RSS, market data via Alpha Vantage, analysis via Claude API.
Sends via iCloud SMTP. Triggered by EventBridge rules at 7 AM, 12:15 PM, and 4:45 PM ET.
"""

import os
import re
import ssl
import subprocess
import collections
import csv
import gzip
import html as _html
import io
import json
import math
import smtplib
import urllib.request
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders
from datetime import datetime, timezone, timedelta

import time
import random
import zlib
from pathlib import Path
from urllib.parse import quote as _urlquote

# The listing classifier (operating company, SPAC, note, fund) lives in its own
# stdlib-only module beside this file so theses/bin can import the same rules
# without importing the pipeline. `python lambda_function.py` already puts this
# directory on the path; the insert covers an import from anywhere else.
import sys as _sys
if str(Path(__file__).resolve().parent) not in _sys.path:
    _sys.path.insert(0, str(Path(__file__).resolve().parent))
import security_type as sectype
# The model portfolios' ledger, valuation and style rules; stdlib only, like the above.
from portfolio import engine as PF

# ── Config ──────────────────────────────────────────────────────────────────

SMTP_USER = "ctlsmith@me.com"  # Apple ID for SMTP auth (must match the APTERREON_ICLOUD_APP_PASSWORD owner)
SENDER_EMAIL = "Daily_Intel_Briefs@icloud.com"  # iCloud alias used as From: header
SENDER_NAME = "Daily Intelligence Brief"
RECIPIENT_EMAIL = os.environ.get("RECIPIENTS", SMTP_USER)
SMTP_SERVER = "smtp.mail.me.com"
SMTP_PORT = 587

# Eastern time, DST-aware. This was a hardcoded timedelta(hours=-4), which is
# EDT only: from early November it would label EST instants as -04:00 and roll
# the calendar day over an hour early, putting late-night rows on the wrong date
# and, at a month boundary, in the wrong monthly CSV.
try:
    from zoneinfo import ZoneInfo
    EASTERN = ZoneInfo("America/New_York")
except Exception:  # pragma: no cover - Windows without the tzdata package
    EASTERN = timezone(timedelta(hours=-4))
    print("WARNING: no IANA tz database; falling back to a fixed EDT offset. "
          "Dates will be an hour early during EST. Run: pip install tzdata")

# Retained so anything still importing it keeps working.
ET_OFFSET = timedelta(hours=-4)

# ── Brand: Apterreon ─────────────────────────────────────────────────────────
APT_RED        = "#CC0000"  # bright red, leads, accent
APT_DARK_RED   = "#7A1010"  # dark red, grounds
APT_GREY       = "#888888"  # grey, recedes
BG_BASE       = "#050810"  # deepest background (page)
BG_SURFACE    = "#0D0F18"  # primary surface (cards, body)
BG_ELEVATED   = "#111420"  # elevated surface (nested cards)
BG_DEEP       = "#070A0F"  # below-base for code / inset boxes
BORDER_DIM    = "#1A2030"
BORDER_RED    = "#3A0A0A"
TEXT_PRIMARY  = "#F0F4F8"
TEXT_BODY     = "#CCD4DC"
TEXT_DIM      = "#9AA8B8"
TEXT_MUTED    = "#6A7888"
TEXT_FAINT    = "#4A5A6A"

# Inline 3-triangle Apterreon mark, scaled by the embedding context.
def apt_logo_svg(width: int = 24, height: int = 32, glow: float = 0.45) -> str:
    return (
        f'<svg width="{width}" height="{height}" viewBox="0 0 90 120" '
        f'style="filter:drop-shadow(0 0 {int(width/4)}px rgba(204,0,0,{glow}));flex-shrink:0">'
        '<polygon points="12.6,25.0 45.9,41.0 52.0,118.0" fill="#888888"/>'
        '<polygon points="38.0,18.0 66.0,42.0 52.0,118.0" fill="#7A1010"/>'
        '<polygon points="64.4,17.8 85.2,48.2 52.0,118.0" fill="#CC0000"/>'
        '</svg>'
    )

# ── Storage Config (filesystem, replaces S3) ────────────────────────────────
REPO_ROOT = Path(__file__).resolve().parent
DOCS_DIR = REPO_ROOT / "docs"
BRIEFS_DIR = DOCS_DIR / "briefs"
STATE_DIR = REPO_ROOT / "state"
for _d in (BRIEFS_DIR, DOCS_DIR, STATE_DIR):
    _d.mkdir(parents=True, exist_ok=True)
# The archive is the point of the Pages site, so nothing is pruned. This was
# already the de-facto behavior: retention used to key off file mtime, and
# actions/checkout resets mtimes on every CI run, so no brief ever aged out.
# Now that s3_cleanup_old_briefs reads the real date from the filename, a 30-day
# window would have deleted the entire back catalogue on the next run.
RETENTION_DAYS = 36500


# ── CSV time series ─────────────────────────────────────────────────────────
#
# The JSON under docs/ is a snapshot: every run overwrites it, so the project had
# no memory. These CSVs are the append-only record, and they are the actual
# product now that the brief no longer writes analysis. Partitioned by month so
# no single file grows without bound.
DATA_DIR = REPO_ROOT / "data"
QUOTES_CSV = DATA_DIR / "quotes.csv"
HEADLINES_CSV_DIR = DATA_DIR / "headlines"
FUNDAMENTALS_CSV_DIR = DATA_DIR / "fundamentals"
# Earnings press releases. The index is a month CSV like the other archives, so
# merge=union covers it; the text is one file per filing because git stores an
# unchanged blob once, while a single growing corpus file would be re-stored in
# full on every commit.
FILINGS_CSV_DIR = DATA_DIR / "filings"
FILINGS_TEXT_DIR = FILINGS_CSV_DIR / "text"
# Reported history, long format, one row per ticker per fiscal period (a second
# row for a period only fills columns the first left blank; see
# record_financials, and read the last row for a period). The panel
# carries five dates and cannot describe a cycle; this carries a decade. MPC's
# annual series shows EPS of -15.13 in 2020, 28.12 in 2022 and 13.22 now, which
# is the context a thesis calling something "peak-cycle earnings" actually needs.
FINANCIALS_CSV_DIR = DATA_DIR / "financials"
FINANCIAL_COLUMNS = ["ticker", "cik", "period", "period_end", "revenue", "gross_profit",
                     "operating_income", "net_income", "eps_diluted", "ocf", "capex",
                     "assets", "equity", "shares_diluted", "revenue_tag", "collected_at"]

# trading_day is the session the price belongs to, which is not the session we
# observed it in. Alpha Vantage GLOBAL_QUOTE returns the previous close, so an
# hourly schedule re-recorded one number all day: 236 rows held 22 distinct
# prices, and SPAXX had a single price across 40 rows. Keeping the field the API
# already returns makes the repetition visible and lets the writer skip it.
QUOTE_COLUMNS = ["observed_at", "ticker", "label", "price", "change_pct",
                 "is_yield", "trading_day"]
HEADLINE_COLUMNS = ["first_seen", "published", "section", "category", "source", "title", "link"]
FILING_COLUMNS = ["filed", "ticker", "cik", "form", "doc_kind", "items", "accession",
                  "exhibit", "text_path", "text_chars", "recorded_at"]

# Nested values (benford is a dict, op_margin_history a list) have no sensible
# CSV representation, so they are dropped rather than stringified.
# Excluded from the panel. The first two are nested structures; the rest are
# screener presentation state that the scoring pass attaches to the same dicts.
# The panel is append-only, so a column added by accident is a column forever,
# and these are recomputed from the panel's own inputs on every run anyway.
FUNDAMENTAL_SKIP_FIELDS = {
    "benford", "op_margin_history",
    "g", "v", "m", "q", "pct", "scorable", "dims_present", "neglect_parts",
    "status",
}
# Leading columns, in this order; every other scalar field follows alphabetically.
FUNDAMENTAL_LEAD = ["date", "ticker", "name", "sector", "sub_industry", "index",
                    "in_index", "price", "change_pct", "market_cap", "pe", "volume"]


def _csv_num(value):
    """Trim binary-float noise before writing.

    Yahoo-derived ratios serialize as 0.22199999999999998 where the real value is
    0.222. Those trailing digits are meaningless and were roughly 60% of the file.
    Whole numbers (market cap, volume) stay integers rather than becoming floats
    or scientific notation."""
    if isinstance(value, bool) or not isinstance(value, float):
        return value
    if not math.isfinite(value):
        return ""
    rounded = round(value, 6)
    return int(rounded) if rounded == int(rounded) else rounded


def _append_csv(path, columns, rows):
    """Append rows, writing a header only when creating the file.

    The file's own header wins when it has one. Adding an entry to `columns`
    after a file exists would otherwise write one more value per row than the
    header declares, and since a CSV row is positional every field after the
    insertion point shifts one place. The result parses, which is the dangerous
    part: an append-only archive would carry silently misaligned columns from
    that run onward. _csv_header was written for this and was not being used
    here."""
    if not rows:
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = _csv_header(path)
    with path.open("a", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=existing or columns,
                                extrasaction="ignore")
        if existing is None:
            writer.writeheader()
        writer.writerows(rows)
    return len(rows)


def _csv_header(path):
    """Existing header of a CSV, or None. Reused so a schema change mid-month
    cannot shift columns out from under rows already written to that file."""
    if not path.exists():
        return None
    try:
        with path.open(encoding="utf-8", newline="") as fh:
            return next(csv.reader(fh), None)
    except Exception:
        return None


def _atomic_write_csv(path, columns, rows):
    """Write a CSV via a temp file and an atomic replace.

    Both callers rewrite a whole file in place. A plain open('w') truncates first,
    so a process killed mid-write (this job has a 60-minute timeout and runs
    unattended) would leave a half-written file, destroying a month of collected
    rows. os.replace is atomic on the same filesystem, so the original survives
    intact until the new file is complete."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=columns, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    os.replace(tmp, path)


def _widen_csv_schema(path, columns):
    """Rewrite a CSV with extra columns appended, preserving existing rows.

    The schema grows when a new field is added to the pipeline. csv.DictWriter is
    configured with extrasaction="ignore", so without this a new column would be
    dropped for the rest of the month with no error at all: the file would simply
    never gain the field, and nothing would say so. Existing rows get an empty
    value for the new columns, which is honest (that data was not collected then).
    Returns the merged column list."""
    existing = _csv_header(path)
    if existing is None:
        return columns
    missing = [c for c in columns if c not in existing]
    if not missing:
        return existing
    merged = list(existing) + missing
    with path.open(encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh))
    _atomic_write_csv(path, merged, [{c: r.get(c, "") for c in merged} for r in rows])
    print(f"csv: widened {path.name} with {len(missing)} new column(s): {', '.join(missing)} "
          f"({len(rows)} existing rows back-filled empty).")
    return merged


def record_quotes(quotes, observed_at):
    """Append one row per quote per trading session, skipping repeats.

    This appended on every run, hourly included, but the source does not change
    hourly: Alpha Vantage GLOBAL_QUOTE returns the previous close. So the file
    was mostly a record of how often we asked. 236 rows carried 22 distinct
    prices, 9.3% information, and the two money-market funds had one price each
    across 40 rows.

    Deduping on (ticker, trading_day) turns it into what it was always meant to
    be: one close per instrument per session. The same shape record_headlines
    already uses for links, and the same reason. Rows whose session is unknown
    fall back to the observation date, so a source that stops reporting one
    degrades to daily rather than to hourly repeats."""
    seen = set()
    if QUOTES_CSV.exists():
        try:
            with QUOTES_CSV.open(encoding="utf-8", newline="") as fh:
                for row in csv.DictReader(fh):
                    seen.add((row.get("ticker", ""), row.get("trading_day", "")))
        except Exception as exc:
            print(f"csv: could not read {QUOTES_CSV.name} for dedupe ({exc}); "
                  f"appending all.")
            seen = set()

    rows = []
    for q in quotes:
        ticker = q.get("ticker", "")
        day = (q.get("trading_day") or observed_at[:10]).strip()
        if (ticker, day) in seen:
            continue
        seen.add((ticker, day))
        rows.append({
            "observed_at": observed_at,
            "ticker": ticker,
            "label": q.get("label", ""),
            # Stored bare so the column is numeric: display strings carry $ and %.
            "price": str(q.get("price", "")).replace("$", "").replace("%", "").strip(),
            "change_pct": str(q.get("change_pct", "")).replace("%", "").strip(),
            "is_yield": "1" if q.get("is_yield") else "0",
            "trading_day": day,
        })
    n = _append_csv(QUOTES_CSV, QUOTE_COLUMNS, rows)
    skipped = len(quotes) - n
    print(f"csv: appended {n} quote rows to data/{QUOTES_CSV.name}"
          + (f" ({skipped} already recorded for their session)." if skipped else "."))
    return n


def record_headlines(headlines, observed_at):
    """Append headlines not already recorded this month.

    Hourly runs re-see the same articles for hours, so dedupe on link against the
    month file. first_seen is therefore genuinely the first time we saw it."""
    path = HEADLINES_CSV_DIR / f"{observed_at[:7]}.csv"
    seen = set()
    if path.exists():
        try:
            with path.open(encoding="utf-8", newline="") as fh:
                for row in csv.DictReader(fh):
                    seen.add(row.get("link", ""))
        except Exception as exc:
            print(f"csv: could not read {path.name} for dedupe ({exc}); appending all.")
    rows = []
    for h in headlines:
        link = h.get("link", "")
        if not link or link in seen:
            continue
        seen.add(link)
        # pub_date is the raw RSS string ("Wed, 03 Sep 2026 19:41:00 GMT");
        # normalize to ISO so the column sorts and parses.
        parsed = parse_rss_date(h.get("pub_date") or "")
        rows.append({
            "first_seen": observed_at,
            "published": parsed.isoformat() if parsed else "",
            "section": h.get("section", ""),
            "category": h.get("category", ""),
            "source": h.get("source", ""),
            "title": h.get("title", ""),
            "link": link,
        })
    n = _append_csv(path, HEADLINE_COLUMNS, rows)
    print(f"csv: appended {n} new headlines to data/headlines/{path.name} "
          f"({len(headlines) - n} already recorded).")
    return n


def record_filings(entries, observed_at):
    """Append filing index rows not already recorded this month.

    Dedupes on accession, which is unique per filing across all of EDGAR, so a
    re-run over the same day is a no-op rather than a duplicate."""
    path = FILINGS_CSV_DIR / f"{observed_at[:7]}.csv"
    seen = set()
    if path.exists():
        try:
            with path.open(encoding="utf-8", newline="") as fh:
                for row in csv.DictReader(fh):
                    seen.add(row.get("accession", ""))
        except Exception as exc:
            print(f"csv: could not read {path.name} for dedupe ({exc}); appending all.")
    rows = []
    for e in entries:
        acc = e.get("accession", "")
        if not acc or acc in seen:
            continue
        seen.add(acc)
        rows.append({**e, "recorded_at": observed_at})
    n = _append_csv(path, FILING_COLUMNS, rows)
    print(f"csv: appended {n} filings to data/filings/{path.name} "
          f"({len(entries) - n} already recorded).")
    return n


def filing_accessions_recorded(month_iso):
    """Accessions already in the month index, so a run can skip re-fetching."""
    path = FILINGS_CSV_DIR / f"{month_iso[:7]}.csv"
    if not path.exists():
        return set()
    try:
        with path.open(encoding="utf-8", newline="") as fh:
            return {r.get("accession", "") for r in csv.DictReader(fh)}
    except Exception:
        return set()


def panel_has_date(date_iso):
    """True when the append-only panel already carries a row for this date."""
    path = FUNDAMENTALS_CSV_DIR / f"{date_iso[:7]}.csv"
    if not path.exists():
        return False
    try:
        with path.open(encoding="utf-8", newline="") as fh:
            for row in csv.DictReader(fh):
                if row.get("date") == date_iso:
                    return True
    except Exception as exc:
        # Unreadable is not the same as absent. Say so rather than silently
        # deciding the day is missing and triggering a full pass every hour.
        print(f"panel: could not read {path.name} ({type(exc).__name__}: {exc}); "
              f"assuming today is already recorded.")
        return True
    return False


def record_fundamentals(stocks, date_iso):
    """Append one row per ticker for the given date, at most once per day.

    Re-running on a date already present is a no-op, so an hourly schedule cannot
    duplicate rows. Column order is pinned to the file's existing header."""
    if not stocks:
        return 0
    path = FUNDAMENTALS_CSV_DIR / f"{date_iso[:7]}.csv"
    if path.exists():
        try:
            with path.open(encoding="utf-8", newline="") as fh:
                if any(r.get("date") == date_iso for r in csv.DictReader(fh)):
                    print(f"csv: fundamentals for {date_iso} already recorded, skipping.")
                    return 0
        except Exception as exc:
            print(f"csv: could not scan {path.name} ({exc}); skipping to avoid duplicates.")
            return 0

    # Schema this run would produce, from the data actually present.
    found = set()
    for s in stocks:
        found.update(k for k, v in s.items()
                     if k not in FUNDAMENTAL_SKIP_FIELDS and not isinstance(v, (dict, list)))
    if any(isinstance(s.get("benford"), dict) for s in stocks):
        found.add("benford_mad")   # the one nested value worth flattening
    rest = sorted(found - set(FUNDAMENTAL_LEAD))
    # Widen an existing file rather than silently dropping fields it lacks.
    columns = _widen_csv_schema(path, FUNDAMENTAL_LEAD + rest)

    rows = []
    skipped_empty = 0
    for s in stocks:
        # Broadening the universe to every US listing brings in thousands of names
        # yfinance has no data for. A row with neither a price nor a market cap
        # carries no information, so record nothing rather than a line of commas.
        # A row flagged price_stale has had its price fields withheld on purpose
        # (see _panel_rows_for_session) and is kept: it still carries the
        # filing-based fields, and dropping it would turn "the close was late"
        # into "the ticker was not in the universe that day".
        if s.get("price") is None and s.get("market_cap") is None and not s.get("price_stale"):
            skipped_empty += 1
            continue
        row = {"date": date_iso}
        for k, v in s.items():
            if k in FUNDAMENTAL_SKIP_FIELDS or isinstance(v, (dict, list)):
                continue
            row[k] = _csv_num(v)
        # Flatten the one nested value worth keeping.
        benford = s.get("benford")
        if isinstance(benford, dict) and "benford_mad" in columns:
            row["benford_mad"] = _csv_num(benford.get("mad"))
        rows.append(row)

    n = _append_csv(path, columns, rows)
    empty_note = f", {skipped_empty} skipped with no price or market cap" if skipped_empty else ""
    print(f"csv: appended {n} fundamentals rows for {date_iso} "
          f"to data/fundamentals/{path.name}{empty_note}.")
    return n


# Every panel field computed from the stored price series, directly or through a
# price. When the series ends before the row's session these describe an older
# day, and a panel row is only worth anything if it describes its own date.
# ev_ebitda and ev_revenue are absent on purpose: they come finished from
# Yahoo's quote summary, which prices them live, not from our stored close.
_PANEL_PRICE_FIELDS = (
    "price", "change_pct", "volume", "volume_trend", "market_cap", "pe",
    "price_book", "fcf_yield", "return_1m", "return_12_2", "return_52w",
    "high52w_proximity", "rel_strength_sp500", "volatility_1y", "beta_1y",
    "sharpe_1y", "max_drawdown_1y",
)
# Share of dated rows allowed to lag before the panel write is deferred. An
# ordinary evening has about 3% (illiquid listings with no trade that day, and
# for them the older close really is the latest). The bad evenings had 81 to 85%.
_PANEL_LAG_DEFER_SHARE = 0.20
# From this hour (ET) a lagging panel is written anyway, stale rows flagged. The
# last promoted run of the day is the 23:xx one; after midnight date_iso moves
# on and the day could never be written at all.
_PANEL_DEFER_UNTIL_HOUR = 23


def _panel_rows_for_session(stocks, session):
    """Copies of `stocks` fit to record under `session`.

    A stock whose price_date is older than the session keeps its row, but with
    every price-derived field removed and price_stale=1. Before this, 2026-09-10,
    09-11, 09-14 and 09-21 each recorded about 4,500 rows carrying an earlier
    session's close under their own date, permanently, in an append-only panel.
    A blank is recoverable by anyone reading the file; a wrong number is not.

    Copies, because the same dicts feed the site and the universe cache, which
    should keep showing the latest close there is. A stock with no price_date has
    no stored series behind its price (Yahoo's live quote, when anything) and is
    left alone, as it was before."""
    out = []
    for s in stocks:
        r = dict(s)
        pdate = r.get("price_date")
        if r.get("price") is not None and pdate and str(pdate) < session:
            for f in _PANEL_PRICE_FIELDS:
                r.pop(f, None)
            # Says why change_pct is blank, and change_pct is gone anyway.
            r.pop("change_gap", None)
            r["price_stale"] = 1
        out.append(r)
    return out


def _panel_gate(stocks, session, session_confirmed, now_et):
    """Whether to write the panel now: ("write" | "defer" | "holiday", lagging, dated).

    Pure, so it is testable. `dated` counts stocks with a price from the stored
    series, `lagging` those whose series ends before `session`.

    holiday  nothing proves the session traded and every dated row lags. That
             is what an exchange holiday looks like to a project with no market
             calendar, and writing would record the prior close under a day with
             no trading. Nothing to write, and nothing wrong.
    defer    more than _PANEL_LAG_DEFER_SHARE lag and it is before
             _PANEL_DEFER_UNTIL_HOUR ET. Yahoo publishes the bar late some
             evenings; the next hourly run is promoted to daily again because
             panel_has_date is still false, and retries with a short recheck
             floor. Also covers a daily run dispatched by hand before the close.
    write    otherwise, including a deferral that never resolved: at 23:xx ET
             the row is written with its lagging rows flagged, because a day
             with flagged blanks beats a day missing from the panel."""
    dated = [s for s in stocks if s.get("price") is not None and s.get("price_date")]
    lagging = sum(1 for s in dated if str(s["price_date"]) < session)
    if dated and lagging == len(dated) and not session_confirmed:
        return "holiday", lagging, len(dated)
    if dated and lagging / len(dated) > _PANEL_LAG_DEFER_SHARE \
            and now_et.hour < _PANEL_DEFER_UNTIL_HOUR:
        return "defer", lagging, len(dated)
    return "write", lagging, len(dated)


TICKERS_CSV = DATA_DIR / "tickers.csv"
# security_type is last so the existing columns keep their positions. The file is
# rewritten whole each run, so the column is complete from the first save.
TICKER_COLUMNS = ["ticker", "name", "sector", "sub_industry", "index",
                  "first_seen", "last_seen_in_index", "status", "dropped_on",
                  "security_type"]

# How long to keep collecting data for a ticker after it leaves every index.
# The registry row is kept forever; this only bounds how long we keep paying to
# fetch prices for it. ~13 months so a full year of post-removal history exists.
RETAIN_DROPPED_DAYS = 400


def load_ticker_registry():
    """Every ticker ever seen, keyed by symbol. Missing file yields {}."""
    if not TICKERS_CSV.exists():
        return {}
    try:
        with TICKERS_CSV.open(encoding="utf-8", newline="") as fh:
            return {r["ticker"]: dict(r) for r in csv.DictReader(fh) if r.get("ticker")}
    except Exception as exc:
        print(f"registry: could not read {TICKERS_CSV.name} ({exc}); starting empty.")
        return {}


def update_ticker_registry(current, today, previously_known=None):
    """Reconcile today's index membership against the registry.

    Index membership changes constantly: names get added, acquired, delisted, or
    demoted out of the S&P indices. Rebuilding the universe from scratch each day
    means a dropped name simply stops appearing, which silently bakes survivorship
    bias into the panel: you would only ever see the companies that made it, and a
    backtest over that data would quietly overstate returns.

    So the registry is append-only. Nothing is ever removed from it, and a ticker
    that leaves the index is marked dropped with the date it left, rather than
    deleted. Returns (registry, added, dropped, retained)."""
    registry = load_ticker_registry()
    current_by_ticker = {s["ticker"]: s for s in current}

    # Bootstrap: on the first run the registry file does not exist yet, so it
    # would learn only about names currently in an index. Anything that had
    # already left before the registry existed would be invisible: never
    # registered, never retained, silently absent from the panel. Seed those from
    # the previous universe cache so they enter as dropped rather than vanishing.
    for s in (previously_known or []):
        ticker = s.get("ticker")
        if not ticker or ticker in registry or ticker in current_by_ticker:
            continue
        registry[ticker] = {
            "ticker": ticker,
            "name": s.get("name", ""),
            "sector": s.get("sector", ""),
            "sub_industry": s.get("sub_industry", ""),
            "index": s.get("index", ""),
            # Unknown when it first appeared; its last refresh is the best proxy.
            "first_seen": s.get("last_updated") or today,
            "last_seen_in_index": s.get("last_updated") or today,
            "status": "active",   # reconciled to dropped by the loop below
            "dropped_on": "",
        }

    added = []
    for ticker, s in current_by_ticker.items():
        row = registry.get(ticker)
        if row is None:
            added.append(ticker)
            row = {"ticker": ticker, "first_seen": today}
            registry[ticker] = row
        row.update({
            "name": s.get("name") or row.get("name", ""),
            "sector": s.get("sector") or row.get("sector", ""),
            "sub_industry": s.get("sub_industry") or row.get("sub_industry", ""),
            "index": s.get("index") or row.get("index", ""),
            "last_seen_in_index": today,
            "status": "active",
            "dropped_on": "",
            # Never empty, because `or` above cannot clear a value: the label is
            # always one of security_type.SECURITY_TYPES.
            "security_type": s.get("security_type") or row.get("security_type", ""),
        })

    dropped, retained = [], []
    for ticker, row in registry.items():
        if ticker in current_by_ticker:
            continue
        if row.get("status") != "dropped":
            # First run where this name is absent from every index.
            row["status"] = "dropped"
            row["dropped_on"] = row.get("last_seen_in_index") or today
            dropped.append(ticker)
        if _days_between(row.get("dropped_on"), today) <= RETAIN_DROPPED_DAYS:
            retained.append(ticker)

    return registry, added, dropped, retained


def _days_between(start_iso, end_iso):
    """Whole days from start to end, or a large number if unparseable."""
    try:
        a = datetime.strptime(start_iso, "%Y-%m-%d")
        b = datetime.strptime(end_iso, "%Y-%m-%d")
        return (b - a).days
    except (TypeError, ValueError):
        return 10 ** 6


def save_ticker_registry(registry):
    """Rewritten in full each run: small (a few thousand rows) and always sorted,
    so the git diff shows exactly which names entered or left.

    A row with no security_type yet (a name dropped before the column existed,
    and past its retention window, so no run labels it from data) gets the
    name-only label, which is the part of the classifier that needs no data."""
    for r in registry.values():
        if not r.get("security_type"):
            r["security_type"] = sectype.classify_row(r)
    rows = [{c: r.get(c, "") for c in TICKER_COLUMNS}
            for r in sorted(registry.values(), key=lambda r: r["ticker"])]
    _atomic_write_csv(TICKERS_CSV, TICKER_COLUMNS, rows)
    active = sum(1 for r in rows if r.get("status") == "active")
    print(f"registry: {len(rows)} tickers known ({active} active, {len(rows) - active} dropped).")


def _age_hours_from_iso(value):
    """Hours since an ISO-8601 timestamp, or None if missing/unparseable.

    Freshness must come from data written into the file, never from the file's
    mtime: actions/checkout stamps every checked-out file with the checkout time,
    so under CI an mtime-based cache looks permanently fresh and never refreshes.
    That bug silently froze docs/news and docs/prices from 2026-05-08 onward."""
    if not value:
        return None
    try:
        ts = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - ts).total_seconds() / 3600


RSS_FEEDS = {
    # ── Google News topic searches (broad net) ──────────────────────────────
    "Markets": "https://news.google.com/rss/search?q=stock+market+today+OR+S%26P+500+OR+nasdaq+OR+treasury+yields+OR+fed+interest+rates&hl=en-US&gl=US&ceid=US:en",
    "Institutional AM": "https://news.google.com/rss/search?q=institutional+asset+management+OR+ETF+launch+OR+private+credit+OR+hedge+fund+OR+mutual+fund&hl=en-US&gl=US&ceid=US:en",
    "Economy": "https://news.google.com/rss/search?q=US+economy+OR+inflation+OR+jobs+report+OR+GDP+OR+recession&hl=en-US&gl=US&ceid=US:en",
    "US Politics": "https://news.google.com/rss/search?q=US+politics+congress+OR+white+house+OR+senate+OR+legislation&hl=en-US&gl=US&ceid=US:en",
    "Policy & Regulation": "https://news.google.com/rss/search?q=SEC+regulation+OR+financial+regulation+OR+federal+policy+OR+executive+order&hl=en-US&gl=US&ceid=US:en",
    "AI & Tech": "https://news.google.com/rss/search?q=artificial+intelligence+OR+LLM+OR+OpenAI+OR+Anthropic+OR+nvidia+OR+AI+startup&hl=en-US&gl=US&ceid=US:en",
    "Tech Industry": "https://news.google.com/rss/search?q=Apple+OR+Google+OR+Microsoft+OR+Meta+tech+news&hl=en-US&gl=US&ceid=US:en",
    "International": "https://news.google.com/rss/search?q=world+news+today+international+geopolitics&hl=en-US&gl=US&ceid=US:en",
    "Middle East": "https://news.google.com/rss/search?q=Middle+East+conflict+OR+Iran+OR+Israel+OR+oil+prices&hl=en-US&gl=US&ceid=US:en",
    "China": "https://news.google.com/rss/search?q=China+economy+OR+China+trade+OR+China+technology&hl=en-US&gl=US&ceid=US:en",
    "Pop Culture": "https://news.google.com/rss/search?q=entertainment+OR+movies+OR+music+OR+celebrity+OR+trending&hl=en-US&gl=US&ceid=US:en",
    "Sports": "https://news.google.com/rss/search?q=NFL+OR+NBA+OR+MLB+OR+sports+today&hl=en-US&gl=US&ceid=US:en",
    "Boston": "https://news.google.com/rss/search?q=Boston+Massachusetts+local+news&hl=en-US&gl=US&ceid=US:en",
    # ── Direct source feeds (reputable, guaranteed quality) ─────────────────
    # Finance & Markets
    # Publisher-scoped feeds use site:, never allinurl:. allinurl: is a Google
    # Search operator that Google News RSS silently ignores, answering 200 with a
    # valid but empty feed, so a query using it reads as a permanently quiet news
    # day. These four sat at zero items for as long as they have existed.
    "Reuters Biz": "https://news.google.com/rss/search?q=site:reuters.com+when:2d+business+OR+markets&hl=en-US&gl=US&ceid=US:en",
    "Bloomberg": "https://news.google.com/rss/search?q=site:bloomberg.com+when:2d+markets+OR+economy&hl=en-US&gl=US&ceid=US:en",
    "WSJ Markets": "https://news.google.com/rss/search?q=site:wsj.com+when:2d+markets+OR+economy&hl=en-US&gl=US&ceid=US:en",
    "FT Markets": "https://news.google.com/rss/search?q=site:ft.com+when:2d+markets+OR+economy&hl=en-US&gl=US&ceid=US:en",
    # Institutional / Pensions
    # pionline.com's own feed now returns a hard 403 to every user agent, from
    # both CI and residential IPs. Google News still indexes them, so reach the
    # same publisher through the proxy used by the topic searches above.
    "P&I": "https://news.google.com/rss/search?q=site:pionline.com+when:2d&hl=en-US&gl=US&ceid=US:en",
    # Policy & Regulation
    "Fed Releases": "https://www.federalreserve.gov/feeds/press_all.xml",
    "SEC Press": "https://www.sec.gov/news/pressreleases.rss",
    # AI & Technology
    "MIT Tech Review": "https://www.technologyreview.com/feed/",
    "Ars Technica": "https://feeds.arstechnica.com/arstechnica/index",
    # Breaking News
    "Breaking": "https://news.google.com/rss/search?q=when:4h+breaking+news+today&hl=en-US&gl=US&ceid=US:en",
}

# Sections that skip Claude insights, just headlines + source
NO_INSIGHT_SECTIONS = {"Breaking News"}

# Fixed sections, always present, always this order
SECTIONS = [
    ("Breaking News", ["Breaking"]),
    ("Finance & Markets", ["Markets", "Institutional AM", "Economy", "Reuters Biz", "Bloomberg", "WSJ Markets", "FT Markets", "P&I"]),
    ("Politics & Policy", ["US Politics", "Policy & Regulation", "Fed Releases", "SEC Press"]),
    ("AI & Technology", ["AI & Tech", "Tech Industry", "MIT Tech Review", "Ars Technica"]),
    ("International", ["International", "Middle East", "China"]),
    ("Culture & Sports", ["Pop Culture", "Sports"]),
    ("Boston", ["Boston"]),
]

# Section accent colors mapped to the Apterreon palette. Tiered hierarchy:
#   tier 1:bright red (#CC0000): primary attention
#   tier 2:dark red  (#7A1010): important context
#   tier 3:grey      (#888888): supporting context
SECTION_COLORS = {
    "Breaking News":     APT_RED,
    "Finance & Markets": APT_RED,
    "Politics & Policy": APT_DARK_RED,
    "AI & Technology":   APT_DARK_RED,
    "International":     APT_GREY,
    "Culture & Sports":  APT_GREY,
    "Boston":            APT_GREY,
}

# Emoji icons retired. Brand is minimalist typography. Section labels use
# numbered prefixes ("01 · BREAKING NEWS") instead.
SECTION_ICONS = {}

# Alpha Vantage tickers for market data bar
MARKET_TICKERS = [
    ("SPY", "S&P 500"),
    ("IWB", "Russell 1000"),
    ("IWM", "Russell 2000"),
    ("EFA", "MSCI EAFE"),
]


# ── Alpha Vantage ───────────────────────────────────────────────────────────

def fetch_market_data():
    """Fetch equity quotes + federal funds rate (MM yield proxy) from Alpha Vantage."""
    api_key = os.environ.get("ALPHAVANTAGE_API_KEY")
    if not api_key:
        print("ALPHAVANTAGE_API_KEY not set, skipping market data")
        return []

    quotes = []

    # Equity tickers (sleep between calls to respect 5/min rate limit)
    for i, (ticker, label) in enumerate(MARKET_TICKERS):
        if i > 0:
            time.sleep(1.5)
        try:
            url = f"https://www.alphavantage.co/query?function=GLOBAL_QUOTE&symbol={ticker}&apikey={api_key}"
            req = urllib.request.Request(url, headers={"User-Agent": "IntelBrief/1.0"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))

            quote = data.get("Global Quote", {})
            price = quote.get("05. price", "")
            change_pct = quote.get("10. change percent", "")

            if price:
                quotes.append({
                    "ticker": ticker,
                    "label": label,
                    "price": f"{float(price):.2f}",
                    "change_pct": change_pct.replace("%", "").strip(),
                    # The session this close belongs to, straight from the API.
                    "trading_day": (quote.get("07. latest trading day") or "").strip(),
                })
        except Exception as e:
            print(f"Alpha Vantage error for {ticker}: {e}")

    # Money market 7-day yields for SPAXX and FZFXX.
    #
    # These used to be regex-scraped from fundresearch.fidelity.com. That page is
    # now a JavaScript shell: the served HTML contains no yield and no percentage
    # at all, so every pattern failed on every run and the brief quietly showed the
    # federal funds rate instead. yfinance is already a dependency, and Yahoo
    # classifies both funds as MONEYMARKET with a yield field, so use that.
    mm_funds = [
        ("SPAXX", "SPAXX 7d Yield"),
        ("FZFXX", "FZFXX 7d Yield"),
    ]
    mm_success = False
    try:
        import yfinance as yf
    except ImportError:
        yf = None
        print("Money market yields: yfinance not installed, falling back to fed funds.")

    if yf is not None:
        for mm_ticker, mm_label in mm_funds:
            try:
                info = yf.Ticker(mm_ticker).info or {}
                raw_yield = None
                for key in ("sevenDayYield", "yield", "annualYield"):
                    if info.get(key) is not None:
                        raw_yield = info[key]
                        break
                if raw_yield is None:
                    print(f"Money market yields: no yield field for {mm_ticker}.")
                    continue
                val = float(raw_yield)
                # Yahoo reports fund yields as a fraction (0.0412) on some records
                # and as a percent (4.12) on others. Normalize to percent.
                if val < 0.5:
                    val *= 100
                if not (0 < val < 25):
                    print(f"Money market yields: implausible yield {val} for {mm_ticker}, ignoring.")
                    continue
                quotes.append({
                    "ticker": mm_ticker,
                    "label": mm_label,
                    "price": f"{val:.2f}%",
                    "change_pct": "0",
                    "is_yield": True,
                })
                mm_success = True
                print(f"Money market yield for {mm_ticker}: {val:.2f}%")
            except Exception as e:
                print(f"Money market yield error for {mm_ticker}: {e}")

    # Fallback: federal funds rate if the money-market yields were unavailable
    if not mm_success:
        time.sleep(1.5)  # Rate limit spacing
        try:
            url = f"https://www.alphavantage.co/query?function=FEDERAL_FUNDS_RATE&interval=daily&apikey={api_key}"
            req = urllib.request.Request(url, headers={"User-Agent": "IntelBrief/1.0"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                ff_data = json.loads(resp.read().decode("utf-8"))

            data_points = ff_data.get("data", [])
            if data_points:
                current_rate = data_points[0].get("value", "")
                if current_rate:
                    quotes.append({
                        "ticker": "FFR",
                        # Labeled for what it is. This is the fed funds rate standing
                        # in for the money-market yields, not an average of them.
                        "label": "Fed Funds Rate",
                        "price": f"{float(current_rate):.2f}%",
                        "change_pct": "0",
                        "is_yield": True,
                    })
        except Exception as e:
            print(f"Alpha Vantage error for federal funds rate: {e}")

    return quotes


# ── RSS Fetcher ─────────────────────────────────────────────────────────────

def parse_rss_date(date_str):
    """Parse RSS pubDate string into a timezone-aware datetime. Returns None on failure."""
    # Standard RSS format: "Mon, 10 Mar 2026 14:30:00 GMT"
    formats = [
        "%a, %d %b %Y %H:%M:%S %Z",
        "%a, %d %b %Y %H:%M:%S %z",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%SZ",
    ]
    for fmt in formats:
        try:
            dt = datetime.strptime(date_str.strip(), fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except (ValueError, TypeError):
            continue
    return None


# Recency windows by brief type (in hours)
# Morning: 10h (captures overnight news from ~9 PM prior evening)
# Midday: 6h (captures morning developments)
# Evening: 6h (captures afternoon developments)
RECENCY_HOURS = {
    "morning": 10,
    "midday": 6,
    "evening": 6,
}


def _extract_feed_items(root):
    """Extract items from RSS or Atom feed XML. Returns list of (title, source, pub_date, link)."""
    # Try RSS format first (<item> elements)
    items = root.findall(".//item")
    if items:
        results = []
        for item in items:
            title = item.findtext("title", "")
            source = item.findtext("source", "")
            pub_date = item.findtext("pubDate", "")
            link = item.findtext("link", "")
            results.append((title, source, pub_date, link))
        return results

    # Try Atom format (<entry> elements, with or without namespace)
    # Atom namespace
    ns = {"atom": "http://www.w3.org/2005/Atom"}
    entries = root.findall(".//atom:entry", ns)
    if not entries:
        entries = root.findall(".//{http://www.w3.org/2005/Atom}entry")
    if not entries:
        # Try without namespace (some feeds omit it)
        entries = root.findall(".//entry")

    results = []
    for entry in entries:
        # Title
        title = entry.findtext("atom:title", "", ns) or entry.findtext("{http://www.w3.org/2005/Atom}title", "") or entry.findtext("title", "")
        # Source / author
        source = entry.findtext("atom:author/atom:name", "", ns) or entry.findtext("{http://www.w3.org/2005/Atom}author/{http://www.w3.org/2005/Atom}name", "") or entry.findtext("author", "")
        # Date
        pub_date = entry.findtext("atom:updated", "", ns) or entry.findtext("{http://www.w3.org/2005/Atom}updated", "") or entry.findtext("updated", "") or entry.findtext("atom:published", "", ns) or entry.findtext("{http://www.w3.org/2005/Atom}published", "") or entry.findtext("published", "")
        # Link (Atom uses <link href="..."/> attribute)
        link_el = entry.find("atom:link", ns) or entry.find("{http://www.w3.org/2005/Atom}link") or entry.find("link")
        link = ""
        if link_el is not None:
            link = link_el.get("href", "") or (link_el.text or "")
        results.append((title, source, pub_date, link))
    return results


# Scripts that do not appear in an English headline. Latin accents are absent
# from this deliberately, so Nestle with an acute and Soderberg with an umlaut
# both pass.
_NON_LATIN_SCRIPTS = re.compile(
    "[\u0400-\u04FF"      # Cyrillic
    "\u0590-\u05FF"       # Hebrew
    "\u0600-\u06FF"       # Arabic
    "\u0700-\u074F"       # Syriac
    "\u0900-\u097F"       # Devanagari
    "\u0E00-\u0E7F"       # Thai
    "\u3040-\u30FF"       # Hiragana and Katakana
    "\u4E00-\u9FFF"       # CJK
    "\uAC00-\uD7AF]"      # Hangul
)


def _is_english(*parts):
    """True unless the text carries enough non-Latin script to be another language.

    Google News formats a title as "Headline - Source", so a foreign outlet
    shows up in the title even when the words are English. Three characters is
    past the point where a stray symbol could trip it and well short of any real
    foreign headline or masthead.
    """
    text = " ".join(p for p in parts if p)
    return len(_NON_LATIN_SCRIPTS.findall(text)) < 3


def fetch_rss_headlines(max_per_feed=4, brief_type="morning"):
    """Fetch headlines from all RSS feeds (RSS + Atom), filtered by recency."""
    now_utc = datetime.now(timezone.utc)
    max_age_hours = RECENCY_HOURS.get(brief_type, 10)
    cutoff = now_utc - timedelta(hours=max_age_hours)

    all_items = []
    stale_count = 0
    per_feed = {}
    for category, url in RSS_FEEDS.items():
        raw_items = 0
        kept_here = 0
        non_english = 0
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "IntelBrief/1.0"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                xml_data = resp.read().decode("utf-8")
            root = ET.fromstring(xml_data)
            feed_items = _extract_feed_items(root)
            raw_items = len(feed_items)
            fresh_count = 0
            for title, source, pub_date, link in feed_items:
                if fresh_count >= max_per_feed:
                    break

                # English-language outlets only, whatever the feed hands back.
                if not _is_english(title, source):
                    non_english += 1
                    continue

                # Filter by recency, drop articles older than the cutoff
                parsed_date = parse_rss_date(pub_date)
                if parsed_date and parsed_date < cutoff:
                    stale_count += 1
                    continue

                section = "Other"
                for sec_name, categories in SECTIONS:
                    if category in categories:
                        section = sec_name
                        break
                all_items.append({
                    "category": category,
                    "section": section,
                    "title": title,
                    "source": source,
                    "pub_date": pub_date,
                    "link": link,
                })
                fresh_count += 1
            kept_here = fresh_count
            per_feed[category] = {"raw": raw_items, "kept": kept_here,
                                  "foreign": non_english, "error": None}
        except Exception as e:
            per_feed[category] = {"raw": 0, "kept": 0, "foreign": 0, "error": str(e)}
            print(f"RSS fetch error for {category}: {e}")

    live = sum(1 for r in per_feed.values() if r["kept"])
    print(f"Recency filter: kept {len(all_items)} articles from {live}/{len(RSS_FEEDS)} feeds, "
          f"dropped {stale_count} stale (>{max_age_hours}h old)")

    # A feed that parses fine and yields nothing is the dangerous case: it looks
    # exactly like a quiet news day. Name it so a persistent one is visible in
    # the log rather than inferred from a total.
    empty = sorted(c for c, r in per_feed.items() if not r["error"] and r["raw"] == 0)
    all_stale = sorted(c for c, r in per_feed.items()
                       if not r["error"] and r["raw"] > 0 and r["kept"] == 0)
    broken = sorted(c for c, r in per_feed.items() if r["error"])
    if empty:
        print(f"RSS: {len(empty)} feed(s) parsed but returned no items at all: {', '.join(empty)}. "
              f"A source answering 200 with an empty body looks identical to a quiet day here; "
              f"if the same name persists across runs, treat it as dead.")
    if all_stale:
        print(f"RSS: {len(all_stale)} feed(s) returned only items older than {max_age_hours}h: "
              f"{', '.join(all_stale)}.")
    if broken:
        print(f"RSS: {len(broken)} feed(s) failed outright: {', '.join(broken)}.")
    foreign = sum(r.get("foreign", 0) for r in per_feed.values())
    if foreign:
        print(f"RSS: dropped {foreign} headline(s) from non-English sources.")
    if not all_items:
        print("RSS: ALL feeds returned nothing. That is a pipeline failure, not a quiet news day.")
    return all_items


SECTION_NAMES = [s[0] for s in SECTIONS]

# Headlines kept per feed per run.
MAX_PER_FEED = 4


def market_bar_email(quotes):
    """Market data row for the email preview (Apterreon)."""
    if not quotes:
        return ""
    cells = ""
    for q in quotes:
        is_yield = q.get("is_yield", False)
        if is_yield:
            cells += f"""<td style="padding:14px 10px;text-align:center;background:#0D0F18;border:1px solid #1A2030">
<div style="font-size:9px;letter-spacing:2px;color:#9AA8B8;text-transform:uppercase;margin-bottom:6px;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif">{q['label']}</div>
<div style="font-size:16px;font-weight:700;color:#E0E8F0;font-family:'SF Mono',Menlo,Consolas,monospace">{q['price']}</div>
<div style="font-size:9px;letter-spacing:2px;color:#9AA8B8;text-transform:uppercase;margin-top:4px;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif">7d yield</div>
</td>"""
        else:
            try:
                change = float(q["change_pct"])
            except (ValueError, KeyError):
                change = 0
            color = "#5599CC" if change >= 0 else "#CC0000"
            arrow = "&#9650;" if change >= 0 else "&#9660;"
            cells += f"""<td style="padding:14px 10px;text-align:center;background:#0D0F18;border:1px solid #1A2030">
<div style="font-size:9px;letter-spacing:2px;color:#9AA8B8;text-transform:uppercase;margin-bottom:6px;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif">{q['label']}</div>
<div style="font-size:16px;font-weight:700;color:#E0E8F0;font-family:'SF Mono',Menlo,Consolas,monospace">{q['price']}</div>
<div style="font-size:11px;color:{color};margin-top:4px;font-family:'SF Mono',Menlo,Consolas,monospace">{arrow} {abs(change):.2f}%</div>
</td>"""
    return f"""<table width="100%" cellpadding="0" cellspacing="0" style="margin:24px 0;border-collapse:collapse">
<tr>{cells}</tr></table>"""


def market_bar_interactive(quotes):
    """Market data row for the interactive HTML."""
    if not quotes:
        return "[]"
    return json.dumps(quotes)


# ── Email Preview ──────────────────────────────────────────────────────────

def build_email_preview(title, data, quotes, timestamp, usage_info=None, brief_url=None, site_url=None):
    """Email preview, Apterreon. Email-safe (inline styles, tables,
    system fonts only, no web fonts since most clients strip @import).
    brief_url: deep link to this brief on the public site.
    site_url: home page link."""
    sans = "-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif"
    mono = "'SF Mono',Menlo,Consolas,'Courier New',monospace"

    usage_html = ""
    market_html = market_bar_email(quotes)
    sections_html = ""

    section_idx = 0
    for sec_name, _ in SECTIONS:
        color = SECTION_COLORS.get(sec_name, APT_GREY)
        sec_data = next((s for s in data.get("sections", []) if s["name"] == sec_name), None)
        if not sec_data or not sec_data.get("stories"):
            continue
        section_idx += 1
        sec_num = f"{section_idx:02d}"

        stories_html = ""
        for story in sec_data["stories"]:
            link = story.get("link", "")
            headline = story["headline"]
            source = story.get("source", "")
            summary = story.get("summary", "") or ""
            insight = story.get("insight", "") or ""

            headline_html = (
                f'<a href="{link}" style="color:#E0E8F0;text-decoration:none;border-bottom:1px solid #1A2030">{headline}</a>'
                if link else f'<span style="color:#E0E8F0">{headline}</span>'
            )

            inner = f"""<div style="font-family:{sans};font-size:14px;font-weight:600;color:#E0E8F0;line-height:1.45;margin-bottom:6px">{headline_html}</div>
<div style="font-family:{mono};font-size:9px;letter-spacing:2px;color:#9AA8B8;text-transform:uppercase;margin-bottom:8px">{source}</div>"""
            if summary:
                inner += f'<div style="font-family:{sans};font-size:13px;color:#CCD4DC;line-height:1.55;margin-bottom:6px">{summary}</div>'
            if insight:
                inner += f'<div style="font-family:{sans};font-size:12px;color:#7A8A9A;line-height:1.55;font-style:italic;border-left:2px solid {color};padding-left:10px;margin-top:8px">{insight}</div>'

            stories_html += f"""<tr><td style="padding:14px 0;border-bottom:1px solid #1A2030">{inner}</td></tr>"""

        sections_html += f"""<table width="100%" cellpadding="0" cellspacing="0" style="margin:32px 0 0;border-collapse:collapse">
<tr><td style="padding-bottom:12px;border-bottom:1px solid {color}">
<span style="font-family:{mono};font-size:10px;letter-spacing:3px;color:{color};text-transform:uppercase">{sec_num} &middot;</span>
<span style="font-family:{sans};font-size:14px;font-weight:700;color:#E0E8F0;text-transform:uppercase;letter-spacing:3px;margin-left:6px">{sec_name}</span>
</td></tr>
{stories_html}</table>"""

    edge_text = data.get("the_edge", "")
    edge_html = ""
    if edge_text:
        edge_html = f"""<table width="100%" cellpadding="0" cellspacing="0" style="margin:36px 0 0;border-collapse:collapse">
<tr><td style="padding:18px 20px;background:#070A0F;border:1px solid #3A0A0A;border-left:3px solid {APT_RED}">
<div style="font-family:{mono};font-size:9px;letter-spacing:4px;color:{APT_RED};text-transform:uppercase;margin-bottom:10px">The Edge</div>
<div style="font-family:{sans};font-size:13px;color:#CCD4DC;line-height:1.6">{edge_text}</div>
</td></tr></table>"""

    tomorrow_text = data.get("tomorrow_watch", "")
    tomorrow_html = ""
    if tomorrow_text:
        tomorrow_html = f"""<table width="100%" cellpadding="0" cellspacing="0" style="margin:18px 0 0;border-collapse:collapse">
<tr><td style="padding:18px 20px;background:#070A0F;border:1px solid #1A2030">
<div style="font-family:{mono};font-size:9px;letter-spacing:4px;color:{APT_GREY};text-transform:uppercase;margin-bottom:10px">Tomorrow Watch</div>
<div style="font-family:{sans};font-size:13px;color:#CCD4DC;line-height:1.6">{tomorrow_text}</div>
</td></tr></table>"""

    return f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="color-scheme" content="dark"><meta name="supported-color-schemes" content="dark"></head>
<body style="margin:0;padding:0;background:#050810">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#050810"><tr><td align="center" style="padding:32px 16px">
<table width="640" cellpadding="0" cellspacing="0" style="max-width:640px;width:100%;background:#0D0F18;border:1px solid #1A2030;border-bottom:2px solid {APT_RED}">
<tr><td style="padding:32px 28px 8px">

<table width="100%" cellpadding="0" cellspacing="0"><tr>
<td style="width:1px;vertical-align:middle;padding-right:16px">{apt_logo_svg(40, 53, 0.55)}</td>
<td style="vertical-align:middle">
<div style="font-family:{mono};font-size:9px;letter-spacing:4px;color:{APT_RED};text-transform:uppercase">Daily Intelligence Brief</div>
</td>
</tr></table>
<div style="height:1px;background:#1A2030;margin:14px 0 18px"></div>

<h1 style="font-family:{sans};font-size:22px;font-weight:800;letter-spacing:1px;color:#FFFFFF;margin:0 0 4px;line-height:1.25">{title}</h1>
<table width="100%" cellpadding="0" cellspacing="0" style="margin-top:4px"><tr>
<td style="font-family:{mono};font-size:10px;letter-spacing:2px;color:#9AA8B8;text-transform:uppercase">{timestamp}</td>
{('<td style="text-align:right;font-family:' + mono + ';font-size:10px;letter-spacing:2px;text-transform:uppercase"><a href="' + brief_url + '" style="color:' + APT_RED + ';text-decoration:none;border-bottom:1px solid ' + APT_DARK_RED + ';padding-bottom:1px">View on web &rarr;</a></td>') if brief_url else ''}
</tr></table>

{usage_html}
{market_html}
{sections_html}
{edge_html}
{tomorrow_html}

<div style="margin-top:48px;padding-top:18px;border-top:1px solid #1A2030">
<table width="100%" cellpadding="0" cellspacing="0"><tr>
<td style="vertical-align:middle">{apt_logo_svg(14, 19, 0.3)} <span style="font-family:{sans};font-size:10px;font-weight:700;color:#6A7888;letter-spacing:1px;vertical-align:middle">Apterreon</span> <span style="font-family:{sans};font-size:10px;color:#4A5A6A;vertical-align:middle">&nbsp;&middot;&nbsp;Explore what&#8217;s out there.</span></td>
<td style="text-align:right;vertical-align:middle">{('<a href="' + site_url + '" style="font-family:' + mono + ';font-size:10px;letter-spacing:2px;color:' + APT_RED + ';text-transform:uppercase;text-decoration:none">Apterreon home &rarr;</a>') if site_url else ''}</td>
</tr></table>
<div style="margin-top:12px;font-family:{mono};font-size:9px;letter-spacing:2px;color:#6A7888">{timestamp}</div>
</div>

</td></tr></table>
</td></tr></table>
</body>
</html>"""


# ── Interactive HTML Attachment ─────────────────────────────────────────────

def build_interactive_html(title, data, quotes, timestamp, usage_info=None):
    """Self-contained interactive HTML brief (Apterreon)."""

    sections_json = json.dumps(data.get("sections", []))
    edge_text = json.dumps(data.get("the_edge", ""))
    tomorrow_text = json.dumps(data.get("tomorrow_watch", ""))
    colors_json = json.dumps(SECTION_COLORS)
    quotes_json = market_bar_interactive(quotes)
    section_order_json = json.dumps(SECTION_NAMES)
    usage_json = json.dumps(usage_info or {})
    json_no_insight = json.dumps(sorted(NO_INSIGHT_SECTIONS))

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="color-scheme" content="dark">
<meta name="theme-color" content="#050810">
<title>{title}</title>
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Instrument+Serif:ital@0;1&family=Space+Grotesk:wght@400;500;700&family=Space+Mono:wght@400;700&display=swap" rel="stylesheet">
<style>
  *,*::before,*::after {{ box-sizing:border-box; margin:0; padding:0; }}
  :root {{
    --bg-base:#050810; --bg-surface:#0D0F18; --bg-elevated:#111420; --bg-deep:#070A0F;
    --border-dim:#1A2030; --border-red:#3A0A0A;
    --apt-red:#CC0000; --apt-dark-red:#7A1010; --apt-grey:#888888;
    --text-primary:#E0E8F0; --text-body:#CCD4DC; --text-dim:#9AA8B8; --text-muted:#6A7888; --text-faint:#4A5A6A;
  }}
  html {{ background:var(--bg-base); color:var(--text-primary); font-family:'Space Mono',ui-monospace,Menlo,Consolas,monospace; font-size:13px; -webkit-font-smoothing:antialiased; }}
  body {{ background:var(--bg-base); min-height:100vh; padding:env(safe-area-inset-top) 0 env(safe-area-inset-bottom); }}
  ::-webkit-scrollbar {{ width:4px; height:4px; }}
  ::-webkit-scrollbar-track {{ background:transparent; }}
  ::-webkit-scrollbar-thumb {{ background:var(--border-dim); border-radius:2px; }}
  a {{ color:inherit; text-decoration:none; }}

  .topnav {{
    position:sticky; top:0; z-index:100; height:52px; background:var(--bg-surface);
    border-bottom:1px solid var(--border-dim); display:flex; align-items:center;
    padding:0 24px; gap:14px;
  }}
  .topnav .back {{
    font-family:'Space Mono',monospace; font-size:10px; letter-spacing:2px;
    color:var(--text-dim); text-transform:uppercase; transition:color .15s;
    display:flex; align-items:center; gap:6px;
  }}
  .topnav .back:hover {{ color:var(--text-primary); }}
  .topnav .lockup {{ display:flex; align-items:center; gap:10px; margin-left:auto; }}
  .topnav .lockup .dm {{ font-family:'Space Grotesk',sans-serif; font-weight:800; font-size:11px; letter-spacing:4px; color:var(--text-primary); text-transform:uppercase; }}
  .topnav .lockup .prod {{ font-family:'Space Grotesk',sans-serif; font-weight:700; font-size:8px; letter-spacing:4px; color:var(--apt-red); text-transform:uppercase; }}
  .topnav .suite {{ display:none; font-size:9px; letter-spacing:2px; color:var(--text-faint); text-transform:uppercase; }}
  @media (min-width:720px) {{ .topnav .suite {{ display:inline; }} }}

  .container {{ max-width:760px; margin:0 auto; padding:32px 24px 96px; }}

  .header {{ margin-bottom:36px; padding-bottom:24px; border-bottom:1px solid var(--border-dim); }}
  .header .tag {{ font-family:'Space Mono',monospace; font-size:10px; letter-spacing:4px; color:var(--apt-red); text-transform:uppercase; margin-bottom:10px; }}
  .header h1 {{ font-family:'Space Grotesk',sans-serif; font-size:30px; font-weight:800; letter-spacing:0.5px; color:#FFFFFF; line-height:1.2; }}
  .header .meta {{ font-family:'Space Mono',monospace; font-size:10px; letter-spacing:2px; color:var(--text-dim); text-transform:uppercase; margin-top:10px; }}

  .market-bar {{
    display:grid; grid-template-columns:repeat(auto-fit,minmax(120px,1fr)); gap:8px;
    margin-bottom:32px;
  }}
  .market-card {{
    background:var(--bg-surface); border:1px solid var(--border-dim);
    padding:14px 12px; text-align:center;
  }}
  .market-card .label {{ font-family:'Space Mono',monospace; font-size:9px; letter-spacing:2px; color:var(--text-dim); text-transform:uppercase; }}
  .market-card .price {{ font-family:'Space Mono',monospace; font-size:18px; font-weight:500; color:var(--text-primary); margin:6px 0 4px; }}
  .market-card .change {{ font-family:'Space Mono',monospace; font-size:11px; }}
  .market-card .change.up {{ color:#5599CC; }}
  .market-card .change.down {{ color:var(--apt-red); }}

  .usage-banner {{
    background:var(--bg-deep); border:1px solid var(--border-dim);
    padding:12px 16px; margin-bottom:24px;
  }}
  .usage-row {{ display:flex; justify-content:space-between; align-items:center; margin-bottom:8px; }}
  .usage-label {{ font-family:'Space Mono',monospace; font-size:9px; letter-spacing:3px; color:var(--text-dim); text-transform:uppercase; }}
  .usage-status {{ font-family:'Space Mono',monospace; font-size:9px; letter-spacing:3px; font-weight:500; text-transform:uppercase; }}
  .usage-bar {{ background:var(--bg-elevated); height:2px; overflow:hidden; margin-bottom:8px; }}
  .usage-bar-fill {{ height:2px; transition:width 0.3s; }}
  .usage-details {{ font-family:'Space Mono',monospace; font-size:10px; color:var(--text-dim); }}

  .widgets {{ display:flex; flex-direction:column; gap:14px; }}

  .widget {{
    background:var(--bg-surface); border:1px solid var(--border-dim);
    transition:border-color .2s;
  }}
  .widget.active {{ border-color:var(--text-muted); }}
  .widget[data-tier="1"] {{ border-bottom:2px solid var(--apt-red); }}
  .widget[data-tier="2"] {{ border-bottom:2px solid var(--apt-dark-red); }}
  .widget[data-tier="3"] {{ border-bottom:2px solid var(--apt-grey); }}

  .widget-header {{ display:flex; align-items:flex-start; padding:18px 22px; cursor:pointer; gap:18px; transition:background .15s; }}
  .widget-header:hover {{ background:rgba(255,255,255,0.02); }}
  .widget-num {{ font-family:'Space Mono',monospace; font-size:10px; letter-spacing:3px; color:var(--text-muted); flex-shrink:0; padding-top:2px; }}
  .widget-info {{ flex:1; min-width:0; }}
  .widget-title {{ font-family:'Space Grotesk',sans-serif; font-size:14px; font-weight:700; letter-spacing:3px; text-transform:uppercase; }}
  .widget-headlines {{ margin:8px 0 0; padding:0; list-style:none; }}
  .widget-headlines li {{ font-family:'Space Mono',monospace; font-size:11px; color:var(--text-dim); line-height:1.55; padding:3px 0; padding-left:14px; position:relative; word-wrap:break-word; }}
  .widget-headlines li::before {{ content:'·'; position:absolute; left:2px; color:var(--text-muted); }}
  .widget-count {{ font-family:'Space Mono',monospace; font-size:10px; letter-spacing:2px; color:var(--text-dim); flex-shrink:0; padding-top:2px; }}
  .widget-chevron {{ color:var(--text-muted); font-size:14px; transition:transform .2s,color .2s; flex-shrink:0; padding-top:4px; }}
  .widget.active .widget-chevron {{ transform:rotate(90deg); color:var(--apt-red); }}

  .widget-body {{ max-height:0; overflow:hidden; transition:max-height .35s ease; }}
  .widget.active .widget-body {{ max-height:4000px; }}

  .widget-stories {{ padding:0 22px 20px; border-top:1px solid var(--border-dim); }}

  .story {{ padding:18px 0; border-top:1px solid var(--border-dim); cursor:pointer; }}
  .story:first-child {{ border-top:none; }}
  .story-headline {{ font-family:'Space Grotesk',sans-serif; font-size:15px; font-weight:600; color:var(--text-primary); line-height:1.4; display:flex; justify-content:space-between; align-items:flex-start; gap:12px; }}
  .story-headline .arrow {{ font-size:11px; color:var(--text-muted); transition:transform .2s,color .2s; flex-shrink:0; padding-top:4px; }}
  .story.open .story-headline .arrow {{ transform:rotate(90deg); color:var(--apt-red); }}
  .story-source {{ font-family:'Space Mono',monospace; font-size:9px; letter-spacing:2px; color:var(--text-muted); text-transform:uppercase; margin-top:6px; }}

  .story-details {{ max-height:0; overflow:hidden; transition:max-height .3s ease; }}
  .story.open .story-details {{ max-height:600px; }}

  .story-summary {{ font-family:'Space Mono',monospace; font-size:13px; color:var(--text-body); margin:14px 0 12px; line-height:1.65; }}
  .story-insight {{ font-family:'Space Mono',monospace; font-size:12px; color:var(--text-body); line-height:1.65; padding:14px 16px; background:var(--bg-deep); border-left:2px solid var(--apt-red); }}
  .insight-label {{ font-family:'Space Mono',monospace; font-size:9px; font-weight:500; text-transform:uppercase; letter-spacing:3px; color:var(--apt-red); margin-bottom:8px; }}
  .story-link {{ display:inline-block; margin-top:12px; font-family:'Space Mono',monospace; font-size:10px; letter-spacing:2px; color:var(--apt-red); text-transform:uppercase; }}
  .story-link:hover {{ color:#FFFFFF; }}

  .panel {{ margin-top:32px; padding:22px 24px; background:var(--bg-deep); border:1px solid var(--border-dim); }}
  .panel.edge {{ border-left:3px solid var(--apt-red); }}
  .panel-title {{ font-family:'Space Mono',monospace; font-size:10px; font-weight:500; text-transform:uppercase; letter-spacing:4px; margin-bottom:12px; }}
  .panel.edge .panel-title {{ color:var(--apt-red); }}
  .panel:not(.edge) .panel-title {{ color:var(--apt-grey); }}
  .panel p {{ font-family:'Space Mono',monospace; font-size:13px; color:var(--text-body); line-height:1.7; }}

  .footer {{ margin-top:64px; padding-top:24px; border-top:1px solid var(--border-dim); display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:12px; }}
  .footer .brand {{ font-family:'Space Grotesk',sans-serif; font-size:9px; font-weight:800; letter-spacing:4px; color:var(--text-muted); text-transform:uppercase; }}
  .footer .ts {{ font-family:'Space Mono',monospace; font-size:9px; letter-spacing:2px; color:var(--text-muted); }}

  @media (max-width:560px) {{
    .container {{ padding:24px 16px 64px; }}
    .header h1 {{ font-size:24px; }}
    .widget-header {{ padding:16px 18px; gap:14px; }}
    .widget-stories {{ padding:0 18px 18px; }}
  }}
</style>
</head>
<body>

<nav class="topnav">
  <a class="back" href="../index.html" title="Apterreon home"><span>&#9664;</span> Home</a>
  <a class="lockup" href="../index.html" style="text-decoration:none;color:inherit">
    {apt_logo_svg(20, 27, 0.45)}
    <div>
      <div class="dm">Apterreon</div>
      <div class="prod">Daily Intelligence Brief</div>
    </div>
  </a>
</nav>

<div class="container">
  <div id="usage"></div>
  <div class="header">
    <div class="tag">Daily Intelligence Brief</div>
    <h1>{title}</h1>
    <div class="meta">{timestamp} &middot; Tap any section to expand</div>
  </div>

  <div id="market-bar" class="market-bar"></div>
  <div id="widgets" class="widgets"></div>
  <div id="edge"></div>
  <div id="tomorrow"></div>

  <div class="footer">
    <span class="brand">Apterreon</span>
    <span class="tagline">Explore what&#8217;s out there.</span>
    <span class="ts">{timestamp}</span>
  </div>
</div>

<script>
const usageInfo = {usage_json};
const rawSections = {sections_json};
const edgeText = {edge_text};
const tomorrowText = {tomorrow_text};
const colors = {colors_json};
const quotes = {quotes_json};
const sectionOrder = {section_order_json};
const noInsight = {json_no_insight};

const TIER_BY_COLOR = {{ '#CC0000':1, '#7A1010':2, '#888888':3 }};

function escapeHtml(s) {{
  return String(s == null ? '' : s)
    .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
    .replace(/\\"/g,'&quot;').replace(/'/g,'&#39;');
}}

// Usage banner
if (usageInfo && usageInfo.cost_monthly_projected !== undefined) {{
  const monthly = usageInfo.cost_monthly_projected;
  const cost = usageInfo.cost_this_call || 0;
  const tokens = usageInfo.total_tokens || 0;
  const budget = 10.0;
  const pct = Math.min(100, (monthly / budget) * 100);
  let barColor, status;
  if (monthly < 2)      {{ barColor = '#5599CC'; status = 'LOW'; }}
  else if (monthly < 5) {{ barColor = '#888888'; status = 'MODERATE'; }}
  else                  {{ barColor = '#CC0000'; status = 'HIGH'; }}

  document.getElementById('usage').innerHTML =
    '<div class="usage-banner">' +
      '<div class="usage-row">' +
        '<span class="usage-label">API Usage</span>' +
        '<span class="usage-status" style="color:' + barColor + '">' + status + '</span>' +
      '</div>' +
      '<div class="usage-bar"><div class="usage-bar-fill" style="background:' + barColor + ';width:' + pct.toFixed(0) + '%"></div></div>' +
      '<div class="usage-details">$' + cost.toFixed(4) + ' this brief &middot; ' + tokens.toLocaleString() + ' tokens &middot; $' + monthly.toFixed(2) + '/mo projected &middot; $10.00 budget</div>' +
    '</div>';
}}

// Market bar
const marketBar = document.getElementById('market-bar');
quotes.forEach(q => {{
  const card = document.createElement('div');
  card.className = 'market-card';
  if (q.is_yield) {{
    card.innerHTML =
      '<div class="label">' + escapeHtml(q.label) + '</div>' +
      '<div class="price">' + escapeHtml(q.price) + '</div>' +
      '<div class="change" style="color:#888888">7d yield</div>';
  }} else {{
    const change = parseFloat(q.change_pct) || 0;
    const dir = change >= 0 ? 'up' : 'down';
    const arrow = change >= 0 ? '&#9650;' : '&#9660;';
    card.innerHTML =
      '<div class="label">' + escapeHtml(q.label) + '</div>' +
      '<div class="price">' + escapeHtml(q.price) + '</div>' +
      '<div class="change ' + dir + '">' + arrow + ' ' + Math.abs(change).toFixed(2) + '%</div>';
  }}
  marketBar.appendChild(card);
}});

// Build sections in fixed order
const sectionsMap = {{}};
rawSections.forEach(s => {{ sectionsMap[s.name] = s; }});
const widgetsContainer = document.getElementById('widgets');

sectionOrder.forEach((secName, idx) => {{
  const section = sectionsMap[secName] || {{ name: secName, stories: [] }};
  const color = colors[secName] || '#888888';
  const tier = TIER_BY_COLOR[color] || 3;
  const stories = section.stories || [];
  const skipInsight = noInsight.includes(secName);
  const num = String(idx + 1).padStart(2, '0');

  let headlineBullets;
  if (stories.length > 0) {{
    headlineBullets = '<ul class="widget-headlines">' +
      stories.map(s => '<li>' + escapeHtml(s.headline) + '</li>').join('') +
    '</ul>';
  }} else {{
    headlineBullets = '<ul class="widget-headlines"><li>No major stories this cycle</li></ul>';
  }}

  const widget = document.createElement('div');
  widget.className = 'widget';
  widget.dataset.tier = tier;

  const header = document.createElement('div');
  header.className = 'widget-header';
  header.innerHTML =
    '<span class="widget-num">' + num + ' &middot;</span>' +
    '<div class="widget-info">' +
      '<div class="widget-title" style="color:' + color + '">' + escapeHtml(secName) + '</div>' +
      headlineBullets +
    '</div>' +
    '<span class="widget-count">' + stories.length + '</span>' +
    '<span class="widget-chevron">&#9656;</span>';
  header.addEventListener('click', () => widget.classList.toggle('active'));

  const body = document.createElement('div');
  body.className = 'widget-body';
  const storiesDiv = document.createElement('div');
  storiesDiv.className = 'widget-stories';

  stories.forEach(story => {{
    const storyEl = document.createElement('div');
    storyEl.className = 'story';
    const headlineHtml = escapeHtml(story.headline);
    const sourceHtml = escapeHtml(story.source || '');
    const linkHtml = story.link
      ? '<a class="story-link" href="' + escapeHtml(story.link) + '" target="_blank" rel="noopener">Read source &#8594;</a>'
      : '';
    if (skipInsight) {{
      storyEl.innerHTML =
        '<div class="story-headline"><span>' + headlineHtml + '</span></div>' +
        '<div class="story-source">' + sourceHtml + '</div>' +
        linkHtml;
    }} else {{
      storyEl.innerHTML =
        '<div class="story-headline"><span>' + headlineHtml + '</span><span class="arrow">&#9656;</span></div>' +
        '<div class="story-source">' + sourceHtml + '</div>' +
        '<div class="story-details">' +
          '<div class="story-summary">' + escapeHtml(story.summary || '') + '</div>' +
          '<div class="story-insight">' +
            '<div class="insight-label">Apterreon Insight</div>' +
            escapeHtml(story.insight || '') +
          '</div>' +
          linkHtml +
        '</div>';
      storyEl.addEventListener('click', e => {{
        if (e.target.tagName === 'A') return;
        e.stopPropagation();
        storyEl.classList.toggle('open');
      }});
    }}
    storiesDiv.appendChild(storyEl);
  }});

  body.appendChild(storiesDiv);
  widget.appendChild(header);
  widget.appendChild(body);
  widgetsContainer.appendChild(widget);
}});

if (edgeText) {{
  document.getElementById('edge').innerHTML =
    '<div class="panel edge"><div class="panel-title">The Edge</div><p>' + escapeHtml(edgeText) + '</p></div>';
}}

if (tomorrowText) {{
  document.getElementById('tomorrow').innerHTML =
    '<div class="panel"><div class="panel-title">Tomorrow Watch</div><p>' + escapeHtml(tomorrowText) + '</p></div>';
}}
</script>
</body>
</html>"""


# ── Email Sender ────────────────────────────────────────────────────────────

def build_static_attachment_html(title, data, quotes, timestamp, usage_info=None):
    """Static HTML attachment (no JavaScript). Renders in any mail client, including iOS."""

    # ── Usage banner ──
    usage_html = ""
    if usage_info and usage_info.get("cost_monthly_projected") is not None:
        monthly = usage_info.get("cost_monthly_projected", 0)
        cost = usage_info.get("cost_this_call", 0)
        tokens = usage_info.get("total_tokens", 0)
        budget = 10.0
        pct = min(100, (monthly / budget) * 100)
        if monthly < 2:
            bar_color, status = "#27ae60", "LOW"
        elif monthly < 5:
            bar_color, status = "#f39c12", "MODERATE"
        else:
            bar_color, status = "#e74c3c", "HIGH"
        usage_html = f"""<div style="background:#141414;border-radius:10px;padding:12px 16px;margin-bottom:20px;border:1px solid #1e1e1e">
  <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px">
    <span style="font-size:10px;color:#666;text-transform:uppercase;letter-spacing:1.5px;font-weight:700">API Usage</span>
    <span style="font-size:10px;font-weight:700;color:{bar_color}">{status}</span>
  </div>
  <div style="background:#1e1e1e;border-radius:3px;height:4px;overflow:hidden;margin-bottom:6px"><div style="background:{bar_color};width:{pct:.0f}%;height:4px;border-radius:3px"></div></div>
  <div style="font-size:10px;color:#555">This brief: ${cost:.4f} ({tokens:,} tokens) &middot; Projected: ${monthly:.2f}/mo &middot; Budget: $10.00/mo</div>
</div>"""

    # ── Market bar ──
    market_html = ""
    if quotes:
        cards = ""
        for q in quotes:
            is_yield = q.get("is_yield", False)
            if is_yield:
                change_html = '<div style="font-size:12px;color:#888">7d yield</div>'
            else:
                try:
                    change = float(q["change_pct"])
                except (ValueError, KeyError):
                    change = 0
                color = "#27ae60" if change >= 0 else "#e74c3c"
                arrow = "&#9650;" if change >= 0 else "&#9660;"
                change_html = f'<div style="font-size:12px;font-weight:600;color:{color}">{arrow} {abs(change):.2f}%</div>'
            cards += f"""<div style="flex:1;min-width:80px;background:#141414;border-radius:10px;padding:12px 10px;text-align:center;border:1px solid #1e1e1e">
  <div style="font-size:10px;color:#666;text-transform:uppercase;letter-spacing:0.5px">{q['label']}</div>
  <div style="font-size:18px;font-weight:700;color:#fff;margin:4px 0 2px">{q['price']}</div>
  {change_html}
</div>"""
        market_html = f'<div style="display:flex;gap:8px;margin-bottom:24px">{cards}</div>'

    # ── Sections with stories ──
    sections_html = ""
    sections_map = {s["name"]: s for s in data.get("sections", [])}

    for sec_name, _ in SECTIONS:
        section = sections_map.get(sec_name)
        if not section or not section.get("stories"):
            continue
        color = SECTION_COLORS.get(sec_name, "#888")
        icon = SECTION_ICONS.get(sec_name, "&#128196;")
        stories = section["stories"]

        is_no_insight = sec_name in NO_INSIGHT_SECTIONS
        stories_html = ""
        for story in stories:
            link_html = ""
            if story.get("link"):
                link_html = f'<a href="{story["link"]}" style="display:inline-block;margin-top:8px;font-size:12px;color:{APT_RED};text-decoration:none">Read source &#8594;</a>'
            if is_no_insight:
                stories_html += f"""<div style="padding:14px 0;border-top:1px solid #1e1e1e">
  <div style="font-size:15px;font-weight:600;color:#e0e0e0">{story['headline']}</div>
  <div style="font-size:11px;color:#555;margin-top:2px">{story.get('source', '')}</div>
  {link_html}
</div>"""
            else:
                stories_html += f"""<div style="padding:14px 0;border-top:1px solid #1e1e1e">
  <div style="font-size:15px;font-weight:600;color:#e0e0e0">{story['headline']}</div>
  <div style="font-size:11px;color:#555;margin-top:2px">{story.get('source', '')}</div>
  <div style="font-size:14px;color:#aaa;margin:12px 0 10px;line-height:1.55">{story['summary']}</div>
  <div style="font-size:13px;color:{APT_RED};line-height:1.55;padding:12px 14px;background:rgba(224,122,47,0.06);border-radius:8px;border-left:3px solid {APT_RED}">
    <div style="font-size:9px;font-weight:700;text-transform:uppercase;letter-spacing:1.5px;color:{APT_RED};opacity:0.6;margin-bottom:4px">Apterreon Insight</div>
    {story['insight']}
  </div>
  {link_html}
</div>"""

        # Build headline bullet list for section header
        headline_bullets = ""
        for story in stories:
            headline_bullets += f'<li style="font-size:12px;color:#888;line-height:1.4;padding:2px 0;padding-left:12px;position:relative;word-wrap:break-word"><span style="position:absolute;left:0;color:#555">&#8226;</span>{story["headline"]}</li>'

        sections_html += f"""<div style="background:#141414;border-radius:12px;border:1px solid #1e1e1e;overflow:hidden;margin-bottom:12px">
  <div style="display:flex;align-items:flex-start;padding:16px 18px;gap:14px">
    <span style="font-size:24px">{icon}</span>
    <div style="flex:1">
      <div style="font-size:14px;font-weight:700;text-transform:uppercase;letter-spacing:1px;color:{color}">{sec_name}</div>
      <ul style="margin:6px 0 0 0;padding:0;list-style:none">{headline_bullets}</ul>
    </div>
  </div>
  <div style="padding:0 18px 16px">{stories_html}</div>
</div>"""

    # ── The Edge ──
    edge_html = ""
    edge_text = data.get("the_edge", "")
    if edge_text:
        edge_html = f"""<div style="margin-top:24px;padding:20px;background:#141414;border-radius:12px;border:1px solid rgba(224,122,47,0.2)">
  <div style="font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:2px;color:{APT_RED};margin-bottom:10px">&#9889; The Edge</div>
  <p style="font-size:14px;color:#ccc;line-height:1.7;margin:0">{edge_text}</p>
</div>"""

    # ── Tomorrow's Watch ──
    tomorrow_html = ""
    tomorrow_text = data.get("tomorrow_watch", "")
    if tomorrow_text:
        tomorrow_html = f"""<div style="margin-top:12px;padding:16px 20px;background:#141414;border-radius:12px;border:1px solid #1e1e1e">
  <div style="font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:2px;color:#666;margin-bottom:8px">&#128337; Tomorrow's Watch</div>
  <p style="font-size:13px;color:#999;line-height:1.55;margin:0">{tomorrow_text}</p>
</div>"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
</head>
<body style="margin:0;padding:0;background:#0a0a0a;color:#e8e8e8;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;line-height:1.6;-webkit-font-smoothing:antialiased">
<div style="max-width:700px;margin:0 auto;padding:20px 16px 60px">
{usage_html}
<div style="margin-bottom:20px">
  <h1 style="font-size:22px;font-weight:700;color:#fff;margin:0">{title}</h1>
  <div style="font-size:11px;color:#555;margin-top:2px">{timestamp}</div>
</div>
{market_html}
{sections_html}
{edge_html}
{tomorrow_html}
<div style="margin-top:32px;text-align:center">
  <p style="font-size:11px;color:#333;margin:0">{timestamp}</p>
</div>
</div>
</body>
</html>"""


# GitHub disables a repository's scheduled workflows after 60 days with no
# repository activity, and the workflow's own commits do not count because they
# are pushed with the built-in token. That is what killed this project on
# 2026-07-07. These two live here rather than in the workflow YAML for the same
# reason send_failure_alert does: embedded Python is parsed for the first time
# when it runs, and the one time this runs is the week it matters.
_KEEPALIVE_BOT = "actions@users.noreply.github.com"
_KEEPALIVE_WARN_AFTER_DAYS = 40
_KEEPALIVE_DISABLE_AT_DAYS = 60


def verify_smtp_login():
    """Prove the SMTP credential still works, without sending anything.

    The alert channel is exercised only when something has already gone wrong,
    which is the worst possible time to discover that an app-specific password
    was revoked. Connecting and authenticating is the whole of what sending
    needs, so doing just that, weekly, turns a silent expiry into a failed job.

    Deliberately does not fall back to emailing the problem, since the thing
    being tested is the ability to email. A non-zero exit makes the workflow red
    and GitHub's own failed-run notification carries the news on a path that
    does not depend on this credential at all.

    Raises on any failure; returns the server greeting on success."""
    app_password = os.environ.get("APTERREON_ICLOUD_APP_PASSWORD")
    if not app_password:
        raise ValueError("APTERREON_ICLOUD_APP_PASSWORD not set")
    context = ssl.create_default_context()
    with smtplib.SMTP(SMTP_SERVER, SMTP_PORT, timeout=30) as server:
        server.starttls(context=context)
        server.login(SMTP_USER, app_password)
        return f"{SMTP_SERVER}:{SMTP_PORT} accepted the credential for {SMTP_USER}"


def last_human_commit():
    """(iso_date, email) of the newest commit not made by the workflow bot."""
    log = subprocess.run(["git", "log", "--format=%cI|%ae"],
                         capture_output=True, text=True, check=True).stdout
    for line in log.splitlines():
        when, _, email = line.partition("|")
        if email.strip().lower() != _KEEPALIVE_BOT:
            return when, email.strip()
    return None, None


def check_inactivity(repo_url):
    """Warn by email when the inactivity clock is close to disabling the schedule.

    Returns the age in days of the last human commit, or None when history holds
    no non-bot commit to measure against."""
    when, email = last_human_commit()
    if not when:
        print("No non-bot commit found in history; nothing to compare against.")
        return None
    age = (datetime.now(timezone.utc) - datetime.fromisoformat(when)).days
    print(f"Last human commit: {when} by {email} ({age} days ago)")
    if age < _KEEPALIVE_WARN_AFTER_DAYS:
        print(f"Under the {_KEEPALIVE_WARN_AFTER_DAYS}-day threshold, "
              f"no warning needed.")
        return age
    left = _KEEPALIVE_DISABLE_AT_DAYS - age
    send_email(
        f"apterreon-brief: schedule stops in ~{left} days",
        "<h2>Push any commit to keep the pipeline running.</h2>"
        f"<p>The last commit that was not from the workflow bot was "
        f"<b>{age} days ago</b> ({when}).</p>"
        "<p>GitHub disables scheduled workflows after 60 days with no human "
        "repository activity. The pipeline's own commits do not reset that "
        "timer, so without a real commit the hourly and daily runs will stop "
        "silently, exactly as they did on 2026-07-07.</p>"
        "<p>Any commit resets the clock. An empty one is enough:</p>"
        "<pre>git commit --allow-empty -m keepalive &amp;&amp; git push</pre>"
        f'<p><a href="{repo_url}">{repo_url}</a></p>',
    )
    print(f"Warning sent: {age} days since the last human commit.")
    return age


def send_failure_alert(mode, status, run_url):
    """The alert that fires when a run fails, times out, or is cancelled.

    This lives here rather than in the workflow because the workflow's version
    was an f-string spanning three lines, which is a SyntaxError. It shipped in
    the same commit as the health assertion, so the only channel that reports a
    failure was itself broken for exactly as long as the check meant to trigger
    it was live. Nothing parsed that script until a failure ran it.

    Anything in this module is parsed on every single run, because the Gather
    step imports it. A typo here fails the run that introduced it, loudly, in
    front of someone who is looking. That is the entire reason for the move."""
    send_email(
        f"apterreon-brief {status.upper()} ({mode})",
        f"<h2>The data pipeline {status}.</h2>"
        f"<p>Mode: <b>{mode}</b></p>"
        "<p>A cancelled job is usually the 120-minute timeout. A failed one "
        "either raised, or completed without accomplishing its purpose and "
        "exited non-zero on that basis.</p>"
        f'<p><a href="{run_url}">Open the run</a></p>',
    )


def send_email(subject, html_body, attachment_html=None, attachment_name="brief.html"):
    """Send HTML email via iCloud SMTP with optional HTML attachment."""
    app_password = os.environ.get("APTERREON_ICLOUD_APP_PASSWORD")
    if not app_password:
        raise ValueError("APTERREON_ICLOUD_APP_PASSWORD not set")

    recipients = [r.strip() for r in RECIPIENT_EMAIL.split(",") if r.strip()]
    if not recipients:
        raise ValueError("RECIPIENTS env var resolved to empty list")

    msg = MIMEMultipart("mixed")
    msg["Subject"] = subject
    msg["From"] = f"{SENDER_NAME} <{SENDER_EMAIL}>"
    msg["To"] = ", ".join(recipients)

    body_part = MIMEMultipart("alternative")
    body_part.attach(MIMEText(html_body, "html"))
    msg.attach(body_part)

    if attachment_html:
        part = MIMEBase("text", "html")
        part.set_payload(attachment_html.encode("utf-8"))
        encoders.encode_base64(part)
        part.add_header("Content-Disposition", f"attachment; filename={attachment_name}")
        msg.attach(part)

    context = ssl.create_default_context()
    with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
        server.starttls(context=context)
        server.login(SMTP_USER, app_password)
        server.sendmail(SENDER_EMAIL, recipients, msg.as_string())

    print(f"Email sent to {len(recipients)} recipient(s): {subject}")


# ── Filesystem Storage (replaces S3) ───────────────────────────────────────


def s3_write_brief(brief_type, date_str_iso, interactive_html, data=None, quotes=None, timestamp=None):
    """Write brief HTML to docs/briefs/YYYY-MM-DD-type.html. Also write a JSON sidecar
    with structured story data so the index page can build a full-text search across
    all archived briefs."""
    key = f"briefs/{date_str_iso}-{brief_type}.html"
    out_path = DOCS_DIR / key
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(interactive_html, encoding="utf-8")
    print(f"Wrote {out_path}")

    if data is not None:
        json_path = out_path.with_suffix(".json")
        sidecar = {
            "key": key,
            "date": date_str_iso,
            "type": brief_type,
            "timestamp": timestamp,
            "sections": data.get("sections", []),
            "the_edge": data.get("the_edge", ""),
            "tomorrow_watch": data.get("tomorrow_watch", ""),
            "quotes": quotes or [],
        }
        json_path.write_text(json.dumps(sidecar, separators=(",", ":")), encoding="utf-8")

    return key


def s3_cleanup_old_briefs():
    """Delete briefs (and their JSON sidecars) older than retention period; skip pinned."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)
    pinned = s3_load_pins()

    deleted = 0
    for path in BRIEFS_DIR.glob("*.html"):
        key = f"briefs/{path.name}"
        if key in pinned:
            continue
        # Date comes from the filename ("2026-04-25-morning"), not the mtime:
        # under CI every file is as old as the checkout, which made this a no-op,
        # and run locally it would instead delete the entire archive at once.
        stem_date = path.stem.rsplit("-", 1)[0]
        try:
            brief_date = datetime.strptime(stem_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            continue  # unrecognized name: never delete something we cannot date
        if brief_date < cutoff:
            path.unlink()
            sidecar = path.with_suffix(".json")
            if sidecar.exists():
                sidecar.unlink()
            deleted += 1
    if deleted:
        print(f"Cleaned up {deleted} old briefs.")
    else:
        print("Nothing to clean up.")


def s3_load_pins():
    """Load set of pinned brief keys from state/pins.json."""
    f = STATE_DIR / "pins.json"
    if not f.exists():
        return set()
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
        return set(data.get("pinned", []))
    except Exception as e:
        print(f"Error loading pins: {e}")
        return set()


def s3_toggle_pin(brief_key):
    """Toggle pin status for a brief. Returns new pin state.
    (Cron context only. No live API endpoint; UI pin button uses localStorage.)"""
    pinned = s3_load_pins()
    if brief_key in pinned:
        pinned.discard(brief_key)
        new_state = False
    else:
        pinned.add(brief_key)
        new_state = True
    (STATE_DIR / "pins.json").write_text(
        json.dumps({"pinned": sorted(pinned)}, indent=2),
        encoding="utf-8",
    )
    return new_state


def s3_list_briefs():
    """List all brief files with metadata + structured story data (when sidecar JSON
    exists). Sorted newest first; secondary sort by edition order morning < midday < evening."""
    pinned = s3_load_pins()
    edition_order = {"morning": 0, "midday": 1, "evening": 2}
    briefs = []
    for path in BRIEFS_DIR.glob("*.html"):
        filename = path.stem  # e.g., "2026-04-25-morning"
        parts = filename.rsplit("-", 1)
        if len(parts) == 2:
            date_part, brief_type = parts
        else:
            date_part, brief_type = filename, "unknown"
        key = f"briefs/{path.name}"

        entry = {
            "key": key,
            "date": date_part,
            "type": brief_type,
            "modified": datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat(),
            "pinned": key in pinned,
        }

        json_path = path.with_suffix(".json")
        if json_path.exists():
            try:
                sidecar = json.loads(json_path.read_text(encoding="utf-8"))
                # Compact representation for the search index. Keep only what's
                # useful for filtering/search and brief previews.
                entry["sections"] = sidecar.get("sections", [])
                entry["the_edge"] = sidecar.get("the_edge", "")
                entry["tomorrow_watch"] = sidecar.get("tomorrow_watch", "")
                entry["timestamp"] = sidecar.get("timestamp", "")
            except Exception as e:
                print(f"Sidecar parse failed for {json_path.name}: {e}")

        briefs.append(entry)

    briefs.sort(
        key=lambda b: (b["date"], edition_order.get(b.get("type"), 99)),
        reverse=True,
    )
    return briefs


# ── Wikipedia constituent scraper ───────────────────────────────────

class _WikiTableParser(HTMLParser):
    """Extract rows from the first <table class="wikitable"> in the document.
    Rows are lists of cell text. Whitespace collapsed, tags stripped, footnote
    markers like [1] stripped."""

    def __init__(self):
        super().__init__()
        self.in_table = False
        self.found_first_table = False
        self.table_depth = 0
        self.in_row = False
        self.in_cell = False
        self.cell_parts = []
        self.current_row = []
        self.rows = []
        self.skip_depth = 0  # for nested elements we want to ignore

    def handle_starttag(self, tag, attrs):
        attr_dict = dict(attrs)
        cls = attr_dict.get("class", "")
        if tag == "table":
            if "wikitable" in cls and not self.found_first_table:
                self.in_table = True
                self.found_first_table = True
                self.table_depth = 1
            elif self.in_table:
                self.table_depth += 1
        elif self.in_table and tag == "tr":
            if self.table_depth == 1:
                self.in_row = True
                self.current_row = []
        elif self.in_row and tag in ("td", "th"):
            self.in_cell = True
            self.cell_parts = []
        elif self.in_cell and tag == "sup":
            # Footnote markers like <sup class="reference">[1]</sup>
            self.skip_depth += 1

    def handle_endtag(self, tag):
        if tag == "table" and self.in_table:
            self.table_depth -= 1
            if self.table_depth == 0:
                self.in_table = False
        elif tag == "tr" and self.in_row:
            self.in_row = False
            if self.current_row:
                self.rows.append(self.current_row)
        elif tag in ("td", "th") and self.in_cell:
            self.in_cell = False
            text = "".join(self.cell_parts).strip()
            text = re.sub(r"\s+", " ", text)
            text = re.sub(r"\[.*?\]", "", text).strip()
            self.current_row.append(text)
        elif self.in_cell and tag == "sup" and self.skip_depth > 0:
            self.skip_depth -= 1

    def handle_data(self, data):
        if self.in_cell and self.skip_depth == 0:
            self.cell_parts.append(data)


WIKIPEDIA_INDEX_SOURCES = [
    {
        "url":   "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
        "label": "S&P 500",
        "ticker_col": 0, "name_col": 1, "sector_col": 2, "sub_col": 3,
    },
    {
        "url":   "https://en.wikipedia.org/wiki/List_of_S%26P_400_companies",
        "label": "S&P 400",
        "ticker_col": 0, "name_col": 1, "sector_col": 2, "sub_col": 3,
    },
    {
        "url":   "https://en.wikipedia.org/wiki/List_of_S%26P_600_companies",
        "label": "S&P 600",
        "ticker_col": 0, "name_col": 1, "sector_col": 2, "sub_col": 3,
    },
]


def fetch_wikipedia_constituents(url, ticker_col=0, name_col=1, sector_col=2, sub_col=3):
    """Fetch one Wikipedia constituent page, parse first wikitable, return list of
    {ticker, name, sector, sub_industry}. Empty list on any failure."""
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Apterreon-IntelBrief/1.0 (research aggregator; ctlsmith@me.com)",
            "Accept": "text/html",
        })
        with urllib.request.urlopen(req, timeout=20) as resp:
            html_content = resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        print(f"Wikipedia fetch failed for {url}: {e}")
        return []

    parser = _WikiTableParser()
    try:
        parser.feed(html_content)
    except Exception as e:
        print(f"Wikipedia parse failed for {url}: {e}")
        return []

    rows = parser.rows
    if len(rows) < 2:
        print(f"Wikipedia parse: no data rows for {url}")
        return []

    out = []
    max_col = max(ticker_col, name_col, sector_col, sub_col)
    # Skip the header row (rows[0]); take all subsequent rows
    for row in rows[1:]:
        if len(row) <= max_col:
            continue
        ticker = row[ticker_col].strip().upper()
        # Tickers from Wikipedia sometimes have backslash or extra refs; normalize
        ticker = ticker.split()[0] if ticker else ""
        if not ticker or len(ticker) > 8 or not re.match(r"^[A-Z][A-Z0-9.\-]*$", ticker):
            continue
        name = row[name_col].strip()
        sector = row[sector_col].strip()
        sub_industry = row[sub_col].strip() if sub_col < len(row) else ""
        if name:
            out.append({
                "ticker": ticker,
                "name": name,
                "sector": sector,
                "sub_industry": sub_industry,
            })
    return out


def fetch_all_wiki_universes():
    """Fetch all configured Wikipedia constituent lists. Returns deduplicated list of
    dicts with {ticker, name, sector, sub_industry, index} (first occurrence wins
    when a ticker appears in multiple indexes)."""
    seen = set()
    out = []
    for src in WIKIPEDIA_INDEX_SOURCES:
        rows = fetch_wikipedia_constituents(
            src["url"],
            ticker_col=src["ticker_col"],
            name_col=src["name_col"],
            sector_col=src["sector_col"],
            sub_col=src["sub_col"],
        )
        kept = 0
        for r in rows:
            t = r["ticker"]
            if t in seen:
                continue
            seen.add(t)
            r["index"] = src["label"]
            out.append(r)
            kept += 1
        print(f"Wikipedia {src['label']}: parsed {len(rows)} rows, kept {kept} new tickers (total now {len(out)}).")
    return out


# ── iShares ETF holdings (Russell 1000/2000) ───────────────────────────────

# Disabled 2026-09-03. The .ajax holdings endpoint still answers 200 with
# Content-Type: text/csv, but the body is the HTML product page: iShares now
# gates the download behind client-side JS. Verified dead with a browser user
# agent, a Referer, and a full cookie-jar session handshake. Because the response
# looks superficially fine, the old failure surfaced only as a confusing
# "parsed 0 rows" line for two months.
#
# The universe still gets ~1,500 names from the S&P 500/400/600 Wikipedia
# sources, so this costs small-cap breadth, not correctness. Restore by putting
# a working CSV URL back in this list; fetch_ishares_holdings is unchanged and
# now reports the real reason when a body is not CSV.
ISHARES_SOURCES = []


def fetch_ishares_holdings(url, label):
    """Download and parse an iShares ETF holdings CSV. The CSV has ~9 lines of
    header metadata before the actual table; we scan for the row that starts with
    'Ticker,'. Returns list of {ticker, name, sector, sub_industry} for equity
    holdings. Empty list on any failure."""
    import csv as _csv
    from io import StringIO
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Apterreon-IntelBrief/1.0 (research aggregator; ctlsmith@me.com)",
            "Accept": "text/csv,application/octet-stream,*/*",
        })
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        print(f"iShares fetch failed for {label}: {e}")
        return []

    lines = raw.splitlines()
    header_idx = None
    for i, line in enumerate(lines):
        if line.lstrip().startswith("Ticker,"):
            header_idx = i
            break
    if header_idx is None:
        # Distinguish "the CSV changed shape" from "this is not a CSV at all",
        # which is what an interstitial or product page looks like here.
        head = " ".join(raw.lstrip()[:200].split())
        if head[:1] == "<":
            print(f"iShares parse failed for {label}: server returned HTML, not CSV. First bytes: {head[:80]!r}")
        else:
            print(f"iShares parse failed for {label}: no Ticker header row. First bytes: {head[:80]!r}")
        return []

    body = "\n".join(lines[header_idx:])
    reader = _csv.DictReader(StringIO(body))
    out = []
    for row in reader:
        ticker = (row.get("Ticker") or "").strip().upper()
        if not ticker or len(ticker) > 8 or not re.match(r"^[A-Z][A-Z0-9.\-]*$", ticker):
            continue
        asset_class = (row.get("Asset Class") or "").strip()
        if asset_class and asset_class.lower() != "equity":
            continue
        name = (row.get("Name") or "").strip()
        sector = (row.get("Sector") or "").strip()
        if name:
            out.append({
                "ticker": ticker,
                "name": name,
                "sector": sector,
                "sub_industry": "",
            })
    return out


# -- NASDAQ Trader symbol directory ----------------------------------------
#
# Replaces the dead iShares Russell feed as the source of small-cap breadth. These
# are plain pipe-delimited text files regenerated once per business day: no API key,
# no quota, and verified to serve identical bytes to five different user agents
# including an empty one, so unlike iShares there is no browser gating to rot.
#
# They carry no sector column; enrich_with_yfinance supplies that. S&P sources are
# merged FIRST in fetch_all_universes so their cleaner GICS classification wins on
# any overlapping ticker, and these only fill in what the indices do not cover.
NASDAQ_TRADER_SOURCES = [
    {"url": "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt", "kind": "nasdaq"},
    {"url": "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt", "kind": "other"},
]

# Security-type tells, used to keep operating companies and drop everything else.
_NT_BAD_NAME = re.compile(
    r"\b(warrant|unit|right|preferred|depositary|debenture|note|"
    r"beneficial interest|etn|when[- ]issued|contingent value)\b", re.I)
_NT_SUFFIX_CODES = set("WRULPZQ")   # 5th char of a 5-char Nasdaq symbol
_NT_FUND_VENUES = {"P", "Z"}        # NYSE Arca, Cboe BZX: fund listing venues
_NT_BAD_FIN_STATUS = {"D", "E", "H", "Q"}  # deficient / delinquent / bankrupt
_NT_EXCHANGES = {"N": "NYSE", "A": "NYSE American", "P": "NYSE Arca", "Z": "Cboe BZX"}


def _nt_reject(symbol, name, is_etf, is_test, nextshares, exchange_code, fin_status):
    """Why this row is not an operating-company common stock, or None to keep it."""
    if is_test == "Y":
        return "test issue"
    if is_etf == "Y":
        return "ETF"
    if nextshares == "Y":
        return "NextShares fund"
    if exchange_code in _NT_FUND_VENUES:
        return "fund listing venue"
    if "$" in symbol or "." in symbol:
        return "class/preferred suffix"
    if len(symbol) == 5 and symbol[4] in _NT_SUFFIX_CODES:
        return "security-type suffix"
    if fin_status in _NT_BAD_FIN_STATUS:
        return "financial status"
    if _NT_BAD_NAME.search(name or ""):
        return "non-common-stock name"
    if not re.fullmatch(r"[A-Z]{1,5}", symbol or ""):
        return "non-alphabetic symbol"
    return None


def fetch_nasdaq_trader_listings():
    """US-listed operating-company common stock from the NASDAQ Trader directory."""
    out, seen, rejected = [], set(), {}
    for source in NASDAQ_TRADER_SOURCES:
        try:
            req = urllib.request.Request(source["url"], headers={
                "User-Agent": EDGAR_USER_AGENT,
                "Accept": "text/plain,*/*",
            })
            with urllib.request.urlopen(req, timeout=30) as resp:
                text = resp.read().decode("utf-8", errors="replace")
        except Exception as exc:
            print(f"nasdaqtrader: fetch failed for {source['kind']}: {exc}")
            continue

        # nasdaqtrader.com answers 200 with an HTML page on a bad path, which is
        # exactly how the iShares feed failed silently for two months.
        if text.lstrip()[:1] == "<":
            head = " ".join(text.lstrip()[:120].split())
            print(f"nasdaqtrader: {source['kind']} returned HTML, not data: {head!r}")
            continue

        # The final line is a "File Creation Time:" trailer, not a record.
        lines = [ln for ln in text.splitlines()
                 if ln and not ln.startswith("File Creation Time")]
        if len(lines) < 100:
            print(f"nasdaqtrader: {source['kind']} only {len(lines)} lines, ignoring.")
            continue

        kept = 0
        for row in csv.DictReader(io.StringIO("\n".join(lines)), delimiter="|"):
            if source["kind"] == "nasdaq":
                symbol = (row.get("Symbol") or "").strip()
                venue = "Nasdaq"
                reason = _nt_reject(symbol, row.get("Security Name"), row.get("ETF"),
                                    row.get("Test Issue"), row.get("NextShares"),
                                    "", row.get("Financial Status"))
            else:
                symbol = (row.get("ACT Symbol") or "").strip()
                exchange_code = (row.get("Exchange") or "").strip()
                venue = _NT_EXCHANGES.get(exchange_code, exchange_code or "Other")
                reason = _nt_reject(symbol, row.get("Security Name"), row.get("ETF"),
                                    row.get("Test Issue"), "", exchange_code, "")
            if reason:
                rejected[reason] = rejected.get(reason, 0) + 1
                continue
            if symbol in seen:
                continue
            seen.add(symbol)
            out.append({
                "ticker": symbol,
                "name": (row.get("Security Name") or "").strip(),
                "sector": "",        # not in these files; yfinance fills it in
                "sub_industry": "",
                "index": venue,
            })
            kept += 1
        print(f"nasdaqtrader {source['kind']}: kept {kept} of {len(lines) - 1} rows.")

    if rejected:
        summary = ", ".join(f"{k} {v}" for k, v in sorted(rejected.items(), key=lambda x: -x[1]))
        print(f"nasdaqtrader: filtered out {sum(rejected.values())} non-operating rows ({summary}).")
    return out


# Yahoo uses its own sector taxonomy, not GICS, and the Wikipedia S&P scrapes use
# GICS. Mixing them splits one sector into two cohorts under different names:
# "Financials" (258 S&P names) alongside "Financial Services" (600 Yahoo names),
# "Health Care" alongside "Healthcare", and so on. Every factor score in this
# pipeline is a z-score against sector peers, so an unmapped taxonomy halves each
# cohort and corrupts the scores on both sides of the split. Normalize to GICS.
YF_SECTOR_TO_GICS = {
    "financial services": "Financials",
    "healthcare": "Health Care",
    "technology": "Information Technology",
    "consumer cyclical": "Consumer Discretionary",
    "consumer defensive": "Consumer Staples",
    "basic materials": "Materials",
    # These already match GICS and are listed so the map doubles as the
    # authoritative set of sector names the pipeline is allowed to emit.
    "industrials": "Industrials",
    "energy": "Energy",
    "real estate": "Real Estate",
    "utilities": "Utilities",
    "communication services": "Communication Services",
    "financials": "Financials",
    "health care": "Health Care",
    "information technology": "Information Technology",
    "consumer discretionary": "Consumer Discretionary",
    "consumer staples": "Consumer Staples",
    "materials": "Materials",
}


def normalize_sector(value):
    """Map a sector label to its GICS name, or "" if unrecognized.

    An unknown label is dropped rather than passed through: a one-off spelling
    would otherwise become its own peer cohort of one, and a cohort of one makes
    every z-score in it exactly zero."""
    if not value or not isinstance(value, str):
        return ""
    return YF_SECTOR_TO_GICS.get(value.strip().lower(), "")


# What each source has historically supplied. A source returning far less than
# this has failed rather than shrunk: index membership moves by a handful of
# names a quarter, not by hundreds in a day.
#
# The total-based guard downstream was written when the universe was ~1,500
# names, where losing one S&P page was a third of everything. At 5,336 it no
# longer covers the case it exists for: losing all of the S&P 500 is 503 names,
# which is 9.4% and slips under a 10% threshold. That would mark 503 live
# constituents delisted and write in_index=0 for them into a panel row that
# record_fundamentals can never rewrite.
EXPECTED_SOURCE_MINIMUM = {
    "S&P 500": 400,
    "S&P 400": 320,
    "S&P 600": 480,
    "NASDAQ": 2500,
}


def fetch_all_universes():
    """Build the full deduplicated stock universe from Wikipedia (S&P 500/400/600),
    plus any working ISHARES_SOURCES. S&P sources go first because their sector
    classification is cleaner, then the ETF holdings fill in everything else.
    First-occurrence-by-ticker wins.

    ISHARES_SOURCES is currently empty (see the note there), so in practice the
    universe is S&P-only at roughly 1,500 names."""
    seen = set()
    out = []

    for src in WIKIPEDIA_INDEX_SOURCES:
        rows = fetch_wikipedia_constituents(
            src["url"],
            ticker_col=src["ticker_col"],
            name_col=src["name_col"],
            sector_col=src["sector_col"],
            sub_col=src["sub_col"],
        )
        kept = 0
        for r in rows:
            t = r["ticker"]
            if t in seen:
                continue
            seen.add(t)
            r["index"] = src["label"]
            out.append(r)
            kept += 1
        print(f"Wikipedia {src['label']}: parsed {len(rows)} rows, kept {kept} new tickers (total now {len(out)}).")
        floor = EXPECTED_SOURCE_MINIMUM.get(src["label"])
        if floor and len(rows) < floor:
            # Not a shrinking index: a failed fetch or a changed table layout.
            # Returning a short universe would mark the missing constituents
            # dropped and stamp in_index=0 into a panel row that can never be
            # rewritten, so refuse the whole build instead.
            print(f"UNIVERSE ABORT: {src['label']} returned {len(rows)} rows against an "
                  f"expected floor of {floor}. One source failing is invisible in the "
                  f"total once the universe is this large, so the check is per source. "
                  f"Keeping yesterday's universe rather than recording {floor - len(rows)}+ "
                  f"false delistings.")
            return []

    # Broad US listings last: they have no sector, so anything already claimed by
    # an S&P index keeps its GICS classification and only genuinely new small caps
    # are added here.
    nt_rows = fetch_nasdaq_trader_listings()
    nt_kept = 0
    for r in nt_rows:
        if r["ticker"] in seen:
            continue
        seen.add(r["ticker"])
        out.append(r)
        nt_kept += 1
    nt_floor = EXPECTED_SOURCE_MINIMUM.get("NASDAQ")
    if nt_rows and nt_kept < nt_floor:
        print(f"UNIVERSE ABORT: nasdaqtrader added only {nt_kept} tickers against an "
              f"expected floor of {nt_floor}. It supplies roughly 72% of the universe, "
              f"so a partial answer here would mark thousands of live listings dropped.")
        return []
    if not nt_rows:
        print("UNIVERSE ABORT: nasdaqtrader returned nothing. It supplies roughly 72% "
              "of the universe; continuing would record the other 3,800 names as "
              "delisted in a panel row that cannot be rewritten.")
        return []
    print(f"nasdaqtrader: added {nt_kept} tickers not in any S&P index (total now {len(out)}).")

    for src in ISHARES_SOURCES:
        rows = fetch_ishares_holdings(src["url"], src["label"])
        kept = 0
        for r in rows:
            t = r["ticker"]
            if t in seen:
                continue
            seen.add(t)
            r["index"] = src["label"]
            out.append(r)
            kept += 1
        print(f"iShares {src['label']}: parsed {len(rows)} rows, kept {kept} new tickers (total now {len(out)}).")

    return out


# ── yfinance enrichment (free, no API key, parallel) ───────────────────────

# Every field below is compared against numeric bounds downstream. Yahoo does not
# guarantee the type: the same key can come back as a float, as a numeric string
# ("12.4"), as "Infinity"/"NaN", or as a non-numeric placeholder. A single str
# reaching one of those comparisons raises TypeError and kills the whole run, so
# the payload is normalized once on arrival instead of guarding ~40 call sites.
YF_NUMERIC_KEYS = frozenset({
    "marketCap", "currentPrice", "regularMarketPrice", "regularMarketChangePercent",
    "trailingPE", "regularMarketVolume", "averageVolume", "revenueGrowth",
    "earningsGrowth", "enterpriseToEbitda", "enterpriseToRevenue", "priceToBook",
    "freeCashflow", "fiftyTwoWeekHigh", "fiftyDayAverage", "52WeekChange",
    "SandP52WeekChange", "averageDailyVolume10Day", "averageVolume10days",
    "regularMarketPreviousClose",
    "returnOnEquity", "totalDebt", "totalCash", "ebitda", "netIncomeToCommon",
    "operatingCashflow", "totalAssets", "operatingMargins", "grossMargins",
    "numberOfAnalystOpinions", "heldPercentInstitutions", "heldPercentInsiders",
    "earningsTimestamp", "earningsTimestampStart", "earningsTimestampEnd",
    "earningsCallTimestampStart",
})


def _coerce_yf_numerics(info):
    """Return a copy of a yfinance `info` dict with every known-numeric key forced
    to a finite float, or None where the value is missing or uninterpretable.
    Booleans are treated as absent: Yahoo uses False as a 'no data' marker on
    numeric fields, and bool is an int subclass that would otherwise slip through."""
    out = dict(info)
    for key in YF_NUMERIC_KEYS:
        if key not in out:
            continue
        val = out[key]
        if val is None or isinstance(val, bool):
            out[key] = None
            continue
        if not isinstance(val, (int, float)):
            try:
                val = float(str(val).replace(",", "").strip())
            except (TypeError, ValueError):
                out[key] = None
                continue
        out[key] = val if math.isfinite(val) else None
    return out


def enrich_with_yfinance(stocks, max_workers=6):
    """Enrich stock dicts in place with live data from Yahoo Finance via yfinance:
    price, market_cap, change_pct, pe, volume. Threaded for speed (~45s for 1500
    tickers with 10 workers under good conditions). Returns count of fields newly
    fetched (not the total cumulative coverage; merge-cache logic upstream tracks
    that). Skips silently if yfinance is not installed."""
    if not stocks:
        return 0
    try:
        import yfinance as yf
    except ImportError:
        print("yfinance: not installed, skipping enrichment.")
        return 0
    from concurrent.futures import ThreadPoolExecutor, as_completed

    by_ticker = {s["ticker"]: s for s in stocks}
    tickers = list(by_ticker.keys())

    def fetch_one(sym, attempts=3):
        """Fetch one ticker's info, retrying through Yahoo's rate limiter.

        Yahoo throttles hard on a universe this size: a single-pass run returned
        usable data for only 55% of 1,506 tickers and finished in 18 seconds,
        which is the signature of most requests being rejected rather than
        answered. A throttled response comes back as an empty or priceless dict
        rather than an exception, so retry on that too, with jittered backoff so
        the threads do not march in lockstep."""
        # Yahoo uses '-' for class shares (BRK-B); Wikipedia uses '.' (BRK.B). Translate.
        yf_sym = sym.replace(".", "-")
        for attempt in range(attempts):
            try:
                info = yf.Ticker(yf_sym).info
                if info and (info.get("marketCap") is not None
                             or info.get("currentPrice") is not None
                             or info.get("regularMarketPrice") is not None):
                    return sym, info
            except Exception:
                pass
            if attempt < attempts - 1:
                time.sleep(1.5 * (attempt + 1) + random.random())
        return sym, None

    enriched = 0
    skipped = 0
    t0 = time.time()
    # Least recently refreshed first, so a budgeted run resumes where the last one
    # stopped instead of re-fetching the same head of the list every time.
    tickers.sort(key=lambda sym: (by_ticker.get(sym) or {}).get("last_updated") or "")

    yf_budget_hit = False
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = [ex.submit(fetch_one, s) for s in tickers]
        for f in as_completed(futures):
            if not yf_budget_hit and time.time() - t0 > _YF_TIME_BUDGET_S:
                yf_budget_hit = True
                for pending in futures:
                    pending.cancel()
            if f.cancelled():
                continue
            sym, info = f.result()
            if not info:
                continue
            s = by_ticker.get(sym)
            if not s:
                continue
            try:
                info = _coerce_yf_numerics(info)
            except Exception:
                skipped += 1
                continue
            try:
                # ── Core fields ─────────────────────────────
                cap = info.get("marketCap")
                price = info.get("currentPrice") or info.get("regularMarketPrice")
                chg = info.get("regularMarketChangePercent")
                pe = info.get("trailingPE")
                vol = info.get("regularMarketVolume") or info.get("averageVolume")
                if cap is not None and 0 < cap < 1e14:
                    s["market_cap"] = cap
                if price is not None and 0 < price < 1e6:
                    s["price"] = price
                prev = info.get("regularMarketPreviousClose")
                pct = None
                if prev is not None and prev > 0 and price is not None and price > 0:
                    # Unambiguous: two prices, one unit.
                    pct = (price - prev) / prev * 100
                elif chg is not None:
                    # No previous close. Yahoo documents this field as a percent,
                    # so take it as one rather than guessing from its magnitude.
                    pct = chg
                if pct is not None and abs(pct) <= 40:
                    s["change_pct"] = pct
                if pe is not None and -500 < pe < 1000:
                    s["pe"] = pe
                if vol is not None and vol > 0:
                    s["volume"] = vol

                # -- Classification --------------------------
                # The NASDAQ Trader directory carries no sector, so for every
                # non-S&P name this is the only place classification comes from.
                # Sector drives the peer-relative factor z-scores on the stocks
                # page, so a blank one silently drops the row out of those stats.
                if not s.get("sector"):
                    mapped = normalize_sector(info.get("sector"))
                    if mapped:
                        s["sector"] = mapped
                if not s.get("sub_industry"):
                    industry_name = info.get("industry")
                    if isinstance(industry_name, str) and industry_name.strip():
                        s["sub_industry"] = industry_name.strip()

                # ── Growth factors ──────────────────────────
                rev_g = info.get("revenueGrowth")
                if rev_g is not None and abs(rev_g) < 5:
                    s["revenue_growth_yoy"] = rev_g
                eps_g = info.get("earningsGrowth")
                if eps_g is not None and abs(eps_g) < 10:
                    s["eps_growth_yoy"] = eps_g

                # ── Value factors ───────────────────────────
                ev_eb = info.get("enterpriseToEbitda")
                # 0 <, not abs(): a negative multiple means negative EBITDA,
                # and inverting it scored the biggest losses as the best value.
                if ev_eb is not None and 0 < ev_eb < 200:
                    s["ev_ebitda"] = ev_eb
                ev_rev = info.get("enterpriseToRevenue")
                if ev_rev is not None and 0 < ev_rev < 100:
                    s["ev_revenue"] = ev_rev
                pb = info.get("priceToBook")
                if pb is not None and 0 < pb < 100:
                    s["price_book"] = pb
                fcf = info.get("freeCashflow")
                if fcf is not None and cap and cap > 0:
                    fcf_yield = fcf / cap
                    if -1 < fcf_yield < 1:
                        s["fcf_yield"] = fcf_yield

                # ── Momentum factors ────────────────────────
                high52 = info.get("fiftyTwoWeekHigh")
                if price and high52 and high52 > 0:
                    s["high52w_proximity"] = (price - high52) / high52
                # return_1m and return_12_2 are set by
                # derive_returns_from_history, from the daily closes on disk.
                # What used to be here computed return_1m as
                # (price - fiftyDayAverage) / fiftyDayAverage and then built
                # return_12_2 out of it by subtraction. Neither is what its
                # label said it was.
                chg52 = info.get("52WeekChange")
                if chg52 is not None and abs(chg52) < 10:
                    s["return_52w"] = chg52
                sp_chg52 = info.get("SandP52WeekChange")
                if chg52 is not None and sp_chg52 is not None:
                    rel = chg52 - sp_chg52
                    if abs(rel) < 5:
                        s["rel_strength_sp500"] = rel
                v10 = info.get("averageDailyVolume10Day") or info.get("averageVolume10days")
                v3m = info.get("averageVolume")
                if v10 and v3m and v3m > 0:
                    vt = v10 / v3m - 1
                    if abs(vt) < 10:
                        s["volume_trend"] = vt

                # ── Quality factors ─────────────────────────
                roe = info.get("returnOnEquity")
                if roe is not None and -3 < roe < 3:
                    s["roe_ttm"] = roe
                debt = info.get("totalDebt") or 0
                tcash = info.get("totalCash") or 0
                ebitda = info.get("ebitda")
                # Positive EBITDA only. With negative EBITDA the ratio flips
                # sign, so a distressed borrower became indistinguishable from a
                # company sitting on net cash, and Quality inverts this field, so
                # the distress scored as prudence.
                if ebitda is not None and ebitda > 0:
                    nde = (debt - tcash) / ebitda
                    if -20 < nde < 50:
                        s["net_debt_ebitda"] = nde
                # accruals_ratio is computed from EDGAR in compute_edgar_factors.
                # It used to be derived here from info["totalAssets"], which
                # yfinance does not expose, so it was never once populated.
                op_m = info.get("operatingMargins")
                if op_m is not None and -2 < op_m < 2:
                    s["operating_margin"] = op_m
                gm = info.get("grossMargins")
                if gm is not None and -2 < gm < 2:
                    s["gross_margin"] = gm

                # ── Neglect inputs (Peter Lynch thesis: under-followed names) ──
                n_analysts = info.get("numberOfAnalystOpinions")
                if isinstance(n_analysts, (int, float)) and 0 <= n_analysts < 200:
                    s["analyst_count"] = int(n_analysts)
                inst = info.get("heldPercentInstitutions")
                if isinstance(inst, (int, float)) and 0 <= inst <= 1.5:
                    s["inst_ownership"] = inst
                ins_o = info.get("heldPercentInsiders")
                if isinstance(ins_o, (int, float)) and 0 <= ins_o <= 1.0:
                    s["insider_ownership"] = ins_o

                # Earnings date, meaning the NEXT one. The key order here is the
                # whole fix: earningsTimestamp is the most recently REPORTED
                # date about as often as it is the next one, and it used to be
                # tried first, so 3,837 of 4,357 populated values pointed into
                # the past and 520 into the future. The column was labelled
                # "Earnings" on the screener and was mostly last quarter.
                #
                # Probing seven large caps: earningsTimestampStart was forward
                # for all seven, earningsTimestamp was forward for four, and
                # earningsCallTimestampStart was BACKWARD for four, so it goes
                # last rather than second.
                #
                # The window was -730 to +730, which is how values from 2024
                # survived. A next-earnings date is not two years old. Two days
                # of slack on the near side covers a call that happened today
                # before Yahoo rolled the field forward.
                ed_iso = None
                for ts_key in ("earningsTimestampStart", "earningsTimestampEnd",
                               "earningsTimestamp", "earningsCallTimestampStart"):
                    ts = info.get(ts_key)
                    if ts and isinstance(ts, (int, float)) and ts > 0:
                        try:
                            ed = datetime.fromtimestamp(ts, tz=timezone.utc).date()
                            delta = (ed - datetime.now(tz=timezone.utc).date()).days
                            if -2 <= delta < 400:
                                ed_iso = ed.isoformat()
                                break
                        except Exception:
                            continue
                if not ed_iso:
                    ed_list = info.get("earningsDate")
                    if isinstance(ed_list, list) and ed_list:
                        raw = ed_list[0]
                        if isinstance(raw, (int, float)) and raw > 0:
                            try:
                                ed = datetime.fromtimestamp(raw, tz=timezone.utc).date()
                                # This path had no sanity check at all, so it
                                # could reinstate exactly what the loop above
                                # just refused.
                                delta = (ed - datetime.now(tz=timezone.utc).date()).days
                                if -2 <= delta < 400:
                                    ed_iso = ed.isoformat()
                            except Exception:
                                pass
                if ed_iso:
                    s["earnings_date"] = ed_iso

                if cap or price:
                    enriched += 1
                    # Per-row freshness stamp: this run successfully fetched yfinance data.
                    s["last_updated"] = datetime.now(EASTERN).strftime("%Y-%m-%d")
            except Exception as exc:
                # A single unexpected payload shape must not take down the run:
                # the brief has already been emailed by this point.
                skipped += 1
                if skipped <= 5:
                    print(f"yfinance: skipped {sym} ({type(exc).__name__}: {exc})")

    elapsed = time.time() - t0
    suffix = f", {skipped} skipped on bad payloads" if skipped else ""
    print(f"yfinance: enriched {enriched}/{len(tickers)} tickers in {elapsed:.1f}s ({max_workers} threads){suffix}.")
    if yf_budget_hit:
        print(f"yfinance: stopped at the {_YF_TIME_BUDGET_S}s budget with {enriched} of "
              f"{len(tickers)} done. Yahoo is throttling. The remainder are the least "
              f"recently refreshed and go first next run. Stopping here means this run "
              f"still commits; overrunning the job timeout would discard all of it.")
    return enriched


# ── News sentiment: Loughran-McDonald financial dict + VADER ────────────────

# Curated subset of the McDonald Master Dictionary's positive and negative word
# lists. Not exhaustive, but covers the high-frequency financial vocabulary that
# shows up in news headlines. Source: Loughran & McDonald (2011) "When is a
# Liability not a Liability? Textual Analysis, Dictionaries, and 10-Ks."
LM_POSITIVE = frozenset("""
able achieve achieved achievement achievements advance advancement advances
advantage advantageous advantages benefit benefits beneficial best better
boost boosted boosts breakthrough breakthroughs collaborate collaborated
collaboration collaborations confident confidence delight delighted deliver
delivered delivers despite distinction distinctions distinctive dynamic
easily easy effective efficient efficiently empower empowered enable enabled
encouraging enhance enhanced enhancement enhancements enjoy enjoyed enjoying
exceeding exceed exceeded exceptional excellence excellent exclusive
favorable favorably gain gained gains good greatest highest improve improved
improvement improvements improving impressive innovate innovated innovation
innovations innovative invent invented invention inventions leadership
leading lucrative meritorious opportunities opportunity outperform outperformed
outperforming positive positively praise praised premier proactively
proficient profitability profitable profitably progress prosperity prosperous
prove proven receptive record records reliable resilient reward rewarded
rewarding satisfaction satisfactory smooth solid stability stable strength
strengthen strengthened strengthening strengths strong stronger strongest
succeed succeeded successes successful successfully surpass surpassed
transparency tremendous unmatched upbeat upturn unprecedented victory wins
winner winning won worthy
""".split())

LM_NEGATIVE = frozenset("""
abandon abandoned abandonment abandoning abnormal abnormally abolish abolished
abrupt abruptly absence accident accidental accidents accusation accusations
accuse accused accuses accusing acquittal acquitted adverse adversely against
allege alleged allegedly allegation allegations alleging anomalies anomaly
antitrust apologize apologized apologizes argue argued aware bad badly
bankrupt bankruptcies bankruptcy barred barrier barriers below blame
blamed blames bottlenecks breach breached breaches breaching break broken
burden burdens cancel canceled canceling cancellation cancellations cancels
challenge challenged challenges challenging chaos circumvent claim claimed
claims closed closure closures collapse collapsed collusion complaint
complaints complicated complication complications concealed concern
concerned concerns concerning conflict conflicts confusing confusion
contradict contradicted contradicting contradiction contraction contractions
controversies controversy convict convicted conviction crime criminal
criminals criminally crisis critical criticism criticisms criticize criticized
criticizes cut cuts cutting damage damaged damages danger dangers default
defaulted defaulting defaults defective defects deficiencies deficiency
deficit deficits delay delayed delays demolish demolished demolishing
demote demoted denial denials denied denies deny denying deplete depleted
deteriorate deteriorated deteriorates deteriorating deterioration detrimental
diminish diminished dire disappear disappeared disappoint disappointed
disappointing disappointment disappointments disapproval disapprove
disapproved disaster disasters disastrous discontinue discontinued
discontinuing discrepancies discrepancy disgorge disgorged disgorgement
dispute disputed disputes disrupt disrupted disrupting disruption
disruptions doubt doubtful doubts down downgrade downgraded downsize
downsized downturn drag dropped drought erode eroded erosion error errors
exaggerate exaggerated excessive excessively exposed exposure failed
failure failures fall fallen falling false falsely fault faults fear
fears felony felonies fictitious fired flaw flawed flaws forced fraud
fraudulent fraudulently halt halted harm harmed harmful harshly hazardous
hindered hindrance hostile hurt illegal illegality illegally illicit
impair impaired impairment impairments impede impeded improper improperly
inadequate inadequately inappropriate incomplete incompetence incorrect
incorrectly indictment indictments inefficient inefficiency injunction
injunctions inquiry insolvency insolvent investigation investigations
irregular irregularities irregularity lacking lawsuit lawsuits liability
liabilities lien liens limitation limitations litigation litigations
lockup loss losses lost manipulate manipulated manipulating manipulation
mediocre mismanage mismanaged mismanagement misrepresent misrepresentation
miss missed missing mistake mistakes negative negatively neglect neglected
nonperformance nonperforming objection objections obstacle obstacles
obstruct obstructed obstruction omission omit omitted oppose opposed
opposes opposition outage outages overstate overstated overstatement
panic peril perils penalize penalized penalties penalty plead pleaded
plummet plummeted plunge plunged poor poorly possibility postpone
postponed postponement precluded predatory prejudice prejudiced
prevent prevented prevents probe probes problem problems prosecute
prosecuted prosecution prosecutions question questionable questioned
recall recalled recalls reduce reduced reduces reduction reductions
reject rejected rejection reluctant remediate remediation reorganization
restate restated restatement restatements restrict restricted restriction
restrictions restructure restructured restructuring revoke revoked
risk risks risky sanction sanctions scandal scrap scrapped seize seized
serious seriously settle settled settlement settlements shortage shortages
shortfall shrink shrinking shut shutdown sluggish slow slowdown slower
strain strains stress stressed strict struggle struggled struggling
subpoena subpoenaed subpoenas suffer suffered suffering suit suspended
terminated termination terror terrorism threat threaten threatened
threatening threats tragedy trouble troubled troubles unable unattractive
uncollectible undercut undercutting underestimate underestimated underperform
underperformed underperforming undermine undermined undue unethical
unexpected unfair unfavorable unfavorably unforeseen unfounded unjust
unlawful unlawfully unprofitable unsafe unsatisfactory unstable unsuccessful
unsuccessfully untimely vandalism verdict violate violated violates violating
violation violations volatile volatility vulnerability vulnerable warn
warned warning warnings weak weaken weakened weakening weaker weakness
weaknesses worse worst worried worry worsen worsened worsening wrong
wrongdoing wrongful wrongly
""".split())


# A tone average built from a single headline is one headline's wording
# presented as a company's coverage.
MIN_LM_HEADLINES = 2


def compute_lm_score(text):
    """Loughran-McDonald financial sentiment polarity, or None.

    Returns (positive - negative) / (positive + negative) in [-1, +1] when the
    text contains any dictionary word, and None when it contains none.

    It used to return 0.0 for that case, which reads on the page as perfectly
    balanced coverage and is really "this headline contains no word the
    dictionary knows". Those are not the same claim, and the second one is by
    far the more common: across the 81,385 headlines cached here, 76% match no
    LM term at all, so a field displayed as sentiment was mostly an average of
    zeros meaning nothing was measured. VADER, scored on the same headlines,
    is 0.0 for 11.7% of tickers, which is what a real neutral rate looks like.

    The dictionary is a subset of the published lists, 149 positive and 520
    negative against roughly 350 and 2,350, so the miss rate is higher here
    than the method itself implies. Returning None keeps that a gap in coverage
    rather than a claim about tone."""
    if not text:
        return None
    words = re.findall(r"[a-z]+", text.lower())
    pos = sum(1 for w in words if w in LM_POSITIVE)
    neg = sum(1 for w in words if w in LM_NEGATIVE)
    total = pos + neg
    if total == 0:
        return None
    return round((pos - neg) / total, 3)


_vader_analyzer = None
def compute_vader_score(text):
    """VADER compound score in [-1, +1]. Lazy-imports the analyzer on first use."""
    global _vader_analyzer
    if _vader_analyzer is None:
        try:
            from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
            _vader_analyzer = SentimentIntensityAnalyzer()
        except Exception:
            return 0.0
    if not text:
        return 0.0
    try:
        return round(_vader_analyzer.polarity_scores(text)["compound"], 3)
    except Exception:
        return 0.0


# ── Per-ticker news (Google News RSS, lazy-loaded by the page) ──────────────

NEWS_DIR = DOCS_DIR / "news"

# Per-ticker news files are a bare JSON list with no room for a file-level
# timestamp, and the stocks page reads that shape directly, so freshness is
# tracked in a sidecar manifest instead of by mtime (which CI resets) or by
# reformatting ~2,900 files. Maps TICKER -> ISO-8601 of last successful fetch.
NEWS_FETCH_LOG = STATE_DIR / "news_fetch_log.json"


def _load_news_fetch_log():
    try:
        data = json.loads(NEWS_FETCH_LOG.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_news_fetch_log(log):
    try:
        NEWS_FETCH_LOG.write_text(json.dumps(log, sort_keys=True, separators=(",", ":")), encoding="utf-8")
    except Exception as exc:
        # A lost manifest only costs a redundant refetch next run, never correctness.
        print(f"news: could not write fetch log ({type(exc).__name__}: {exc}).")


# Google News does not honour quoted phrases as strict boolean literals; it
# expands semantically. Querying '"ALL" OR "Allstate"' returned fifteen stories
# about interstate highways, rugby, the NBA, diesel prices and a balloon museum,
# and not one about the insurer. 242 tickers in the registry are one or two
# characters and many more are ordinary words: KEY, ON, IT, CAT, NOW, WELL.
#
# Nothing downstream could detect this. The contaminated count feeds
# news_count_7d, the sentiment averages and the neglect score, and those land in
# the panel permanently while the headlines that produced them are never
# archived. So the filter has to run here, before the file is written.
NEWS_MAX_ITEMS = 15

# Two kinds of word get stripped before a company name is used as a search
# term. Corporate furniture ("Inc", "Holdings") carries no identity. Industry
# nouns are worse than useless: "ON Semiconductor" reduces to "semiconductor",
# which matches every chip story published that day, and "Panel-Mount Regulators
# Market Forecast" duly came back tagged as company news for ON.
_NEWS_NAME_STOPWORDS = {
    # corporate suffixes and legal form
    "the", "and", "for", "inc", "ltd", "plc", "llc", "lp", "co", "corp", "company",
    "companies", "corporation", "incorporated", "holding", "holdings", "group",
    "groupe", "partners", "partnership", "trust", "fund", "funds", "class",
    "common", "ordinary", "shares", "share", "stock", "adr", "sa", "ag", "nv",
    "se", "spa", "ab", "oyj", "as", "bv",
    # geography and generic qualifiers
    "international", "global", "worldwide", "american", "america", "national",
    "united", "states", "usa", "european", "asia", "pacific", "atlantic",
    "northern", "southern", "eastern", "western", "north", "south", "east",
    "west", "new", "first", "second", "third", "general", "standard", "premier",
    "united", "allied", "associated", "consolidated", "continental",
    # industry nouns that describe a sector rather than a company
    "semiconductor", "semiconductors", "technologies", "technology", "systems",
    "solutions", "services", "service", "industries", "industrial", "enterprises",
    "pharmaceutical", "pharmaceuticals", "pharma", "biosciences", "bioscience",
    "biotech", "therapeutics", "health", "healthcare", "medical", "medicine",
    "bank", "bancorp", "bancshares", "banking", "financial", "finance",
    "capital", "investment", "investments", "insurance", "assurance",
    "energy", "resources", "resource", "mining", "minerals", "petroleum",
    "oil", "gas", "power", "electric", "utilities", "utility",
    "realty", "properties", "property", "estate", "communications",
    "media", "entertainment", "networks", "network", "digital", "data",
    "software", "hardware", "electronics", "materials", "chemical", "chemicals",
    "motors", "motor", "airlines", "airways", "air", "transport", "logistics",
    "foods", "food", "beverage", "beverages", "brands", "retail", "stores",
    "manufacturing", "products", "laboratories", "labs", "research",
    "development", "holdings", "management", "acquisition", "ventures",
}


def _news_is_relevant(title, ticker, clean_name):
    """True when the headline is plausibly about this company.

    Four ways to qualify, in descending order of confidence:

      1. The ticker in an explicit ticker context: $ON, (ON), NASDAQ: ON. This
         is unambiguous at any ticker length.
      2. A bare uppercase ticker token, but only at three characters or more.
         "IT spending to rise 8%" is not a story about Gartner, and 242 tickers
         in the registry are one or two characters.
      3. The full company name as a contiguous, CASE-SENSITIVE phrase, on
         word boundaries. Case is what separates "ON Semiconductor surges"
         from "mood high on semiconductor demand", and nothing else does.
         This is also the only rule that can match "3M" or "AT&T".
      4. Distinctive name tokens: two of them, or one of at least five
         characters. Generic industry nouns are stripped first.

    This is string matching, not comprehension, so it does not catch every
    case. A company's own arena keeps its naming rights ("Garth Brooks at
    Allstate Arena") and a company named after an animal keeps the animal.
    Callers should treat kept/considered as the filter's own confidence."""
    if not title:
        return False

    if ticker:
        esc = re.escape(ticker)
        if re.search(r"(?:\$" + esc + r"|\(" + esc + r"\)|:\s*" + esc + r")(?![A-Za-z0-9])", title):
            return True
        if len(ticker) >= 3 and re.search(
                r"(?<![A-Za-z0-9])" + esc + r"(?![A-Za-z0-9])", title):
            return True

    name = (clean_name or "").strip()
    if len(name) >= 2 and re.search(
            r"(?<![A-Za-z0-9])" + re.escape(name) + r"(?![A-Za-z0-9])", title):
        # Case-sensitive, and short names must qualify here rather than by
        # token: "3M" and "AT&T" tokenize to nothing usable, and a one-letter
        # ticker cannot be matched bare.
        return True

    raw_tokens = [t for t in re.split(r"[^A-Za-z]+", name.lower()) if len(t) > 2]
    tokens = [t for t in raw_tokens if t not in _NEWS_NAME_STOPWORDS]
    if not tokens and len(raw_tokens) >= 2:
        # Some companies are made entirely of furniture: "First National Bank".
        # Stripping generics leaves nothing, and returning False here would
        # reject the company's own earnings story. Falling back is only safe
        # with two or more tokens, because the two-match rule is what supplies
        # the discrimination. A lone generic token is the case this filter
        # exists to stop: "ON Semiconductor" reduces to "semiconductor" and
        # would otherwise match every chip story printed that day.
        tokens = raw_tokens
    if not tokens:
        return False

    low = title.lower()
    uniq = set(tokens)
    hits = sum(1 for t in uniq if re.search(r"\b" + re.escape(t) + r"\b", low))
    if len(uniq) >= 2:
        return hits >= 2
    return hits >= 1 and len(tokens[0]) >= 5


def fetch_company_news(ticker, name="", max_items=NEWS_MAX_ITEMS):
    """Pull recent news for a ticker from Google News RSS. Returns list of
    {title, source, link, ts (unix int)}.

    Returns None if the fetch itself failed, and [] if the fetch succeeded and
    the company genuinely has no recent coverage. The caller must not persist
    the first case: overwriting a good file with an empty one feeds a false zero
    into news_count_7d and therefore into the neglect score."""
    import urllib.parse as _up
    if not ticker:
        return []
    # Build query: bias toward financial coverage, include company name as fallback.
    clean_name = (name or "").strip()
    for suffix in [", Inc.", " Inc.", " Inc", ", Ltd.", " Ltd.", " Ltd",
                   " Corporation", " Corp.", " Corp", " Holdings", " Co.",
                   " Group", " Plc", " plc"]:
        clean_name = clean_name.replace(suffix, "")
    clean_name = clean_name.strip().rstrip(".")
    # A short ticker is not a search term. Three characters or fewer are almost
    # always an ordinary word somewhere, and OR-ing one against the company name
    # lets the word half dominate the result set: '"ALL" OR "Allstate"' came
    # back as highways, rugby and a balloon museum, with no Allstate at all. The
    # company name alone is the more specific half, so when the ticker is short
    # we drop it from the query rather than filtering its damage out afterwards.
    if clean_name and clean_name.upper() != ticker and len(clean_name) > 2:
        if len(ticker) <= 3:
            query = f'"{clean_name}"'
        else:
            query = f'"{ticker}" OR "{clean_name}"'
    else:
        query = f'"{ticker}" stock'
    url = f"https://news.google.com/rss/search?q={_up.quote(query)}&hl=en-US&gl=US&ceid=US:en"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Apterreon-IntelBrief/1.0"})
        with urllib.request.urlopen(req, timeout=12) as resp:
            xml_data = resp.read().decode("utf-8", errors="replace")
        root = ET.fromstring(xml_data)
    except Exception:
        return None
    items = []
    considered = 0
    for item in root.findall(".//item")[:max_items * 3]:
        title = (item.findtext("title") or "").strip()
        pub_date = (item.findtext("pubDate") or "").strip()
        link = (item.findtext("link") or "").strip()
        source = (item.findtext("source") or "").strip()
        if not title or not link:
            continue
        # Same rule as the site feeds: a company's news should be readable.
        if not _is_english(title, source):
            continue
        considered += 1
        if not _news_is_relevant(title, ticker, clean_name):
            continue
        parsed = parse_rss_date(pub_date)
        ts = int(parsed.timestamp()) if parsed else 0
        clean_title = title[:200]
        items.append({
            "title": clean_title,
            "source": source[:60],
            "link": link,
            "ts": ts,
            "lm": compute_lm_score(clean_title),
            "vader": compute_vader_score(clean_title),
        })
        if len(items) >= max_items:
            break
    if considered and not items:
        # Genuinely nothing on topic. That is a real zero, not a failed fetch,
        # and the caller is entitled to persist it.
        print(f"news: {ticker} kept 0/{considered} headlines (none on topic).")
    return items


# Windows reserves these device-name filenames in any directory. Renaming the
# JSON file with a "_" prefix avoids tripping git checkout on Windows hosts.
_WIN_RESERVED = {"CON", "PRN", "AUX", "NUL", "COM1", "COM2", "COM3", "COM4", "COM5",
                 "COM6", "COM7", "COM8", "COM9", "LPT1", "LPT2", "LPT3", "LPT4",
                 "LPT5", "LPT6", "LPT7", "LPT8", "LPT9"}


def _news_filename(ticker):
    """Return the on-disk filename for a ticker's news JSON. Prefixes with '_'
    when the ticker collides with a Windows reserved device name (e.g. CON)."""
    base = ticker.upper()
    if base in _WIN_RESERVED:
        return f"_{base}.json"
    return f"{base}.json"


def enrich_with_news(stocks, max_age_hours=12, max_workers=10):
    """For each stock, write news items to docs/news/{TICKER}.json. Skips tickers
    fetched within max_age_hours per state/news_fetch_log.json (so midday + evening
    workflow runs reuse morning's news without re-hitting Google). Returns count
    fetched."""
    if not stocks:
        return 0
    NEWS_DIR.mkdir(parents=True, exist_ok=True)
    fetch_log = _load_news_fetch_log()

    def needs_fetch(ticker):
        f = NEWS_DIR / _news_filename(ticker)
        if not f.exists():
            return True
        # Schema bump: if cached items lack the new sentiment fields, force refresh
        # regardless of age. Old files get upgraded on the next morning workflow run.
        try:
            cached = json.loads(f.read_text(encoding="utf-8"))
            if isinstance(cached, list) and cached and "vader" not in cached[0]:
                return True
        except Exception:
            return True
        age = _age_hours_from_iso(fetch_log.get(ticker.upper()))
        return age is None or age > max_age_hours

    todo = [s for s in stocks if needs_fetch(s["ticker"])]
    skipped = len(stocks) - len(todo)
    if not todo:
        print(f"news: all {len(stocks)} ticker files within {max_age_hours}h, skipping fetch.")
        return 0

    from concurrent.futures import ThreadPoolExecutor, as_completed

    def process(s):
        try:
            items = fetch_company_news(s["ticker"], s.get("name", ""))
            if items is None:
                # Leave whatever is on disk alone. A stale file is a better
                # answer than a file that says this company has no coverage.
                return s["ticker"], 0, True
            (NEWS_DIR / _news_filename(s["ticker"])).write_text(
                json.dumps(items, separators=(",", ":")), encoding="utf-8"
            )
            return s["ticker"], len(items), False
        except Exception:
            return s["ticker"], 0, True

    fetched = 0
    news_errors = 0
    empty_ok = 0
    t0 = time.time()
    stamp = datetime.now(timezone.utc).isoformat()
    news_budget_hit = False
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = [ex.submit(process, s) for s in todo]
        for f in as_completed(futures):
            # Same reasoning as the yfinance budget: Google throttles, the pass is
            # unbounded, and a job killed by the timeout commits nothing.
            if not news_budget_hit and time.time() - t0 > _NEWS_TIME_BUDGET_S:
                news_budget_hit = True
                for pending in futures:
                    pending.cancel()
            if f.cancelled():
                continue
            sym, n, failed = f.result()
            if failed:
                news_errors += 1
                continue
            if n > 0:
                fetched += 1
                # Stamped on the main thread as results arrive, so the manifest
                # needs no lock and only ever records genuine successes.
                fetch_log[sym.upper()] = stamp
            else:
                empty_ok += 1
    _save_news_fetch_log(fetch_log)
    elapsed = time.time() - t0
    print(f"news: wrote {fetched}/{len(todo)} ticker files in {elapsed:.1f}s "
          f"({skipped} cached < {max_age_hours}h, {empty_ok} genuinely no coverage, "
          f"{news_errors} fetch failures left untouched).")
    if news_errors and news_errors > len(todo) * 0.5:
        print(f"news: WARNING, {news_errors} of {len(todo)} fetches failed. "
              f"Treat news_count_7d and the neglect score as unreliable for this run.")
    if news_budget_hit:
        print(f"news: stopped at the {_NEWS_TIME_BUDGET_S}s budget. The untouched "
              f"tickers keep their existing files and are retried next run.")
    return fetched


# A composite of one is not a composite. Before this gate, 1,842 of 5,339
# tickers (34%) carried a neglect_score resting on a single component, and in
# every one of those cases the component was the same: news_count_7d. The number
# presented as a three-factor Lynch signal was, for a third of the universe,
# `1 - items_found / 20` and nothing else.
MIN_NEGLECT_COMPONENTS = 2


def compute_neglect_score(stocks):
    """Peter Lynch neglect signal: under-followed names tend to have asymmetric
    upside when something good happens, because Wall Street is not watching.
    Composite of up to three normalized 0-to-1 components, each scoring "more
    neglected" higher:
      - analyst coverage:  1 - min(analyst_count, 30) / 30
      - institutional %:   1 - min(inst_ownership, 0.50) / 0.50
      - news mentions 7d:  1 - min(news_count_7d, N) / N, N = NEWS_MAX_ITEMS
    Score in [0, 1]; >0.7 is genuinely off-the-radar, <0.3 is heavily covered.

    The news denominator has to be the fetch cap, not a round number. It was 20
    while fetch_company_news has always stopped at 15, so the component could
    not fall below 0.25 no matter how heavily covered a company was, and 858
    tickers sat pinned at the cap. A third of the scale was unreachable.

    An absent analyst_count is not missing data. Yahoo omits
    numberOfAnalystOpinions precisely when nobody publishes an estimate, which
    is the strongest neglect signal the composite can receive, and treating it
    as unknown discarded the signal exactly where it was loudest. It is read as
    zero coverage, but only when inst_ownership came back for the same ticker,
    which is the evidence that the yfinance pass actually reached this row
    rather than failing on it. Without that guard a throttled fetch would look
    identical to an uncovered micro cap.

    Two components minimum, because a single one is not a composite and the
    single one was always the same: news_count_7d is stamped on every ticker
    that has a news file, including the 447 whose file is empty, while
    analyst_count and inst_ownership come from Yahoo and are frequently absent.
    So a third of the universe was scored on our own search hit count alone.

    That matters most exactly where the score is loudest. A ticker we found no
    headlines for scores 1.0, maximally neglected, and zero is ambiguous: it can
    mean nobody covers the company, or it can mean the query did not match it.
    Four of the feeds in this project were returning nothing at all until
    recently and looked no different. With a second component present, a real
    zero is tempered by evidence from somewhere other than the same search.

    Coverage cost is 1,842 tickers, and they are not the large ones: median cap
    among those keeping a score is $1,431M against $1,382M for the universe."""
    scored = 0
    dropped = 0
    for s in stocks:
        parts = []
        yf_reached = isinstance(s.get("inst_ownership"), (int, float))
        if isinstance(s.get("analyst_count"), (int, float)):
            parts.append(1 - min(s["analyst_count"], 30) / 30)
        elif yf_reached:
            # No estimate published for a ticker the fetch did reach: zero
            # analysts, maximally neglected on this axis.
            parts.append(1.0)
        if yf_reached:
            parts.append(1 - min(s["inst_ownership"], 0.50) / 0.50)
        if isinstance(s.get("news_count_7d"), (int, float)):
            parts.append(1 - min(s["news_count_7d"], NEWS_MAX_ITEMS) / NEWS_MAX_ITEMS)
        if len(parts) >= MIN_NEGLECT_COMPONENTS:
            s["neglect_score"] = sum(parts) / len(parts)
            s["neglect_parts"] = len(parts)
            scored += 1
        else:
            # Clear rather than leave: on the cached path these dicts persist
            # between runs, and a score whose inputs have since gone missing
            # would otherwise sit there looking freshly computed.
            s.pop("neglect_score", None)
            s.pop("neglect_parts", None)
            if parts:
                dropped += 1
    print(f"neglect_score: computed for {scored} tickers; "
          f"{dropped} had only one component and were left unscored.")
    return scored


def aggregate_news_sentiment(stocks):
    """Read each ticker's news file and stamp aggregate LM/VADER scores onto the
    stock dict so the Overlays filters can run client-side without lazy-loading
    every ticker's news. Aggregates across the last 7 days (most actionable
    window). Cheap: filesystem only, no network."""
    if not stocks:
        return 0
    now_ts = time.time()
    week_secs = 7 * 24 * 3600
    stamped = 0
    for s in stocks:
        f = NEWS_DIR / _news_filename(s["ticker"])
        if not f.exists():
            continue
        try:
            items = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(items, list):
            continue
        recent = [i for i in items if isinstance(i, dict) and i.get("ts") and (now_ts - i["ts"]) < week_secs]
        # Recomputed from the stored title rather than read back from the file,
        # so the headlines already cached with lm 0.0 are corrected without
        # refetching any of them. The files themselves heal on their next
        # 12-hour refresh.
        lm_vals = [v for v in (compute_lm_score(i.get("title") or "") for i in recent)
                   if v is not None]
        vd_vals = [i["vader"] for i in recent if isinstance(i.get("vader"), (int, float))]
        if len(lm_vals) >= MIN_LM_HEADLINES:
            s["news_lm_avg"] = sum(lm_vals) / len(lm_vals)
        else:
            # Clear rather than leave: these dicts persist across runs on the
            # cached path, and a value whose headlines have since rolled out of
            # the window would sit there looking current.
            s.pop("news_lm_avg", None)
        if vd_vals:
            s["news_vader_avg"] = sum(vd_vals) / len(vd_vals)
        s["news_count_7d"] = len(recent)
        if lm_vals or vd_vals:
            stamped += 1
    print(f"news_sentiment: aggregated for {stamped} tickers.")
    return stamped


# ── Per-ticker price history (yfinance bulk download, 1y daily) ─────────────

PRICES_DIR = DOCS_DIR / "prices"


# ── Provenance: how every derived number is produced ────────────────────────
#
# One entry per computed field, and the only place methodology is written down.
# The screener's methodology panel and the README's provenance table are both
# generated from this, so a formula and its description cannot drift apart.
#
# They had drifted. return_1m was labelled "Last 30 calendar days price change"
# while computing distance from the 50-day moving average, and return_12_2 cited
# Jegadeesh-Titman while subtracting one return from another. Both descriptions
# lived loose in the JavaScript with nothing tying them to the code.

FIELD_SOURCES = {
    "price_history": "Daily closes and volumes, downloaded in bulk from Yahoo and stored per ticker",
    "market_series": "13-week Treasury bill (^IRX), 10-year Treasury yield (^TNX) and "
                     "S&P 500 (^GSPC), stored once per run",
    "edgar": "SEC EDGAR XBRL company facts",
    "form4": "SEC EDGAR Form 4 filings",
    "yfinance": "Yahoo Finance quote summary, one request per ticker",
    "news": "Google News RSS, one search per ticker",
    "index": "Wikipedia index constituent tables and the NASDAQ Trader directory",
    "benchmark_series": "Russell ETF daily closes (IWY, IWL, IWX, IWP, IWR, IWS, IWO, IWM, IWN, "
                        "IWF, IWD, IWV) and the eleven SPDR sector funds (XLK, XLC, XLE, XLB, "
                        "XLU, XLF, XLRE, XLV, XLY, XLP, XLI) from Yahoo, not adjusted for "
                        "dividends, stored once per run",
    "analyst_ledger": "The analyst's calls in theses/ledger/events.csv and the notes they name, "
                      "priced at the stored daily closes",
    "fundamentals_panel": "The daily fundamentals panel in data/fundamentals",
    "portfolio_ledger": "The model portfolios' trades in portfolio/ledger, valued at the stored daily closes",
}

# How often the underlying input can actually change. A number is not stale
# because time has passed; it is stale when its input has moved and we have not.
# A quarterly figure that is 70 days old is exactly as current as it can be.
REFRESH_CLASSES = {
    "daily": "Changes every trading day",
    "12h": "Refreshed twice a day",
    "weekly": "Swept once a week",
    "quarterly": "Changes only when the company files",
    "static": "Rarely changes; carried forward until it does",
}

# Why a value is missing or older than today. Stamped per field, only when there
# is something to say, so the payload stays sparse.
FIELD_STATUS = {
    "awaiting_filing": "Waiting on the next quarterly report. This is as current as the filings allow.",
    "no_coverage": "The source has nothing for this company.",
    "insufficient_history": "Not enough observations to compute this honestly.",
    "cohort_too_small": "Too few sector peers to rank against.",
    "deferred_budget": "The fetch pass ran out of time this run and will reach it next run.",
    "not_meaningful": "The inputs make this arithmetic meaningless, such as a multiple on negative earnings.",
    "vendor_value": "Taken from the market-data vendor, because the filings carry no earnings "
                    "per share we can use for this company. Not built from the same inputs "
                    "as the filing-derived figures beside it.",
    "source_error": "The source was reachable but the fetch or parse failed.",
    # Stamped by apply_security_types on every field it blanks. Distinct from
    # not_meaningful, which is about arithmetic on a real company's inputs.
    "not_applicable": "Does not apply to this kind of security. A note, a fund or a "
                      "blank-check shell has no business of its own, and any figure "
                      "here would describe its issuer or its placeholder instead.",
    # Stamped by derive_from_price_history on change_pct when the stored series
    # skips a session, which is when change_gap is set.
    "gap": "The previous session's close is missing from the stored prices, so the "
           "change on the day cannot be worked out. It is left blank rather than "
           "estimated.",
}

FIELD_METHODS = {
    "price": {
        "label": "Price", "units": "USD", "source": "price_history",
        "refresh": "daily", "asof": "prices_updated",
        "formula": "closes[-1]",
        "note": "The most recent daily close, not a live or intraday quote. The "
                "daily run happens after the US close, so the close of the "
                "session just ended is the current price.",
    },
    "price_date": {
        "label": "Price Date", "units": "date", "source": "price_history",
        "refresh": "daily", "asof": "prices_updated",
        "formula": "dates[-1]",
        "note": "The session the stored close belongs to. prices_updated is when "
                "the series was last requested, which is not the same thing: a "
                "download late on 2026-09-21 returned series ending on the Friday "
                "and was stamped fresh. Blank on panel rows written before "
                "the column existed, when it was not recorded.",
    },
    "price_stale": {
        "label": "Stale Price Flag", "units": "flag", "source": "price_history",
        "refresh": "daily", "asof": "prices_updated",
        "formula": "1 if price_date < panel date else blank",
        "note": "Set on a panel row whose stored close is from an older session "
                "than the row's date. Every field computed from the price series "
                "is then left blank on that row rather than recorded under the "
                "wrong session. Also back-filled on 2026-09-10, 09-11, 09-14 and "
                "09-21, where the row's price exactly matches an earlier "
                "session's close and not its own; those older rows keep their "
                "original values, so filter on this column before using them.",
    },
    "change_pct": {
        "label": "1-Day Move", "units": "percent", "source": "price_history",
        "refresh": "daily", "asof": "prices_updated",
        "formula": "(closes[-1] / closes[-2] - 1) * 100",
        "note": "Close-to-close, one session. Stored in percent, not as a "
                "fraction, which is why it is the one percentage field not "
                "scaled by 100 for display. Blank, with change_gap set, when "
                "the stored series skips a session between its last two closes.",
    },
    "change_gap": {
        "label": "Missing Session Flag", "units": "flag", "source": "price_history",
        "refresh": "daily", "asof": "prices_updated",
        "formula": "1 if a session falls between dates[-2] and dates[-1] else blank",
        "note": "Set when the stored series has no close for the session before "
                "its last one, so change_pct is left blank rather than computed "
                "across two sessions. A session is a date the benchmark or at "
                "least 0.5% of stored series have a bar for. Also back-filled on "
                "2026-09-23, where the download had dropped 2026-09-22 and "
                "change_pct was recorded as a two-day move; those rows keep "
                "their original values, so filter on this column before using "
                "change_pct on that date.",
    },
    "return_1m": {
        "label": "1-Month Return", "units": "fraction", "source": "price_history",
        "refresh": "daily", "asof": "prices_updated",
        "formula": "closes[-1] / closes[-22] - 1",
        "note": "The price change over the last 21 trading days, which is one calendar month of "
                "trading.",
    },
    "return_12_2": {
        "label": "12-2 Month Return", "units": "fraction", "source": "price_history",
        "refresh": "daily", "asof": "prices_updated",
        "formula": "closes[-22] / closes[-253] - 1",
        "note": "The price change over the twelve months ending one month ago, the momentum measure "
                "from Jegadeesh and Titman's research. The latest month is left out on purpose, because "
                "that is where short-term moves tend to reverse. Needs at least 200 trading days of "
                "prices.",
    },
    "return_52w": {
        "label": "52-Week Return", "units": "fraction", "source": "price_history",
        "refresh": "daily", "asof": "prices_updated",
        "formula": "closes[-1] / closes[-253] - 1",
        "note": "The price change over the year of prices we hold.",
    },
    "high52w_proximity": {
        "label": "52-Week High Proximity", "units": "fraction", "source": "price_history",
        "refresh": "daily", "asof": "prices_updated",
        "formula": "closes[-1] / max(closes) - 1",
        "note": "How far the price sits below its highest close of the past year, as a negative "
                "percentage; 0 means it is at the high. It uses closing prices, so it comes out "
                "slightly higher than a version using intraday highs.",
    },
    "rel_strength_sp500": {
        "label": "Relative Strength vs S&P 500", "units": "fraction",
        "source": "price_history", "refresh": "daily", "asof": "prices_updated",
        "formula": "return_52w(stock) - return_52w(^GSPC)",
        "note": "The stock's 52-week return minus the S&P 500's over the same trading days, the usual "
                "way to build it. It is a difference, not a ratio and not a regression; beta is the "
                "regression.",
    },
    "volume": {
        "label": "Volume", "units": "shares", "source": "price_history",
        "refresh": "daily", "asof": "prices_updated",
        "formula": "volumes[-1]",
        "note": "Shares traded on the latest trading day.",
    },
    "volume_trend": {
        "label": "Volume Trend", "units": "fraction", "source": "price_history",
        "refresh": "daily", "asof": "prices_updated",
        "formula": "mean(volumes[-10:]) / mean(volumes[-63:]) - 1",
        "note": "Average daily volume over the last 10 trading days against the average over the last "
                "three months. Positive means trading has picked up. It matches the ratio of Yahoo's "
                "own 10-day and three-month average volumes.",
    },
    "volatility_1y": {
        "label": "Volatility (1y)", "units": "fraction", "source": "price_history",
        "refresh": "daily", "asof": "prices_updated",
        "formula": "stdev(daily returns) * sqrt(252)",
        "note": "How much the price swings: the standard deviation of simple daily returns over the "
                "year of prices we hold (the sample version), scaled up to a yearly figure.",
    },
    "beta_1y": {
        "label": "Beta vs S&P 500", "units": "ratio", "source": "market_series",
        "refresh": "daily", "asof": "prices_updated",
        "formula": "cov(r_stock, r_index) / var(r_index)",
        "note": "How much the stock tends to move when the S&P 500 moves: the slope of a least-squares "
                "line through daily returns, on the days both traded. 1.00 moves in step with the "
                "index.",
    },
    "sharpe_1y": {
        "label": "Sharpe (1y)", "units": "ratio", "source": "market_series",
        "refresh": "daily", "asof": "prices_updated",
        "formula": "mean(r - rf) / stdev(r - rf) * sqrt(252)",
        "note": "Return for the risk taken: the average daily return above the 13-week Treasury bill "
                "rate, divided by how much that return varies, scaled up to a yearly figure. Left blank "
                "when the Treasury rate is missing, rather than assuming a zero rate, which would "
                "inflate every Sharpe ratio by roughly the level of short-term interest rates.",
    },
    "max_drawdown_1y": {
        "label": "Max Drawdown (1y)", "units": "fraction", "source": "price_history",
        "refresh": "daily", "asof": "prices_updated",
        "formula": "min(close / running_max(close) - 1)",
        "note": "The largest fall from a peak to a later low over the year of closing prices we hold, "
                "as a negative percentage.",
    },
    "market_cap": {
        "label": "Market Cap", "units": "USD", "source": "edgar",
        "refresh": "daily", "asof": "prices_updated",
        "formula": "price * shares_outstanding",
        "note": "Shares outstanding, as stated on the cover of the latest filing, times the latest "
                "close. Not the average share count for a period, which describes a period rather than "
                "a moment and understates a company in the middle of a buyback. It matches the data "
                "vendor's figure to 0.0% for the companies checked.",
    },
    "pe": {
        "label": "P/E (Trailing)", "units": "ratio", "source": "edgar",
        "refresh": "quarterly", "asof": "fiscal_period_end",
        "formula": "price / sum(last 4 quarters of diluted EPS)",
        "note": "Price divided by diluted earnings per share (EPS) over the last four quarters. "
                "Diluted, not basic, because that is the share count an outside shareholder is actually "
                "diluted by. Left blank when those earnings are zero or negative, or when there is no "
                "usable EPS and the reported net income is zero or negative. The four quarters must "
                "make up one year on one share basis, so a sum that crosses a stock split is left blank "
                "rather than published. A company that files no quarterly figures uses its latest "
                "fiscal year, if that year ended within 15 months (see eps_basis). Where no filed EPS "
                "can be used, the data vendor's trailing P/E is shown instead, marked as from the data "
                "vendor (status vendor_value); the same applies to a depositary share, whose filed EPS "
                "is per ordinary share rather than per depositary share.",
    },
    "eps_basis": {
        "label": "EPS Basis", "units": "text", "source": "edgar",
        "refresh": "quarterly", "asof": "fiscal_period_end",
        "formula": "ttm, annual or basic",
        "note": "Which earnings per share ttm_eps_diluted, and so pe, is built "
                "on. ttm is four quarters of diluted EPS. annual is the latest "
                "fiscal year's diluted EPS, for a filer with no quarterly "
                "figures (a 20-F or 40-F filer), used only while that year ended "
                "within 15 months. basic means no diluted figure is filed and "
                "basic EPS stands in, which overstates EPS where there is real "
                "dilution. Blank when there is no filing EPS.",
    },
    "price_book": {
        "label": "Price/Book", "units": "ratio", "source": "edgar",
        "refresh": "quarterly", "asof": "fiscal_period_end",
        "formula": "market_cap / stockholders_equity",
        "note": "Market cap divided by the parent company's shareholders' equity. The version that "
                "includes minority (noncontrolling) interests is not used, because it counts equity "
                "that common shareholders have no claim on.",
    },
    "roe_ttm": {
        "label": "ROE (TTM)", "units": "fraction", "source": "edgar",
        "refresh": "quarterly", "asof": "fiscal_period_end",
        "formula": "ttm_net_income / mean(equity_now, equity_a_year_ago)",
        "note": "Net income over the last four quarters divided by the average of shareholders' equity "
                "now and a year ago, not the year-end balance, because equity changes through the year.",
    },
    "gross_margin": {
        "label": "Gross Margin", "units": "fraction", "source": "edgar",
        "refresh": "quarterly", "asof": "fiscal_period_end",
        "formula": "ttm_gross_profit / ttm_revenue",
        "note": "Gross profit divided by revenue, both over the same last four quarters.",
    },
    "operating_margin": {
        "label": "Operating Margin", "units": "fraction", "source": "edgar",
        "refresh": "quarterly", "asof": "fiscal_period_end",
        "formula": "ttm_operating_income / ttm_revenue",
        "note": "Operating income divided by revenue, both over the same last four quarters.",
    },
    "fcf_yield": {
        "label": "FCF Yield", "units": "fraction", "source": "edgar",
        "refresh": "quarterly", "asof": "fiscal_period_end",
        "formula": "(ttm_operating_cash_flow - ttm_capex) / market_cap",
        "note": "Free cash flow (cash from operations minus capital spending, over the last four "
                "quarters) divided by market cap. Capital spending is shown as a positive outflow in "
                "the cash flow statement, so its size is subtracted. This deliberately differs from the "
                "data vendor's free cash flow, which implies about $16bn for Microsoft against roughly "
                "$70bn of actual free cash flow; ours is rebuilt from the filed statements.",
    },
    "revenue_growth_yoy": {
        "label": "Revenue Growth YoY", "units": "fraction", "source": "edgar",
        "refresh": "quarterly", "asof": "fiscal_period_end",
        "formula": "ttm_revenue / prior_ttm_revenue - 1",
        "note": "Revenue over the last four quarters against the four quarters before them, the "
                "smoother and more usual measure for a screen. The data vendor's revenue growth "
                "compares a single quarter with the same quarter a year earlier, so the two agree only "
                "when growth is steady.",
    },
    "eps_growth_yoy": {
        "label": "EPS Growth YoY", "units": "fraction", "source": "edgar",
        "refresh": "quarterly", "asof": "fiscal_period_end",
        "formula": "ttm_diluted_eps / prior_ttm_diluted_eps - 1",
        "note": "Diluted earnings per share over the last four quarters against the four quarters "
                "before them. It differs from the data vendor's figure in the same way as revenue "
                "growth: theirs compares a single quarter, so it can even have the opposite sign.",
    },
    "sector": {
        "label": "Sector", "units": "text", "source": "yfinance",
        "refresh": "static", "asof": "last_updated",
        "formula": "normalize_sector(yahoo.sector)",
        "note": "Yahoo's own eleven-sector taxonomy, mapped onto the GICS sector "
                "NAMES. It is not licensed GICS, which is a commercial product of "
                "S&P Dow Jones Indices and MSCI and is not publicly available. "
                "The names match; the classifications are Yahoo's.",
    },
    "security_type": {
        "label": "Security Type", "units": "text", "source": "index",
        "refresh": "static", "asof": "last_updated",
        "formula": "security_type.classify_row(name, index, sub_industry, EDGAR footprint)",
        "note": "What the listing is: operating, lp, bdc, royalty_trust, spac, "
                "debt, structured, equity_units or cef. Read from the exchange "
                "security name, then corrected from data: Yahoo's Shell "
                "Companies industry marks a blank-check shell, an Asset "
                "Management filer with net income but no revenue line is a BDC, "
                "and one with no EDGAR filings at all is a fund. Real reported "
                "revenue turns a shell, fund or BDC label back into operating. "
                "S&P 1500 constituents are always operating. The last five "
                "are not businesses: they are left out of sector peer groups "
                "and scoring, and issuer numbers they would otherwise inherit "
                "are withheld as not applicable. Recomputed every run, because "
                "a SPAC becomes a company when its merger closes.",
    },
    # The model portfolios (portfolio/engine.py). Computed from the panel and the
    # ledger for the Portfolios pages, not stored on panel rows.
    "style_size": {
        "label": "Size", "units": "text", "source": "fundamentals_panel",
        "refresh": "daily", "asof": "date",
        "formula": "rank by market_cap: 1 to 200 large, 201 to 1000 mid, 1001 to 3000 small, else micro",
        "note": "Operating companies ranked by market cap, as the Russell indexes are: the Top "
                "200, the Midcap (201 to 1,000) and the 2000 (1,001 to 3,000). Depositary shares "
                "and annual-only foreign filers (20-F, 40-F) are left out; foreign issuers filing "
                "IFRS statements carry no marker in the panel and stay in. Share classes of one "
                "company reporting the same market cap count once.",
    },
    "sales_ps_growth_3y": {
        "label": "Sales per share growth (3y)", "units": "fraction", "source": "edgar",
        "refresh": "quarterly", "asof": "fiscal_period_end",
        "formula": "((revenue / diluted_shares)[FY] / (revenue / diluted_shares)[FY-3]) ** (1/3) - 1",
        "note": "From annual 10-K facts only, each year's revenue over the same year's weighted "
                "diluted share count. Blank across a change in the share count of 1.8 times or "
                "more between consecutive years (a split or a merger), and when either revenue "
                "is not positive. Stored in data/financials/style_history.csv.",
    },
    "eps_growth_3y": {
        "label": "EPS growth (3y)", "units": "fraction", "source": "edgar",
        "refresh": "quarterly", "asof": "fiscal_period_end",
        "formula": "((net_income / diluted_shares)[FY] / (net_income / diluted_shares)[FY-3]) ** (1/3) - 1",
        "note": "EPS is rebuilt as net income over the weighted diluted share count of the same "
                "year, never from per-share figures, so a split cannot distort it. Blank when "
                "either year's earnings were zero or negative, since a growth rate from a loss "
                "is not defined.",
    },
    "style_value_score": {
        "label": "Value score", "units": "ratio", "source": "fundamentals_panel",
        "refresh": "daily", "asof": "date",
        "formula": "mean(z(1 / price_book), z(1 / pe), z(revenue / market_cap), "
                   "z(operating_cash_flow / market_cap)), at least 2 of 4",
        "note": "Robust z-scores within the company's size and sector (size alone when the sector "
                "has fewer than 10 companies). Earnings yield is diluted EPS over price where P/E "
                "is blank. Revenue is the trailing four quarters; operating cash flow is the "
                "latest fiscal year from style_history.csv.",
    },
    "style_growth_score": {
        "label": "Growth score", "units": "ratio", "source": "edgar",
        "refresh": "quarterly", "asof": "fiscal_period_end",
        "formula": "mean(z(sales_ps_growth_3y), z(eps_growth_3y)), at least 1 of 2",
        "note": "Robust z-scores within the company's size and sector (size alone when the sector "
                "has fewer than 10 companies), so a sector-wide boom does not read as growth. No "
                "price momentum.",
    },
    "style_quality_score": {
        "label": "Quality score", "units": "ratio", "source": "fundamentals_panel",
        "refresh": "daily", "asof": "date",
        "formula": "mean(z(roe_ttm), z(earnings_consistency), -z(net_debt_ebitda), "
                   "-z(op_margin_stability), -z(accruals_ratio)), at least 2 of 5",
        "note": "Robust z-scores within the company's size. Used only to rank companies inside a "
                "style box; a company with fewer than 2 inputs counts as average.",
    },
    "style_box": {
        "label": "Style box", "units": "text", "source": "fundamentals_panel",
        "refresh": "daily", "asof": "date",
        "formula": "size + (growth if growth_score - value_score is above the size median, else value)",
        "note": "Every company with a score is in exactly one box; ties at the median go to value. "
                "A company without a three-year annual history is not placed.",
    },
    "book_nav": {
        "label": "Net asset value", "units": "USD", "source": "portfolio_ledger",
        "refresh": "daily", "asof": "date",
        "formula": "cash + sum(shares * stored close on that date)",
        "note": "A holding with no stored close for the date is not valued from any other day: "
                "the day is marked partial and its value is left blank. Price-only: dividends "
                "are not counted. Every trade pays 5 basis points of its value.",
    },
    "book_exposure": {
        "label": "Gross and net exposure", "units": "fraction", "source": "portfolio_ledger",
        "refresh": "daily", "asof": "date",
        "formula": "gross = (long_value + |short_value|) / nav; net = (long_value - |short_value|) / nav",
        "note": "A short position is valued as a negative holding and its sale proceeds sit in cash.",
    },
    "cash_return": {
        "label": "Cash return since inception", "units": "fraction", "source": "market_series",
        "refresh": "daily", "asof": "date",
        "formula": "product(1 + irx(session) / 100 / 252) - 1 over sessions after inception",
        "note": "What the book's starting cash would have earned in 13-week Treasury bills. Blank "
                "when any session's rate is not stored.",
    },
    "book_return": {
        "label": "Return since inception", "units": "fraction", "source": "portfolio_ledger",
        "refresh": "daily", "asof": "date",
        "formula": "book_nav / capital - 1",
        "note": "Price-only, after trading costs.",
    },
    "benchmark_return": {
        "label": "Benchmark return since inception", "units": "fraction",
        "source": "benchmark_series", "refresh": "daily", "asof": "date",
        "formula": "close(date) / close(inception) - 1",
        "note": "From the ETF's daily closes, not adjusted for dividends, so it is price-only like "
                "the books. Blank when either close is not stored.",
    },
    # Scoring and attribution (theses/bin/score.py, portfolio/bin/score_pm.py), for
    # the Scorecard page and each book's page. Written to data/scoring and
    # data/portfolio by the daily run, never onto panel rows.
    "call_excess_return": {
        "label": "Excess return of a call", "units": "fraction", "source": "analyst_ledger",
        "refresh": "daily", "asof": "date",
        "formula": "sign * ((close(end) / close(start) - 1) - (fund(end) / fund(start) - 1)); "
                   "sign +1 long, -1 short, avoid or exit",
        "note": "The stock's price return minus its SPDR sector fund's over the same dates, turned "
                "round for a short, an avoid or an exit. An avoid is right when the stock lags its "
                "sector; it is never scored as a short. Both returns use stored closes for the exact "
                "dates, and a missing close leaves it blank. Marked every session, final only at "
                "the horizon.",
    },
    "call_hit_rate": {
        "label": "Hit rate of calls", "units": "fraction", "source": "analyst_ledger",
        "refresh": "daily", "asof": "date",
        "formula": "count(call_excess_return > 0) / count(scored calls)",
        "note": "Only calls scored against a sector fund count. A group with fewer than 10 scored "
                "calls is shown with its count and labelled too few to read.",
    },
    "call_brier": {
        "label": "Scenario calibration (Brier score)", "units": "ratio", "source": "analyst_ledger",
        "refresh": "daily", "asof": "date",
        "formula": "sum over bull, base, bear of (probability - (1 if landed else 0)) ** 2",
        "note": "A call lands on the case whose return from the note's entry price is nearest the "
                "stock's actual return at the horizon. 0 is perfect and 2 the worst; giving each "
                "case one chance in three scores 0.667 whatever happens.",
    },
    "pm_value_added": {
        "label": "PM value added", "units": "fraction", "source": "portfolio_ledger",
        "refresh": "daily", "asof": "date",
        "formula": "book_nav / capital - shadow_nav / capital",
        "note": "The shadow book holds the rules candidate book, bought as the real book was on its "
                "first day and rebalanced at the stored close on every date the real book trades, "
                "paying the same 5 basis points. Style books only.",
    },
    "attribution_allocation": {
        "label": "Allocation", "units": "fraction", "source": "portfolio_ledger",
        "refresh": "daily", "asof": "date",
        "formula": "sum over sectors of (w_s - W_s) * (R_s - R_b)",
        "note": "Brinson-Fachler, daily, linked over time by Carino's method. W and R come from a "
                "proxy benchmark we can see inside: the book's own box weighted by market value, or "
                "for the hedge and free-hand books every operating company weighted by market value.",
    },
    "attribution_selection": {
        "label": "Selection", "units": "fraction", "source": "portfolio_ledger",
        "refresh": "daily", "asof": "date",
        "formula": "sum over sectors of W_s * (r_s - R_s); interaction (w_s - W_s) * (r_s - R_s) apart",
        "note": "For a book that is short some names the interaction is included in selection, "
                "because a sector's net weight can be near zero. Allocation, selection, interaction, "
                "cash drag and trading costs add up exactly to the return against the proxy.",
    },
    "cash_drag": {
        "label": "Cash drag", "units": "fraction", "source": "portfolio_ledger",
        "refresh": "daily", "asof": "date",
        "formula": "cash / nav * (0 - R_b)",
        "note": "Cash in the ledger earns nothing, so this is what holding it cost, or saved, against "
                "the proxy benchmark.",
    },
    "proxy_error": {
        "label": "Proxy error", "units": "fraction", "source": "benchmark_series",
        "refresh": "daily", "asof": "date",
        "formula": "product(1 + R_b) - product(1 + fund return) over sessions with both",
        "note": "How far the proxy benchmark's return was from the fund the book is measured against. "
                "Large when the fund holds companies our panel does not place in the box.",
    },
    "book_beta": {
        "label": "Beta to the S&P 500", "units": "ratio", "source": "portfolio_ledger",
        "refresh": "daily", "asof": "date",
        "formula": "cov(r_book, r_^GSPC) / var(r_^GSPC) over at least 60 paired sessions",
        "note": "Hedge and free-hand books only. Beta-adjusted excess is the book's compounded return "
                "minus beta times the S&P 500's over the same sessions.",
    },
    "sizing_effect": {
        "label": "Sizing effect", "units": "fraction", "source": "portfolio_ledger",
        "refresh": "daily", "asof": "date",
        "formula": "product(1 + r_book) - product(1 + r_book - sum_i (w_i - sign_i * gross / N) * r_i)",
        "note": "The book against equal weights of the same holdings on the same sides, day by day.",
    },
}


# Trading days, not calendar days, because that is what the series holds.
_RET_1M_DAYS = 21
_RET_12M_DAYS = 252
# 12-2 needs most of a year behind it to mean anything. A ticker that listed
# four months ago gets no value, rather than a number computed over four months
# and labelled twelve.
_RET_12M_MIN_DAYS = 200


def _benchmark_levels():
    """The S&P 500 close on each date, for matched relative-strength windows."""
    try:
        data = json.loads((PRICES_DIR / MARKET_FILE).read_text(encoding="utf-8"))
    except Exception:
        return {}
    out = {}
    for row in data.get("benchmark") or []:
        try:
            out[str(row[0])] = float(row[1])
        except Exception:
            continue
    return out


def _nearest_on_or_before(levels_sorted, levels, day):
    """Value on `day`, or the last one before it. Calendars disagree on
    holidays, so an exact-match-only lookup silently drops comparisons."""
    if day in levels:
        return levels[day]
    lo, hi = 0, len(levels_sorted)
    while lo < hi:
        mid = (lo + hi) // 2
        if levels_sorted[mid] <= day:
            lo = mid + 1
        else:
            hi = mid
    return levels[levels_sorted[lo - 1]] if lo else None


# A date counts as a session when the benchmark has a bar on it or at least this
# share of stored series do. Exchange holidays are not in any calendar here
# (_last_expected_session), and on every holiday in the published year no series
# had a bar at all, while the day Yahoo dropped (2026-09-22) was still in 385 of
# 5,497 files. The floor stops one mis-dated bar from turning a holiday into a
# session, which would blank the next day's change for the whole universe.
_SESSION_MIN_SHARE = 0.005


def _session_calendar(date_lists, bench_dates=()):
    """The set of dates the market traded, as far as the stored series show.

    Pure, so it is testable. `date_lists` is one list of bar dates per series.
    A weekday that no series has a bar for (a holiday) is not a session. The
    evidence is the data itself, the same test _panel_gate applies to decide a
    day was a holiday."""
    counts = collections.Counter()
    n = 0
    for dates in date_lists:
        n += 1
        counts.update(set(dates))
    floor = max(1, math.ceil(n * _SESSION_MIN_SHARE))
    out = {d for d, c in counts.items() if c >= floor}
    out.update(bench_dates)
    return out


def _follows_previous_session(prev_date, date, sessions):
    """True when prev_date is the session immediately before date: no session in
    `sessions` falls strictly between them. Dates are YYYY-MM-DD strings."""
    return not any(prev_date < s < date for s in sessions)


def derive_from_price_history(stocks):
    """Everything the stored daily series can answer, without a network call.

    enrich_with_prices downloads a year of closes and volumes per ticker in
    bulk, one request per ~200 tickers. Yahoo's per-ticker quote summary, which
    is one request each and the slowest pass in the run, was answering several
    questions the bulk data already contains. Where both can answer, this wins:
    it is complete for every ticker with a stored series rather than for
    whichever ones a budgeted pass happened to reach.

    Sets price, price_date, change_pct, return_1m, return_12_2, return_52w,
    high52w_proximity, rel_strength_sp500, volume and volume_trend. Every
    formula is in FIELD_METHODS, which is what the methodology panel reads.

    return_1m was previously (price - fiftyDayAverage) / fiftyDayAverage: the
    distance from the 50-day moving average, published as "Last 30 calendar days
    price change". Those disagree most exactly where momentum matters, and 21%
    of tickers had the opposite sign."""
    if not stocks:
        return 0
    # A series whose last close is old would make a month-old price the current
    # one. Ten days covers a holiday week plus one missed refresh.
    cutoff = (datetime.now(timezone.utc).date() - timedelta(days=10)).isoformat()
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    levels = _benchmark_levels()
    levels_sorted = sorted(levels)

    # Every series first, because whether two bars are consecutive sessions is
    # a question about the market, which one series cannot answer.
    blobs = []
    for s in stocks:
        try:
            blobs.append(json.loads((PRICES_DIR / _news_filename(s["ticker"]))
                                    .read_text(encoding="utf-8")))
        except Exception:
            blobs.append(None)
    sessions = _session_calendar(
        ([str(c[0]) for c in (b.get("closes") or []) if isinstance(c, list) and c]
         for b in blobs if isinstance(b, dict)),
        levels_sorted)

    counts = collections.Counter()
    missing = short = stale = gaps = 0
    for s, blob in zip(stocks, blobs):
        status = s.get("status") or {}
        try:
            closes = blob.get("closes") or []
        except Exception:
            missing += 1
            for f in ("price", "change_pct", "return_1m", "return_12_2", "return_52w",
                      "high52w_proximity", "rel_strength_sp500", "volume", "volume_trend"):
                status[f] = "no_coverage"
            s["status"] = status
            continue
        if len(closes) < 2:
            short += 1
            continue
        if str(closes[-1][0]) < cutoff:
            stale += 1
            status["price"] = "source_error"
            s["status"] = status
            continue
        try:
            dates = [str(d) for d, _ in closes]
            px = [float(p) for _, p in closes]
        except (TypeError, ValueError, IndexError):
            missing += 1
            continue
        if any(p <= 0 for p in px):
            missing += 1
            continue

        s["price"] = px[-1]
        s["prices_updated"] = stamp
        # The session this close belongs to. prices_updated says when we asked,
        # which on 2026-09-21 was 20:34 ET with a series ending on the Friday, so
        # a stale close read as fresh. Nothing downstream could tell. The panel
        # compares this date with the row's own session (_panel_rows_for_session).
        s["price_date"] = dates[-1]
        counts["price"] += 1

        # A day's change only from two consecutive sessions. On 2026-09-23 most
        # series came back without 09-22, and closes[-2] was the 09-21 close, so
        # a two-day move was published as one day's. When a session is missing
        # the change is left blank and flagged: never estimated.
        s.pop("change_gap", None)
        if not _follows_previous_session(dates[-2], dates[-1], sessions):
            s.pop("change_pct", None)
            s["change_gap"] = 1
            status["change_pct"] = "gap"
            s["status"] = status
            gaps += 1
        elif px[-2] > 0:
            # Percent, not a fraction. This is the one percentage field stored
            # in percent, which is why it is absent from the client's PCT_FIELDS.
            chg = (px[-1] / px[-2] - 1) * 100
            if abs(chg) <= 40:
                s["change_pct"] = chg
                counts["change_pct"] += 1

        month_ago = px[-1 - _RET_1M_DAYS] if len(px) > _RET_1M_DAYS else None
        if month_ago and month_ago > 0:
            r1 = px[-1] / month_ago - 1
            if abs(r1) < 2:
                s["return_1m"] = r1
                counts["return_1m"] += 1
        elif len(px) <= _RET_1M_DAYS:
            status["return_1m"] = "insufficient_history"

        anchor_i = max(0, len(px) - 1 - _RET_12M_DAYS)
        long_enough = len(px) - anchor_i >= _RET_12M_MIN_DAYS
        anchor = px[anchor_i]
        if long_enough and anchor > 0:
            r52 = px[-1] / anchor - 1
            if abs(r52) < 10:
                s["return_52w"] = r52
                counts["return_52w"] += 1
            if month_ago and month_ago > 0:
                r122 = month_ago / anchor - 1
                if abs(r122) < 10:
                    s["return_12_2"] = r122
                    counts["return_12_2"] += 1
            # Matched window: the benchmark measured between the same two dates,
            # not over its own separate year.
            if levels_sorted:
                b0 = _nearest_on_or_before(levels_sorted, levels, dates[anchor_i])
                b1 = _nearest_on_or_before(levels_sorted, levels, dates[-1])
                if b0 and b1 and b0 > 0 and "return_52w" in s:
                    rel = s["return_52w"] - (b1 / b0 - 1)
                    if abs(rel) < 5:
                        s["rel_strength_sp500"] = rel
                        counts["rel_strength_sp500"] += 1
            else:
                status["rel_strength_sp500"] = "no_coverage"
        else:
            for f in ("return_52w", "return_12_2", "rel_strength_sp500"):
                status[f] = "insufficient_history"

        peak = max(px)
        if peak > 0:
            s["high52w_proximity"] = px[-1] / peak - 1
            counts["high52w_proximity"] += 1

        vols = blob.get("volumes")
        if isinstance(vols, list) and len(vols) == len(px):
            recent = [v for v in vols[-10:] if isinstance(v, (int, float))]
            base = [v for v in vols[-63:] if isinstance(v, (int, float))]
            last = vols[-1]
            if isinstance(last, (int, float)) and last > 0:
                s["volume"] = last
                counts["volume"] += 1
            if len(recent) >= 5 and len(base) >= 30:
                mb = sum(base) / len(base)
                if mb > 0:
                    vt = (sum(recent) / len(recent)) / mb - 1
                    if abs(vt) < 10:
                        s["volume_trend"] = vt
                        counts["volume_trend"] += 1
        else:
            # Old files predate the volume column and refill on the next price
            # refresh, which is a wait rather than a gap.
            status["volume"] = "deferred_budget"
            status["volume_trend"] = "deferred_budget"

        if status:
            s["status"] = status

    print(f"price-derived: " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items()))
          + f" of {len(stocks)} tickers "
          f"({missing} no history, {short} too short, {stale} stale; {gaps} with no day change "
          f"because the previous session is missing from the series).")
    return counts["price"]


# ── Risk metrics from the history we already store ─────────────────────────
#
# enrich_with_prices keeps 251 daily closes for 5,447 tickers so the expanded
# row can draw a chart. That is a year of daily returns per company, sitting on
# disk and read by nothing but the chart. Volatility, drawdown, beta and Sharpe
# all fall out of it, and only the last two need anything we do not already have.
#
# What they need is a risk-free rate and a benchmark. Both are free and need no
# key: ^IRX is the 13-week Treasury bill, the standard short-rate proxy, and
# ^GSPC is the S&P 500 itself. Both come through yfinance, which is already a
# dependency, so this adds a data source without adding a service, a credential
# or a second thing that can go down.
#
# The Treasury publishes the same series as keyless CSV at home.treasury.gov if
# Yahoo ever stops carrying it; the shape here is deliberately small enough that
# swapping the source means rewriting one function.
MARKET_FILE = "_MARKET.json"
_RISK_FREE_SYMBOL = "^IRX"       # 13-week T-bill discount rate, quoted in percent
# The 10-year Treasury yield, quoted in percent like ^IRX. Nothing in the pipeline
# computes with it: it is stored for the analyst's discount rate, which is set on
# a long-dated yield rather than on a three-month bill. Keyless, like the others.
_TEN_YEAR_SYMBOL = "^TNX"
_BENCHMARK_SYMBOL = "^GSPC"      # S&P 500
_TRADING_DAYS = 252
# Enough of a year to annualize honestly. Below this the numbers are noise
# wearing an annual label.
_MIN_RISK_OBS = 120


def enrich_with_market_series(max_age_hours=24):
    """Store the risk-free rate, the 10-year yield and the benchmark beside the
    ticker histories.

    Three symbols, one file, same 24h cache as the per-ticker prices. Returns True
    when a usable series is on disk afterwards. A cached file without the 10-year
    series (one written before it was added) is refetched rather than reused."""
    PRICES_DIR.mkdir(parents=True, exist_ok=True)
    path = PRICES_DIR / MARKET_FILE
    if path.exists():
        try:
            cached = json.loads(path.read_text(encoding="utf-8"))
            age = _age_hours_from_iso(cached.get("updated"))
            if (age is not None and age <= max_age_hours and cached.get("risk_free")
                    and cached.get("ten_year")):
                print(f"market: risk-free, 10-year and benchmark are {age:.1f}h old, reusing.")
                return True
        except Exception:
            pass
    try:
        import yfinance as yf
    except ImportError:
        print("market: yfinance not installed, skipping risk-free and benchmark.")
        return path.exists()

    def series(symbol):
        try:
            hist = yf.download(symbol, period="1y", interval="1d",
                               progress=False, auto_adjust=True, threads=False)
            if hist is None or hist.empty:
                return []
            col = hist["Close"]
            # A single-symbol download still comes back with MultiIndex columns
            # on current yfinance, so take the first column rather than assuming.
            if hasattr(col, "columns"):
                col = col.iloc[:, 0]
            out = []
            for ts, val in col.dropna().items():
                try:
                    out.append([ts.date().isoformat(), round(float(val), 6)])
                except Exception:
                    continue
            return out
        except Exception as exc:
            print(f"market: {symbol} fetch failed ({type(exc).__name__}: {exc}).")
            return []

    rf = series(_RISK_FREE_SYMBOL)
    ten = series(_TEN_YEAR_SYMBOL)
    bench = series(_BENCHMARK_SYMBOL)
    if not rf and not bench:
        print("market: risk-free and benchmark both empty, keeping whatever is already on disk.")
        return path.exists()
    if not ten:
        # Keep the last stored 10-year series rather than blanking it for a day.
        # Every point carries its own date, so a reader can see how old it is.
        try:
            ten = json.loads(path.read_text(encoding="utf-8")).get("ten_year") or []
        except Exception:
            ten = []
        if ten:
            print(f"market: {_TEN_YEAR_SYMBOL} fetch empty, keeping the stored series "
                  f"(last point {ten[-1][0]}).")
    payload = {"updated": datetime.now(timezone.utc).isoformat(),
               "risk_free_symbol": _RISK_FREE_SYMBOL, "risk_free": rf,
               "ten_year_symbol": _TEN_YEAR_SYMBOL, "ten_year": ten,
               "benchmark_symbol": _BENCHMARK_SYMBOL, "benchmark": bench}
    path.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    print(f"market: stored {len(rf)} risk-free, {len(ten)} 10-year and {len(bench)} benchmark "
          f"observations ({_RISK_FREE_SYMBOL} latest {rf[-1][1] if rf else 'n/a'}%, "
          f"{_TEN_YEAR_SYMBOL} latest {ten[-1][1] if ten else 'n/a'}%).")
    return True


def _benchmark_closes(frame, symbol):
    """[[date, close], ...] for one symbol out of a yf.download frame, unadjusted."""
    try:
        if hasattr(frame.columns, "get_level_values") and symbol in frame.columns.get_level_values(0):
            col = frame[symbol]["Close"]
        else:
            col = frame["Close"]
        if hasattr(col, "columns"):
            col = col[symbol] if symbol in col.columns else col.iloc[:, 0]
    except Exception:
        return []
    out = []
    for ts, val in col.dropna().items():
        try:
            v = float(val)
        except (TypeError, ValueError):
            continue
        if math.isfinite(v) and v > 0:
            out.append([ts.date().isoformat(), round(v, 6)])
    return out


def enrich_with_benchmark_series(max_age_hours=24):
    """Store the model portfolios' benchmarks: the six style books' Russell ETFs, the core and
    tax books' ones (portfolio.engine.BENCHMARK_SYMBOLS), in docs/prices/_BENCHMARKS.json.

    Keyless, through yfinance like the market series, with the same 24h cache. The
    closes are NOT adjusted for dividends (auto_adjust=False), because the books'
    returns are price-only and the comparison must be too. Every point carries its
    date. A download covers a year, so points older than that are kept from the
    stored file (portfolio.engine.merge_benchmark_series): a book's inception close
    has to outlive the window. Returns the number of symbols with a stored series."""
    PRICES_DIR.mkdir(parents=True, exist_ok=True)
    path = PRICES_DIR / PF.BENCHMARKS_FILE
    stored, prev_updated = {}, ""
    if path.exists():
        try:
            cached = json.loads(path.read_text(encoding="utf-8"))
            stored = cached.get("series") or {}
            prev_updated = cached.get("updated") or ""
            age = _age_hours_from_iso(cached.get("updated"))
            if (age is not None and age <= max_age_hours
                    and all((stored.get(s) or {}).get("closes") for s in PF.BENCHMARK_SYMBOLS)):
                print(f"benchmarks: all {len(PF.BENCHMARK_SYMBOLS)} series are {age:.1f}h old, reusing.")
                return len(PF.BENCHMARK_SYMBOLS)
        except Exception:
            stored = {}
    try:
        import yfinance as yf
    except ImportError:
        print("benchmarks: yfinance not installed, skipping.")
        return sum(1 for s in stored.values() if (s or {}).get("closes"))
    try:
        frame = yf.download(" ".join(PF.BENCHMARK_SYMBOLS), period="1y", interval="1d",
                            group_by="ticker", progress=False, auto_adjust=False, threads=False)
    except Exception as exc:
        print(f"benchmarks: download failed ({type(exc).__name__}: {exc}).")
        frame = None
    now = datetime.now(timezone.utc).isoformat()
    series, fetched, kept = {}, [], []
    for sym in PF.BENCHMARK_SYMBOLS:
        fresh = _benchmark_closes(frame, sym) if frame is not None and not frame.empty else []
        old = (stored.get(sym) or {})
        merged = PF.merge_benchmark_series(old.get("closes") or [], fresh)
        if fresh:
            fetched.append(sym)
        elif merged:
            kept.append(sym)
        series[sym] = {"name": PF.BENCHMARK_NAMES.get(sym, sym), "closes": merged,
                       "fetched": now if fresh else old.get("fetched", "")}
    if not fetched and not stored:
        print("benchmarks: nothing fetched and nothing stored; no file written.")
        return 0
    payload = {"updated": now if fetched else prev_updated,
               "basis": "Daily close, not adjusted for dividends (price return)",
               "source": "Yahoo Finance via yfinance, keyless",
               "series": series}
    path.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    print(f"benchmarks: fetched {len(fetched)} of {len(PF.BENCHMARK_SYMBOLS)}"
          + (f"; kept the stored series for {', '.join(kept)}" if kept else "") + ".")
    return sum(1 for s in series.values() if s["closes"])


def _benchmarks_quietly():
    """The benchmark fetch, never allowed to cost the universe its refresh."""
    try:
        return enrich_with_benchmark_series()
    except Exception as exc:
        print(f"benchmarks: skipped ({type(exc).__name__}: {exc}).")
        return 0


def _load_market_series():
    """(risk_free_by_date, benchmark_daily_return_by_date). Empty when absent."""
    try:
        data = json.loads((PRICES_DIR / MARKET_FILE).read_text(encoding="utf-8"))
    except Exception:
        return {}, {}
    # ^IRX is quoted as a percentage, so 3.775 means 3.775% a year. Convert to a
    # daily decimal rate the same way the return series is daily.
    rf = {}
    for row in data.get("risk_free") or []:
        try:
            rf[row[0]] = float(row[1]) / 100.0 / _TRADING_DAYS
        except Exception:
            continue
    bench_closes = []
    for row in data.get("benchmark") or []:
        try:
            bench_closes.append((row[0], float(row[1])))
        except Exception:
            continue
    bench_closes.sort()
    bench = {}
    for i in range(1, len(bench_closes)):
        prev, cur = bench_closes[i - 1][1], bench_closes[i][1]
        if prev > 0:
            bench[bench_closes[i][0]] = cur / prev - 1
    return rf, bench


# Listing names that mark an American depositary share. "ADS" must not be
# followed by a hyphen or a letter: ADS-TEC Energy is an ordinary share.
_DEPOSITARY_NAME = re.compile(r"depositar|depositor|\bADRs?\b|\bADS(?![-\w])", re.I)
# The pe codes derive_ratios_from_fundamentals owns, cleared when none applies
# so a stamp from an earlier pass on the same cached row cannot linger.
_PE_STATUS_CODES = ("awaiting_filing", "not_meaningful", "vendor_value")


def derive_ratios_from_fundamentals(stocks):
    """Valuation and profitability ratios, from filings and a price.

    Every one of these was arriving as a finished number from Yahoo's
    per-ticker quote summary, which is the slowest request in the run and the
    one most likely to be cut short by its time budget. The inputs are all in
    the XBRL facts already fetched for the trend factors, and combining them
    with a close is arithmetic.

    Conventions, all of them the ordinary ones:

      market cap    price x cover-page shares outstanding, not the
                    weighted average, which describes a period rather than a
                    moment and understates a company mid-buyback
      P/E           price / trailing four quarters of DILUTED EPS
      P/B           market cap / parent-company equity, excluding
                    noncontrolling interests that common holders cannot claim
      ROE           trailing net income / AVERAGE equity over the same window,
                    since the denominator moves through the year
      EV            market cap + total debt - cash and short-term investments
      FCF           trailing operating cash flow - capital expenditure

    Only computed where the filing inputs exist, which is why coverage tracks
    the EDGAR sweep rather than the price series."""
    if not stocks:
        return 0
    counts = collections.Counter()

    def put(s, key, value, lo, hi):
        if value is None or not math.isfinite(value) or not (lo < value < hi):
            return
        s[key] = value
        counts[key] += 1

    for s in stocks:
        price = s.get("price")
        shares = s.get("shares_outstanding")
        equity = s.get("equity")
        rev = s.get("ttm_revenue")
        ebitda = s.get("ttm_ebitda")

        cap = None
        if _finite(price) and _finite(shares) and price > 0 and shares > 0:
            cap = price * shares
            put(s, "market_cap", cap, 0, 1e14)
        if cap is None:
            cap = s.get("market_cap") if _finite(s.get("market_cap")) else None

        eps = s.get("ttm_eps_diluted")
        ni_ttm = s.get("ttm_net_income")
        pe_code = None
        if _finite(eps) and eps > 0 and _DEPOSITARY_NAME.search(s.get("name") or ""):
            # A depositary share is not a share. Filings report EPS per
            # ordinary share and one ADS can be a twentieth of one or ten of
            # them: DoubleDown's ADS priced at $12.75 against 46.20 of EPS per
            # ordinary share, a P/E of 0.28. The vendor quotes it per ADS.
            pe_code = "vendor_value" if s.get("pe") is not None else None
            counts["pe_left_to_vendor_depositary"] += 1
        elif _finite(price) and _finite(eps) and eps > 0:
            s.pop("pe", None)
            put(s, "pe", price / eps, -500, 1000)
            # Outside the bounds means earnings a rounding error from zero.
            pe_code = "awaiting_filing" if s.get("pe") is not None else "not_meaningful"
        elif _finite(eps) and eps <= 0:
            # Declining to compute one is not the same as withholding one.
            # enrich_with_yfinance has already written Yahoo's trailingPE, which
            # can be positive on a company losing money because it is not built
            # from the same EPS. Skipping the write here left that vendor number
            # in place on exactly the names where our own filing data says it is
            # wrong: HIMS carried pe 51.70 against ttm_eps_diluted of -0.63, and
            # 64 rows on 2026-09-11 had a positive P/E with non-positive TTM EPS.
            # The README documents this field as withheld when TTM EPS <= 0, so
            # withhold it.
            if s.pop("pe", None) is not None:
                counts["pe_withheld_negative_eps"] += 1
            pe_code = "not_meaningful"
        elif _finite(ni_ttm) and ni_ttm <= 0:
            # No EPS we trust, but the filings say the company lost money. A
            # positive vendor P/E there contradicts the filing: 9 rows on
            # 2026-09-21, plus every annual-only loss-maker.
            if s.pop("pe", None) is not None:
                counts["pe_withheld_filed_loss"] += 1
            pe_code = "not_meaningful"
        elif s.get("pe") is not None:
            # What is left is Yahoo's trailingPE, written by
            # enrich_with_yfinance. It used to be stamped awaiting_filing like
            # any filing-derived number, which told the reader it came from a
            # filing when nothing we hold supports it.
            pe_code = "vendor_value"
            counts["pe_vendor"] += 1
        pe_status = s.get("status") or {}
        if pe_code:
            pe_status["pe"] = pe_code
        elif pe_status.get("pe") in _PE_STATUS_CODES:
            del pe_status["pe"]
        if pe_status:
            s["status"] = pe_status
        else:
            s.pop("status", None)

        if cap and _finite(equity) and equity > 0:
            put(s, "price_book", cap / equity, 0, 100)

        ni = s.get("ttm_net_income")
        prior_eq = s.get("prior_equity")
        if _finite(ni) and _finite(equity):
            denom = ((equity + prior_eq) / 2) if _finite(prior_eq) else equity
            if denom and denom > 0:
                put(s, "roe_ttm", ni / denom, -3, 3)

        if _finite(rev) and rev > 0:
            gp = s.get("ttm_gross_profit")
            # An exact zero is a missing tag, not a measurement. Banks and
            # insurers do not report a gross profit line at all, so the sum
            # comes out 0.0 and the ratio prints as a 0% gross margin, which
            # reads as a business selling below cost. 127 of 164 gated bank
            # names carried exactly 0.0. Declining to compute it is not
            # withholding it: the field is simply absent for these filers.
            if _finite(gp) and gp != 0:
                put(s, "gross_margin", gp / rev, -2, 1)
            oi = s.get("ttm_operating_income")
            if _finite(oi):
                put(s, "operating_margin", oi / rev, -5, 1)

        fcf = s.get("ttm_fcf")
        if cap and _finite(fcf):
            put(s, "fcf_yield", fcf / cap, -1, 1)

        prior_rev = s.get("prior_ttm_revenue")
        if _finite(rev) and _finite(prior_rev) and prior_rev > 0:
            put(s, "revenue_growth_yoy", rev / prior_rev - 1, -5, 5)
        prior_eps = s.get("prior_ttm_eps_diluted")
        if _finite(eps) and _finite(prior_eps) and prior_eps > 0:
            put(s, "eps_growth_yoy", eps / prior_eps - 1, -10, 10)

        # ev_ebitda, ev_revenue and net_debt_ebitda are deliberately NOT derived
        # here, and keep coming from the vendor.
        #
        # All three need total debt, and total debt cannot be composed from XBRL
        # reliably. Filers split short-term borrowing across ShortTermBorrowings,
        # CommercialPaper, DebtCurrent and the current portion of long-term debt,
        # in combinations that overlap: Coca-Cola tags both ShortTermBorrowings
        # and CommercialPaper, so picking one understates and summing both risks
        # counting the same paper twice. Measured against the vendor, this
        # construction put Coca-Cola's total debt at $36.8bn against roughly
        # $45bn, and net-debt-to-EBITDA disagreed by a median of 43%.
        #
        # A leverage figure that is wrong by half is worse than no leverage
        # figure, and unlike revenue growth or free cash flow there is no
        # independent check here that says our version is the better one. The
        # ttm_ebitda, total_debt and cash_and_investments aggregates are still
        # published for anyone who wants to build their own.

        # A filing-derived number is as current as the filing, not as the run.
        if s.get("fiscal_period_end"):
            status = s.get("status") or {}
            # pe is stamped above, by where it came from.
            for f in ("price_book", "roe_ttm", "gross_margin", "operating_margin",
                      "fcf_yield", "ev_ebitda", "ev_revenue", "net_debt_ebitda",
                      "revenue_growth_yoy", "eps_growth_yoy"):
                if s.get(f) is not None:
                    status[f] = "awaiting_filing"
            if status:
                s["status"] = status

    print("filing-derived: " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items()))
          + f" of {len(stocks)} tickers.")
    return counts["market_cap"]


def derive_risk_metrics(stocks):
    """Volatility, drawdown, beta and Sharpe, from the stored daily closes.

    Nothing here is fetched. enrich_with_prices already keeps a year of closes
    per ticker for the chart card, and this reads the same files:

      volatility_1y   annualized standard deviation of daily returns
      max_drawdown_1y worst peak-to-trough fall over the window, as a negative
      beta_1y         slope against the S&P 500 on the days both traded
      sharpe_1y       mean daily excess return over the 13-week bill, annualized

    Volatility and drawdown need only the ticker's own history, so they are
    produced whether or not the market file is there. Beta and Sharpe need the
    benchmark and the risk-free rate and are skipped when it is missing, rather
    than substituting a zero rate, which would quietly inflate every Sharpe in
    the universe by roughly the level of short rates."""
    if not stocks:
        return 0
    rf_by_date, bench_by_date = _load_market_series()
    have_market = bool(rf_by_date and bench_by_date)
    if not have_market:
        print("risk: no market series on disk, computing volatility and drawdown only.")

    cutoff = (datetime.now(timezone.utc).date() - timedelta(days=10)).isoformat()
    n_vol = n_beta = n_sharpe = 0
    skipped = 0
    for s in stocks:
        try:
            closes = json.loads((PRICES_DIR / _news_filename(s["ticker"]))
                                .read_text(encoding="utf-8")).get("closes") or []
        except Exception:
            skipped += 1
            continue
        if len(closes) < _MIN_RISK_OBS or str(closes[-1][0]) < cutoff:
            skipped += 1
            continue
        try:
            dated = [(str(d), float(p)) for d, p in closes if float(p) > 0]
        except (TypeError, ValueError):
            skipped += 1
            continue
        if len(dated) < _MIN_RISK_OBS:
            skipped += 1
            continue

        rets = []
        for i in range(1, len(dated)):
            rets.append((dated[i][0], dated[i][1] / dated[i - 1][1] - 1))

        mean = sum(r for _, r in rets) / len(rets)
        var = sum((r - mean) ** 2 for _, r in rets) / (len(rets) - 1)
        sd = var ** 0.5
        vol = sd * (_TRADING_DAYS ** 0.5)
        if 0 < vol < 10:
            s["volatility_1y"] = vol
            n_vol += 1

        peak = dated[0][1]
        worst = 0.0
        for _, px in dated:
            if px > peak:
                peak = px
            dd = px / peak - 1
            if dd < worst:
                worst = dd
        s["max_drawdown_1y"] = worst

        if not have_market:
            continue

        # Beta on the days both actually traded. Aligning by position instead
        # would silently pair a stock's Tuesday with the index's Wednesday for
        # every holiday the two calendars disagree on.
        pairs = [(r, bench_by_date[d]) for d, r in rets if d in bench_by_date]
        if len(pairs) >= _MIN_RISK_OBS:
            mb = sum(b for _, b in pairs) / len(pairs)
            ms = sum(a for a, _ in pairs) / len(pairs)
            cov = sum((a - ms) * (b - mb) for a, b in pairs) / (len(pairs) - 1)
            varb = sum((b - mb) ** 2 for _, b in pairs) / (len(pairs) - 1)
            if varb > 0:
                beta = cov / varb
                if abs(beta) < 10:
                    s["beta_1y"] = beta
                    n_beta += 1

        excess = [r - rf_by_date[d] for d, r in rets if d in rf_by_date]
        if len(excess) >= _MIN_RISK_OBS:
            me = sum(excess) / len(excess)
            ve = sum((e - me) ** 2 for e in excess) / (len(excess) - 1)
            se = ve ** 0.5
            if se > 0:
                sharpe = me / se * (_TRADING_DAYS ** 0.5)
                if abs(sharpe) < 20:
                    s["sharpe_1y"] = sharpe
                    n_sharpe += 1

    print(f"risk: volatility for {n_vol}, beta for {n_beta}, Sharpe for {n_sharpe} "
          f"of {len(stocks)} tickers ({skipped} without usable history).")
    return n_vol


# A stale file is only worth rechecking so often. Holidays are invisible to this
# project, so without a floor a holiday would refetch ~5,300 tickers every run.
# This is the floor for the case where nothing proves the session happened.
_PRICE_RECHECK_FLOOR_H = 6
# The floor once something does prove it: an Alpha Vantage quote stamped with
# today's trading day, or any stored series that already carries the bar. Then
# a file without the bar is not a holiday, it is a vendor that has not caught
# up, and six hours was long enough to lose the day. On 2026-09-21 the promoted
# run downloaded at 20:34 ET, Yahoo returned 4,894 of 5,489 series ending on the
# Friday, and every later run that night found the files under six hours old
# and refetched nothing. The next day's download had the bar for 5,373 of them:
# the data existed, it was just late. 45 minutes means the next hourly run
# retries.
_PRICE_LAG_RETRY_FLOOR_H = 0.75
# In-run retry of the series that came back without the session's bar. Every
# price pass between about 20:00 and 20:40 ET lost the bar for most liquid
# tickers (three of three), which looks like Yahoo rolling its daily candle
# over after the post-market session, so a short wait is worth a second try
# before the run gives up and leaves it to the next hourly one. Only when the
# lagging share is large: illiquid names with no trade that day lag every day,
# and waiting six minutes for them would be waste.
_PRICE_RETRY_ROUNDS = 2
_PRICE_RETRY_WAIT_S = 180
_PRICE_RETRY_MIN_SHARE = 0.10


def _price_file_needs_fetch(data, expected_close, max_age_hours, session_confirmed):
    """Whether a stored price file must be downloaded again. Pure, so it is testable.

    `data` is the parsed docs/prices/<T>.json, or None when the file is missing
    or unreadable. Freshness comes from the "updated" field the writer stores in
    the file, never the mtime, which CI resets on every checkout.

    How recently we asked is the wrong question. The panel takes its price from
    the last close in this file, so what matters is whether that close is the
    expected session's.

    On a 24h window alone it never was. The file refreshed around 00:23 UTC and
    the panel was written around 23:21 UTC, roughly 23 hours later and just
    inside the window, so the fetch was skipped and the panel recorded a close
    one session old. Every run. On 2026-09-11, 4,703 of 5,340 tickers carried an
    identical price across three consecutive panel dates, and prices_updated
    advanced anyway because it stamps the attempt rather than a new value.

    A file lacking the expected bar is rechecked after a floor rather than on
    every run, and the floor depends on whether the session is known to have
    traded. Exchange holidays are not detected, so on a holiday expected_close
    names a session that never happens; unconfirmed, the six-hour floor bounds
    that cost. Confirmed, the lag is the vendor's, and the short floor lets the
    next hourly run fix it instead of the next day's."""
    if not isinstance(data, dict):
        return True
    age = _age_hours_from_iso(data.get("updated"))
    if age is None or age > max_age_hours:
        return True
    last = _price_file_last_date(data)
    if not last or last >= expected_close:
        return False
    floor = _PRICE_LAG_RETRY_FLOOR_H if session_confirmed else _PRICE_RECHECK_FLOOR_H
    return age > floor


def _price_file_last_date(data):
    """Date of the last stored bar, or None."""
    closes = (data or {}).get("closes") or []
    if closes and isinstance(closes[-1], list) and closes[-1]:
        return str(closes[-1][0])
    return None


# A stored bar is kept only when the stored and fresh series agree on the bars
# either side of it, to this relative tolerance. Yahoo's closes are adjusted for
# dividends and splits after the fact, so after an ex-date the whole earlier
# history moves. A bar kept from before the adjustment would sit on a different
# basis from its neighbours (after a 2:1 split, at twice the price), and the
# chart and every return would show a move that never happened.
_PRICE_MERGE_BASIS_TOL = 5e-4


def _merge_price_series(closes, volumes, stored):
    """Fresh closes and volumes, plus the stored bars the fresh download lacks.

    Returns (closes, volumes, kept). Pure, so it is testable.

    The price pass downloads a year and used to replace the file with it. On
    2026-09-23 Yahoo's response left out the 2026-09-22 bar for 4,869 of 5,254
    tickers, so a day we already held was deleted, and the next change_pct was a
    two-day move published as one (CF: -2.13% against a real +0.04%).

    Only bars Yahoo actually returned on an earlier fetch are kept, with the
    close and volume exactly as stored. Nothing is computed to fill a missing
    bar. A stored bar is kept when its date is inside the fresh window (between
    the fresh first and last dates), absent from the fresh series, and the
    nearest date both series share on each side (one side, when the bar is the
    last stored one) carries the same close in both (see
    _PRICE_MERGE_BASIS_TOL). Bars before the fresh window are dropped as they
    always were, so the file stays about a year long. volumes stays aligned with
    closes by index; a kept bar takes its stored volume or None."""
    if not closes or not isinstance(stored, dict):
        return closes, volumes, 0
    old = stored.get("closes") or []
    old_vol = stored.get("volumes")
    if not isinstance(old_vol, list) or len(old_vol) != len(old):
        old_vol = [None] * len(old)
    fresh = {}
    for c in closes:
        fresh[c[0]] = c[1]
    first, last = closes[0][0], closes[-1][0]
    stored_px = {}
    for o in old:
        try:
            stored_px[str(o[0])] = float(o[1])
        except (TypeError, ValueError, IndexError):
            continue
    shared = sorted(d for d in fresh if d in stored_px)

    def agrees(d):
        a, b = fresh[d], stored_px[d]
        return b > 0 and abs(a / b - 1) <= _PRICE_MERGE_BASIS_TOL

    extra = []
    for k, o in enumerate(old):
        try:
            d, px = str(o[0]), float(o[1])
        except (TypeError, ValueError, IndexError):
            continue
        if d in fresh or not (first < d < last) or not (0 < px < 1e6):
            continue
        lo = hi = None
        for s in shared:
            if s < d:
                lo = s
            elif s > d:
                hi = s
                break
        # Both neighbours when there are two. The incident's bar had only one:
        # 09-22 was the last stored bar and 09-23 arrived with this download.
        sides = [x for x in (lo, hi) if x is not None]
        if not sides or not all(agrees(x) for x in sides):
            continue
        v = old_vol[k]
        extra.append((d, o[1], v if isinstance(v, int) and v >= 0 else None))
    if not extra:
        return closes, volumes, 0
    vols = volumes if isinstance(volumes, list) and len(volumes) == len(closes) \
        else [None] * len(closes)
    merged = [(c[0], c[1], vols[i]) for i, c in enumerate(closes)] + extra
    merged.sort(key=lambda x: x[0])
    return [[d, p] for d, p, _ in merged], [v for _, _, v in merged], len(extra)


def _last_expected_session(now=None):
    """The most recent date a US close should exist for, as YYYY-MM-DD.

    Weekday after the close, that is today; otherwise the previous weekday.
    Exchange holidays are not detected anywhere in this project, so on a holiday
    this points at a session that never happened. Callers must treat it as an
    upper bound and rate-limit on it rather than refetching forever."""
    now = now or datetime.now(tz=EASTERN)
    d = now.date()
    # 16:00 ET close; allow a margin for the data to settle at the vendor.
    if now.weekday() < 5 and now.hour < 17:
        d -= timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d.isoformat()


def enrich_with_prices(stocks, max_age_hours=24, batch_size=200, session_confirmed=False):
    """Fetch ~1y daily closes and volumes per ticker via the yf.download bulk
    endpoint and write docs/prices/{TICKER}.json. The bulk endpoint is dramatically faster than
    per-ticker .history() (one HTTP per batch instead of one per ticker), and is
    much friendlier to Yahoo's rate limiter. 24h cache per file so the midday/
    evening runs are no-ops. Stored shape: {"updated": iso, "closes": [[date, close], ...]}.
    Skipped silently if yfinance is missing.

    `session_confirmed` is the caller's evidence that the expected session
    actually traded (an Alpha Vantage quote stamped with it). It only shortens
    the recheck floor for files that lack the session's bar; see
    _price_file_needs_fetch."""
    if not stocks:
        return 0
    try:
        import yfinance as yf
    except ImportError:
        print("prices: yfinance not installed, skipping.")
        return 0
    PRICES_DIR.mkdir(parents=True, exist_ok=True)

    expected_close = _last_expected_session()

    stored = {}
    for s in stocks:
        f = PRICES_DIR / _news_filename(s["ticker"])
        try:
            stored[s["ticker"]] = json.loads(f.read_text(encoding="utf-8")) if f.exists() else None
        except Exception:
            stored[s["ticker"]] = None
    # A stored series that already carries the expected bar is proof the session
    # traded, from data already on disk. It covers the run where Alpha Vantage
    # was down but an earlier pass reached some tickers.
    confirmed = session_confirmed or any(
        (_price_file_last_date(d) or "") >= expected_close for d in stored.values() if d)

    todo = [s for s in stocks
            if _price_file_needs_fetch(stored.get(s["ticker"]), expected_close,
                                       max_age_hours, confirmed)]
    skipped = len(stocks) - len(todo)
    if not todo:
        print(f"prices: all {len(stocks)} ticker files within {max_age_hours}h, skipping fetch.")
        return 0

    t0 = time.time()
    # Yahoo uses '-' for class shares (BRK-B); Wikipedia uses '.' (BRK.B). Translate.
    sym_map = {s["ticker"].replace(".", "-"): s["ticker"] for s in todo}
    yf_syms = list(sym_map.keys())
    kept = {}                            # ticker -> stored bars kept by the merge

    def download(syms):
        """One bulk pass over `syms`. Returns {ticker: date of its last bar} for
        every file written."""
        last_bar = {}
        for i in range(0, len(syms), batch_size):
            chunk = syms[i:i + batch_size]
            try:
                df = yf.download(
                    tickers=" ".join(chunk),
                    period="1y",
                    interval="1d",
                    group_by="ticker",
                    auto_adjust=True,
                    threads=True,
                    progress=False,
                )
            except Exception as e:
                print(f"prices: bulk download failed for batch {i//batch_size + 1}: {e}")
                continue
            if df is None or df.empty:
                continue
            for yf_sym in chunk:
                try:
                    if len(chunk) == 1:
                        series = df["Close"] if "Close" in df.columns else None
                    elif yf_sym in df.columns.get_level_values(0):
                        series = df[yf_sym]["Close"] if "Close" in df[yf_sym].columns else None
                    else:
                        series = None
                    if series is None or series.empty:
                        continue
                    # Volume comes back in the same download and was being thrown
                    # away, so volume and volume_trend were coming from a per-ticker
                    # .info request instead. Stored as a bare array aligned with
                    # closes by index rather than repeating every date.
                    vol_series = None
                    try:
                        if len(chunk) == 1:
                            vol_series = df["Volume"] if "Volume" in df.columns else None
                        elif yf_sym in df.columns.get_level_values(0):
                            sub = df[yf_sym]
                            vol_series = sub["Volume"] if "Volume" in sub.columns else None
                    except Exception:
                        vol_series = None

                    closes = []
                    volumes = []
                    clean = series.dropna()
                    for idx, val in clean.items():
                        try:
                            date_str = idx.strftime("%Y-%m-%d")
                            v = float(val)
                            if not (0 < v < 1e6):
                                continue
                            closes.append([date_str, round(v, 4)])
                            vol = None
                            if vol_series is not None:
                                try:
                                    raw_vol = vol_series.get(idx)
                                    if raw_vol is not None and raw_vol == raw_vol:
                                        vol = int(float(raw_vol))
                                except Exception:
                                    vol = None
                            volumes.append(vol if (vol is not None and vol >= 0) else None)
                        except Exception:
                            continue
                    if not closes:
                        continue
                    ticker = sym_map[yf_sym]
                    path = PRICES_DIR / _news_filename(ticker)
                    # Merge with the file on disk rather than replace it, so a
                    # bar Yahoo left out of this response but returned before
                    # is not lost (_merge_price_series). The file on disk, not
                    # `stored`, so a retry round also keeps what the first
                    # round of this run wrote.
                    try:
                        on_disk = json.loads(path.read_text(encoding="utf-8")) \
                            if path.exists() else None
                    except Exception:
                        on_disk = None
                    closes, volumes, n_kept = _merge_price_series(closes, volumes, on_disk)
                    if n_kept:
                        kept[ticker] = n_kept
                    else:
                        kept.pop(ticker, None)
                    # "updated" records when we asked, not when the vendor last
                    # had something new. That is why _price_file_needs_fetch
                    # reads the last bar's date as well, and why the panel
                    # carries price_date rather than trusting this stamp.
                    payload = {
                        "ticker": ticker,
                        "updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                        "closes": closes,
                    }
                    # Only when we actually got some, so a file without the key is
                    # an old file rather than a ticker Yahoo reports no volume for.
                    if any(v is not None for v in volumes):
                        payload["volumes"] = volumes
                    path.write_text(
                        json.dumps(payload, separators=(",", ":")), encoding="utf-8"
                    )
                    last_bar[ticker] = closes[-1][0]
                except Exception:
                    continue
        return last_bar

    last_bar = download(yf_syms)
    fetched = len(last_bar)

    # A download that returns series without the session that just closed used
    # to be accepted as fresh. Retry the lagging ones in this run when something
    # proves the session traded: the caller's quotes, a stored file, or any
    # series in this very download that has the bar. On a holiday none does, so
    # nothing is retried. Tickers that returned nothing at all are not retried
    # here; that is a coverage gap, not a late bar. Nor is a handful of lagging
    # names, which is what an ordinary day looks like: illiquid listings with no
    # trade that session.
    for _ in range(_PRICE_RETRY_ROUNDS):
        confirmed = confirmed or any(d >= expected_close for d in last_bar.values())
        lagging = [y for y in yf_syms
                   if sym_map[y] in last_bar and last_bar[sym_map[y]] < expected_close]
        if not (confirmed and lagging) or len(lagging) < _PRICE_RETRY_MIN_SHARE * len(last_bar):
            break
        print(f"prices: {len(lagging)} of {len(last_bar)} series lack the {expected_close} bar "
              f"although the session traded; retrying them in {_PRICE_RETRY_WAIT_S}s.")
        time.sleep(_PRICE_RETRY_WAIT_S)
        last_bar.update(download(lagging))

    behind = sum(1 for d in last_bar.values() if d < expected_close)
    elapsed = time.time() - t0
    print(f"prices: wrote {fetched}/{len(todo)} ticker files in {elapsed:.1f}s "
          f"({skipped} cached < {max_age_hours}h); {behind} still end before "
          f"{expected_close} (session {'confirmed' if confirmed else 'unconfirmed'}).")
    if kept:
        # Per ticker, the last round's count: a retry that merged again
        # replaces the first round's number rather than adding to it.
        print(f"prices: kept {sum(kept.values())} stored bars in {len(kept)} files that the "
              f"download left out (Yahoo returned them on an earlier fetch).")
    return fetched


# ── SEC EDGAR (free, official) for quarterly-trend factors ──────────────────

EDGAR_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
EDGAR_FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"
# SEC requires a User-Agent identifying the requester with a contact email.
EDGAR_USER_AGENT = "Apterreon-IntelBrief/1.0 (ctlsmith@me.com)"

# US-GAAP XBRL concept fallbacks. Companies tag the same economic concept under
# different names depending on industry / vintage. Try each in order, keep the first.
EDGAR_CONCEPT_FALLBACKS = {
    "revenue": [
        "Revenues",
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "RevenueFromContractWithCustomerIncludingAssessedTax",
        "SalesRevenueNet",
        "SalesRevenueGoodsNet",
    ],
    "gross_profit": ["GrossProfit"],
    "operating_income": ["OperatingIncomeLoss"],
    "cfo": ["NetCashProvidedByUsedInOperatingActivities"],
    "capex": [
        "PaymentsToAcquirePropertyPlantAndEquipment",
        "PaymentsToAcquireProductiveAssets",
    ],
    "eps_basic": ["EarningsPerShareBasic"],
    "net_income": ["NetIncomeLoss", "ProfitLoss",
                   "NetIncomeLossAvailableToCommonStockholdersBasic"],
    # Balance-sheet total assets. Instant fact, not a duration: see
    # _extract_instant_series for why it needs its own extractor.
    "total_assets": ["Assets"],
    # Everything below was chosen by counting which tags eight large filers
    # across four sectors actually use, rather than by picking the name that
    # sounds right. The order is by that hit rate.
    #
    # Diluted, not basic: a trailing P/E is quoted on diluted EPS because that
    # is the share count an outside holder is actually diluted by.
    #
    # The two concepts this list used to hold left 79 profitable filers on the
    # vendor's P/E: Exxon and Visa tag EarningsPerShareBasicAndDiluted, and
    # partnerships (ET, MPLX) report per unit, not per share. A combined
    # basic-and-diluted figure IS the diluted figure. Basic EPS is the last
    # resort, for a filer that reports nothing else, and is labeled as such in
    # eps_basis, because it overstates EPS wherever there is real dilution.
    "eps_diluted": ["EarningsPerShareDiluted",
                    "IncomeLossFromContinuingOperationsPerDilutedShare",
                    "EarningsPerShareBasicAndDiluted",
                    "IncomeLossFromContinuingOperationsPerBasicAndDilutedShare",
                    "NetIncomeLossPerOutstandingLimitedPartnershipUnitDilutedNetOfTax",
                    "NetIncomeLossPerOutstandingLimitedPartnershipUnitBasicAndDilutedNetOfTax",
                    "EarningsPerShareBasic",
                    "NetIncomeLossPerOutstandingLimitedPartnershipUnitBasicNetOfTax"],
    # Earnings attributable to common holders and the weighted diluted share
    # count: the two halves of diluted EPS. Used to check an EPS series against
    # itself and to rebuild a quarter the filer never tagged (see
    # _reconcile_eps_quarters). The common-holder figure is preferred because
    # NetIncomeLoss still includes preferred dividends, which would put a bank
    # with preferred stock several percent high.
    "net_income_common": ["NetIncomeLossAvailableToCommonStockholdersDiluted",
                          "NetIncomeLossAvailableToCommonStockholdersBasic",
                          "NetIncomeLoss", "ProfitLoss"],
    "shares_diluted_weighted": ["WeightedAverageNumberOfDilutedSharesOutstanding",
                                "WeightedAverageNumberOfShareOutstandingBasicAndDiluted",
                                "WeightedAverageLimitedPartnershipUnitsOutstandingDiluted",
                                "WeightedAverageLimitedPartnershipUnitsOutstanding",
                                "WeightedAverageNumberOfSharesOutstandingBasic"],
    # Cover-page share count, which is the one closest to today. The
    # weighted-average figures describe a period, not a moment, and would
    # understate a company that has been buying back stock.
    "shares_outstanding": ["EntityCommonStockSharesOutstanding",
                           "CommonStockSharesOutstanding"],
    # Parent-company equity. The IncludingNoncontrollingInterest variant counts
    # equity that common holders have no claim on.
    "equity": ["StockholdersEquity"],
    "cash": ["CashAndCashEquivalentsAtCarryingValue"],
    "short_term_investments": ["ShortTermInvestments", "MarketableSecuritiesCurrent"],
    "long_term_debt": ["LongTermDebtNoncurrent", "LongTermDebt"],
    # Current portion of long-term debt plus other short-term borrowing. Kept
    # as two separate lookups rather than one fallback chain, because a filer
    # reporting both is reporting two different things.
    "current_debt": ["LongTermDebtCurrent"],
    "short_term_borrowings": ["ShortTermBorrowings", "CommercialPaper"],
    # A combined figure where the filer reports one. Microsoft does not: it
    # tags depreciation and intangible amortization as separate lines, so a
    # single-concept lookup returned nothing and its EBITDA went missing
    # entirely rather than wrong, which is the better failure but still a gap.
    "dep_amort": ["DepreciationDepletionAndAmortization",
                  "DepreciationAndAmortization",
                  "DepreciationAmortizationAndAccretionNet"],
    # Only used when no combined tag exists, so a filer reporting both cannot
    # be counted twice.
    "depreciation_only": ["Depreciation"],
    "amortization_only": ["AmortizationOfIntangibleAssets"],
}


# Four quarters is a trailing twelve months. Fewer is not, and summing three
# and calling it TTM is how a ratio ends up 25% light.
_TTM_QUARTERS = 4

# Every field compute_edgar_factors emits that something downstream depends on.
# When one of these is absent across the entire cache, the cache predates it and
# the weekly stamp has to be ignored, because the tickers that would carry the
# new field are exactly the ones already marked fresh.
#
# This list was two names, hardcoded, and adding the trailing aggregates without
# extending it would have been silent: 4,631 of 5,339 tickers were stamped inside
# the current ISO week, so the ratios built on those aggregates would have
# reached about 700 companies and the rest would have waited for Monday. Anything
# added to compute_edgar_factors that another pass reads belongs here.
EDGAR_SCHEMA_SENTINELS = (
    "accruals_ratio", "op_margin_history",
    # Trailing aggregates and balance-sheet values behind the valuation ratios.
    "shares_outstanding", "ttm_revenue", "ttm_eps_diluted", "equity",
    "fiscal_period_end",
    # Which EPS the P/E is built on. Added with the split-safe per-share
    # quarters, so its absence across the cache is what forces every ticker's
    # EPS to be recomputed rather than keeping a Q4 derived across a split.
    "eps_basis",
)

# The EPS labels compute_edgar_factors writes to eps_basis, weakest last.
EPS_BASIS_TTM = "ttm"        # four quarters of diluted EPS
EPS_BASIS_ANNUAL = "annual"  # the latest fiscal year's diluted EPS, no quarters filed
EPS_BASIS_BASIC = "basic"    # basic EPS, because no diluted figure is filed at all
# Basic-only concepts in EDGAR_CONCEPT_FALLBACKS["eps_diluted"].
_BASIC_EPS_CONCEPTS = frozenset({
    "EarningsPerShareBasic",
    "NetIncomeLossPerOutstandingLimitedPartnershipUnitBasicNetOfTax",
})


def _ttm(series, offset=0):
    """Sum of four consecutive quarters, most recent first, or None.

    offset=4 gives the four quarters before those, which is what a
    year-over-year comparison needs on the same basis."""
    if not series or len(series) < offset + _TTM_QUARTERS:
        return None
    window = series[offset:offset + _TTM_QUARTERS]
    try:
        return sum(float(r["val"]) for r in window)
    except (TypeError, ValueError, KeyError):
        return None


def _latest(series):
    """Most recent value in an instant series, or None."""
    if not series:
        return None
    try:
        return float(series[0]["val"])
    except (TypeError, ValueError, KeyError):
        return None


def fetch_edgar_ticker_cik_map():
    """Pull SEC's master ticker -> CIK mapping. ~14k entries, ~700KB. Cache friendly."""
    try:
        req = urllib.request.Request(EDGAR_TICKERS_URL, headers={"User-Agent": EDGAR_USER_AGENT})
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        out = {}
        for v in data.values():
            t = (v.get("ticker") or "").upper().strip()
            cik = v.get("cik_str")
            if t and cik:
                out[t] = int(cik)
        return out
    except Exception as e:
        print(f"EDGAR ticker map fetch failed: {e}")
        return {}



# --- 8-K Item 2.02 earnings press releases -----------------------------------
#
# The panel is all numbers. It has no account of what management said about the
# quarter, and no guidance at all. The EX-99.1 exhibit attached to an earnings
# 8-K is the cheapest way to fix that: it is public domain, it lands within
# hours of the call, and it is roughly a tenth the size of a 10-K.
#
# Measured on 20 large caps before building this: the exhibit was retrievable
# for 20 of 20, and 9 of 20 carried a real forward-guidance section. So it is
# reliable for reported results and management's framing, and a coin flip for
# guidance. Nothing downstream may assume guidance is present.
#
# Discovery runs off the daily index rather than per-ticker submissions. One
# 737 KB request lists every filing EDGAR received that day, so a run costs
# 1 + N + M requests (N = 8-Ks matching our universe, M = those carrying item
# 2.02) instead of one request per ticker. At ~5,300 tickers that is the
# difference between ~40 seconds and ~33 minutes.
EDGAR_DAILY_INDEX_URL = ("https://www.sec.gov/Archives/edgar/daily-index/"
                         "{year}/QTR{qtr}/form.{ymd}.idx")
EDGAR_ARCHIVE_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{nod}/{name}"
EDGAR_HEADER_URL = EDGAR_ARCHIVE_URL.format(cik="{cik}", nod="{nod}",
                                            name="{acc}-index-headers.html")
_FILING_MIN_BODY = 500          # a shorter body is EDGAR returning nothing
_FILING_MAX_TEXT = 400_000      # guards against an 8 MB filing with inline exhibits
_FILINGS_TIME_BUDGET_S = 900
# Four days so a weekend, a holiday, or a dropped cron slot is caught up rather
# than lost. Already-recorded accessions are skipped before any request is made,
# so in steady state the extra days cost one index fetch each and nothing more.
FILINGS_DAYS_BACK = 4


# Fetches that failed for a reason other than "no such document". A caller that
# must tell "could not read it" from "read it, nothing there" compares this
# before and after: both cases return None.
_EDGAR_FETCH_FAILURES = [0]


def _edgar_get(url, accept=None, tries=3):
    """Throttled EDGAR fetch returning decoded bytes, or None.

    Two things this has to handle that a plain urlopen does not. Asking for
    gzip saves real bandwidth on 700 KB index files, but urllib does not
    decompress, so a caller would get 0x8b garbage. And EDGAR occasionally
    answers with an empty body and a 200, which is indistinguishable from a
    genuinely absent document unless the length is checked."""
    headers = {"User-Agent": EDGAR_USER_AGENT, "Accept-Encoding": "gzip, deflate"}
    if accept:
        headers["Accept"] = accept
    for attempt in range(tries):
        _sec_throttle()
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=25) as resp:
                data = resp.read()
                encoding = (resp.headers.get("Content-Encoding") or "").lower()
            if encoding == "gzip":
                data = gzip.decompress(data)
            elif encoding == "deflate":
                data = zlib.decompress(data, -zlib.MAX_WBITS)
            if len(data) < _FILING_MIN_BODY:
                raise ValueError(f"short body {len(data)}B")
            return data
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None          # weekend, holiday, or no such document
            if attempt == tries - 1:
                _EDGAR_FETCH_FAILURES[0] += 1
                return None
        except Exception:
            if attempt == tries - 1:
                _EDGAR_FETCH_FAILURES[0] += 1
                return None
        time.sleep(1.0 * (3 ** attempt))
    _EDGAR_FETCH_FAILURES[0] += 1
    return None


def _filing_to_text(raw, limit=None):
    """Strip a filing document to readable text.

    The ix:header removal is not optional. An inline-XBRL filing carries a
    couple of KB of taxonomy context at the top, and naive tag-stripping turns
    it into a wall of 'http://fasb.org/us-gaap/2025#LongTermDebtNoncurrent'.

    `limit` overrides _FILING_MAX_TEXT for a caller that needs a section from
    deep in a long filing, which is where a 10-K keeps Item 7."""
    try:
        text = raw.decode("utf-8", "replace")
    except Exception:
        return ""
    text = re.sub(r"(?is)<ix:header.*?</ix:header>", " ", text)
    text = re.sub(r"(?is)<(script|style).*?</\1>", " ", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = _html.unescape(text)
    text = text.replace("\xa0", " ").replace("\u200b", "")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    text = text.strip()
    # EDGAR's viewer prepends its own document wrapper, which strips down to
    #   EX-99.1 / 2 / rh-20260910xex99d1.htm / EX-99.1
    # before the press release begins. It is noise in every single filing, so
    # drop it up to and including the last "Exhibit 99.x" marker near the top.
    head = text[:600]
    m = None
    for m in re.finditer(r"(?i)exhibit\s+99\.?\d*\s*", head):
        pass
    if m:
        text = text[m.end():].lstrip()
    return text[:limit or _FILING_MAX_TEXT]


def _daily_index_filings(date_iso, want_form="8-K"):
    """Every filing of one form type that EDGAR received on a date.

    Returns [(cik, accession)]. Empty on weekends and holidays, which 404.

    form.*.idx is FIXED-WIDTH, not pipe-delimited; the pipe format belongs to
    master.idx. Splitting on "|" here silently matched nothing at all, which
    looked exactly like a quiet day. Rather than parse columns, which breaks on
    form types containing spaces ("1-A POS") and on company names containing
    almost anything, both identifiers are read straight out of the archive path
    at the end of the line, where their shape is unambiguous."""
    dt = datetime.strptime(date_iso, "%Y-%m-%d")
    url = EDGAR_DAILY_INDEX_URL.format(year=dt.year, qtr=(dt.month - 1) // 3 + 1,
                                       ymd=dt.strftime("%Y%m%d"))
    raw = _edgar_get(url)
    if not raw:
        return []
    prefix = want_form.upper()
    out, seen = [], set()
    for line in raw.decode("utf-8", "replace").splitlines():
        head = line[:len(prefix) + 1].upper()
        if not head.startswith(prefix) or (len(line) > len(prefix)
                                           and not line[len(prefix)].isspace()):
            continue
        m = re.search(r"edgar/data/(\d+)/(\d{10}-\d{2}-\d{6})\.txt", line)
        if not m:
            continue
        key = m.group(2)
        if key in seen:
            continue
        seen.add(key)
        out.append((int(m.group(1)), key))
    return out


# The index-headers page writes item DESCRIPTIONS, not numbers:
#   ITEM INFORMATION:\t\tResults of Operations and Financial Condition
# Nothing on that page carries "2.02" as a string, so matching on the code
# returned no earnings filings at all while looking like a quiet fortnight.
# Only the submissions JSON exposes numeric items, and that costs one request
# per issuer, which is the cost this whole daily-index path exists to avoid.
_EDGAR_ITEM_CODES = {
    "entry into a material definitive agreement": "1.01",
    "termination of a material definitive agreement": "1.02",
    "completion of acquisition or disposition of assets": "2.01",
    "results of operations and financial condition": "2.02",
    "creation of a direct financial obligation": "2.03",
    "triggering events that accelerate": "2.04",
    "costs associated with exit or disposal activities": "2.05",
    "material impairments": "2.06",
    "notice of delisting or failure to satisfy": "3.01",
    "unregistered sales of equity securities": "3.02",
    "material modifications to rights of security holders": "3.03",
    "material modification to rights of security holders": "3.03",
    "changes in registrant's certifying accountant": "4.01",
    "non-reliance on previously issued financial statements": "4.02",
    "changes in control of registrant": "5.01",
    "departure of directors or certain officers": "5.02",
    "amendments to articles of incorporation or bylaws": "5.03",
    "amendments to the registrant's code of ethics": "5.05",
    "amendment to registrant's code of ethics": "5.05",
    "change in shell company status": "5.06",
    "submission of matters to a vote of security holders": "5.07",
    "regulation fd disclosure": "7.01",
    "other events": "8.01",
    "financial statements and exhibits": "9.01",
}
_EARNINGS_ITEM = "2.02"


def _edgar_item_codes(text):
    """Map ITEM INFORMATION descriptions to 8-K item numbers.

    Prefix matching, because SEC truncates the longer descriptions and pads
    them with tabs. An unrecognised description is kept verbatim so a new or
    renamed item shows up in the index rather than vanishing."""
    codes = []
    for desc in re.findall(r"ITEM INFORMATION:\s*([^\n<]+)", text):
        key = " ".join(desc.split()).strip().lower()
        code = next((v for k, v in _EDGAR_ITEM_CODES.items() if key.startswith(k)), None)
        codes.append(code or key.replace(",", " ")[:40].strip())
    return ",".join(sorted(set(codes)))


def _filing_header(cik, accession):
    """Read one filing's SGML header. Returns (items, exhibit_filename).

    The header page HTML-escapes its own SGML, so <TYPE> arrives as &lt;TYPE&gt;
    and nothing matches until it is unescaped. That is the single most likely
    thing to silently break here."""
    nod = accession.replace("-", "")
    raw = _edgar_get(EDGAR_HEADER_URL.format(cik=cik, nod=nod, acc=accession))
    if not raw:
        return "", None
    text = _html.unescape(raw.decode("utf-8", "replace"))
    items = _edgar_item_codes(text)
    exhibit = None
    docs = re.findall(r"<TYPE>([^<\s]+).*?<FILENAME>([^<\s]+)", text, re.S)
    for want in ("EX-99.1", "EX-99"):
        for doc_type, filename in docs:
            if doc_type.upper() == want and filename.lower().endswith((".htm", ".html", ".txt")):
                exhibit = filename
                break
        if exhibit:
            break
    return items, exhibit


# --- segment notes --------------------------------------------------------
#
# Three of the four theses written on 2026-09-12 named the segment split as the
# thing they could not see. MPC's Refining / Midstream / Retail mix IS the
# thesis on MPC; AAPL's Products versus Services mix is most of the long-run
# story. The panel has no segment data and cannot get any: companyfacts returns
# consolidated figures only, with no dimensions on the facts.
#
# It is in the filing, though, in the rendered R-files. Every XBRL filing ships
# a FilingSummary.xml listing one report per statement and note, and the segment
# note is one of them. Confirmed against MPC, AAPL, JPM and KO.
#
# MPC 10-Q 2026-08-04, from R75: Refining & Marketing 49,299, Midstream 1,455,
# Renewable Diesel 1,240, against a 51,994 total. That is the number the MPC
# note said was unobtainable.
_SEGMENT_MIN_CHARS = 600
_SEGMENT_MAX_CHARS = 30000


def _segment_note_file(cik, accession):
    """The segment note's R-file, from the filing's own report index.

    Matching on the word "segment" alone is not enough. JPM's filing carries
    "Loans - By Portfolio Segment" and "Goodwill by Business Segment", neither
    of which is the segment note. The note proper has no parenthetical suffix;
    the (Tables), (Policies) and (Details) variants are fragments of it."""
    nod = accession.replace("-", "")
    raw = _edgar_get(f"https://www.sec.gov/Archives/edgar/data/{cik}/{nod}/FilingSummary.xml")
    if not raw:
        return None, None
    xml = raw.decode("utf-8", "replace")
    best = None
    for block in re.findall(r"<Report[^>]*>(.*?)</Report>", xml, re.S):
        nm = re.search(r"<ShortName>(.*?)</ShortName>", block, re.S)
        fn = re.search(r"<HtmlFileName>(.*?)</HtmlFileName>", block, re.S)
        if not (nm and fn):
            continue
        name = _html.unescape(nm.group(1)).strip()
        low = name.lower()
        if "(" in name:                      # (Tables) / (Policies) / (Details)
            continue
        if re.match(r"^(business )?(operating )?segments?\b", low) or \
           low.startswith("segment information") or "disaggregat" in low:
            best = (name, fn.group(1).strip())
            break
    return best if best else (None, None)


def _fetch_segment_note(cik, ticker, rec=None):
    """Latest 10-Q or 10-K segment note as text.

    Returns (accession, form, filing_date, name, text). The date is the periodic
    filing's own, not the date of the 8-K that triggered the fetch: the note
    describes the quarter the 10-Q covers, and stamping it with today's date
    would make a note from August read as current on the page.

    `rec` is the submissions listing when the caller already holds it."""
    if rec is None:
        rec = _submissions_recent(cik)
    if not rec:
        return None
    forms, accs = rec.get("form", []), rec.get("accessionNumber", [])
    dates = rec.get("filingDate", [])
    for i, f in enumerate(forms):
        if f not in ("10-Q", "10-K"):
            continue
        name, fname = _segment_note_file(cik, accs[i])
        if not fname:
            return None                      # newest periodic filing has no note
        nod = accs[i].replace("-", "")
        doc = _edgar_get(EDGAR_ARCHIVE_URL.format(cik=cik, nod=nod, name=fname))
        if not doc:
            return None
        text = _filing_to_text(doc)
        # The R-file viewer prepends its own banner: "XML 48 R21.htm IDEA: XBRL
        # DOCUMENT v3.26.1". Present on every one of them and carries nothing.
        text = re.sub(r"^\s*XML\s+\d+\s+R\d+\.htm\s+IDEA:\s*XBRL DOCUMENT\s*\S*\s*", "",
                      text, count=1)
        text = text[:_SEGMENT_MAX_CHARS]
        if len(text) < _SEGMENT_MIN_CHARS:
            return None
        return accs[i], f, (dates[i] if i < len(dates) else ""), name, text
    return None


# --- 10-K narrative items -------------------------------------------------
#
# Item 1 is what the company says it does; Item 1A is what it says could go
# wrong. Neither is in the panel and neither can be inferred from it.
#
# Boundaries are found by taking the LAST match of each item header. The first
# matches are the table of contents: on AAPL's 10-K the TOC hits cluster around
# offset 19,100 while the body's Item 1 begins at 22,374. Measured on 25 filers,
# Item 1A extracted cleanly for 24. Item 7 managed 18, and its failures are not
# a parsing problem: APH and MOS incorporate MD&A by reference to an exhibit, so
# there is nothing in the 10-K to extract. Item 7 is therefore not collected on
# the earnings path. The reading pack further down does collect management's
# discussion, for the few names the analyst is about to write up, and says why.
# Management's discussion is kept nearly whole. At 120,000 characters CF's was cut
# mid-sentence in the paragraph on its senior notes, which is where the total it
# owes is stated, and the note written from it had to say it did not know.
_ITEM_MAX = {"business": 26000, "risk_factors": 42000,
             "mdna_10q": 200000, "mdna_10k": 200000}
_ITEM_MIN = 1500
# A real section runs at least this far before the next item heading. Table of
# contents entries are a few hundred characters apart at most.
_ITEM_SECTION_MIN = 3000
# See _extract_item. The widest table-of-contents gap measured was 104 characters.
_ITEM_TOC_GAP = 300
_ITEM_INTRO_NAMES_NEXT = {"mdna_10q", "mdna_10k"}
# Both patterns key on the "Item 1." / "Item 1A." prefix, which is a convention
# rather than a requirement: a filer may organise the 10-K however it likes so
# long as a cross-reference index maps the items. Intel does exactly that, using
# named headings ("Risk Factors" with no item number) and an index at the back,
# and this extractor returns nothing for it.
#
# Measured before deciding not to handle it: in a random sample of 45 filers over
# $1B, 39 file a 10-K and all 39 use the item prefix. The other 6 file 20-F or are
# funds with no 10-K at all. Matching bare headings as a fallback would mean
# matching the phrase "Risk Factors", which appears 22 times in Intel's own
# filing, mostly as prose. A ticker with no items collected is reported as such
# in the dossier rather than filled in with a worse guess.
_ITEM_BOUNDS = {
    "business":     (r"item\s*1\s*[\.\:\-–—]?\s*business",
                     r"item\s*1a\s*[\.\:\-–—]?\s*risk\s*factors"),
    "risk_factors": (r"item\s*1a\s*[\.\:\-–—]?\s*risk\s*factors",
                     r"item\s*(1b|2)\s*[\.\:\-–—]?\s*(unresolved|propert)"),
    # Management's discussion. Part I Item 2 of a 10-Q, Item 7 of a 10-K. The
    # apostrophe is matched loosely because filers use ', ’ and nothing at all.
    "mdna_10q":     (r"item\s*2\s*[\.\:\-–—]?\s*management.{0,3}s\s*discussion",
                     r"item\s*3\s*[\.\:\-–—]?\s*quantitative"),
    "mdna_10k":     (r"item\s*7\s*[\.\:\-–—]?\s*management.{0,3}s\s*discussion",
                     r"item\s*7a\s*[\.\:\-–—]?\s*quantitative"
                     r"|item\s*8\s*[\.\:\-–—]?\s*financial\s*statements"),
}


# A heading that is being *named* rather than *opened* is preceded by one of
# these within a clause. "as described in Item 1A. Risk Factors" is a pointer;
# a real heading follows the end of the previous section.
_ITEM_XREF_LEAD = re.compile(
    r"\b(?:see|in|under|within|refer|referred|reference|described|discussed|"
    r"included|contained|set forth|pursuant to)\b[^.]{0,40}$", re.I)
# "Part I" is deliberately not in that list. It reads like a cross-reference
# lead ("described in Part II, Item 7") but it is also what immediately precedes
# every genuine Item 1 heading, and excluding it dropped Item 1 on 9 of 10 test
# filers while changing no Item 1A result.
# ...and is followed by prose that continues the sentence: a closing quote, a
# comma, or another item reference a few words later.
_ITEM_XREF_TRAIL = re.compile(r"\bItems?\s+\d", re.I)
_ITEM_TRAIL_PUNCT = '\u201d"\'\u2019,;)'


def _opens_a_section(text, start, end):
    """True when the heading matched at [start:end] begins a section.

    The same words appear three ways in a filing: in the table of contents, as
    the actual heading, and as a cross-reference from other sections. The span
    minimum removes the first. This removes the third, which is what put KO's
    Item 1A at a forward-looking-statements paragraph ('Item 1A. Risk Factors"
    and elsewhere in this report') and MPC's at a list of pointers ('Item 1A.
    Risk Factors, Item 3. Legal Proceedings, Item 7. ...')."""
    if _ITEM_XREF_LEAD.search(text[max(0, start - 60):start]):
        return False
    after = text[end:end + 200]
    if after.lstrip()[:1] in _ITEM_TRAIL_PUNCT:
        return False
    return not _ITEM_XREF_TRAIL.search(after)


def _extract_item(text, kind):
    """Slice one 10-K item out of stripped filing text, or None."""
    start_pat, end_pat = _ITEM_BOUNDS[kind]
    starts = [(m.start(), m.end()) for m in re.finditer(start_pat, text, re.I)]
    if not starts:
        return None
    ends = [m.start() for m in re.finditer(end_pat, text, re.I)]
    if not ends:
        return None

    # Take the EARLIEST candidate that opens a substantial section, not the
    # widest. Table-of-contents entries sit a few characters from the next
    # heading and are removed by the minimum; the real heading is the first one
    # left that survives the cross-reference test. Choosing the widest span
    # instead picked up cross-references late in the filing, because a reference
    # followed by a distant boundary measures enormous: MPC opened on "Item 1.
    # Business - Regulatory Matters for additional information" and KO on
    # 'Item 1. Business" of this report'.
    best = best_len = None
    for st, en in starts:
        after = [e for e in ends if e > st]
        if not after:
            continue
        if kind in _ITEM_INTRO_NAMES_NEXT and after[0] - st >= _ITEM_TOC_GAP:
            # Management's discussion opens with a forward-looking-statements
            # paragraph, and MPC's names the next item in it: "particularly Item 2.
            # ... and Item 3. Quantitative and Qualitative Disclosures", 555
            # characters in. Read as the end of the section, that left nothing. A
            # table-of-contents entry has its next item within a line, so a mention
            # further off than that but too near to close a real section is prose.
            after = [e for e in after if e - st >= _ITEM_SECTION_MIN]
            if not after:
                continue
        span = after[0] - st
        if span >= _ITEM_SECTION_MIN and _opens_a_section(text, st, en):
            best, best_len = st, span
            break
    if best is None:
        return None
    body = text[best:best + min(best_len, _ITEM_MAX[kind])].strip()
    return body if len(body) >= _ITEM_MIN else None


def _submissions_recent(cik):
    """A company's recent filings from the submissions API, newest first, or None."""
    raw = _edgar_get(EDGAR_SUBMISSIONS_URL.format(cik=int(cik)))
    if not raw:
        return None
    try:
        return json.loads(raw).get("filings", {}).get("recent", {})
    except Exception:
        return None


def fetch_10k_items(cik, ticker, rec=None):
    """Item 1 and Item 1A from the latest 10-K. Returns [(kind, accession, filed, text)].

    `rec` is the submissions listing when the caller already holds it."""
    if rec is None:
        rec = _submissions_recent(cik)
    if not rec:
        return []
    forms = rec.get("form", [])
    idx = next((i for i, f in enumerate(forms) if f == "10-K"), None)
    if idx is None:
        return []
    acc = rec["accessionNumber"][idx]
    filed = rec["filingDate"][idx]
    doc = rec.get("primaryDocument", [None] * len(forms))[idx]
    if not doc:
        return []
    raw_doc = _edgar_get(EDGAR_ARCHIVE_URL.format(cik=cik, nod=acc.replace("-", ""), name=doc))
    if not raw_doc:
        return []
    text = _filing_to_text(raw_doc)
    out = []
    for kind in ("business", "risk_factors"):
        body = _extract_item(text, kind)
        if body:
            out.append((kind, acc, filed, body))
    return out


# --- the reading pack -------------------------------------------------------
#
# Everything above is collected when a company reports, forward from 2026-09-12.
# That leaves the analyst blind on any name that last reported before then, which
# a week later was most of them. CF's note said it had no fertiliser or gas
# prices and nothing from management about buying back shares. CF's 10-Q of
# 2026-08-06 prints the selling price per ton, the gas cost per MMBtu and the
# shares bought back in the quarter. None of it had been fetched.
#
# So the names the analyst is about to be handed are topped up here: the latest
# earnings release however old, management's discussion from the latest periodic
# filing, and the 10-K items and segment note where the earnings path never ran.
# A dozen names a day, and a name already held costs one request to confirm.
#
# Item 7 was measured at 18 of 25 above and left out. It is collected here for
# two reasons. The 10-Q's Item 2 is the source three quarters of the year and is
# almost never incorporated by reference. And a miss costs nothing: the dossier
# says the discussion is absent, which is what it said for every name before.
_PACK_TIME_BUDGET_S = 600
_PACK_SCREEN_SLOTS = 12        # the analyst takes 4; the rest cover a changed panel
_PACK_PERIODIC_TEXT = 1_500_000
_PACK_STATE = STATE_DIR / "reading_pack.json"


def fetch_mdna(cik, rec):
    """Management's discussion from the newest periodic filing that yields one.

    Returns (accession, form, filed, text), or (None, tried) where `tried` lists
    the accessions that were read and gave nothing, so the caller can avoid
    downloading the same multi-megabyte filing again tomorrow. Only the two
    newest filings are tried: anything older describes a different year."""
    forms = rec.get("form", [])
    docs = rec.get("primaryDocument") or [None] * len(forms)
    tried = []
    for i, f in enumerate(forms):
        if f not in ("10-Q", "10-K"):
            continue
        if len(tried) == 2:
            break
        acc = rec["accessionNumber"][i]
        tried.append(acc)
        if not docs[i]:
            continue
        raw_doc = _edgar_get(EDGAR_ARCHIVE_URL.format(cik=int(cik), nod=acc.replace("-", ""),
                                                      name=docs[i]))
        if not raw_doc:
            continue
        body = _extract_item(_filing_to_text(raw_doc, limit=_PACK_PERIODIC_TEXT),
                             "mdna_10q" if f == "10-Q" else "mdna_10k")
        if body:
            return (acc, f, rec["filingDate"][i], body), tried
    return None, tried


def fetch_latest_earnings_release(cik, rec):
    """The newest 8-K carrying item 2.02, however old.

    Returns (accession, filed, items, exhibit, text) or None. The earnings path
    reads the daily index and so only ever sees the last few days."""
    forms = rec.get("form", [])
    items = rec.get("items") or [""] * len(forms)
    tried = 0
    for i, f in enumerate(forms):
        if f != "8-K" or _EARNINGS_ITEM not in (items[i] or ""):
            continue
        tried += 1
        if tried > 2:
            break
        acc = rec["accessionNumber"][i]
        hdr_items, exhibit = _filing_header(int(cik), acc)
        if not exhibit:
            continue
        doc = _edgar_get(EDGAR_ARCHIVE_URL.format(cik=int(cik), nod=acc.replace("-", ""),
                                                  name=exhibit))
        if not doc:
            continue
        text = _filing_to_text(doc)
        if len(text) < 400:
            continue
        return acc, rec["filingDate"][i], hdr_items or items[i], exhibit, text
    return None


_PACK_MISS_DAYS = 14          # a recorded miss is asked again after this long


def _filing_accessions_held():
    """Every (ticker, accession) in every month's index.

    With the ticker, because two share classes share one CIK and so one set of
    accessions: Alphabet's documents filed under GOOGL would otherwise read as
    held for GOOG, whose dossier looks its documents up by ticker and finds none.

    Every month, because the index is filed under the month a document was
    recorded, and a 10-K collected in September is still the current 10-K in
    January. filing_accessions_recorded reads one month and would miss it."""
    accessions = set()
    for path in sorted(FILINGS_CSV_DIR.glob("*.csv")):
        try:
            with path.open(encoding="utf-8", newline="") as fh:
                accessions |= {(r.get("ticker", ""), r.get("accession", ""))
                               for r in csv.DictReader(fh)}
        except Exception as exc:
            print(f"pack: could not read {path.name} ({type(exc).__name__}: {exc}).")
    return accessions


def _director_plan_tickers(today=None):
    """Tickers assigned by any director plan whose week has not ended, in plan
    order. Empty when there is no plan, so the pack is then exactly as before."""
    today = today or datetime.now(tz=EASTERN).date()
    folder = THESES_DIR / "director"
    if not folder.is_dir():
        return []
    since = (today - timedelta(days=7)).isoformat()
    out = []
    for p in sorted(folder.glob("*.md")):
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}\.md", p.name) or p.stem < since:
            continue
        try:
            head = p.read_text(encoding="utf-8").split("\n---", 1)[0]
        except OSError:
            continue
        for line in head.splitlines():
            m = re.match(r"^\s*-\s+(\{.*\})\s*$", line)
            a = _plan_flow_map(m.group(1)) if m else None
            if a and a.get("ticker"):
                out.append(a["ticker"].strip().upper())
    return out


def reading_pack_tickers(stocks=None):
    """The names the analyst is likely to be handed next, and every name it has written up.

    The screen is run as the analyst's own prepare.py runs it, asked for more
    slots than the analyst takes: it reads the pushed panel, which during a daily
    run is one session behind the row being written, so the four it names today
    are not always the four it names on Monday.

    Given today's universe, non-operating listings are left out. A note ticker
    maps to its parent's CIK, so a pack for DUKU would file Duke Energy's 10-K
    and financial history under the note's symbol."""
    tickers = []
    wl = THESES_DIR / "watchlist.txt"
    if wl.exists():
        for line in wl.read_text(encoding="utf-8").splitlines():
            line = line.split("#", 1)[0].strip().upper()
            if line:
                tickers.append(line)
    # The Research Director's assignments for this week and next: the screen
    # cannot see many of them (a bank, a company with no revenue), so without
    # this they would reach the analyst with no filing text at all.
    tickers += _director_plan_tickers()
    notes = THESES_DIR / "notes"
    if notes.is_dir():
        tickers += sorted(p.name for p in notes.iterdir() if p.is_dir())
    try:
        import sys
        out = subprocess.run([sys.executable, str(THESES_DIR / "bin" / "screen.py")],
                             capture_output=True, text=True, timeout=300,
                             env={**os.environ, "THESES_SLOTS": str(_PACK_SCREEN_SLOTS)})
        if out.returncode == 0:
            tickers += [s["ticker"] for s in json.loads(out.stdout).get("slots", [])]
        else:
            print(f"pack: the screen failed, so only the watchlist and covered names are "
                  f"topped up. {out.stderr.strip()[-300:]}")
    except Exception as exc:
        print(f"pack: could not run the screen ({type(exc).__name__}: {exc}).")
    labels = {s.get("ticker"): s.get("security_type") for s in (stocks or [])}
    seen, ordered, skipped = set(), [], []
    for t in tickers:
        if t and t not in seen:
            seen.add(t)
            if not sectype.is_operating(labels.get(t)):
                skipped.append(t)
                continue
            ordered.append(t)
    if skipped:
        print(f"pack: skipped {len(skipped)} non-operating listing(s): {', '.join(skipped)}.")
    return ordered


def collect_reading_packs(tickers, ticker_to_cik, budget_s=_PACK_TIME_BUDGET_S):
    """Top up the filing text held for `tickers`. Returns index rows for record_filings."""
    started = time.time()
    accessions = _filing_accessions_held()
    try:
        state = json.loads(_PACK_STATE.read_text(encoding="utf-8"))
    except Exception:
        state = {}
    misses = state.setdefault("misses", {})
    today = datetime.now(tz=timezone.utc).date().isoformat()
    # A miss is only believed for a while. One that was recorded wrongly, or for a
    # filing whose layout a later fix can read, heals itself.
    miss_cutoff = (datetime.now(tz=timezone.utc).date()
                   - timedelta(days=_PACK_MISS_DAYS)).isoformat()
    entries, checked = [], 0

    def keep(ticker, cik, rel, text, row):
        path = FILINGS_CSV_DIR / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        accessions.add((ticker, row["accession"]))
        entries.append({"ticker": ticker, "cik": int(cik), "exhibit": "", "text_path": rel,
                        "text_chars": len(text), **row})

    for ticker in tickers:
        if time.time() - started > budget_s:
            print(f"pack: time budget reached after {checked} names; the rest wait a day.")
            break
        cik = ticker_to_cik.get(ticker) or ticker_to_cik.get(ticker.replace(".", "-"))
        if not cik:
            continue
        rec = _submissions_recent(cik)
        if not rec:
            continue
        checked += 1
        forms, accs = rec.get("form", []), rec.get("accessionNumber", [])
        items = rec.get("items") or [""] * len(forms)
        periodic = [a for i, a in enumerate(accs) if forms[i] in ("10-Q", "10-K")]
        annual = [a for i, a in enumerate(accs) if forms[i] == "10-K"]
        releases = [a for i, a in enumerate(accs)
                    if forms[i] == "8-K" and _EARNINGS_ITEM in (items[i] or "")]
        # A key is settled once it is held or once it was read and gave nothing.
        # A filing never changes, so neither answer is worth asking for twice.
        settled = lambda key: ((ticker, key) in accessions
                               or misses.get(key, "") >= miss_cutoff)
        # A miss means the filing was READ and had nothing usable. A download that
        # failed says nothing about the filing, and recording it as a miss let one
        # bad ten minutes at www.sec.gov suppress a company's whole pack for good.
        failures = lambda: _EDGAR_FETCH_FAILURES[0]

        # Management's discussion, from the two newest periodic filings.
        if periodic and not settled(f"{periodic[0]}-mdna"):
            before = failures()
            got, tried = fetch_mdna(cik, rec)
            # The older of the two may already be held: it is what the dossier has
            # been showing while the newest one would not extract.
            if got and (ticker, f"{got[0]}-mdna") not in accessions:
                acc, form, filed, text = got
                keep(ticker, cik, f"text/{ticker}/{acc}-mdna.txt", text,
                     {"filed": filed, "form": form, "doc_kind": "mdna",
                      "items": "2" if form == "10-Q" else "7", "accession": f"{acc}-mdna"})
            if failures() == before:
                for a in tried:
                    if not got or a != got[0]:
                        misses[f"{a}-mdna"] = today

        # The latest earnings release, however long ago it was filed. The earnings
        # path stores a release under its bare accession, so that is the key.
        if releases and not settled(releases[0]):
            before = failures()
            got = fetch_latest_earnings_release(cik, rec)
            if got and (ticker, got[0]) not in accessions:
                acc, filed, r_items, exhibit, text = got
                keep(ticker, cik, f"text/{ticker}/{acc}.txt", text,
                     {"filed": filed, "form": "8-K", "doc_kind": "earnings_release",
                      "items": r_items, "accession": acc, "exhibit": exhibit})
            if (not got or got[0] != releases[0]) and failures() == before:
                misses[releases[0]] = today

        # The 10-K items and the segment note, where the earnings path never ran.
        if annual and not (settled(f"{annual[0]}-business")
                           or settled(f"{annual[0]}-risk_factors")):
            before = failures()
            found = fetch_10k_items(cik, ticker, rec=rec)
            for kind, k_acc, k_filed, k_text in found:
                if (ticker, f"{k_acc}-{kind}") not in accessions:
                    keep(ticker, cik, f"text/{ticker}/{k_acc}-{kind}.txt", k_text,
                         {"filed": k_filed, "form": "10-K", "doc_kind": kind,
                          "items": "1" if kind == "business" else "1A",
                          "accession": f"{k_acc}-{kind}"})
            if not found and failures() == before:
                misses[f"{annual[0]}-business"] = today
        if periodic and not (settled(periodic[0]) or settled(f"{periodic[0]}-segment")):
            before = failures()
            seg = _fetch_segment_note(cik, ticker, rec=rec)
            if seg and (ticker, seg[0]) not in accessions:
                s_acc, s_form, s_filed, s_name, s_text = seg
                keep(ticker, cik, f"text/{ticker}/{s_acc}-segment.txt", s_text,
                     {"filed": s_filed, "form": s_form, "doc_kind": "segment_note",
                      "items": s_name[:60], "accession": s_acc})
            if not seg and failures() == before:
                misses[f"{periodic[0]}-segment"] = today

    try:
        _PACK_STATE.parent.mkdir(parents=True, exist_ok=True)
        _PACK_STATE.write_text(json.dumps(state, indent=1, sort_keys=True), encoding="utf-8")
    except Exception as exc:
        print(f"pack: could not save {_PACK_STATE.name} ({type(exc).__name__}: {exc}).")
    kinds = {}
    for e in entries:
        kinds[e["doc_kind"]] = kinds.get(e["doc_kind"], 0) + 1
    detail = ", ".join(f"{v} {k}" for k, v in sorted(kinds.items())) or "nothing new"
    print(f"pack: {detail} for {checked} of {len(tickers)} names, "
          f"{time.time() - started:.0f}s.")
    return entries


# --- reported history -----------------------------------------------------
#
# companyfacts carries every XBRL fact a company has filed, back to 2009 for
# most. Consolidated only, with no dimensions, which is why segments come from
# the R-files instead. But for a straight history of revenue, earnings and cash
# flow it is complete and free.
#
# Two series are kept. ANNUAL is the useful one: there is no missing Q4, because
# the 10-K reports the full year, and cash flow is not year-to-date at that
# duration so OCF and capex arrive complete. QUARTERLY is denser but has holes,
# since Q4 is never filed as a quarter and 10-Q cash flows are cumulative.
_FIN_CONCEPTS = {
    "revenue":          ["RevenueFromContractWithCustomerExcludingAssessedTax",
                         "Revenues", "SalesRevenueNet"],
    "gross_profit":     ["GrossProfit"],
    "operating_income": ["OperatingIncomeLoss"],
    # CF has not tagged NetIncomeLoss since 2011 and every year after it came back
    # blank. It tags the figure for common shareholders instead. ProfitLoss is
    # last because it includes the share of profit owed to minority partners,
    # which for CF is a fifth of the total.
    "net_income":       ["NetIncomeLoss", "NetIncomeLossAvailableToCommonStockholdersBasic",
                         "ProfitLoss"],
    "eps_diluted":      ["EarningsPerShareDiluted"],
    "ocf":              ["NetCashProvidedByUsedInOperatingActivities",
                         "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"],
    # The same tags, in the same order, as the panel's trailing free cash flow
    # (EDGAR_CONCEPT_FALLBACKS["capex"]), so a year's operating cash flow less
    # capex here is built the way ttm_fcf is. The first tag alone left capex
    # blank on all twelve NVDA fiscal years: NVIDIA files its "purchases related
    # to property and equipment and intangible assets" as
    # PaymentsToAcquireProductiveAssets, which also counts intangible assets.
    "capex":            list(EDGAR_CONCEPT_FALLBACKS["capex"]),
    "assets":           ["Assets"],
    "equity":           ["StockholdersEquity"],
    "shares_diluted":   ["WeightedAverageNumberOfDilutedSharesOutstanding"],
}
_FIN_FLOW = {"revenue", "gross_profit", "operating_income", "net_income",
             "eps_diluted", "ocf", "capex", "shares_diluted"}
_FIN_ANNUAL = (350, 380, 12)
_FIN_QUARTERLY = (80, 100, 20)


def _fin_series(facts, metric, lo, hi, forms=None):
    """One metric as {period_end: (value, tag)}.

    `forms`, when given, is a tuple of form-type prefixes a fact must come from
    ("10-K" matches 10-K, 10-K/A and 10-KT).

    Earlier tags in the list win outright and a later tag only fills a period the
    earlier one left empty, so revenue spanning the ASC 606 tag change keeps its
    pre-2018 history instead of starting when the new tag does. Within one tag
    the latest filing wins, which is how a restatement is picked up. The tag is
    returned so a reader can see where the seam is."""
    out = {}
    for tag in _FIN_CONCEPTS[metric]:
        node = facts.get(tag)
        if not node:
            continue
        for rows in node.get("units", {}).values():
            for f in rows:
                end, val, filed = f.get("end"), f.get("val"), f.get("filed", "")
                if end is None or val is None:
                    continue
                if forms and not str(f.get("form") or "").startswith(forms):
                    continue
                if metric in _FIN_FLOW:
                    start = f.get("start")
                    if not start:
                        continue
                    try:
                        days = (datetime.strptime(end, "%Y-%m-%d")
                                - datetime.strptime(start, "%Y-%m-%d")).days
                    except ValueError:
                        continue
                    if not (lo <= days <= hi):
                        continue
                prev = out.get(end)
                if prev is None or (prev[2] == tag and filed >= prev[1]):
                    out[end] = (val, filed, tag)
    return {k: (v[0], v[2]) for k, v in out.items()}


def _fin_periods(facts, lo, hi, n):
    """Periods keyed on FLOW metrics, with balance-sheet items attached after.

    Keying on everything lets assets and equity, which are filed every quarter,
    occupy all the slots in an annual series and push the annual revenue out of
    its own table."""
    per = {}
    for metric, tags in _FIN_CONCEPTS.items():
        if metric not in _FIN_FLOW:
            continue
        for end, (val, tag) in _fin_series(facts, metric, lo, hi).items():
            per.setdefault(end, {})[metric] = val
            if metric == "revenue":
                per[end]["revenue_tag"] = tag
    ends = sorted(per, reverse=True)[:n]
    for metric in ("assets", "equity"):
        pit = _fin_series(facts, metric, lo, hi)
        for e in ends:
            if e in pit:
                per[e][metric] = pit[e][0]
    return [{"period_end": e, **per[e]} for e in sorted(ends)]


def fetch_financial_history(cik, ticker):
    """Annual and quarterly reported history. Returns a list of CSV rows."""
    raw = _edgar_get(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{int(cik):010d}.json")
    if not raw:
        return []
    try:
        facts = json.loads(raw).get("facts", {}).get("us-gaap", {})
    except Exception:
        return []
    if not facts:
        return []
    rows = []
    for label, (lo, hi, n) in (("FY", _FIN_ANNUAL), ("Q", _FIN_QUARTERLY)):
        for r in _fin_periods(facts, lo, hi, n):
            rows.append({"ticker": ticker, "cik": int(cik), "period": label, **r})
    return rows


def financials_tickers_held():
    """Tickers with any reported period on file."""
    path = FINANCIALS_CSV_DIR / "reported.csv"
    if not path.exists():
        return set()
    try:
        with path.open(encoding="utf-8", newline="") as fh:
            return {r.get("ticker", "") for r in csv.DictReader(fh)}
    except Exception as exc:
        print(f"csv: could not read reported.csv ({type(exc).__name__}: {exc}).")
        return set()


_FIN_VALUE_COLUMNS = [c for c in FINANCIAL_COLUMNS
                      if c not in ("ticker", "cik", "period", "period_end", "collected_at")]


def record_financials(rows, observed_at):
    """Append reported periods not already held, and fill blanks in held ones.

    Dedupe is on ticker+period+end. A period already held is appended again only
    when this fetch has a value for a column the held row left blank, and the
    new row is the held row with those blanks filled: nothing already recorded
    changes, so a restatement cannot creep in this way. Readers take the last
    row for a period (theses/bin/dossier.py reported_history does).

    Without this, a column that a collection fix starts to fill reaches only
    periods reported after the fix. capex was blank on every NVDA fiscal year
    for want of a tag, and dedupe alone would have kept those years blank for
    good. Appending rather than rewriting keeps the file append-only, which is
    what makes merge=union in .gitattributes safe: a rewrite merged against
    another run's append keeps both copies of every line."""
    if not rows:
        return 0
    path = FINANCIALS_CSV_DIR / "reported.csv"
    held = {}
    if path.exists():
        try:
            with path.open(encoding="utf-8", newline="") as fh:
                for r in csv.DictReader(fh):
                    key = (r.get("ticker"), r.get("period"), r.get("period_end"))
                    # A later row for the same period supersedes an earlier one.
                    held[key] = {**held.get(key, {}),
                                 **{k: v for k, v in r.items() if k and v not in ("", None)}}
        except Exception as exc:
            print(f"csv: could not read reported.csv for dedupe ({exc}); appending all.")
    fresh, filled = [], 0
    for r in rows:
        key = (r["ticker"], r["period"], r["period_end"])
        have = held.get(key)
        if have is None:
            held[key] = dict(r)
            fresh.append({**r, "collected_at": observed_at})
            continue
        gaps = {c: r[c] for c in _FIN_VALUE_COLUMNS
                if have.get(c) in ("", None) and r.get(c) not in ("", None)}
        if not gaps:
            continue
        merged = {**have, **gaps}
        held[key] = merged
        fresh.append({**merged, "collected_at": observed_at})
        filled += 1
    n = _append_csv(path, FINANCIAL_COLUMNS, fresh)
    if n:
        print(f"csv: appended {n} reported periods to data/financials/reported.csv"
              + (f", {filled} of them filling blanks in periods already held." if filled else "."))
    return n


# ── Multi-year history for the style books ───────────────────────────────────
#
# The style books score growth over three fiscal years, which needs four annual
# periods for every company, not only the reading-pack names reported.csv
# carries. enrich_with_edgar already holds each company's companyfacts in hand,
# so the periods are read from there at no extra request and the result goes to
# its own append-only file, data/financials/style_history.csv (one row per company
# per latest fiscal year). A separate file rather than new panel columns: the
# panel's header must not widen while runs append to it concurrently.
STYLE_HISTORY_CSV = FINANCIALS_CSV_DIR / "style_history.csv"
_STYLE_HISTORY_METRICS = ("revenue", "net_income", "shares_diluted", "ocf")


def _annual_10k_periods(facts):
    """Annual periods (350 to 380 days) from 10-K facts only: [{period_end, revenue,
    net_income, shares_diluted, ocf}], oldest first. A 20-F or 40-F filer's facts
    are not used."""
    usg = (facts or {}).get("us-gaap") or {}
    per = {}
    for metric in _STYLE_HISTORY_METRICS:
        for end, (val, _tag) in _fin_series(usg, metric, _FIN_ANNUAL[0], _FIN_ANNUAL[1],
                                            forms=("10-K",)).items():
            per.setdefault(end, {})[metric] = val
    return [{"period_end": e, **per[e]} for e in sorted(per)]


def _annual_form(facts):
    """The form type of the latest annual revenue fact under any form, so a 20-F or
    40-F filer can be told apart from a 10-K filer. "" when there is none."""
    usg = (facts or {}).get("us-gaap") or {}
    best = ("", "", "")
    for tag in _FIN_CONCEPTS["revenue"]:
        for rows in ((usg.get(tag) or {}).get("units") or {}).values():
            for f in rows:
                start, end = f.get("start"), f.get("end")
                if not start or not end or f.get("val") is None:
                    continue
                try:
                    days = (datetime.strptime(end, "%Y-%m-%d")
                            - datetime.strptime(start, "%Y-%m-%d")).days
                except ValueError:
                    continue
                if _FIN_ANNUAL[0] <= days <= _FIN_ANNUAL[1]:
                    key = (end, f.get("filed") or "", str(f.get("form") or ""))
                    if key > best:
                        best = key
    return best[2]


def compute_style_history(facts, ticker, cik):
    """One style_history.csv row for a company, or None when it has no annual
    10-K period at all. The arithmetic is portfolio.engine.style_growth."""
    periods = _annual_10k_periods(facts)
    form = _annual_form(facts)
    if not periods and not form:
        return None
    g = PF.style_growth(periods)
    if not periods:
        g["note"] = "no annual 10-K periods"
    return {"ticker": ticker, "cik": int(cik), "annual_form": form, **g}


def record_style_history(rows, observed_at):
    """Append each company's row unless the last row held for it is identical.
    Readers take the last row per ticker (portfolio.engine.load_style_history)."""
    if not rows:
        return 0
    held = {}
    for r in PF.read_rows(STYLE_HISTORY_CSV):
        held[r.get("ticker")] = r
    fresh = []
    for r in rows:
        cell = {k: PF._cell(r.get(k)) for k in PF.STYLE_HISTORY_COLUMNS if k != "collected_at"}
        have = held.get(r["ticker"])
        if have and all((have.get(k) or "") == v for k, v in cell.items()):
            continue
        fresh.append({**cell, "collected_at": observed_at})
        held[r["ticker"]] = {**cell}
    n = PF.append_rows(STYLE_HISTORY_CSV, PF.STYLE_HISTORY_COLUMNS, fresh)
    if n:
        print(f"csv: appended {n} rows to data/financials/style_history.csv.")
    return n


def collect_earnings_filings(cik_to_ticker, days_back=1, budget_s=_FILINGS_TIME_BUDGET_S):
    """Harvest EX-99.1 earnings releases filed in the last `days_back` days.

    Writes one text file per filing and returns index rows for record_filings.
    Anything already in this month's index is skipped, so re-running is cheap
    and idempotent."""
    if not cik_to_ticker:
        return [], {}
    started = time.time()
    today = datetime.now(tz=timezone.utc).date()
    # Both months, because a run on the 1st looks at filings from the 28th. With
    # only the current month loaded, those would read as unrecorded and be
    # fetched and written a second time, into a second month's index.
    reported = {}          # ticker -> cik, for the financial-history pass
    already = filing_accessions_recorded(today.isoformat())
    already |= filing_accessions_recorded((today.replace(day=1) - timedelta(days=1)).isoformat())
    entries, scanned, skipped = [], 0, 0

    for back in range(days_back):
        day_date = today - timedelta(days=back)
        # EDGAR answers 503, not 404, for a date with no index, so a weekend
        # would otherwise cost a full retry cycle each. Holidays still do.
        if day_date.weekday() >= 5:
            continue
        day = day_date.isoformat()
        for cik, accession in _daily_index_filings(day):
            if time.time() - started > budget_s:
                print(f"filings: time budget reached after {scanned} headers; "
                      f"remaining filings will be picked up next run.")
                return entries, reported
            ticker = cik_to_ticker.get(cik)
            if not ticker:
                continue
            if accession in already:
                skipped += 1
                continue
            scanned += 1
            items, exhibit = _filing_header(cik, accession)
            if _EARNINGS_ITEM not in (items or "") or not exhibit:
                continue
            nod = accession.replace("-", "")
            doc = _edgar_get(EDGAR_ARCHIVE_URL.format(cik=cik, nod=nod, name=exhibit))
            if not doc:
                continue
            text = _filing_to_text(doc)
            if len(text) < 400:
                # An exhibit this short is a cover page or a failed strip, not a
                # press release. Recording it would claim coverage we lack.
                continue
            rel = f"text/{ticker}/{accession}.txt"
            out_path = FILINGS_CSV_DIR / rel
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(text, encoding="utf-8")
            entries.append({
                "filed": day, "ticker": ticker, "cik": cik, "form": "8-K",
                "doc_kind": "earnings_release",
                "items": items, "accession": accession, "exhibit": exhibit,
                "text_path": rel, "text_chars": len(text),
            })
            # A company that just reported has a fresh segment note in its last
            # periodic filing. Tying the fetch to the earnings event rate-limits
            # it to roughly four per company per year, and collects it exactly
            # when the numbers changed.
            reported[ticker] = cik
            # Item 1 and Item 1A from the latest 10-K. Annual documents, so this
            # is a no-op after the first time a company is seen in a given year:
            # the accession is already recorded and the write is skipped.
            for kind, k_acc, k_filed, k_text in fetch_10k_items(cik, ticker):
                k_key = f"{k_acc}-{kind}"
                if k_key in already:
                    continue
                already.add(k_key)
                k_rel = f"text/{ticker}/{k_acc}-{kind}.txt"
                k_path = FILINGS_CSV_DIR / k_rel
                k_path.parent.mkdir(parents=True, exist_ok=True)
                k_path.write_text(k_text, encoding="utf-8")
                entries.append({
                    "filed": k_filed, "ticker": ticker, "cik": cik, "form": "10-K",
                    "doc_kind": kind, "items": "1" if kind == "business" else "1A",
                    "accession": k_key, "exhibit": "", "text_path": k_rel,
                    "text_chars": len(k_text),
                })
            seg = _fetch_segment_note(cik, ticker)
            if seg:
                s_acc, s_form, s_filed, s_name, s_text = seg
                if s_acc not in already:
                    s_rel = f"text/{ticker}/{s_acc}-segment.txt"
                    s_path = FILINGS_CSV_DIR / s_rel
                    s_path.parent.mkdir(parents=True, exist_ok=True)
                    s_path.write_text(s_text, encoding="utf-8")
                    entries.append({
                        "filed": s_filed or day, "ticker": ticker, "cik": cik, "form": s_form,
                        "doc_kind": "segment_note", "items": s_name[:60],
                        "accession": s_acc, "exhibit": "", "text_path": s_rel,
                        "text_chars": len(s_text),
                    })
    kinds = {}
    for e in entries:
        kinds[e["doc_kind"]] = kinds.get(e["doc_kind"], 0) + 1
    detail = ", ".join(f"{v} {k}" for k, v in sorted(kinds.items())) or "nothing"
    print(f"filings: {detail}; {scanned} 8-K headers read, "
          f"{skipped} already recorded, {time.time() - started:.0f}s.")
    # Both values, matching the early return above. The caller unpacks a pair.
    return entries, reported


def fetch_edgar_company_facts(cik):
    """Fetch full XBRL facts for one CIK. Returns the 'facts' dict or None.
    Routes through _sec_throttle (defined later in the file) so that EDGAR and
    Insider Form 4 share one rate-limit budget against SEC."""
    try:
        _sec_throttle()
    except NameError:
        # Throttle helper isn't defined yet during module import; fall through.
        pass
    try:
        url = EDGAR_FACTS_URL.format(cik=int(cik))
        req = urllib.request.Request(url, headers={"User-Agent": EDGAR_USER_AGENT, "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return data.get("facts", {})
    except Exception:
        return None


_QUARTER_MIN_DAYS = 60
# 120, not 100, because a 4-4-5 retail calendar makes the fourth quarter 16
# weeks. At 100 days Costco's Q4 fell outside the window every single year, so
# it was neither read nor reconstructable and the series ran three quarters to
# the year. Nothing legitimate sits between 120 days and the 340 the annual
# window starts at, so widening cannot pull in a half-year by mistake.
_QUARTER_MAX_DAYS = 120
_ANNUAL_MIN_DAYS = 340
_ANNUAL_MAX_DAYS = 380
# A concept whose newest fact trails the newest fact any candidate offers by
# more than this is a retired tag, not a preference.
_CONCEPT_STALE_DAYS = 400


def _period_days(start, end):
    """Length of an XBRL duration in days, or None if either date is unusable."""
    from datetime import date as _date
    try:
        return (_date.fromisoformat(end) - _date.fromisoformat(start)).days
    except Exception:
        return None


def _shift_iso(day, delta):
    from datetime import date as _date
    try:
        return (_date.fromisoformat(day) + timedelta(days=delta)).isoformat()
    except Exception:
        return day


# A per-share figure that moved by this factor or more between two filings of
# the same period was restated onto a new share basis: a split or a reverse
# split. Ordinary restatements move EPS by a few percent, and the smallest split
# in common use is 2-for-1. A 3-for-2 split sits under the bar and is missed;
# the error it leaves in a trailing sum is at most a third of one quarter.
_SHARE_BASIS_JUMP = 1.8
# Per-share values below this are mostly rounding (0.01 against 0.02 is a
# factor of two), so they neither reveal nor disprove a change of basis.
_PER_SHARE_MIN_ABS = 0.05


def _per_share_breaks(records):
    """Filing dates on which a per-share concept moved to a new share basis.

    The evidence is a period filed twice, the later filing restating it by a
    split-like factor. Alight's second quarter of 2025 was filed at -2.04 and
    refiled a year later, after its 1-for-20 reverse split, at -40.61; the
    later filing's date is when the new basis is known to be in use."""
    by_period = {}
    for r in records:
        start, end, val, filed = r.get("start"), r.get("end"), r.get("val"), r.get("filed") or ""
        if not start or not end or not filed or not isinstance(val, (int, float)):
            continue
        by_period.setdefault((start, end), {})[filed] = val
    breaks = set()
    for filings in by_period.values():
        dated = sorted(filings.items())
        for (_, old), (filed, new) in zip(dated, dated[1:]):
            if min(abs(old), abs(new)) < _PER_SHARE_MIN_ABS or (old > 0) != (new > 0):
                continue
            if max(abs(old), abs(new)) / min(abs(old), abs(new)) >= _SHARE_BASIS_JUMP:
                breaks.add(filed)
    return sorted(breaks)


def _quarterly_from_records(records, per_share=False):
    """Every ~90-day duration in a concept's records, from any form, with the
    fourth quarter reconstructed where the filer never tagged one.

    This used to take 10-Q rows only. A 10-Q covers Q1, Q2 and Q3; the fourth
    quarter of every fiscal year is in the 10-K or nowhere. So the series had a
    hole at every fourth entry, invisible in a list sorted by date and fatal to
    anything reading it positionally. fcf_growth_yoy compared Q[n] against
    Q[n-4] believing that to be a year, when it was fifteen months against
    sixteen. op_margin_stability measured every retailer's volatility with the
    holiday quarter removed. accruals_ratio summed four entries and called the
    result a TTM.

    Two ways back. Walmart and Target tag Q4 in the 10-K, so simply reading
    every form recovers it: 9 quarters for WMT, 5 for TGT. Coca-Cola never tags
    one, but files an annual figure alongside three quarters, and the fourth is
    the difference. Only a year with exactly three quarters and exactly one
    quarter-shaped gap qualifies; anything else is left missing rather than
    guessed at.

    per_share=True is for EPS, where a subtraction is only valid if every input
    is on the same share basis. Each period keeps its most recently filed value,
    so after a split the restated quarters and the unrestated ones sit side by
    side: Alight's fourth quarter of 2025 came out as the 2025 annual EPS
    (-5.87, pre-split) less three quarters one of which had been refiled at
    twenty times its size (-40.61), which is +36.81 for a quarter that lost
    $932m. So a derivation whose inputs were filed on both sides of a
    _per_share_breaks date is refused, and the quarter is left missing."""
    breaks = _per_share_breaks(records) if per_share else []

    def straddles(filed_dates):
        dates = [f for f in filed_dates if f]
        if not breaks or not dates:
            return False
        lo, hi = min(dates), max(dates)
        return any(lo < b <= hi for b in breaks)

    quarters, annuals = {}, {}
    for r in records:
        start, end, val = r.get("start"), r.get("end"), r.get("val")
        if not start or not end or val is None:
            continue
        days = _period_days(start, end)
        if days is None:
            continue
        if _QUARTER_MIN_DAYS <= days <= _QUARTER_MAX_DAYS:
            bucket = quarters
        elif _ANNUAL_MIN_DAYS <= days <= _ANNUAL_MAX_DAYS:
            bucket = annuals
        else:
            continue
        filed = r.get("filed", "")
        cur = bucket.get((start, end))
        if not cur or filed > cur["filed"]:
            bucket[(start, end)] = {"start": start, "end": end, "val": val,
                                    "filed": filed, "derived": False}

    for (a_start, a_end), annual in annuals.items():
        inside = sorted((q for q in quarters.values()
                         if q["start"] >= a_start and q["end"] <= a_end),
                        key=lambda q: q["start"])
        if len(inside) != 3:
            continue
        # The missing quarter is the year's one uncovered stretch. Consecutive
        # quarters abut with a day between them, so a real gap is the only edge
        # wide enough to hold a quarter. Two wide gaps means the three periods
        # do not tile the year and nothing is safe to infer.
        edges = [(a_start, inside[0]["start"]),
                 (inside[0]["end"], inside[1]["start"]),
                 (inside[1]["end"], inside[2]["start"]),
                 (inside[2]["end"], a_end)]
        gaps = [(s, e) for s, e in edges
                if (_period_days(s, e) or 0) >= _QUARTER_MIN_DAYS]
        if len(gaps) != 1:
            continue
        g_start, g_end = gaps[0]
        # The boundary day belongs to the quarter that reported it.
        if any(q["end"] == g_start for q in inside):
            g_start = _shift_iso(g_start, 1)
        if any(q["start"] == g_end for q in inside):
            g_end = _shift_iso(g_end, -1)
        span = _period_days(g_start, g_end)
        if span is None or not (_QUARTER_MIN_DAYS <= span <= _QUARTER_MAX_DAYS):
            continue
        if (g_start, g_end) in quarters:
            continue
        if straddles([annual["filed"]] + [q["filed"] for q in inside]):
            continue
        try:
            missing = annual["val"] - sum(q["val"] for q in inside)
        except TypeError:
            continue
        # A subtraction is only as good as its inputs agreeing on what they
        # measure. Where a filer restated the year but not the quarters that
        # made it up, the difference absorbs the restatement: Target's fiscal
        # 2013, restated for the Canada exit, comes out 6% under the figure it
        # reported. That is small, twelve years stale, and far outside the
        # twelve periods anything reads, but the failure mode has no upper
        # bound, so reject a result that cannot be a quarter of this year.
        sibling_max = max(abs(q["val"]) for q in inside)
        if sibling_max and abs(missing) > 3 * sibling_max:
            continue
        quarters[(g_start, g_end)] = {"start": g_start, "end": g_end,
                                      "val": missing, "filed": annual["filed"],
                                      "derived": True}

    # Cash-flow statements are filed year-to-date, not per quarter. A 10-Q's
    # cash-flow column covers fiscal-year-start to quarter-end, so only Q1 is
    # ever ~90 days long and everything above lands nothing else: Coca-Cola's
    # "quarterly" operating cash flow was four first quarters from four
    # different years, one per year, and summing them produced a trailing
    # twelve months of minus $2.5bn for a company that earns about ten.
    #
    # It also silently broke the year-over-year comparisons that read this
    # series positionally. fcf_growth_yoy compares entry 0 against entry 4,
    # which on a one-Q1-per-year series is a four-year growth rate wearing a
    # year-over-year label.
    #
    # Consecutive year-to-date figures sharing a fiscal-year start differ by
    # exactly one quarter, so difference the chain: Q2 = H1 - Q1, Q3 = 9M - H1,
    # Q4 = FY - 9M. Income-statement facts are unaffected, since a 10-Q tags
    # both a three-month and a year-to-date column and the three-month one is
    # picked up directly above.
    by_start = {}
    for r in records:
        start, end, val = r.get("start"), r.get("end"), r.get("val")
        if not start or not end or val is None:
            continue
        days = _period_days(start, end)
        if days is None or not (_QUARTER_MIN_DAYS <= days <= _ANNUAL_MAX_DAYS):
            continue
        by_start.setdefault(start, []).append(
            {"end": end, "days": days, "val": val, "filed": r.get("filed", "")})

    for start, chain in by_start.items():
        if len(chain) < 2:
            continue
        # One row per period length, most recently filed winning, so a restated
        # year does not appear twice in the same chain.
        best = {}
        for item in chain:
            cur = best.get(item["days"])
            if cur is None or item["filed"] > cur["filed"]:
                best[item["days"]] = item
        ordered = sorted(best.values(), key=lambda x: x["days"])
        prev = None
        for item in ordered:
            if prev is not None:
                span = item["days"] - prev["days"]
                if _QUARTER_MIN_DAYS <= span <= _QUARTER_MAX_DAYS:
                    q_start = _shift_iso(prev["end"], 1)
                    key = (q_start, item["end"])
                    if key not in quarters and not straddles([prev["filed"], item["filed"]]):
                        try:
                            quarters[key] = {
                                "start": q_start, "end": item["end"],
                                "val": item["val"] - prev["val"],
                                "filed": item["filed"], "derived": True}
                        except TypeError:
                            pass
            prev = item

    # One value per period end. A quarter the filer actually tagged beats one
    # worked out by subtraction, whatever the filing dates say.
    by_end = {}
    for q in quarters.values():
        cur = by_end.get(q["end"])
        if cur is None or (not q["derived"], q["filed"]) > (not cur["derived"], cur["filed"]):
            by_end[q["end"]] = q
    return sorted(by_end.values(), key=lambda q: q["end"], reverse=True)


def _instants_from_records(records):
    """Point-in-time (balance-sheet) values, most recent first.

    Instants carry an end and no start. Duration facts land in the same unit
    array and would otherwise be read as a balance at their end date."""
    by_end = {}
    for r in records:
        if r.get("start"):
            continue
        if r.get("form") not in ("10-Q", "10-Q/A", "10-K", "10-K/A"):
            continue
        end, val = r.get("end"), r.get("val")
        if not end or val is None:
            continue
        filed = r.get("filed", "")
        cur = by_end.get(end)
        if not cur or filed > cur["filed"]:
            by_end[end] = {"end": end, "val": val, "filed": filed}
    return sorted(by_end.values(), key=lambda x: x["end"], reverse=True)


def _select_concept_series(facts, concept_keys, builder, unit_keys=None,
                           return_concept=False):
    """Build a series from the best concept, not merely the first that answers.

    The old rule was first-match-wins, which is only right if every candidate
    tag is current. They are not. Apple's `Revenues` holds nothing since 2018,
    because its modern revenue is tagged
    RevenueFromContractWithCustomerExcludingAssessedTax; the 10-Q-only filter
    happened to return nothing for it, so the fallback list moved on and the
    bug stayed hidden. Reading every form makes those 2018 rows answer, and
    first-match-wins would then have stopped there and published eight-year-old
    revenue as current. That is the same failure that already had 66 tickers
    computing margins off filings from 2014.

    So: candidates whose newest fact is well behind the best available are
    retired tags and are dropped. Among what remains, concept_keys order still
    decides, because that order is a real preference between live tags.

    return_concept=True returns (series, concept name) instead, for a caller
    that has to say which tag it ended up on."""
    from datetime import date as _date
    # dei carries the cover-page facts, us-gaap the statements.
    pools = (facts.get("us-gaap", {}), facts.get("dei", {}))
    candidates = []
    for rank, concept in enumerate(concept_keys):
        node = pools[0].get(concept) or pools[1].get(concept)
        if not node:
            continue
        units = node.get("units", {})
        # Share counts are filed under a "shares" unit, not USD, so a
        # USD-only lookup returns nothing for them.
        records = []
        for u in (unit_keys or ("USD", "USD/shares")):
            if units.get(u):
                records = units[u]
                break
        if not records:
            continue
        series = builder(records)
        if series:
            candidates.append((rank, series, concept))
    if not candidates:
        return ([], None) if return_concept else []
    newest = max(s[0]["end"] for _, s, _c in candidates)
    try:
        cutoff = (_date.fromisoformat(newest) - timedelta(days=_CONCEPT_STALE_DAYS)).isoformat()
    except Exception:
        cutoff = ""
    live = [c for c in candidates if c[1][0]["end"] >= cutoff]
    _rank, series, concept = min(live or candidates, key=lambda c: c[0])
    return (series, concept) if return_concept else series


def _extract_quarterly_series(facts, concept_keys, max_periods=12, per_share=False,
                              unit_keys=None, return_concept=False):
    """Quarterly values for the best matching concept, most recent first."""
    builder = ((lambda recs: _quarterly_from_records(recs, per_share=True))
               if per_share else _quarterly_from_records)
    got = _select_concept_series(facts, concept_keys, builder, unit_keys=unit_keys,
                                 return_concept=return_concept)
    if return_concept:
        return got[0][:max_periods], got[1]
    return got[:max_periods]


def _extract_instant_series(facts, concept_keys, max_periods=12, unit_keys=None):
    """Balance-sheet values for the best matching concept, most recent first.

    Balance-sheet facts are instants: they carry an `end` and no `start`, so
    the duration path drops every one of them at its `if not start` guard.
    That is why accruals_ratio sat at 0% coverage across the whole universe."""
    return _select_concept_series(facts, concept_keys, _instants_from_records,
                                 unit_keys=unit_keys)[:max_periods]


def compute_benford(facts):
    """Compute Benford's law fit across all USD-denominated XBRL values for a company.

    Returns dict with both first-digit and second-digit distributions:
      observed (1st-digit, 9 values), chi_sq, n, fit
      observed_d2 (2nd-digit, 10 values), chi_sq_d2, n_d2, fit_d2 (when n_d2 >= 30)

    The verdict is MAD, not chi-square, and the distinction matters. A 10-K dump
    carries thousands of values (median n here is 10,732), and at that sample
    size chi-square rejects conformity for any deviation at all: 8 df puts the
    5% critical value at 15.51, while the median company in this universe scores
    around 75. That is not evidence of fraud, it is what chi-square does with
    large n. Nigrini's mean absolute deviation is the forensic-accounting
    standard precisely because it does not scale with sample size.

    chi_sq is still reported, correctly computed, for anyone who wants it. It
    should not be read against textbook critical values at these sample sizes.

    Second-digit Benford is harder to game: most manipulators only fudge first
    digits to look natural, leaving the second digit to leak the truth."""
    import math as _math
    if not facts:
        return None
    digits_d1 = [0] * 10  # index 1..9 used
    digits_d2 = [0] * 10  # index 0..9 used
    n_d1 = 0
    n_d2 = 0
    us_gaap = facts.get("us-gaap", {})
    for concept, data in us_gaap.items():
        units = data.get("units", {})
        usd_records = units.get("USD", [])
        for r in usd_records:
            val = r.get("val")
            if val is None:
                continue
            try:
                abs_val = abs(float(val))
            except (TypeError, ValueError):
                continue
            if abs_val < 1:
                continue
            try:
                exp = int(_math.floor(_math.log10(abs_val)))
                leading = int(abs_val / (10 ** exp))
                if 1 <= leading <= 9:
                    digits_d1[leading] += 1
                    n_d1 += 1
                    # Second digit only meaningful if value >= 10 (has at least 2 digits)
                    if abs_val >= 10:
                        second = int(abs_val / (10 ** (exp - 1))) % 10
                        if 0 <= second <= 9:
                            digits_d2[second] += 1
                            n_d2 += 1
            except Exception:
                continue
    if n_d1 < 30:
        return None
    observed_d1 = [round(digits_d1[d] / n_d1 * 100, 1) for d in range(1, 10)]
    expected_d1 = [_math.log10(1 + 1 / d) * 100 for d in range(1, 10)]
    # Chi-square is defined on counts. This was computed from the percentages
    # above, which yields 100 * sum((p_obs - p_exp)^2 / p_exp) where the real
    # statistic is n * sum(...) of the same thing. With a median n of 10,732
    # that is off by a factor of about 107: the median company published 0.7,
    # against a critical value of 15.51 quoted in this very docstring, when its
    # actual statistic was around 75. A reader who knows what chi-square means
    # read overwhelming nonconformity as a near-perfect fit.
    #
    # Computed from the raw counts now, not from the rounded percentages, since
    # rounding to one decimal is itself a large perturbation at this n.
    chi_sq_d1 = sum(
        (digits_d1[d] - n_d1 * _math.log10(1 + 1 / d)) ** 2 / (n_d1 * _math.log10(1 + 1 / d))
        for d in range(1, 10)
    )
    # Mean Absolute Deviation in proportion units (Nigrini, Forensic Analytics).
    # Chi-square is too sensitive at large n (any 10-K dump has thousands of values
    # so even tiny structural rounding flips the fit to "poor"). MAD is the
    # forensic-accounting standard and gives more honest labels.
    mad_d1 = sum(abs(observed_d1[i] - expected_d1[i]) for i in range(9)) / 9 / 100
    # Nigrini 1st-digit thresholds: <0.006 close, 0.006-0.012 acceptable,
    # 0.012-0.015 marginal, >0.015 nonconformity. We use 3 buckets:
    #   good = close + acceptable, fair = marginal, poor = nonconformity.
    if mad_d1 < 0.012:
        fit = "good"
    elif mad_d1 < 0.018:
        fit = "fair"
    else:
        fit = "poor"
    result = {
        "observed": observed_d1,
        "chi_sq": round(chi_sq_d1, 1),
        "mad": round(mad_d1, 4),
        "n": n_d1,
        "fit": fit,
    }
    if n_d2 >= 30:
        observed_d2 = [round(digits_d2[d] / n_d2 * 100, 1) for d in range(0, 10)]
        # Expected second-digit Benford: P(d2=d) = sum_{k=1..9} log10(1 + 1/(10k+d))
        expected_d2 = [
            sum(_math.log10(1 + 1 / (10 * k + d)) for k in range(1, 10)) * 100
            for d in range(0, 10)
        ]
        # Same correction as the first digit: counts, not percentages.
        chi_sq_d2 = sum(
            (digits_d2[d] - n_d2 * expected_d2[d] / 100) ** 2 / (n_d2 * expected_d2[d] / 100)
            for d in range(10)
        )
        mad_d2 = sum(abs(observed_d2[i] - expected_d2[i]) for i in range(10)) / 10 / 100
        # Nigrini 2nd-digit thresholds: <0.008 close, 0.008-0.010 acceptable,
        # 0.010-0.012 marginal, >0.012 nonconformity. Same 3-bucket mapping but
        # widened a touch since trailing-zero rounding (digit "0" inflates) is
        # a benign and very common pattern in financial reporting.
        if mad_d2 < 0.014:
            fit_d2 = "good"
        elif mad_d2 < 0.022:
            fit_d2 = "fair"
        else:
            fit_d2 = "poor"
        result["observed_d2"] = observed_d2
        result["chi_sq_d2"] = round(chi_sq_d2, 1)
        result["mad_d2"] = round(mad_d2, 4)
        result["n_d2"] = n_d2
        result["fit_d2"] = fit_d2
    return result


# A TTM is assembled from quarterly facts. When the preferred concept is filed
# only once a year, it yields no quarterly series at all, so it never becomes a
# candidate and a narrower concept wins by default.
#
# JPM is the clean example. It tags `Revenues` once a year and nothing else:
# FY2025 is 182,447,000,000. There are zero quarterly `Revenues` facts, so the
# selector fell through to RevenueFromContractWithCustomerExcludingAssessedTax,
# which for a bank is fee revenue only, and published a ttm_revenue of
# 95,112,000,000. Every field built on revenue inherited it, and the screen
# ranks on several of them.
#
# The guard compares the assembled TTM against the most recent annual figure of
# the highest-ranked concept that files one. A TTM materially below it means the
# TTM is measuring a narrower thing, and the annual figure is the better answer
# even though it is staler.
_ANNUAL_MIN_DAYS, _ANNUAL_MAX_DAYS = 340, 400
# Below this share of the annual figure, the TTM is a different concept rather
# than a business that shrank. A real contraction of more than 30 percent in a
# year happens, so the substitution is logged and the threshold kept loose.
_TTM_ANNUAL_FLOOR = 0.70


# How old a fiscal year can be and still stand in for a trailing twelve months,
# measured from the year's end. Fifteen months covers a 20-F filed on the
# regulatory deadline, four months after year end, plus a normal quarter of
# slack; anything older is the year before last and describes a different
# company from the one the price is for.
_ANNUAL_FALLBACK_MAX_AGE_DAYS = 457


def _latest_annual_value(facts, concept_keys, unit_keys=("USD",), max_age_days=None,
                         as_of=None):
    """(value, end, concept) for the highest-ranked concept filed annually.

    unit_keys widens the lookup beyond USD, which EPS needs ("USD/shares").
    max_age_days drops a concept whose latest year ended longer ago than that
    before as_of (default today), and moves on to the next concept, so a
    retired tag cannot answer for a live one."""
    from datetime import date as _date
    cutoff = ""
    if max_age_days is not None:
        cutoff = ((as_of or _date.today()) - timedelta(days=max_age_days)).isoformat()
    pools = (facts.get("us-gaap", {}), facts.get("dei", {}))
    for concept in concept_keys:
        node = pools[0].get(concept) or pools[1].get(concept)
        if not node:
            continue
        best = None
        for unit in unit_keys:
            for rec in node.get("units", {}).get(unit, []):
                start, end, val = rec.get("start"), rec.get("end"), rec.get("val")
                if not start or not end or val is None:
                    continue
                try:
                    days = (_date.fromisoformat(end) - _date.fromisoformat(start)).days
                except Exception:
                    continue
                if not _ANNUAL_MIN_DAYS <= days <= _ANNUAL_MAX_DAYS:
                    continue
                if best is None or end > best[1]:
                    best = (val, end, concept)
        if best and best[1] >= cutoff:
            return best
    return (None, None, None)


def _durations_from_records(records):
    """Quarter- and year-length durations exactly as filed, most recent first.

    Nothing is derived. A weighted-average share count is an average over its
    period, so a year minus three quarters is not a fourth quarter of anything."""
    best = {}
    for r in records:
        start, end, val = r.get("start"), r.get("end"), r.get("val")
        if not start or not end or not isinstance(val, (int, float)):
            continue
        days = _period_days(start, end)
        if days is None or not (_QUARTER_MIN_DAYS <= days <= _QUARTER_MAX_DAYS
                                or _ANNUAL_MIN_DAYS <= days <= _ANNUAL_MAX_DAYS):
            continue
        filed = r.get("filed", "")
        cur = best.get((start, end))
        if cur is None or filed > cur["filed"]:
            best[(start, end)] = {"start": start, "end": end, "val": val,
                                  "filed": filed, "days": days}
    return sorted(best.values(), key=lambda d: d["end"], reverse=True)


def _share_counts(durations):
    """({quarter end: shares}, {fiscal year end: shares}) from _durations_from_records."""
    quarter, annual = {}, {}
    for d in durations or ():
        if not d["val"] or d["val"] <= 0:
            continue
        if _QUARTER_MIN_DAYS <= d["days"] <= _QUARTER_MAX_DAYS:
            quarter.setdefault(d["end"], d["val"])
        else:
            annual.setdefault(d["end"], d["val"])
    return quarter, annual


def _implied_shares(eps, income):
    """income / eps when the pair is informative about the share basis, else None."""
    if eps is None or income is None or abs(eps) < _PER_SHARE_MIN_ABS or not income:
        return None
    if (eps > 0) != (income > 0):
        return None
    return income / eps


def _quarter_share_count(end, q_shares, fy_shares):
    """Weighted diluted shares for the quarter ending `end`, or None.

    The quarter's own tagged count where there is one. Otherwise, for a fiscal
    year's closing quarter, four times the year's average less the three
    quarters before it, since the year's weighted average is (to within the
    quarters' unequal lengths) the mean of its four quarters. That tracks a
    buyback or an issuance inside the fourth quarter, which the year's average
    alone does not: Tulip's diluted count went from 1.77m to about 4.8m in its
    last quarter of fiscal 2026, and dividing by the year's 2.52m would
    overstate that quarter's loss per share by 90%. The year's own count is the
    fallback when the three quarters are missing, disagree with each other, or
    give an estimate that is not a share count (negative, or over four times
    the largest of them, which is a split between the year and its quarters)."""
    if q_shares.get(end):
        return q_shares[end]
    fy = fy_shares.get(end)
    if not fy:
        return None
    year_ago = _shift_iso(end, -370)
    inside = [v for e, v in q_shares.items() if year_ago < e < end]
    if len(inside) == 3 and max(inside) / min(inside) < _SHARE_BASIS_JUMP:
        est = 4 * fy - sum(inside)
        if 0 < est <= 4 * max(inside):
            return est
    return fy


def _reconcile_eps_quarters(eps, ni_by_end, q_shares, fy_shares):
    """The EPS series with every quarter the filer never tagged checked, and
    rebuilt from its halves where the subtraction cannot be trusted.

    A derived quarter is kept only if it agrees with the same quarter's
    earnings: same sign, and an implied share count (earnings / EPS) within
    _SHARE_BASIS_JUMP of the quarter's weighted diluted shares, or where none
    can be had, of the implied counts of the filed quarters beside it. Failing
    that, and for a quarter the subtraction refused outright, EPS is rebuilt
    as earnings over weighted diluted shares (_quarter_share_count).

    A tagged count for the quarter itself wins outright: earnings over it is
    the definition of diluted EPS. Otherwise the subtraction is preferred when
    it passes, because on a consistent basis it is exact to the cent and an
    estimated share count is not."""
    filed = [q for q in eps if not q.get("derived")]
    # A share count is only used here if it is in the same units as the EPS
    # it would rebuild. Hub Group tags its weighted diluted shares in
    # thousands (61,104 for 61.1m), and dividing its $24m fourth quarter by
    # that made an EPS of 398. Measured on the filed quarters: earnings over
    # EPS must land within _SHARE_BASIS_JUMP of the tagged count.
    ratios = sorted(s / q_shares[q["end"]] for q in filed if q_shares.get(q["end"])
                    for s in [_implied_shares(q["val"], ni_by_end.get(q["end"]))]
                    if s is not None)
    if ratios and not (1 / _SHARE_BASIS_JUMP < ratios[len(ratios) // 2] < _SHARE_BASIS_JUMP):
        q_shares, fy_shares = {}, {}
    out = []
    for q in eps:
        if not q.get("derived"):
            out.append(q)
            continue
        income = ni_by_end.get(q["end"])
        if q_shares.get(q["end"]) and income is not None:
            out.append(dict(q, val=income / q_shares[q["end"]], rebuilt=True))
            continue
        shares = _quarter_share_count(q["end"], q_shares, fy_shares)
        if _derived_eps_coherent(q, income, shares, filed, ni_by_end):
            out.append(q)
            continue
        if shares and income is not None:
            out.append(dict(q, val=income / shares, rebuilt=True))
    # A fourth quarter the subtraction refused (inputs on two share bases)
    # leaves a gap that earnings over shares can still fill.
    have = {q["end"] for q in out}
    oldest = min(have) if have else ""
    for end, income in ni_by_end.items():
        if end in have or end < oldest:
            continue
        shares = _quarter_share_count(end, q_shares, fy_shares)
        if shares:
            out.append({"start": None, "end": end, "val": income / shares,
                        "filed": "", "derived": True, "rebuilt": True})
    out.sort(key=lambda q: q["end"], reverse=True)
    # A rebuilt quarter has no start of its own; it runs from the day after
    # the quarter before it, which the contiguity check in _per_share_ttm reads.
    # With no quarter before it, or a gap, it is given a nominal 91 days.
    for i, q in enumerate(out):
        if not q.get("start"):
            prev = out[i + 1]["end"] if i + 1 < len(out) else None
            gap = _period_days(prev, q["end"]) if prev else None
            q["start"] = (_shift_iso(prev, 1)
                          if gap and _QUARTER_MIN_DAYS <= gap <= _QUARTER_MAX_DAYS
                          else _shift_iso(q["end"], -91))
    return out


def _derived_eps_coherent(q, income, shares, filed, ni_by_end):
    """Whether a subtracted EPS quarter is consistent with that quarter's earnings."""
    if income is None or not q["val"]:
        return True
    if income and (q["val"] > 0) != (income > 0):
        return False
    implied = income / q["val"] if income else None
    if implied is None:
        return True
    ref = shares
    if not ref:
        year_ago = _shift_iso(q["end"], -370)
        siblings = sorted(s for s in (_implied_shares(f["val"], ni_by_end.get(f["end"]))
                                      for f in filed if year_ago < f["end"] < q["end"])
                          if s is not None)
        if not siblings:
            return True
        ref = siblings[len(siblings) // 2]
    return max(implied / ref, ref / implied) < _SHARE_BASIS_JUMP


def _mixed_share_basis(quarters, ni_by_end, q_shares):
    """True when a run of EPS quarters is not all on one share basis.

    Reads the tagged weighted share counts where at least two quarters have
    one, since those do not depend on the earnings concept being right, and
    otherwise the share count each quarter implies (earnings / EPS). Alight's
    trailing four quarters to June 2026 ran from 527m shares to 26m: the June
    quarter was filed after its reverse split and the March quarter before.

    The test is a jump between neighbouring quarters, not the spread across
    the run. A split moves the count by its whole factor from one quarter to
    the next; a company issuing stock grows it a step at a time. Measured on
    reported.csv, a spread test also withheld Healthy Choice Wellness (12m to 27m
    over four quarters of offerings), whose EPS sum is fine."""
    tagged = [q_shares[q["end"]] for q in quarters if q_shares.get(q["end"])]
    if len(tagged) >= 2:
        counts = tagged
    else:
        counts = [s for s in (_implied_shares(q["val"], ni_by_end.get(q["end"]))
                              for q in quarters) if s is not None]
    return any(max(a / b, b / a) >= _SHARE_BASIS_JUMP for a, b in zip(counts, counts[1:]))


def _per_share_ttm(quarters, ni_by_end, q_shares, offset=0):
    """Trailing four quarters of a per-share figure, or None.

    _ttm with two more refusals, both of which a per-share sum needs and a
    dollar sum does not. The four quarters must tile one year, since a missing
    quarter would otherwise pull a fifth into the window. And everything from
    the newest quarter back through the window must be on one share basis:
    adding a pre-split quarter to a post-split one is adding two currencies.
    offset=4 checks all eight quarters, so a year-over-year comparison is never
    made across a split either."""
    if not quarters or len(quarters) < offset + _TTM_QUARTERS:
        return None
    window = quarters[offset:offset + _TTM_QUARTERS]
    span = _period_days(window[-1].get("start") or "", window[0]["end"])
    if span is None or not (_ANNUAL_MIN_DAYS <= span <= _ANNUAL_MAX_DAYS):
        return None
    if _mixed_share_basis(quarters[:offset + _TTM_QUARTERS], ni_by_end, q_shares):
        return None
    return _ttm(quarters, offset)


# Banks below a certain size do not tag `Revenues` at all. Their income
# statement is interest income plus noninterest income, under this taxonomy, and
# neither half alone is revenue. EWBC files 4,293,396,000 and 379,227,000 for
# 2025 and no Revenues fact; the panel published 0.1B and an implied net margin
# of 2,372 percent. Both legs are required, which is what keeps this from
# matching a non-bank.
_BANK_REVENUE_LEGS = ("InterestAndDividendIncomeOperating", "NoninterestIncome")
# Below this share of the two legs combined, the standard concept caught a
# fragment rather than revenue. JPM sits well above it and keeps its own
# Revenues tag; EWBC's fee line is about 1 percent of its total and does not.
_BANK_FRAGMENT_SHARE = 0.40


def _bank_annual_revenue(facts):
    """(value, end) for a bank's total revenue, or (None, None)."""
    legs, end = [], None
    for concept in _BANK_REVENUE_LEGS:
        val, leg_end, _c = _latest_annual_value(facts, [concept])
        if val is None:
            return (None, None)
        legs.append(val)
        if end is None or (leg_end or "") > end:
            end = leg_end
    return (sum(legs), end)


def _ttm_with_annual_guard(facts, concept_keys, ttm_value, label, ticker=""):
    """Replace a TTM that is measuring a narrower concept than it claims."""
    if not facts:
        return ttm_value, None
    annual, end, concept = _latest_annual_value(facts, concept_keys)
    if label == "revenue":
        # A bank can file both: JPM tags Revenues at 182.4B net of interest
        # expense, and the two legs gross up higher than that, so the composite
        # must not simply win. It is used only when the standard concept is
        # absent, or caught so small a fragment that it cannot be revenue.
        bank, bank_end = _bank_annual_revenue(facts)
        if bank and bank > 0 and (annual is None or annual < bank * _BANK_FRAGMENT_SHARE):
            annual, end, concept = bank, bank_end, " + ".join(_BANK_REVENUE_LEGS)
    if annual is None or annual <= 0:
        return ttm_value, None
    if ttm_value is not None and ttm_value >= annual * _TTM_ANNUAL_FLOOR:
        return ttm_value, None
    had = "no quarterly facts" if ttm_value is None else f"{ttm_value:,.0f}"
    print(f"edgar: {ticker or '?'} {label} TTM was {had} against {annual:,.0f} "
          f"reported for the year to {end} under {concept}; using the annual figure.")
    return annual, {"source": "annual", "concept": concept, "period_end": end}


def compute_edgar_factors(facts, as_of=None):
    """Compute the 5 quarterly-trend factors from a CIK's XBRL facts dict.
    Each factor goes through a plausibility clamp; out-of-range values are dropped.

    as_of (a date, default today) is only read by the annual fallback's
    freshness limit."""
    if not facts:
        return {}

    revenues = _extract_quarterly_series(facts, EDGAR_CONCEPT_FALLBACKS["revenue"])
    gp = _extract_quarterly_series(facts, EDGAR_CONCEPT_FALLBACKS["gross_profit"])
    op_inc = _extract_quarterly_series(facts, EDGAR_CONCEPT_FALLBACKS["operating_income"])
    cfo = _extract_quarterly_series(facts, EDGAR_CONCEPT_FALLBACKS["cfo"])
    capex = _extract_quarterly_series(facts, EDGAR_CONCEPT_FALLBACKS["capex"])
    eps = _extract_quarterly_series(facts, EDGAR_CONCEPT_FALLBACKS["eps_basic"])
    net_income = _extract_quarterly_series(facts, EDGAR_CONCEPT_FALLBACKS["net_income"])
    assets = _extract_instant_series(facts, EDGAR_CONCEPT_FALLBACKS["total_assets"])
    eps_dil, eps_concept = _extract_quarterly_series(
        facts, EDGAR_CONCEPT_FALLBACKS["eps_diluted"], per_share=True,
        unit_keys=("USD/shares",), return_concept=True)
    dep_amort = _extract_quarterly_series(facts, EDGAR_CONCEPT_FALLBACKS["dep_amort"])
    if not dep_amort:
        # Components, summed, only when the combined line is absent.
        dep_only = _ttm(_extract_quarterly_series(
            facts, EDGAR_CONCEPT_FALLBACKS["depreciation_only"]))
        amort_only = _ttm(_extract_quarterly_series(
            facts, EDGAR_CONCEPT_FALLBACKS["amortization_only"]))
        split_da = None if dep_only is None else dep_only + (amort_only or 0)
    else:
        split_da = None
    shares = _extract_instant_series(facts, EDGAR_CONCEPT_FALLBACKS["shares_outstanding"],
                                     unit_keys=("shares",))
    equity = _extract_instant_series(facts, EDGAR_CONCEPT_FALLBACKS["equity"])
    cash = _extract_instant_series(facts, EDGAR_CONCEPT_FALLBACKS["cash"])
    st_inv = _extract_instant_series(facts, EDGAR_CONCEPT_FALLBACKS["short_term_investments"])
    lt_debt = _extract_instant_series(facts, EDGAR_CONCEPT_FALLBACKS["long_term_debt"])
    cur_debt = _extract_instant_series(facts, EDGAR_CONCEPT_FALLBACKS["current_debt"])
    st_borrow = _extract_instant_series(facts, EDGAR_CONCEPT_FALLBACKS["short_term_borrowings"])

    # Diluted EPS, checked against its own two halves. The earnings concept is
    # the common holders' where one is tagged, so the check is not thrown off
    # by preferred dividends.
    ni_common = _extract_quarterly_series(facts, EDGAR_CONCEPT_FALLBACKS["net_income_common"],
                                          unit_keys=("USD",))
    ni_by_end = {q["end"]: q["val"] for q in ni_common}
    q_shares, fy_shares = _share_counts(_select_concept_series(
        facts, EDGAR_CONCEPT_FALLBACKS["shares_diluted_weighted"], _durations_from_records,
        unit_keys=("shares",)))
    eps_q = _reconcile_eps_quarters(eps_dil, ni_by_end, q_shares, fy_shares)
    ttm_eps = _per_share_ttm(eps_q, ni_by_end, q_shares)
    prior_ttm_eps = _per_share_ttm(eps_q, ni_by_end, q_shares, _TTM_QUARTERS)
    eps_basis = None
    if ttm_eps is not None:
        eps_basis = EPS_BASIS_BASIC if eps_concept in _BASIC_EPS_CONCEPTS else EPS_BASIS_TTM
    ttm_ni = _ttm(net_income)
    annual_end = None
    # Annual-only filers. A 20-F or 40-F carries no quarterly XBRL at all, so
    # 190 companies with a real, filed, full-year EPS had no P/E but the
    # vendor's, and the loss-makers among them kept a vendor P/E our own
    # filings contradict. The latest fiscal year stands in for the trailing
    # twelve months when it ended within _ANNUAL_FALLBACK_MAX_AGE_DAYS.
    #
    # Only when no quarter falls inside or after that year. A quarterly filer
    # whose trailing sum was withheld for mixing share bases must not fall back
    # to an annual figure on the older of the two bases.
    if ttm_eps is None:
        val, end, concept = _latest_annual_value(
            facts, EDGAR_CONCEPT_FALLBACKS["eps_diluted"], unit_keys=("USD/shares",),
            max_age_days=_ANNUAL_FALLBACK_MAX_AGE_DAYS, as_of=as_of)
        if val is not None and (not eps_dil
                                or eps_dil[0]["end"] < _shift_iso(end, -_ANNUAL_MIN_DAYS)):
            ttm_eps = float(val)
            eps_basis = EPS_BASIS_BASIC if concept in _BASIC_EPS_CONCEPTS else EPS_BASIS_ANNUAL
            annual_end = end
    if ttm_ni is None:
        val, end, _concept = _latest_annual_value(
            facts, EDGAR_CONCEPT_FALLBACKS["net_income"], unit_keys=("USD",),
            max_age_days=_ANNUAL_FALLBACK_MAX_AGE_DAYS, as_of=as_of)
        if val is not None and (not net_income
                                or net_income[0]["end"] < _shift_iso(end, -_ANNUAL_MIN_DAYS)):
            ttm_ni = float(val)
            annual_end = max(annual_end or "", end)

    out = {}
    # Revenue gets the annual guard because it is the field most other fields
    # are built from, and because banks and insurers routinely file it only
    # once a year.
    ttm_rev, rev_note = _ttm_with_annual_guard(
        facts, EDGAR_CONCEPT_FALLBACKS["revenue"], _ttm(revenues), "revenue")
    # Trailing-twelve-month aggregates and latest balance-sheet values. These
    # are the inputs the price-dependent ratios need; the ratios themselves are
    # computed in derive_ratios_from_fundamentals once a price is known.
    for key, val in (
        ("ttm_revenue", ttm_rev),
        ("ttm_gross_profit", _ttm(gp)),
        ("ttm_operating_income", _ttm(op_inc)),
        ("ttm_net_income", ttm_ni),
        ("ttm_eps_diluted", ttm_eps),
        ("ttm_dep_amort", _ttm(dep_amort) if dep_amort else split_da),
        ("prior_ttm_revenue", _ttm(revenues, _TTM_QUARTERS)),
        ("prior_ttm_eps_diluted", prior_ttm_eps),
        ("shares_outstanding", _latest(shares)),
        ("equity", _latest(equity)),
        ("prior_equity", (float(equity[_TTM_QUARTERS]["val"])
                          if len(equity) > _TTM_QUARTERS else None)),
        ("cash_and_investments", (_latest(cash) or 0) + (_latest(st_inv) or 0)
         if _latest(cash) is not None else None),
        ("total_debt", ((_latest(lt_debt) or 0) + (_latest(cur_debt) or 0)
                        + (_latest(st_borrow) or 0)) or None),
    ):
        if val is not None and math.isfinite(val):
            out[key] = val
    ttm_cfo, ttm_capex = _ttm(cfo), _ttm(capex)
    if ttm_cfo is not None and ttm_capex is not None:
        # Capex is reported as a positive outflow in the cash-flow statement.
        out["ttm_fcf"] = ttm_cfo - abs(ttm_capex)
    if out.get("ttm_operating_income") is not None and out.get("ttm_dep_amort") is not None:
        out["ttm_ebitda"] = out["ttm_operating_income"] + out["ttm_dep_amort"]
    # The period the newest fact covers. This, not the day we ran, is what makes
    # a filing-derived number as current as it can be.
    # An annual-only filer has no quarter at all, and its year is the period.
    newest = max([s[0]["end"] for s in (revenues, net_income, eps_dil) if s]
                 + [annual_end or ""])
    if newest:
        out["fiscal_period_end"] = newest
    if "ttm_eps_diluted" in out:
        out["eps_basis"] = eps_basis

    # Accruals ratio (Sloan 1996): (TTM net income - TTM operating cash flow)
    # / average total assets. High accruals mean earnings are not backed by cash,
    # which predicts weak future returns, so it is inverted in SCORE_GROUPS.
    #
    # Sourced from EDGAR rather than yfinance deliberately. The previous
    # implementation read totalAssets off yfinance's .info, which does not expose
    # it (it lives on the balance sheet), so the guard never passed and this
    # factor was silently 0% covered across all 5,336 tickers, quietly making
    # Quality a four-field dimension instead of five.
    if len(net_income) >= 4 and len(cfo) >= 4 and assets:
        try:
            ttm_ni = sum(r["val"] for r in net_income[:4])
            ttm_cfo = sum(r["val"] for r in cfo[:4])
            # Average with the year-ago balance where available; a single balance
            # date would let one acquisition swing the denominator.
            recent = [r["val"] for r in assets[:5] if r["val"]]
            avg_assets = (recent[0] + recent[4]) / 2 if len(recent) >= 5 else recent[0]
            if avg_assets and avg_assets > 0:
                ratio = (ttm_ni - ttm_cfo) / avg_assets
                if -1 < ratio < 1:
                    out["accruals_ratio"] = ratio
        except (TypeError, ValueError, IndexError, ZeroDivisionError):
            pass

    # Revenue Acceleration: ΔYoY growth quarter-over-quarter
    # = (Q[n] vs Q[n-4]) growth - (Q[n-1] vs Q[n-5]) growth
    if len(revenues) >= 5:
        try:
            base_curr = revenues[3].get("val")
            base_prev = revenues[4].get("val")
            if base_curr and base_prev and base_curr != 0 and base_prev != 0:
                curr_g = (revenues[0]["val"] - base_curr) / abs(base_curr)
                prev_g = (revenues[1]["val"] - base_prev) / abs(base_prev)
                accel = curr_g - prev_g
                if abs(accel) < 1:
                    out["revenue_acceleration"] = accel
        except (TypeError, KeyError):
            pass

    # Gross Margin Trend: this Q margin - same Q prior year margin
    if revenues and gp:
        rev_by_end = {r["end"]: r["val"] for r in revenues if r.get("val")}
        gp_by_end = {r["end"]: r["val"] for r in gp if r.get("val")}
        common = sorted(set(rev_by_end) & set(gp_by_end), reverse=True)
        if len(common) >= 4:
            try:
                if rev_by_end[common[0]] > 0 and rev_by_end[common[3]] > 0:
                    curr_m = gp_by_end[common[0]] / rev_by_end[common[0]]
                    prev_m = gp_by_end[common[3]] / rev_by_end[common[3]]
                    trend = curr_m - prev_m
                    if abs(trend) < 0.5:
                        out["gross_margin_trend"] = trend
            except (TypeError, KeyError):
                pass

    # FCF Growth YoY: TTM FCF current vs TTM FCF prior year
    # FCF = CFO - CapEx (per quarter), TTM = sum of last 4 quarters
    if cfo and capex:
        cfo_by_end = {r["end"]: r["val"] for r in cfo if r.get("val") is not None}
        capex_by_end = {r["end"]: r["val"] for r in capex if r.get("val") is not None}
        common = sorted(set(cfo_by_end) & set(capex_by_end), reverse=True)
        if len(common) >= 8:
            try:
                curr_ttm = sum(cfo_by_end[d] - capex_by_end[d] for d in common[:4])
                prev_ttm = sum(cfo_by_end[d] - capex_by_end[d] for d in common[4:8])
                if prev_ttm != 0:
                    growth = (curr_ttm - prev_ttm) / abs(prev_ttm)
                    if abs(growth) < 5:
                        out["fcf_growth_yoy"] = growth
            except (TypeError, KeyError):
                pass

    # Earnings Consistency: 1 / (1 + coefficient_of_variation) of quarterly EPS.
    # Higher value = more consistent (range 0 to 1, intuitive for users).
    if len(eps) >= 4:
        vals = [r["val"] for r in eps[:8] if r.get("val") is not None]
        if len(vals) >= 4:
            mean_v = sum(vals) / len(vals)
            if abs(mean_v) > 0.001:
                variance = sum((v - mean_v) ** 2 for v in vals) / len(vals)
                stddev = variance ** 0.5
                cv = stddev / abs(mean_v)
                if 0 <= cv < 100:
                    out["earnings_consistency"] = 1 / (1 + cv)

    # Op Margin Stability: stddev of quarterly operating margins (lower = more stable).
    # Also emit op_margin_history for the chart card. We report raw stddev so users see
    # the dispersion directly; smaller is better.
    if revenues and op_inc:
        rev_by_end = {r["end"]: r["val"] for r in revenues if r.get("val")}
        op_by_end = {r["end"]: r["val"] for r in op_inc if r.get("val") is not None}
        common = sorted(set(rev_by_end) & set(op_by_end), reverse=True)
        history = []
        for d in common[:12]:
            if rev_by_end[d] > 0:
                m = op_by_end[d] / rev_by_end[d]
                if -2 < m < 2:
                    history.append({"end": d, "margin": m})
        if history:
            out["op_margin_history"] = history
        margins = [h["margin"] for h in history[:8]]
        if len(margins) >= 4:
            mean_m = sum(margins) / len(margins)
            variance = sum((m - mean_m) ** 2 for m in margins) / len(margins)
            stddev = variance ** 0.5
            if 0 <= stddev < 1:
                out["op_margin_stability"] = stddev

    return out


# ── SEC Form 4 (insider transactions, Seyhun signal) ───────────────────────

EDGAR_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
# Form 4 XML document fetched at:
# https://www.sec.gov/Archives/edgar/data/{cik}/{accession_no_dashes}/{primary_doc}
_INSIDER_LOOKBACK_DAYS = 90
# Every field this pass emits is named _90d or measures a 30-day cluster inside
# that window, and all of them are computed from whatever filings we fetched. At
# a cap of 8 that was not a 90-day window at all: Microsoft and Procter & Gamble
# file in batches, so their eight most recent Form 4s span zero days. Microsoft's
# "90-day insider signal" was one afternoon's filings, reported to the cent.
#
# Measured across ten of the largest filers: median 12 Form 4s per 90 days, with
# MSFT and PG at 38. 60 clears that with room, and the wall-clock budget below,
# not the document cap, is what keeps the pass inside the job now.
_INSIDER_MAX_DOCS_PER_TICKER = 60
# Roughly 7 fetches/sec against the SEC limit, so this is about 6,000 documents.
# A ticker not reached keeps its stale insider_updated stamp and is picked up by
# the next run, the same way the EDGAR sweep builds coverage across runs.
_INSIDER_TIME_BUDGET_S = 900
_INSIDER_TOP_N_BY_MARKET_CAP = 600  # only fetch insider data for the largest N tickers
# A backstop, not the real limit. The wall-clock budget below is what keeps
# this pass inside the job, the same way it does for yfinance, news and insider.
#
# The count cap was 2,000, chosen before any of those budgets existed, when a
# ceiling on fetches was the only available guard. It is not a good one: it
# spends the same allowance whether the SEC is answering in 30ms or 3s, and it
# turned a sweep that measured 4,631 tickers in 690 seconds, about 6.7 a second,
# into a three-run job for no reason. 6,000 covers the present universe with
# room, and the budget stops the pass if the rate ever collapses.
_EDGAR_MAX_FETCH_PER_RUN = 6000
# 15 minutes. At the measured rate that is roughly 6,000 companies, and a run
# that hits it stops and commits what it has rather than being killed with
# everything still in memory.
_EDGAR_TIME_BUDGET_S = 900

# Wall-clock budgets for the passes that scale with the universe and depend on a
# rate-limited third party. The job allows 120 minutes; these leave room for
# scoring, the site rebuild and the commit. A pass that hits its budget stops and
# reports how far it got, which lands, rather than being killed mid-flight with
# everything still in memory.
_YF_TIME_BUDGET_S = 2100      # 35 min. Uncapped worst case measured was 4,354s.
_NEWS_TIME_BUDGET_S = 900     # 15 min across ~5,400 Google RSS fetches.
                                     # (small caps Form 4 is noisier and not worth the latency)

# Shared SEC rate limiter. SEC's documented limit is 10 req/sec/IP. We aim for
# ~8 to leave headroom. All threads call _sec_throttle() before each request so
# the cumulative rate stays compliant regardless of worker count.
import threading as _threading
_sec_lock = _threading.Lock()
_sec_last_req_ts = [0.0]
_SEC_MIN_INTERVAL = 1.0 / 8.0  # 125 ms between requests across all threads

def _sec_throttle():
    with _sec_lock:
        now = time.time()
        gap = _sec_last_req_ts[0] + _SEC_MIN_INTERVAL - now
        if gap > 0:
            time.sleep(gap)
        _sec_last_req_ts[0] = time.time()


def _fetch_recent_form4_filings(cik):
    """Read /submissions/CIK{cik}.json and return a list of recent Form 4
    filings within the lookback window. Each entry is
    {accession, filing_date, primary_doc}.

    Returns None if the submissions fetch failed, and [] if it succeeded and the
    company simply has no Form 4 in the window. Collapsing the two is how an SEC
    outage came to be logged as "no insider activity" for every ticker at once."""
    _sec_throttle()
    try:
        url = EDGAR_SUBMISSIONS_URL.format(cik=int(cik))
        req = urllib.request.Request(url, headers={"User-Agent": EDGAR_USER_AGENT, "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None
    recent = (data.get("filings", {}) or {}).get("recent", {}) or {}
    forms = recent.get("form") or []
    accessions = recent.get("accessionNumber") or []
    dates = recent.get("filingDate") or []
    primary_docs = recent.get("primaryDocument") or []
    cutoff = (datetime.now(timezone.utc).date() - timedelta(days=_INSIDER_LOOKBACK_DAYS)).isoformat()
    out = []
    total = 0
    for i, form in enumerate(forms):
        if form != "4":
            continue
        if i >= len(accessions) or i >= len(dates) or i >= len(primary_docs):
            continue
        if dates[i] < cutoff:
            continue
        # Counted before the cap, so the caller can tell a complete window from
        # a truncated one instead of inferring it from the number of documents
        # it happened to receive.
        total += 1
        if len(out) < _INSIDER_MAX_DOCS_PER_TICKER:
            out.append({
                "accession": accessions[i],
                "filing_date": dates[i],
                "primary_doc": primary_docs[i],
            })
    return out, total


def _parse_form4_xml(cik, accession, primary_doc):
    """Fetch + parse one Form 4 XML. Returns list of nonDerivative transactions
    {date, code, shares, price, value, acquired_disposed, owner}. Open-market
    purchases are code='P', open-market sales are code='S'. Skips derivative
    table for v1 (options/restricted units add noise to the buy/sell signal).
    Note: SEC's primaryDocument path often points to the XSL-rendered HTML
    view (xslF345X06/wk-form4_*.xml). Strip that prefix to get raw XML."""
    _sec_throttle()
    try:
        acc_no_dashes = (accession or "").replace("-", "")
        # Drop the XSL stylesheet prefix when present so we get raw XML.
        doc_raw = primary_doc or ""
        if "/" in doc_raw:
            doc_raw = doc_raw.rsplit("/", 1)[-1]
        url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc_no_dashes}/{doc_raw}"
        req = urllib.request.Request(url, headers={"User-Agent": EDGAR_USER_AGENT, "Accept": "application/xml"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            xml_bytes = resp.read()
    except Exception:
        return []
    try:
        root = ET.fromstring(xml_bytes)
    except Exception:
        return []

    # XPath helpers tolerant of optional namespaces (Form 4 XML usually has none).
    def _find_text(elem, *paths):
        if elem is None:
            return None
        for p in paths:
            n = elem.find(p)
            if n is not None and n.text is not None and n.text.strip():
                return n.text.strip()
        return None

    owner = _find_text(root, "./reportingOwner/reportingOwnerId/rptOwnerName") or ""
    out = []
    for tx in root.findall("./nonDerivativeTable/nonDerivativeTransaction"):
        date = _find_text(tx, "./transactionDate/value")
        code = _find_text(tx, "./transactionCoding/transactionCode")
        shares_s = _find_text(tx, "./transactionAmounts/transactionShares/value")
        price_s = _find_text(tx, "./transactionAmounts/transactionPricePerShare/value")
        ad = _find_text(tx, "./transactionAmounts/transactionAcquiredDisposedCode/value")
        if not date or not code or not shares_s:
            continue
        try:
            shares = float(shares_s)
            price = float(price_s) if price_s else 0.0
        except ValueError:
            continue
        # Sign shares by acquired/disposed flag so totals add cleanly.
        signed_shares = shares if ad == "A" else -shares
        out.append({
            "date": date,
            "code": code,
            "shares": signed_shares,
            "price": price,
            "value": signed_shares * price,
            "owner": owner,
        })
    return out


def fetch_insider_form4(cik):
    """Top-level per-ticker Form 4 fetch. Returns (transactions, truncated) over
    the last _INSIDER_LOOKBACK_DAYS. Empty list when there is no Form 4 activity.

    truncated says the company filed more Form 4s in the window than the cap
    allows, so what came back is the most recent slice and not the window the
    field names claim. The caller drops the aggregates in that case: a net-buy
    total, a distinct-buyer count and a rolling-cluster maximum are all biased
    by which filings were kept, and no label repairs a dollar figure summed over
    an arbitrary subset.

    Raises if the SEC fetch failed, so the caller counts it as an error rather
    than as an absence of insider trading."""
    result = _fetch_recent_form4_filings(cik)
    if result is None:
        raise IOError(f"SEC submissions fetch failed for CIK {cik}")
    filings, total = result
    if not filings:
        return [], False
    txs = []
    for f in filings:
        txs.extend(_parse_form4_xml(cik, f["accession"], f["primary_doc"]))
    return txs, total > len(filings)


def compute_insider_signal(transactions):
    """Aggregate per-ticker insider transactions into Seyhun-style signals.
    Counts only open-market purchases (P) and sales (S); skips awards (A),
    option exercises (M), gifts (G), discretionary transactions (F), etc.
    Cluster score: max number of distinct buyers in any rolling 30-day window
    over the lookback period, normalized by 5. Seyhun's research shows that
    >=3 distinct insiders buying within 30 days is the strongest forward signal."""
    if not transactions:
        return {}

    purchases = [t for t in transactions if t["code"] == "P"]
    sales     = [t for t in transactions if t["code"] == "S"]

    # Net buy in dollars. Purchases have positive value (signed_shares > 0);
    # sales are negative. Sum gives net flow.
    net_buy_usd = sum(t["value"] for t in purchases) + sum(t["value"] for t in sales)
    buyer_count = len({t["owner"] for t in purchases if t.get("owner")})
    seller_count = len({t["owner"] for t in sales if t.get("owner")})

    # Cluster: scan 30-day rolling windows over the purchase dates.
    purchase_dates = sorted([(t["date"], t["owner"]) for t in purchases if t.get("owner")])
    max_cluster = 0
    if purchase_dates:
        from datetime import date as _date
        for i, (d_i, _) in enumerate(purchase_dates):
            try:
                start = _date.fromisoformat(d_i)
            except Exception:
                continue
            window_owners = set()
            for d_j, owner_j in purchase_dates[i:]:
                try:
                    end = _date.fromisoformat(d_j)
                except Exception:
                    continue
                if (end - start).days > 30:
                    break
                window_owners.add(owner_j)
            if len(window_owners) > max_cluster:
                max_cluster = len(window_owners)
    cluster_score = min(max_cluster / 5.0, 1.0) if max_cluster else 0.0

    return {
        "insider_net_buy_90d": round(net_buy_usd, 2),
        "insider_buyer_count_90d": buyer_count,
        "insider_seller_count_90d": seller_count,
        "insider_cluster_max_30d": max_cluster,
        "insider_cluster_score": round(cluster_score, 3),
        "insider_tx_count_90d": len(purchases) + len(sales),
    }


def enrich_with_insider(stocks, ticker_cik_map, max_workers=4):
    """For each ticker with a CIK, fetch recent Form 4 filings and aggregate the
    Seyhun signal. SEC enforces a strict 10 req/sec/IP limit; we throttle via
    _sec_throttle() to ~8 req/sec. To keep the workflow under the timeout, we
    only fetch insider data for the top _INSIDER_TOP_N_BY_MARKET_CAP tickers by
    market cap (small-cap Form 4 is noisier and has thinner coverage anyway)."""
    if not stocks or not ticker_cik_map:
        print("Insider enrichment: no stocks or empty CIK map, skipping.")
        return 0
    from concurrent.futures import ThreadPoolExecutor, as_completed

    # Largest-cap subset only. Stocks without a market_cap go to the back.
    # Non-operating listings are filtered BEFORE the slice: ten of them (TBB at a
    # fabricated $133bn, SOMN, DUKU, PPLC) held top-600 places with their
    # parent's cap, re-pulled the parent's Form 4s and pushed real companies out.
    cap_sorted = sorted((s for s in stocks if sectype.is_operating(s.get("security_type"))),
                        key=lambda s: -(s.get("market_cap") or 0))
    target = cap_sorted[:_INSIDER_TOP_N_BY_MARKET_CAP]
    by_ticker = {s["ticker"]: s for s in target}
    matched = [(t, ticker_cik_map.get(t)) for t in by_ticker.keys()]
    matched = [(t, cik) for t, cik in matched if cik]

    # Freshness is decided per ticker, the same way enrich_with_edgar decides it.
    # The caller's gate used to sample one cached record and skip the pass for
    # everybody, so a ticker that entered the top 600 after the last run could
    # never get its first pull: the names already stamped kept the gate closed.
    ins_year, ins_week, _ = datetime.now(EASTERN).isocalendar()

    def _insider_is_fresh(sym):
        stamp = (by_ticker.get(sym) or {}).get("insider_updated")
        if not stamp:
            return False
        try:
            y, w, _ = datetime.strptime(stamp, "%Y-%m-%d").date().isocalendar()
        except (TypeError, ValueError):
            return False
        return y == ins_year and w == ins_week

    total_matched = len(matched)
    matched = [(t, cik) for t, cik in matched if not _insider_is_fresh(t)]
    if total_matched != len(matched):
        print(f"Insider: {total_matched - len(matched)} of {total_matched} already stamped "
              f"this week, {len(matched)} to fetch.")
    today_str = datetime.now(EASTERN).strftime("%Y-%m-%d")

    def process(item):
        sym, cik = item
        try:
            txs, truncated = fetch_insider_form4(cik)
            if truncated:
                # More filings than we fetched, so every aggregate below would
                # describe a slice while being named for the window.
                return sym, {}, len(txs), None, True
            return sym, compute_insider_signal(txs), len(txs), None, False
        except Exception as e:
            return sym, {}, 0, str(e), False

    enriched = 0
    no_activity = 0
    errors = 0
    truncated_count = 0
    budget_hit = False
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = [ex.submit(process, item) for item in matched]
        for f in as_completed(futures):
            # Same reasoning as the yfinance and news budgets: a pass that
            # overruns the job timeout commits nothing at all. A ticker not
            # reached keeps its old insider_updated stamp and goes first next run.
            if not budget_hit and time.time() - t0 > _INSIDER_TIME_BUDGET_S:
                budget_hit = True
                for pending in futures:
                    pending.cancel()
            if f.cancelled():
                continue
            sym, signal, n_tx, err, truncated = f.result()
            s = by_ticker.get(sym)
            if not s:
                continue
            # Always stamp insider_updated even when signal is empty, so we know
            # we tried this ticker (avoids re-fetching on every workflow run).
            s["insider_updated"] = today_str
            if err:
                errors += 1
            elif truncated:
                truncated_count += 1
                for k in ("insider_net_buy_90d", "insider_buyer_count_90d",
                          "insider_seller_count_90d", "insider_cluster_max_30d",
                          "insider_cluster_score", "insider_tx_count_90d"):
                    s.pop(k, None)
            elif signal:
                s.update(signal)
                enriched += 1
            else:
                no_activity += 1
    elapsed = time.time() - t0
    print(f"Insider Form 4 enrichment: signals for {enriched}/{len(matched)} tickers "
          f"({no_activity} no activity, {errors} errors, {truncated_count} filed more "
          f"than {_INSIDER_MAX_DOCS_PER_TICKER} forms and were left unscored) "
          f"in {elapsed:.1f}s.")
    if budget_hit:
        print(f"Insider: stopped at the {_INSIDER_TIME_BUDGET_S}s budget. The rest "
              f"keep their previous stamp and are refetched next run, which is "
              f"cheaper than overrunning the job timeout and committing nothing.")
    # Insider buying is sparse, but not this sparse. Across several hundred large
    # caps some Form 4 activity is a near certainty in any 90-day window, so a
    # universal blank is a broken parser or a blocked IP, not a quiet market.
    if matched and enriched == 0:
        print(f"Insider: WARNING, 0 of {len(matched)} tickers produced a signal. "
              f"Across this many large caps that is a fetch or parse failure, "
              f"not an absence of insider trading.")
    elif errors and errors > len(matched) * 0.25:
        print(f"Insider: WARNING, {errors} of {len(matched)} fetches failed; "
              f"insider fields are incomplete for this run.")
    return enriched


# compute_edgar_factors fields whose absence from a fresh result means the
# filings do not support a value, so a carried-forward one must go too.
_EDGAR_WITHHELD_ON_ABSENCE = ("ttm_eps_diluted", "prior_ttm_eps_diluted", "eps_basis")


def enrich_with_edgar(stocks, ticker_cik_map, max_workers=8):
    """For each stock with a CIK match, fetch EDGAR companyfacts and compute the
    5 quarterly-trend factors. Updates dicts in place. Honors SEC's 10 req/sec
    rate limit via 8 worker threads (each thread sleeps minimally between calls).
    Returns count of tickers enriched."""
    if not stocks or not ticker_cik_map:
        print("EDGAR enrichment: no stocks or empty CIK map, skipping.")
        return 0
    from concurrent.futures import ThreadPoolExecutor, as_completed

    # A note, a trust certificate or a unit listing resolves to its PARENT's CIK
    # in SEC's own ticker map, so fetching "its" companyfacts wrote the parent's
    # shares and EPS onto the listing: ADAMG, a 9.125% note, came out with
    # Adamas Trust's 89.9M shares and a $2.25bn market cap. Skipped here, and
    # anything already carried forward is withheld by apply_security_types.
    by_ticker = {s["ticker"]: s for s in stocks
                 if s.get("security_type") not in sectype.DEBT_LIKE}
    matched = [(t, ticker_cik_map.get(t)) for t in by_ticker.keys()]
    matched = [(t, cik) for t, cik in matched if cik]

    today_str = datetime.now(EASTERN).strftime("%Y-%m-%d")
    iso_year, iso_week, _ = datetime.now(EASTERN).isocalendar()

    # Freshness is decided per ticker, not for the run as a whole. The caller
    # used to skip the entire pass whenever ANY cached stock carried a stamp
    # from this week, which meant that when the universe grew from 1,506 to
    # 5,336 the 3,830 new names inherited a "done" flag they had no part in:
    # every one of them showed no XBRL data, and would have kept showing none
    # for as long as the older names kept the weekly gate satisfied.
    def is_fresh(sym):
        stamp = (by_ticker.get(sym) or {}).get("edgar_updated")
        if not stamp:
            return False
        try:
            d = datetime.strptime(stamp, "%Y-%m-%d").date()
        except (TypeError, ValueError):
            return False
        y, w, _ = d.isocalendar()
        return y == iso_year and w == iso_week

    # A field added after the last run cannot wait for the ISO week to turn:
    # the tickers that would carry it are exactly the ones already stamped.
    # accruals_ratio sat at 0 of 5,336 for precisely this reason, which left
    # Quality scoring on 4 of its 5 inputs for every company in the universe.
    # EDGAR_SCHEMA_SENTINELS is the list, kept beside compute_edgar_factors so
    # that adding a field and forgetting to register it is harder to do.
    schema_gap = [f for f in EDGAR_SCHEMA_SENTINELS
                  if not any(s.get(f) is not None for s in stocks)]
    if schema_gap:
        print(f"EDGAR: {', '.join(schema_gap)} missing across the whole cache, "
              f"ignoring the weekly stamp and refetching everything.")

    total_matched = len(matched)
    if schema_gap:
        # Tickers still missing the field that opened the gap go first, so each
        # run closes part of it instead of every run redoing the same head of
        # the list and never reaching the tail.
        missing = schema_gap[0]
        matched.sort(key=lambda tc: 0 if (by_ticker.get(tc[0]) or {}).get(missing) is None else 1)
    else:
        matched = [(t, cik) for t, cik in matched if not is_fresh(t)]
        if total_matched != len(matched):
            print(f"EDGAR: {total_matched - len(matched)} tickers already stamped this week, "
                  f"{len(matched)} to fetch.")

    if len(matched) > _EDGAR_MAX_FETCH_PER_RUN:
        print(f"EDGAR: {len(matched)} tickers to fetch, capping at "
              f"{_EDGAR_MAX_FETCH_PER_RUN} for this run. The remainder follow on "
              f"later runs; a sweep that overruns the job timeout commits nothing "
              f"at all, so partial progress that lands beats a full pass that does not.")
        matched = matched[:_EDGAR_MAX_FETCH_PER_RUN]

    def process(item):
        sym, cik = item
        facts = fetch_edgar_company_facts(cik)
        if not facts:
            return sym, {}
        out = compute_edgar_factors(facts)
        # Benford analysis on the same fetched facts (no extra HTTP)
        benford = compute_benford(facts)
        if benford:
            out["benford"] = benford
        # Three fiscal years of history for the style books, same facts again.
        try:
            hist = compute_style_history(facts, sym, cik)
        except Exception as exc:
            print(f"style history: {sym} skipped ({type(exc).__name__}: {exc}).")
            hist = None
        return sym, (out, hist)

    enriched = 0
    budget_hit = False
    histories = []
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = [ex.submit(process, item) for item in matched]
        for f in as_completed(futures):
            # Same reasoning as every other fetch pass: a job killed by the
            # timeout commits nothing at all, so stopping with partial coverage
            # that lands beats a full sweep that does not.
            if not budget_hit and time.time() - t0 > _EDGAR_TIME_BUDGET_S:
                budget_hit = True
                for pending in futures:
                    pending.cancel()
            if f.cancelled():
                continue
            sym, result = f.result()
            factors, hist = result if isinstance(result, tuple) else (result, None)
            if hist:
                histories.append(hist)
            if not factors:
                continue
            s = by_ticker.get(sym)
            if s:
                s.update(factors)
                # A fresh fetch that withholds EPS is a decision, not a gap.
                # update() alone would keep the value carried forward from the
                # previous cache, which is the number that was withheld: Alight
                # would have kept its 34.37 after the fix that refuses it.
                for f in _EDGAR_WITHHELD_ON_ABSENCE:
                    if f not in factors:
                        s.pop(f, None)
                s["edgar_updated"] = today_str
                enriched += 1
    elapsed = time.time() - t0
    try:
        record_style_history(histories, datetime.now(EASTERN).isoformat(timespec="seconds"))
    except Exception as exc:
        print(f"style history: not recorded ({type(exc).__name__}: {exc}).")
    print(f"EDGAR enrichment: enriched {enriched}/{len(matched)} matched tickers "
          f"({len(by_ticker) - len(matched)} no CIK match) in {elapsed:.1f}s.")
    if budget_hit:
        print(f"EDGAR: stopped at the {_EDGAR_TIME_BUDGET_S}s budget with {enriched} "
              f"of {len(matched)} done. The rest keep their previous stamp and go "
              f"first next run.")
    return enriched


# ── Stocks universe (weekly cached) ─────────────────────────────────────────

# -- Peer scoring: sector z-scores and percentile ranks ---------------------
#
# Computed here rather than in the browser. The page used to build these stats at
# load time; at 5,336 tickers x 20 fields that is a large accumulation before the
# first paint, and it has to be redone on every reload.
#
# The four dimension scores are emitted, NOT the composite. The composite is a
# weighted mean and the page has sliders for those weights, so it stays on the
# client where four multiplications are free.

# Mirrors SCORE_GROUPS in STOCKS_JS_TEMPLATE. Fields where a LOWER raw value is
# better are inverted so that, everywhere downstream, higher always means better.
SCORE_GROUPS_PY = {
    "Growth":   {"fields": ["revenue_growth_yoy", "eps_growth_yoy", "revenue_acceleration",
                            "gross_margin_trend", "fcf_growth_yoy"], "invert": []},
    "Value":    {"fields": ["pe", "ev_ebitda", "ev_revenue", "price_book", "fcf_yield"],
                 "invert": ["pe", "ev_ebitda", "ev_revenue", "price_book"]},
    "Momentum": {"fields": ["return_12_2", "return_1m", "high52w_proximity",
                            "rel_strength_sp500", "volume_trend"], "invert": []},
    "Quality":  {"fields": ["roe_ttm", "earnings_consistency", "net_debt_ebitda",
                            "op_margin_stability", "accruals_ratio"],
                 "invert": ["net_debt_ebitda", "op_margin_stability", "accruals_ratio"]},
}
SCORE_FIELDS = [f for g in SCORE_GROUPS_PY.values() for f in g["fields"]]

# Ranked against sector peers and shown, but never summed into a dimension or
# the composite. Order matters: it is appended to the positional pct array.
DISPLAY_PCT_FIELDS = ["news_vader_avg", "news_lm_avg", "news_count_7d", "neglect_score",
                      "volatility_1y", "beta_1y", "sharpe_1y", "max_drawdown_1y"]
PCT_ARRAY_FIELDS = SCORE_FIELDS + DISPLAY_PCT_FIELDS
INVERTED_FIELDS = {f for g in SCORE_GROUPS_PY.values() for f in g["invert"]}

# A percentile needs a cohort large enough to mean something. With 20 peers the
# finest distinction expressible is 5 points; below that a "73rd percentile"
# claims precision the sample cannot support.
MIN_COHORT_FOR_PERCENTILE = 20
# A z-score divides by the cohort's standard deviation, so the cohort has to be
# big enough for that number to mean something. This was an inline 5, while the
# percentile beside it required 20: the figure used to rank a stock was computed
# on samples too small to show it a percentile for. An sd from five observations
# carries roughly 35% relative error, which lands directly in every z built on
# it. One threshold now, for both.
MIN_COHORT_FOR_ZSCORE = MIN_COHORT_FOR_PERCENTILE

# A dimension needs enough of its five inputs present to be called a score.
MIN_FIELDS_PER_DIMENSION = 2
# And a composite needs enough dimensions, or a stock rated on Growth alone would
# be ranked against one rated on all four as though they were the same claim.
MIN_DIMENSIONS_FOR_COMPOSITE = 3


def _finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


# ── Security type: label every listing, withhold what does not apply ─────────
#
# Every issuer-level number on a note, trust certificate or unit listing is its
# parent's. Measured on 2026-09-21 across 151 such rows: all 116 market caps were
# the listing's price times the PARENT's shares, and 84 of 88 P/Es were the
# listing's price over the parent's EPS (AFGB, a baby bond, at a P/E of 1.94).
# The price-derived fields (price, returns, volatility, drawdown, beta) are the
# listing's own series and are kept.
_NA_DEBT_LIKE = (
    "market_cap", "pe", "price_book", "roe_ttm", "gross_margin", "operating_margin",
    "fcf_yield", "ev_ebitda", "ev_revenue", "net_debt_ebitda", "accruals_ratio",
    "earnings_consistency", "revenue_growth_yoy", "eps_growth_yoy",
    "revenue_acceleration", "gross_margin_trend", "fcf_growth_yoy", "op_margin_stability",
    "ttm_revenue", "ttm_gross_profit", "ttm_operating_income", "ttm_net_income",
    "ttm_eps_diluted", "ttm_dep_amort", "ttm_fcf", "ttm_ebitda",
    "prior_ttm_revenue", "prior_ttm_eps_diluted",
    "shares_outstanding", "equity", "prior_equity", "cash_and_investments", "total_debt",
    "insider_net_buy_90d", "insider_buyer_count_90d", "insider_seller_count_90d",
    "insider_cluster_max_30d", "insider_cluster_score", "insider_tx_count_90d",
)
# The parent's bookkeeping behind those fields: removed, but not stamped, since
# they are stamps and nested structures rather than values a reader looks up.
_DROP_DEBT_LIKE = ("op_margin_history", "benford", "fiscal_period_end", "eps_basis",
                   "edgar_updated", "insider_updated")
# Withheld whatever their value, per type. A shell's margins are Yahoo's 0.0
# placeholder (289 of 292 on 2026-09-21), a fund's P/E and margins are vendor
# numbers with no filing behind them (CCD at a P/E of 2.71), and a lender or a
# royalty trust has no cost of goods, so its gross margin is the vendor's 1.0.
_NA_ALWAYS = {
    "debt": _NA_DEBT_LIKE, "structured": _NA_DEBT_LIKE, "equity_units": _NA_DEBT_LIKE,
    "spac": ("gross_margin", "operating_margin"),
    "cef": ("pe", "gross_margin", "operating_margin"),
    "bdc": ("gross_margin",),
    "royalty_trust": ("gross_margin",),
}
# Withheld only when exactly zero: a shell's ratios are otherwise its own trust
# arithmetic (86 carry a P/E from trust income), but a 0.0 is a placeholder.
_NA_IF_ZERO = {
    "spac": ("pe", "price_book", "roe_ttm", "fcf_yield", "ev_ebitda", "ev_revenue",
             "net_debt_ebitda"),
}


def apply_security_types(stocks):
    """Label every row with security_type and withhold the fields that do not
    apply to it, stamping status "not_applicable" on each. Returns a Counter of
    labels.

    Pure over the row and idempotent, so it runs on every path that hands a
    universe onward: the fresh build, the same-day cache, both stale fallbacks
    and publish mode. A label computed once and cached would be stale or missing
    on exactly the runs that skip the rebuild.

    Order matters on the fresh path. It has to run AFTER
    derive_ratios_from_fundamentals, which rebuilds market cap and P/E from
    shares and EPS, and after the carry-forward merge, which refills any absent
    field from the previous cache. Anything withheld earlier comes straight back.
    Its output is safe to carry: the classifier never reads a field this
    removes, apart from gross_margin, which it reads as "no real gross margin"
    so that a blank still counts.

    Rows are withheld from, never removed. record_fundamentals skips a row with
    neither price nor market cap; every debt-like row on 2026-09-21 had a price
    of its own, so the panel keeps all of them."""
    labels = collections.Counter()
    withheld = 0
    for s in stocks:
        cat = sectype.classify_row(s)
        s["security_type"] = cat
        labels[cat] += 1
        always = _NA_ALWAYS.get(cat, ())
        if_zero = _NA_IF_ZERO.get(cat, ())
        status = s.get("status") or {}
        # A label can change (a SPAC completes its merger), and the same-day
        # cache keeps the status dict, so clear stamps that no longer hold.
        for f in [k for k, v in status.items() if v == "not_applicable"]:
            if (f not in always and f not in if_zero) or s.get(f) is not None:
                del status[f]
        for f in always:
            if s.pop(f, None) is not None:
                withheld += 1
            status[f] = "not_applicable"
        for f in if_zero:
            if s.get(f) == 0:
                s.pop(f)
                withheld += 1
                status[f] = "not_applicable"
        if cat in sectype.DEBT_LIKE:
            for f in _DROP_DEBT_LIKE:
                s.pop(f, None)
        if status:
            s["status"] = status
        else:
            s.pop("status", None)
    non_op = sum(n for k, n in labels.items() if k in sectype.NON_OPERATING)
    print(f"security_type: {len(stocks) - non_op} operating, {non_op} non-operating "
          f"({', '.join(f'{k} {labels[k]}' for k in sectype.SECURITY_TYPES if labels[k])}); "
          f"{withheld} inapplicable values withheld.")
    return labels


def _relabel_cached_universe(stocks):
    """Label a universe that did not come from a fresh build, then re-score it.

    The same-day cache, the stale fallbacks and publish mode hand the cached
    dicts onward without running compute_peer_scores, so a cache written before
    this label existed would publish shells and funds as scorable members of
    Financials. Scoring is pure arithmetic over fields already on the rows, so
    re-running it is cheap and changes nothing else."""
    if not stocks:
        return
    apply_security_types(stocks)
    compute_peer_scores(stocks)


def compute_peer_scores(stocks):
    """Stamp g/v/m/q dimension scores and per-field sector percentiles onto each stock.

    Sets, per stock: g, v, m, q (sector z-scores, None when under-covered),
    dims_present (0-4), and pct (a dict of field -> 0-100 percentile rank).
    Returns a summary dict for logging.

    Non-operating listings (security_type in NON_OPERATING) are neither peers
    nor scored. Shells and funds were 20% of the Financials cohort, with
    near-zero volatility and returns that moved real Financials names by up to
    14 percentile points on price-derived fields, and three shells (NBRG, UYSC,
    WTG) were scorable on trust-account income."""
    # Cohort values per sector per field, non-null only.
    cohorts = {}
    for s in stocks:
        sector = s.get("sector")
        if not sector:
            continue          # no sector means no peers; scored as unknown below
        if not sectype.is_operating(s.get("security_type")):
            continue          # not a business, so not anybody's peer
        bucket = cohorts.setdefault(sector, {})
        for f in PCT_ARRAY_FIELDS:
            v = s.get(f)
            if _finite(v):
                bucket.setdefault(f, []).append(v)

    # Mean/stddev for z-scores, and a sorted copy for percentile ranks.
    stats = {}
    for sector, fields in cohorts.items():
        st = stats.setdefault(sector, {})
        for f, vals in fields.items():
            n = len(vals)
            mean = sum(vals) / n
            var = sum((v - mean) ** 2 for v in vals) / n
            st[f] = {"n": n, "mean": mean, "sd": var ** 0.5, "sorted": sorted(vals)}

    def percentile_of(sorted_vals, v):
        """Fraction of the cohort at or below v, as 0-100."""
        lo, hi = 0, len(sorted_vals)
        while lo < hi:
            mid = (lo + hi) // 2
            if sorted_vals[mid] <= v:
                lo = mid + 1
            else:
                hi = mid
        return round(lo / len(sorted_vals) * 100)

    scored = collections.Counter()
    pct_emitted = 0
    excluded = 0
    for s in stocks:
        if not sectype.is_operating(s.get("security_type")):
            # Every screener gate keys on scorable, so this one line removes the
            # row from ranking, the composite, the map and its scale.
            s["g"] = s["v"] = s["m"] = s["q"] = None
            s["dims_present"] = 0
            s["pct"] = None
            s["scorable"] = 0
            excluded += 1
            continue
        sector_stats = stats.get(s.get("sector") or "", {})
        pct = {}
        dim_scores = {}
        for dim, group in SCORE_GROUPS_PY.items():
            zs = []
            for f in group["fields"]:
                v = s.get(f)
                st = sector_stats.get(f)
                if not _finite(v) or not st:
                    continue
                if st["n"] >= MIN_COHORT_FOR_PERCENTILE:
                    p = percentile_of(st["sorted"], v)
                    # Invert so that a high percentile always reads as "better",
                    # matching the direction of the z-scores and the radar.
                    pct[f] = (100 - p) if f in INVERTED_FIELDS else p
                    pct_emitted += 1
                if st["sd"] and st["n"] >= MIN_COHORT_FOR_ZSCORE:
                    z = (v - st["mean"]) / st["sd"]
                    if f in group["invert"]:
                        z = -z
                    zs.append(max(-3.0, min(3.0, z)))
            # A plain mean of k z-scores has spread proportional to 1/sqrt(k),
            # so a dimension resting on two fields swings wider than the same
            # dimension resting on five, purely from having been averaged less.
            # It is an artefact of the arithmetic, not a claim about the company,
            # and it puts thinly covered names at both extremes of any sort.
            # Measured on the live universe: Growth ran sd 0.740 at k=2 against
            # 0.407 at k=5, almost exactly the 1.82x that 1/sqrt(k) predicts, and
            # stocks with two fields or fewer were 12.3% of the Growth pool but
            # 28% of its top 50.
            #
            # Scaling by sqrt(k/K) cancels it exactly, bringing every k down to
            # the spread of a fully covered dimension. It only ever shrinks: at
            # k = K the factor is 1, so a completely measured company is left
            # alone and a half-measured one is pulled toward the middle it has
            # not earned its distance from.
            if len(zs) >= MIN_FIELDS_PER_DIMENSION:
                coverage = (len(zs) / len(group["fields"])) ** 0.5
                dim_scores[dim] = round(sum(zs) / len(zs) * coverage, 4)
            else:
                dim_scores[dim] = None

        s["g"] = dim_scores["Growth"]
        s["v"] = dim_scores["Value"]
        s["m"] = dim_scores["Momentum"]
        s["q"] = dim_scores["Quality"]
        present = sum(1 for d in dim_scores.values() if d is not None)
        s["dims_present"] = present
        # Positional, in SCORE_FIELDS order, not a dict. Twenty full field names
        # repeated across 5,336 stocks cost 0.72 MB in key strings alone, which
        # was 80% of what this map weighed in the payload.
        # The display fields are ranked here rather than inside the dimension
        # loop above, so there is no path by which they can reach a z-score.
        for f in DISPLAY_PCT_FIELDS:
            v = s.get(f)
            st = sector_stats.get(f)
            if _finite(v) and st and st["n"] >= MIN_COHORT_FOR_PERCENTILE:
                pct[f] = percentile_of(st["sorted"], v)
                pct_emitted += 1
        s["pct"] = [pct.get(f) for f in PCT_ARRAY_FIELDS]
        if not any(v is not None for v in s["pct"]):
            s["pct"] = None
        # Only stocks measured on enough dimensions get a rankable score. The rest
        # keep their dimension values for display but are not ranked against them.
        s["scorable"] = 1 if present >= MIN_DIMENSIONS_FOR_COMPOSITE else 0
        scored[present] += 1

    summary = {
        "sectors": len(stats),
        "dims_distribution": dict(sorted(scored.items())),
        "scorable": sum(v for k, v in scored.items() if k >= MIN_DIMENSIONS_FOR_COMPOSITE),
        "percentiles_emitted": pct_emitted,
        "non_operating_excluded": excluded,
    }
    # The denominator is operating listings: a note or a fund is not a company
    # the scorer failed on, so counting it would understate coverage.
    print(f"scoring: {summary['sectors']} sector cohorts; "
          f"{summary['scorable']}/{len(stocks) - excluded} operating stocks scorable "
          f"(>= {MIN_DIMENSIONS_FOR_COMPOSITE} of 4 dimensions); "
          f"{excluded} non-operating listings excluded from cohorts and scoring; "
          f"dimension counts {summary['dims_distribution']}; "
          f"{pct_emitted:,} percentile ranks emitted.")
    return summary


def get_or_generate_stocks_universe(session_confirmed=False):
    """Cached US stocks universe scraped from Wikipedia (S&P 500/400/600), enriched
    with live quote data from Yahoo Finance via yfinance.

    Cache strategy: reuse the cache when it is from today (Eastern) and passes the
    schema check below. Otherwise re-pull the constituent lists, merge the previous
    enrichment as a fallback layer, then attempt a fresh yfinance pass. Yahoo
    throttles hard enough that one run reaches roughly 850 tickers, so the cache is
    what accumulates a full sweep across about a week of runs.

    The cache is state/stocks_universe.json and is not in git. CI restores it from
    the universe-cache release asset before the run and saves it back after.

    `session_confirmed` goes to enrich_with_prices: the caller has proof the
    expected session traded, so price files lacking its bar are retried after
    45 minutes rather than six hours."""
    now = datetime.now(EASTERN)
    iso_year, iso_week, _ = now.isocalendar()
    week_key = f"{iso_year}-W{iso_week:02d}"
    date_key = now.strftime("%Y-%m-%d")
    cache_path = STATE_DIR / "stocks_universe.json"

    last_known = None
    if cache_path.exists():
        try:
            last_known = json.loads(cache_path.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"stocks_universe: cache read error: {e}")

    # Short-circuit when the cache is from today and passes the schema check.
    # News still gets a chance to fetch on its own cadence so a redeploy shortly
    # after a run does not wait until tomorrow. Any cached stock lacking the newer
    # fields (see schema_ok) forces a full rebuild even on a same-day cache.
    cached_stocks = (last_known or {}).get("stocks") or []
    schema_ok = (
        any(s.get("op_margin_history") for s in cached_stocks)
        and any((s.get("benford") or {}).get("mad") is not None for s in cached_stocks)
        and any(s.get("analyst_count") is not None for s in cached_stocks)
        # Need at least one ticker with an actual signal (not just the timestamp),
        # otherwise we're trusting a known-broken first run.
        and any(s.get("insider_tx_count_90d") for s in cached_stocks)
    )
    # Refreshed daily, not weekly: the yfinance fields (price, market cap, P/E,
    # momentum) genuinely move day to day, and the CSV panel wants real daily
    # rows rather than one value repeated seven times. The expensive EDGAR and
    # insider passes below keep their own ISO-week gates, so filings data is
    # still pulled once a week.
    if last_known and last_known.get("date") == date_key and schema_ok:
        print(f"stocks_universe: already refreshed today ({date_key}), using cache.")
        # News + prices have their own per-file caches; these calls are no-ops for
        # tickers already cached and just fill any holes.
        cached_list = last_known.get("stocks") or []
        enrich_with_news(cached_list)
        aggregate_news_sentiment(cached_list)
        compute_neglect_score(cached_list)
        enrich_with_prices(cached_list, session_confirmed=session_confirmed)
        derive_from_price_history(cached_list)
        enrich_with_market_series()
        _benchmarks_quietly()
        derive_risk_metrics(cached_list)
        derive_ratios_from_fundamentals(cached_list)
        # After the ratios, which would otherwise rebuild what this withholds.
        _relabel_cached_universe(cached_list)
        return last_known
    if last_known and not schema_ok:
        print("stocks_universe: schema bump, bypassing the daily cache.")

    # Build fresh universe from Wikipedia (S&P 500/400/600) + iShares (Russell 1000/2000)
    stocks = fetch_all_universes()
    if not stocks:
        print("stocks_universe: Wikipedia + iShares returned nothing, falling back to last cache.")
        _relabel_cached_universe((last_known or {}).get("stocks") or [])
        # Tag the fallback so the caller can tell a real scrape from a repeat of
        # yesterday. Its prices are the prior session's and must not be written
        # into the panel under today's date.
        return {**(last_known or {"iso_week": week_key, "generated_at": now.isoformat(), "stocks": []}),
                "stale": True}

    # A PARTIAL scrape is more dangerous than a total one, because it looks fine.
    # fetch_wikipedia_constituents returns [] on a timeout, an HTTP error or a table
    # layout change, and fetch_all_universes just concatenates whatever it gets. If
    # one of the three S&P pages fails, ~600 live constituents go missing, the
    # reconciliation below marks every one of them dropped, and in_index=0 is written
    # into the append-only panel. The registry self-heals the next day; the panel
    # never does, because record_fundamentals refuses to rewrite an existing date.
    # The row count is unchanged on such a day (false drops are re-added as
    # retained), so only this check catches it. Real single-day index turnover is
    # a fraction of a percent; anything past 10% is a scrape failure by definition.
    prior_active = sum(1 for r in load_ticker_registry().values()
                       if r.get("status") == "active")
    if prior_active and len(stocks) < 0.9 * prior_active:
        print(f"stocks_universe: ABORT, scrape returned {len(stocks)} tickers vs "
              f"{prior_active} active yesterday ({len(stocks) / prior_active:.0%}). "
              f"Treating as a source failure, not {prior_active - len(stocks)} delistings. "
              f"Keeping yesterday's universe; the registry and in_index are untouched.")
        _relabel_cached_universe((last_known or {}).get("stocks") or [])
        # Tag the fallback so the caller can tell a real scrape from a repeat of
        # yesterday. Its prices are the prior session's and must not be written
        # into the panel under today's date.
        return {**(last_known or {"iso_week": week_key, "generated_at": now.isoformat(), "stocks": []}),
                "stale": True}

    # A first label from the name, so the registry carries one even if the run
    # dies before enrichment. Relabeled from data after yfinance and again after
    # the last derivation; the registry is saved a second time with the final one.
    for s in stocks:
        s["security_type"] = sectype.classify_row(s)

    # Reconcile against the permanent registry and mark who is currently indexed.
    registry, added, dropped, retained = update_ticker_registry(
        stocks, date_key, previously_known=(last_known or {}).get("stocks") or [])
    for s in stocks:
        s["in_index"] = 1

    # Heal sector labels already carried forward from previous runs, which may
    # predate the GICS mapping and would otherwise keep their Yahoo spelling
    # forever via CARRY_FIELDS.
    remapped = 0
    for s in stocks:
        current = s.get("sector")
        if current:
            mapped = normalize_sector(current)
            if mapped != current:
                s["sector"] = mapped
                remapped += 1
    if remapped:
        print(f"sector: normalized {remapped} labels to GICS.")
    if added:
        print(f"registry: {len(added)} new tickers: {', '.join(sorted(added)[:12])}"
              + (" ..." if len(added) > 12 else ""))
    if dropped:
        print(f"registry: {len(dropped)} left the index: {', '.join(sorted(dropped)[:12])}"
              + (" ..." if len(dropped) > 12 else ""))

    # Keep collecting recently-dropped names. Without this the panel would only
    # ever contain survivors, and any return computed over it would be overstated.
    # They carry in_index=0 so consumers can filter, and their last known static
    # fields come from the registry since the index scrape no longer supplies them.
    current_tickers = {s["ticker"] for s in stocks}
    revived = 0
    for ticker in retained:
        if ticker in current_tickers:
            continue
        reg_row = registry.get(ticker, {})
        # Seed ONLY the static identity fields. Everything else is left absent so
        # the CARRY_FIELDS merge below fills it from the previous cache, which
        # deliberately excludes price, change_pct and volume. Copying the whole
        # prior record here would smuggle yesterday's price into a row stamped
        # with today's date, which is precisely the staleness the merge avoids.
        stocks.append({
            "ticker": ticker,
            "name": reg_row.get("name", ""),
            "sector": reg_row.get("sector", ""),
            "sub_industry": reg_row.get("sub_industry", ""),
            "index": reg_row.get("index", ""),
            "in_index": 0,
        })
        revived += 1
    if revived:
        print(f"registry: still collecting {revived} dropped tickers "
              f"(within {RETAIN_DROPPED_DAYS} days of removal).")
    save_ticker_registry(registry)

    # Merge static enrichment fields from previous cache as a fallback layer.
    # Slow-changing fields are carried forward; volatile intraday fields (price,
    # change_pct, volume) are NOT, to avoid showing yesterday's number as today's.
    CARRY_FIELDS = (
        "market_cap", "pe", "sector", "sub_industry",
        "revenue_growth_yoy", "eps_growth_yoy",
        "ev_ebitda", "ev_revenue", "price_book", "fcf_yield",
        "high52w_proximity", "return_1m", "return_52w", "return_12_2",
        "rel_strength_sp500", "volume_trend",
        "roe_ttm", "net_debt_ebitda", "accruals_ratio",
        "operating_margin", "gross_margin",
        # EDGAR-derived (refreshed weekly)
        "revenue_acceleration", "gross_margin_trend", "fcf_growth_yoy",
        "earnings_consistency", "op_margin_stability", "op_margin_history", "edgar_updated",
        # Trailing aggregates and balance-sheet values behind the ratios. The
        # EDGAR sweep is capped per run, so without these a ticker's ratios
        # would vanish on any rebuild that did not happen to refetch it.
        "ttm_revenue", "ttm_gross_profit", "ttm_operating_income", "ttm_net_income",
        "ttm_eps_diluted", "ttm_dep_amort", "ttm_fcf", "ttm_ebitda",
        "prior_ttm_revenue", "prior_ttm_eps_diluted", "eps_basis",
        "shares_outstanding", "equity", "prior_equity",
        "cash_and_investments", "total_debt", "fiscal_period_end",
        "benford",
        # News-derived sentiment aggregates
        "news_lm_avg", "news_vader_avg", "news_count_7d",
        # Neglect inputs + composite (Lynch)
        "analyst_count", "inst_ownership", "insider_ownership", "neglect_score",
        # Index membership: 1 currently indexed, 0 retained after removal
        "in_index",
        # Insider Form 4 signals (Seyhun, refreshed weekly)
        "insider_net_buy_90d", "insider_buyer_count_90d", "insider_seller_count_90d",
        "insider_cluster_max_30d", "insider_cluster_score", "insider_tx_count_90d",
        "insider_updated",
        # Per-row freshness + earnings calendar
        "last_updated", "earnings_date", "prices_updated",
    )
    def _absent(v):
        """Missing, for carry-forward purposes.

        The test used to be `is None`, which is right for the numeric fields and
        wrong for every string one. A fresh universe build sets sector to "" for
        anything the index scrapes do not classify, normalize_sector returns ""
        for a label it does not recognise, and "" is not None. So 3,833 tickers
        could never inherit a sector: whatever yfinance learned about them was
        discarded on the next rebuild, and sector coverage sat at exactly the
        number of names Wikipedia classifies, 1,506, no matter how many times
        the pass ran.

        That gates everything downstream. No sector means no peer cohort, no
        cohort means no z-scores, and no z-scores means not scorable."""
        return v is None or (isinstance(v, str) and not v.strip())

    carried_forward = 0
    sectors_carried = 0
    if last_known and last_known.get("stocks"):
        prev_by_ticker = {s["ticker"]: s for s in last_known["stocks"]}
        for s in stocks:
            prev = prev_by_ticker.get(s["ticker"])
            if not prev:
                continue
            for field in CARRY_FIELDS:
                # A P/E we computed from filings is recomputed from the carried
                # EPS below, or deliberately not; carrying the old ratio would
                # bring back the one derive_ratios_from_fundamentals withheld,
                # labeled as the vendor's. Only the vendor's own P/E is carried.
                if field == "pe" and (prev.get("status") or {}).get("pe") == "awaiting_filing":
                    continue
                if not _absent(prev.get(field)) and _absent(s.get(field)):
                    s[field] = prev[field]
                    if field == "sector":
                        sectors_carried += 1
            if prev.get("market_cap"):
                carried_forward += 1
    if carried_forward:
        print(f"stocks_universe: carried forward enrichment for {carried_forward} tickers "
              f"from previous cache ({sectors_carried} sectors).")

    # Fresh yfinance pass overwrites carried-forward data where successful and adds
    # the intraday fields (price, change_pct, volume).
    fresh_count = enrich_with_yfinance(stocks)

    # Relabel now that Yahoo's sub_industry is known ("Shell Companies" is the
    # only tell for 21 shells), before the EDGAR and insider passes read it.
    for s in stocks:
        s["security_type"] = sectype.classify_row(s)

    # EDGAR enrichment: weekly cadence (heavy: ~3 min for 1500 tickers).
    # Skip if any cached ticker has edgar_updated stamped within this ISO week.
    # Schema bump: if no cached stock has op_margin_history yet, force a re-run
    # so newly-added EDGAR-derived fields populate without waiting a week.
    # enrich_with_edgar now decides freshness per ticker, so the only question
    # here is whether anything is stale at all. Counting rather than sampling:
    # the previous check looked at the FIRST stock carrying a stamp and skipped
    # the whole pass on that basis, which is how 3,830 newly added tickers ended
    # up with no XBRL data at all.
    def _edgar_is_current(stock):
        stamp = stock.get("edgar_updated")
        if not stamp:
            return False
        try:
            y, w, _ = datetime.strptime(stamp, "%Y-%m-%d").date().isocalendar()
        except (TypeError, ValueError):
            return False
        return y == iso_year and w == iso_week

    # Reported, not gated. enrich_with_edgar stamps edgar_updated only on a
    # successful fetch, so a ticker with no CIK or no companyfacts is never
    # stamped and this count can never reach zero. The old skip branch was
    # unreachable and its message claimed a decision the code never made. The
    # per-ticker is_fresh check inside the pass is what actually saves the work.
    stale = sum(1 for s in stocks if not _edgar_is_current(s))
    should_run_edgar = True
    print(f"EDGAR: {stale} of {len(stocks)} tickers unstamped for this week; "
          f"the pass filters per ticker.")
    edgar_count = 0
    cik_map = None
    if should_run_edgar:
        cik_map = fetch_edgar_ticker_cik_map()
        if cik_map:
            edgar_count = enrich_with_edgar(stocks, cik_map)

    # Insider Form 4 enrichment: weekly cadence, gated like EDGAR. Heavy: ~12-15 min
    # for 2941 tickers. Schema bump triggers a re-run when insider_updated is missing
    # across the cached universe.
    # enrich_with_insider now decides freshness per ticker, so the only question
    # here is whether anything in the top 600 is stale at all. Counting rather
    # than sampling: the old check looked at the FIRST stock carrying a stamp and
    # skipped the pass on that basis, which is how names that entered the top 600
    # after the previous run were never pulled even once.
    def _insider_is_current(stock):
        stamp = stock.get("insider_updated")
        if not stamp:
            return False
        try:
            y, w, _ = datetime.strptime(stamp, "%Y-%m-%d").date().isocalendar()
        except (TypeError, ValueError):
            return False
        return y == iso_year and w == iso_week

    # The same operating-only slice enrich_with_insider takes, or this count
    # would ask about notes the pass will never fetch.
    top_n = sorted((s for s in stocks if sectype.is_operating(s.get("security_type"))),
                   key=lambda s: -(s.get("market_cap") or 0))[:_INSIDER_TOP_N_BY_MARKET_CAP]
    insider_stale = sum(1 for s in top_n if not _insider_is_current(s))
    has_real_signal = any(s.get("insider_tx_count_90d") for s in stocks)
    should_run_insider = insider_stale > 0 or not has_real_signal
    if not should_run_insider:
        print("Insider: every ticker in the top 600 stamped this week, skipping.")
    elif not has_real_signal:
        print("Insider: previous run produced 0 signals, forcing re-run (likely a parser fix).")
    else:
        print(f"Insider: {insider_stale} of {len(top_n)} tickers need a pull.")
    insider_count = 0
    if should_run_insider:
        if cik_map is None:
            cik_map = fetch_edgar_ticker_cik_map()
        if cik_map:
            insider_count = enrich_with_insider(stocks, cik_map)

    # Per-ticker news fetched once a day (12h cache) so midday/evening workflow
    # runs reuse morning's pull. Writes one small JSON per ticker, lazy-loaded
    # by the page on row expand.
    news_count = enrich_with_news(stocks)

    # Aggregate per-ticker LM/VADER scores into the universe so the Overlays
    # filters can run client-side without loading every news file.
    aggregate_news_sentiment(stocks)

    # Lynch-style neglect score: needs analyst_count + inst_ownership + news_count_7d
    # which are all set by this point in the pipeline.
    compute_neglect_score(stocks)

    # Per-ticker daily price history (1y). 24h cache, bulk download via
    # yf.download in batches so we hit Yahoo once per ~200 tickers. This used to
    # run after scoring, back when it only fed the chart card. The momentum
    # returns are read back out of it now, so it has to come first.
    enrich_with_prices(stocks, session_confirmed=session_confirmed)
    derive_from_price_history(stocks)
    enrich_with_market_series()
    _benchmarks_quietly()
    derive_risk_metrics(stocks)
    # After the price derivation, because every ratio here needs a price.
    derive_ratios_from_fundamentals(stocks)

    # The final label, and the withholding that goes with it. After the ratios
    # and after the carry-forward merge above, both of which would put back
    # anything withheld earlier; before scoring, which excludes by the label.
    apply_security_types(stocks)
    for s in stocks:
        if s["ticker"] in registry:
            registry[s["ticker"]]["security_type"] = s["security_type"]
    save_ticker_registry(registry)

    # Peer scoring last: it reads every factor the steps above populate.
    compute_peer_scores(stocks)

    # Coverage is measured over operating listings. A note has no filings of its
    # own and a fund no market cap worth the name, so counting them in the
    # denominator understated coverage, while their parent-inherited caps and
    # EDGAR stamps overstated it in the numerator.
    operating = [s for s in stocks if sectype.is_operating(s.get("security_type"))]
    total_with_cap = sum(1 for s in operating if s.get("market_cap"))
    total_with_price = sum(1 for s in operating if s.get("price"))
    total_with_edgar = sum(1 for s in operating if s.get("edgar_updated"))

    result = {
        "iso_week": week_key,
        "date": date_key,
        "generated_at": now.isoformat(),
        # Set explicitly so the caller never has to infer freshness from a
        # missing key. Only this path builds a universe from a live scrape.
        "stale": False,
        "source": "wikipedia + yfinance + edgar",
        "enriched": bool(total_with_cap),
        "fresh_this_run": fresh_count,
        "edgar_this_run": edgar_count,
        "total_with_market_cap": total_with_cap,
        "total_with_price": total_with_price,
        "total_with_edgar": total_with_edgar,
        "total_operating": len(operating),
        "stocks": stocks,
    }
    cache_path.write_text(json.dumps(result, separators=(",", ":")), encoding="utf-8")
    pct_cap = (total_with_cap / len(operating) * 100) if operating else 0
    pct_price = (total_with_price / len(operating) * 100) if operating else 0
    pct_edgar = (total_with_edgar / len(operating) * 100) if operating else 0
    print(f"stocks_universe: regenerated for {date_key} ({len(stocks)} listings, {len(operating)} operating; {fresh_count} fresh yfinance, {edgar_count} fresh EDGAR, {insider_count} insider signals, {news_count} news pulls; coverage: {pct_cap:.0f}% market_cap, {pct_price:.0f}% price, {pct_edgar:.0f}% EDGAR).")
    return result



# ── The site: the Ledger pages ───────────────────────────────────────────────
#
# Every page shares one look (paper, ink and a red accent) and one script. Each
# page written here is a shell: the masthead and footer, and a JSON blob
# (window.APT_PAGE) holding what that page needs. web/ledger.js draws the body.
# The script, the stylesheet and the z engine live in web/ as ordinary files so
# they can be read, diffed and run in node; generate_site copies them to
# docs/assets/ with a content hash in the URL, so a new version is never served
# from a stale cache.
#
#   index.html     the brief's top stories, the screen of the day, research calls
#   today.html     the newest brief by section, repeats folded together
#   stories.html   the story library
#   stocks.html    every listing and every metric in one grid, tinted by universe
#                  z, filtered from a command bar (stocks.html#q=gm>1 pe<-0.5), with a
#                  3D factor map of the same screen beside it (stocks.html#view=map)
#   company.html   company.html#TICKER, one page per listing, thesis included
#   research.html  an index of theses and the record
#   portfolios.html one card per model portfolio; book.html#ID one book in full
#
# The Stocks and company pages load stocks-data.json and compute robust z in the
# browser (web/zengine.js). Nothing on them is hardcoded per security type: the
# fields that do not apply to a note or a shell are withheld by
# apply_security_types and stamped not_applicable, and the page reads that.

WEB_DIR = REPO_ROOT / "web"
ASSETS_DIR = DOCS_DIR / "assets"
LEDGER_ASSETS = ("ledger.css", "zengine.js", "ledger.js")
LEDGER_FONTS = ("https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500"
                "&family=Newsreader:ital,opsz,wght@0,6..72,400;0,6..72,500;0,6..72,600;1,6..72,400"
                "&family=Source+Sans+3:ital,wght@0,400;0,600;1,400&display=swap")

# The metrics the grid and the company page place against the universe, in
# display order: (key, command-bar alias, label, display unit). The group and the
# direction of the twenty scored fields come from SCORE_GROUPS_PY and
# INVERTED_FIELDS, so the page cannot call "better" what the score calls worse.
# Units: pct is a fraction shown as a percent, x a multiple, usd and shares are
# sizes, count an integer, ratio a bare number.
_LEDGER_METRIC_SPEC = [
    ("revenue_growth_yoy", "rev", "Revenue growth", "pct"),
    ("eps_growth_yoy", "eps", "EPS growth", "pct"),
    ("revenue_acceleration", "racc", "Revenue acceleration", "pct"),
    ("gross_margin_trend", "gmt", "Gross margin trend", "pct"),
    ("fcf_growth_yoy", "fcfg", "FCF growth", "pct"),
    ("pe", "pe", "P/E", "x"),
    ("ev_ebitda", "eve", "EV/EBITDA", "x"),
    ("ev_revenue", "evr", "EV/Revenue", "x"),
    ("price_book", "pb", "Price/Book", "x"),
    ("fcf_yield", "fcfy", "FCF yield", "pct"),
    ("return_12_2", "r122", "12-2 month return", "pct"),
    ("return_1m", "r1m", "1-month return", "pct"),
    ("high52w_proximity", "hi52", "Distance from 52-week high", "pct"),
    ("rel_strength_sp500", "rs", "Return vs S&P 500", "pct"),
    ("volume_trend", "vtr", "Volume trend", "pct"),
    ("return_52w", "r52", "52-week return", "pct"),
    ("roe_ttm", "roe", "ROE", "pct"),
    ("earnings_consistency", "econ", "Earnings consistency", "ratio"),
    ("net_debt_ebitda", "nde", "Net debt/EBITDA", "x"),
    ("op_margin_stability", "omv", "Operating margin volatility", "ratio"),
    ("accruals_ratio", "accr", "Accruals ratio", "pct"),
    ("gross_margin", "gm", "Gross margin", "pct"),
    ("operating_margin", "om", "Operating margin", "pct"),
    ("volatility_1y", "vol", "Volatility (1y)", "pct"),
    ("beta_1y", "beta", "Beta (1y)", "ratio"),
    ("sharpe_1y", "shp", "Sharpe ratio (1y)", "ratio"),
    ("max_drawdown_1y", "mdd", "Max drawdown (1y)", "pct"),
    ("market_cap", "mcap", "Market cap", "usd"),
    ("volume", "vlm", "Volume", "shares"),
    ("news_vader_avg", "tone", "News tone", "ratio"),
    ("news_count_7d", "news", "News stories (7 days)", "count"),
    ("neglect_score", "negl", "Neglect score", "ratio"),
    ("inst_ownership", "inst", "Institutional ownership", "pct"),
    ("insider_ownership", "insd", "Insider ownership", "pct"),
]
# Shown and ranked, never scored: group and direction (+1 higher reads better,
# -1 lower reads better, 0 neither). return_52w sits with the scored momentum
# fields it belongs beside.
_LEDGER_UNSCORED = {
    "return_52w": ("Momentum", 1),
    "gross_margin": ("Profitability", 1), "operating_margin": ("Profitability", 1),
    "volatility_1y": ("Risk", -1), "beta_1y": ("Risk", 0), "sharpe_1y": ("Risk", 1),
    "max_drawdown_1y": ("Risk", 1),
    "market_cap": ("Size", 0), "volume": ("Size", 0),
    "news_vader_avg": ("Attention", 1), "news_count_7d": ("Attention", 0),
    "neglect_score": ("Attention", 0),
    "inst_ownership": ("Ownership", 0), "insider_ownership": ("Ownership", 0),
}
# Size is multiplicative, so these are log10 before the z.
_LEDGER_LOG10 = {"market_cap", "volume"}


def _ledger_metrics():
    group_of = {f: g for g, spec in SCORE_GROUPS_PY.items() for f in spec["fields"]}
    out = []
    for key, alias, label, unit in _LEDGER_METRIC_SPEC:
        if key in group_of:
            group, better = group_of[key], (-1 if key in INVERTED_FIELDS else 1)
        else:
            group, better = _LEDGER_UNSCORED[key]
        out.append({"key": key, "alias": alias, "label": label, "group": group,
                    "unit": unit, "better": better,
                    "transform": "log10" if key in _LEDGER_LOG10 else None})
    order = list(SCORE_GROUPS_PY) + [g for g in ("Profitability", "Risk", "Size",
                                                 "Attention", "Ownership")]
    return sorted(out, key=lambda m: order.index(m["group"]))


LEDGER_METRICS = _ledger_metrics()

# The ready-made screens, in the page's query language. Thresholds are robust z
# against the operating universe; "<" includes its bound. The home page shows the
# first as the screen of the day, computed here by _ledger_screen with the same
# arithmetic as web/zengine.js, over the same rounded values the page loads.
LEDGER_SCREENS = [
    {"id": "qarp", "name": "Quality at a fair price",
     "blurb": "High and steady returns on equity, a P/E below the median, and not much debt.",
     "q": "roe>0.5 econ>0.5 pe<0 nde<0.5"},
    {"id": "cheap", "name": "Cash-rich and speeding up",
     "blurb": "A free cash flow yield well above most companies' (a z-score of +1 or more), and revenue growth that is speeding up.",
     "q": "fcfy>1 racc>0.5"},
    {"id": "calm", "name": "Steady momentum",
     "blurb": "Strong gains over the past year, leaving out the latest month, with price swings no bigger than the typical company's.",
     "q": "r122>1 vol<0"},
    {"id": "quiet", "name": "Profitable and overlooked",
     "blurb": "Little news coverage (a high neglect score) and a healthy operating margin.",
     "q": "negl>1 om>0.5"},
    {"id": "energy", "name": "Cash-rich energy companies",
     "blurb": "Energy companies whose free cash flow yield is well above other energy companies' (a z-score of +1 or more within the sector).",
     "q": "sector:energy scope:sector fcfy>1"},
]
LEDGER_SCREEN_OF_DAY = "qarp"

# Every page's data script. The Stocks and company pages carry FIELD_METHODS (the
# methodology behind each metric, shown on hover) and the non-operating labels
# from security_type.py, so neither is restated in the script.
STOCKS_JS_TEMPLATE = """window.APT_PAGE = __PAGE_JSON__;
window.APT_PAGE.fieldMethods = __FIELD_METHODS_JSON__;
window.APT_PAGE.fieldStatus = __FIELD_STATUS_JSON__;
window.APT_PAGE.nonop = __NON_OPERATING_JSON__;"""


def _stocks_data_row(stock):
    """A universe row as stocks-data.json carries it: nulls and empty strings
    dropped (absent keys read the same to the client) and floats rounded to 4
    places, which otherwise carry ~12 digits of binary noise apiece. Together
    these took the payload from 4.64 MB to 3.3 MB before gzip."""
    out = {}
    for k, v in stock.items():
        if v is None or v == "":
            continue
        out[k] = round(v, 4) if isinstance(v, float) else v
    return out


def _script_json(value):
    """JSON safe inside a <script>: "<", ">" and "&" escaped, because notes and
    headlines are text nobody vetted and json.dumps leaves "</script>" alone."""
    return (json.dumps(value, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
            .replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026"))


_SPACED_DASH = re.compile(r"\s+[–—]\s+|\s*—\s*")


def _plain_text(value):
    """Headline and source text as the pages print it: entities decoded, and the
    long dashes feeds use as separators written as commas (an en dash inside a
    range becomes a hyphen)."""
    s = _html.unescape(str(value or "")).strip()
    return _SPACED_DASH.sub(", ", s).replace("–", "-")


def _write_ledger_assets():
    """Copy web/ into docs/assets/ and return a short content hash for the URLs."""
    ASSETS_DIR.mkdir(parents=True, exist_ok=True)
    blob = b""
    for name in LEDGER_ASSETS:
        data = (WEB_DIR / name).read_bytes()
        (ASSETS_DIR / name).write_bytes(data)
        blob += data
    return f"{zlib.crc32(blob) & 0xffffffff:08x}"


def _ledger_close_date(stocks):
    """The session most rows' prices are the close of (price_date), or ""."""
    counts = collections.Counter(s.get("price_date") for s in stocks
                                 if s.get("price") is not None and s.get("price_date"))
    if not counts:
        return ""
    top = max(counts.values())
    return max(d for d, n in counts.items() if n == top)


def _ledger_common(universe):
    """What every page's script reads, whatever the page."""
    stocks = universe.get("stocks") or []
    tickers = {s.get("ticker") for s in stocks}
    return {
        "asof": universe.get("date") or "",
        "close": _ledger_close_date(stocks),
        "metrics": LEDGER_METRICS,
        "minCohort": MIN_COHORT_FOR_ZSCORE,
        "nListings": len(stocks),
        "sampleTicker": "AAPL" if "AAPL" in tickers or not stocks else stocks[0].get("ticker"),
        "viewWords": _VIEW_WORDS,
        "statusWords": _STATUS_WORDS,
        "repoUrl": _NOTE_REPO_URL,
    }


# The strip above the masthead on every page.
LEDGER_DISCLAIMER = ("A personal project. The data is collected automatically and not checked by hand. "
                     "Nothing here is investment advice.")

_LEDGER_NAV = (("home", "index.html", "Home"), ("today", "today.html", "Today"),
               ("stories", "stories.html", "Stories"), ("stocks", "stocks.html", "Stocks"),
               ("research", "research.html", "Research"),
               ("scorecard", "scorecard.html", "Scorecard"),
               ("portfolios", "portfolios.html", "Portfolios"))


def render_ledger_page(page, title, cfg, version, description="", loading="Loading",
                       script=None, engine=False):
    """One page of the site: head, the disclaimer strip, masthead, an empty main for
    ledger.js to fill, footer, the page's data and the scripts. The theme choice is applied before
    the stylesheet renders so a dark reader never sees a light flash."""
    e = _html.escape
    nav_on = {"company": "stocks", "book": "portfolios"}.get(page, page)
    current = ' aria-current="page"'
    nav = "".join(
        f'<a href="{href}"{current if key == nav_on else ""}>{label}</a>'
        for key, href, label in _LEDGER_NAV)
    asof = cfg.get("asof") or ""
    data_js = script if script is not None else f"window.APT_PAGE = {_script_json(cfg)};"
    engine_tag = f'<script src="assets/zengine.js?v={version}"></script>\n' if engine else ""
    stamp = datetime.now(EASTERN).strftime("%Y-%m-%d %H:%M ET")
    try:
        d = datetime.strptime(asof, "%Y-%m-%d") if asof else None
        asof_words = f"{d.day} {d:%B %Y}" if d else ""
    except ValueError:
        asof_words = asof
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="color-scheme" content="light dark">
<meta name="theme-color" content="#eee8dc" media="(prefers-color-scheme: light)">
<meta name="theme-color" content="#15130f" media="(prefers-color-scheme: dark)">
<title>{e(title)}</title>
<meta name="description" content="{e(description)}">
<link rel="manifest" href="manifest.json">
<script>
// A theme the reader chose wins over the system setting; with none, CSS follows the system.
(function(){{try{{var t=localStorage.getItem('apt-theme-v2');if(t==='light'||t==='dark')document.documentElement.setAttribute('data-theme',t);}}catch(e){{}}}})();
</script>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="{LEDGER_FONTS}" rel="stylesheet">
<link rel="stylesheet" href="assets/ledger.css?v={version}">
</head>
<body data-page="{e(page)}">
<div class="ld-root">
<div class="ld-disc" role="note">{e(LEDGER_DISCLAIMER)}</div>
<header class="ld-mast"><div class="ld-wrap ld-mast-in">
<a class="ld-brand" href="index.html">Apterreon</a>
<span class="ld-tagline">Explore what&rsquo;s out there</span>
<nav class="ld-nav" aria-label="Site">{nav}</nav>
<button type="button" class="ld-theme" aria-label="Switch theme"></button>
</div></header>
<main class="ld-main" id="ld-main"><div class="ld-wrap"><p class="ld-loading">{e(loading)}</p>
<noscript><p class="ld-loading">This page is built by a script. Turn on JavaScript to read it.</p></noscript></div></main>
<footer class="ld-foot"><div class="ld-wrap"><span>Apterreon &middot; Daily brief and stock screener{" &middot; data as of " + e(asof_words) if asof else ""}</span><span>Built {e(stamp)} &middot; GitHub Pages</span></div></footer>
</div>
<script>
{data_js}
</script>
{engine_tag}<script src="assets/ledger.js?v={version}"></script>
</body>
</html>
"""


def _stock_page_script(cfg):
    return (STOCKS_JS_TEMPLATE
            .replace("__FIELD_METHODS_JSON__", _script_json(FIELD_METHODS))
            .replace("__FIELD_STATUS_JSON__", _script_json(FIELD_STATUS))
            .replace("__NON_OPERATING_JSON__", _script_json(sorted(sectype.NON_OPERATING)))
            .replace("__PAGE_JSON__", _script_json(cfg)))


# ---- the screen of the day, as web/zengine.js computes it ------------------

def _ledger_stat(values):
    """zengine.js stat(): robust centre and scale, falling back to mean and sd
    when more than half the values are identical. Summed in sorted order so the
    result is the same double the browser gets."""
    s = sorted(values)
    n = len(s)

    def med(a):
        h = len(a) >> 1
        return a[h] if len(a) % 2 else (a[h - 1] + a[h]) / 2

    centre = med(s)
    mad = med(sorted(abs(x - centre) for x in s))
    total = 0.0
    for x in s:
        total += x
    mean = total / n
    sq = 0.0
    for x in s:
        sq += (x - mean) * (x - mean)
    sd = math.sqrt(sq / (n - 1)) if n > 1 else float("nan")
    method, scale = "robust", 1.4826 * mad
    if not scale > 0:
        method, centre, scale = "sd", mean, sd
    if not scale > 0:
        method = "none"
    return {"method": method, "center": centre, "scale": scale, "n": n}


def _ledger_bands(query):
    """The band tokens of a ready-made screen ("roe>0.5 pe<0"): alias -> [lo, hi]
    in z, None for an open end. Only what the screen of the day uses."""
    by_alias = {m["alias"]: m["key"] for m in LEDGER_METRICS}
    bands = {}
    for tok in query.split():
        m = re.match(r"^([a-z0-9_]+)(>=|<=|>|<)([+-]?\d*\.?\d+)$", tok)
        if not m or m.group(1) not in by_alias:
            raise ValueError(f"screen token {tok!r} is not a universe band")
        lo, hi = (float(m.group(3)), None) if m.group(2)[0] == ">" else (None, float(m.group(3)))
        bands[by_alias[m.group(1)]] = [lo, hi]
    return bands


def _ledger_screen(rows, screen, top=8):
    """Run one universe-scope screen over stocks-data.json rows: the match count,
    the operating cohort size, and the top rows by composite score."""
    bands = _ledger_bands(screen["q"])
    metric = {m["key"]: m for m in LEDGER_METRICS}
    operating = [r for r in rows if (r.get("security_type") or "operating") not in sectype.NON_OPERATING]

    def value(r, key):
        v = r.get(key)
        if not _finite(v):
            return None
        if metric[key]["transform"] == "log10":
            return math.log10(v) if v > 0 else None
        return float(v)

    stats, zs = {}, {}
    for key in bands:
        vals = [v for v in (value(r, key) for r in operating) if v is not None]
        st = _ledger_stat(vals) if vals else {"method": "none", "center": 0.0, "scale": 0.0, "n": 0}
        stats[key] = st
        col = []
        for r in operating:
            v = value(r, key)
            if v is None or st["method"] == "none":
                col.append(None)
            else:
                col.append(max(-5.0, min(5.0, (v - st["center"]) / st["scale"])))
        zs[key] = col

    hits = []
    for i, r in enumerate(operating):
        ok = True
        for key, (lo, hi) in bands.items():
            z = zs[key][i]
            if z is None or (lo is not None and z < lo) or (hi is not None and z > hi):
                ok = False
                break
        if ok:
            dims = [r.get(k) for k in ("g", "v", "q", "m") if _finite(r.get(k))]
            score = sum(dims) / len(dims) if r.get("scorable") and dims else None
            hits.append((i, score))
    hits.sort(key=lambda h: (h[1] is None, -(h[1] or 0.0)))
    return {
        "id": screen["id"], "name": screen["name"], "blurb": screen["blurb"], "q": screen["q"],
        "bands": bands, "match": len(hits), "of": len(operating),
        "stats": {k: {"method": st["method"], "center": st["center"], "scale": st["scale"]}
                  for k, st in stats.items()},
        "hits": [{"t": operating[i]["ticker"], "name": _plain_text(operating[i].get("name")),
                  "kind": operating[i].get("security_type") or "operating",
                  "z": {k: (round(zs[k][i], 4) if zs[k][i] is not None else None) for k in bands},
                  "score": round(score, 4) if score is not None else None}
                 for i, score in hits[:top]],
    }


# ---- briefs and stories ------------------------------------------------------

_EDITION_RANK = {"morning": 0, "midday": 1, "evening": 2}


def _ledger_story_rows(briefs):
    """Every story in every brief, newest first, as the story library reads it."""
    out = []
    for b in briefs:
        for sec in b.get("sections", []) or []:
            for st in sec.get("stories", []) or []:
                if not st.get("headline"):
                    continue
                out.append({"h": _plain_text(st.get("headline")),
                            "src": _plain_text(st.get("source")),
                            "sum": _plain_text(st.get("summary")),
                            "link": st.get("link") or (b.get("key") or ""),
                            "d": b.get("date", ""), "ed": b.get("type", ""),
                            "sec": sec.get("name", "")})
    out.sort(key=lambda s: (s["d"], _EDITION_RANK.get(s["ed"], 99)), reverse=True)
    return out


def _ledger_brief(briefs):
    """The newest brief that has stories, with its quotes, and the other
    editions filed the same day."""
    latest = next((b for b in briefs if any(sec.get("stories") for sec in b.get("sections") or [])),
                  briefs[0] if briefs else None)
    if not latest:
        return None, [], []
    quotes = []
    key = latest.get("key") or ""
    sidecar = DOCS_DIR / key.replace(".html", ".json") if key else None
    if sidecar and sidecar.exists():
        try:
            for q in json.loads(sidecar.read_text(encoding="utf-8")).get("quotes") or []:
                try:
                    chg = float(q.get("change_pct"))
                except (TypeError, ValueError):
                    chg = None
                quotes.append({"t": q.get("ticker", ""), "label": _plain_text(q.get("label")),
                               "price": str(q.get("price", "")), "y": bool(q.get("is_yield")),
                               "chg": chg if chg is not None and math.isfinite(chg) else None})
        except Exception as exc:
            print(f"home: could not read the quotes in {sidecar.name} ({exc}).")
    brief = {"date": latest.get("date", ""), "type": latest.get("type", ""),
             "ts": latest.get("timestamp", ""), "key": key,
             "sections": [{"name": sec.get("name", ""),
                           "stories": [{"h": _plain_text(st.get("headline")),
                                        "src": _plain_text(st.get("source")),
                                        "link": st.get("link") or key}
                                       for st in sec.get("stories") or [] if st.get("headline")]}
                          for sec in latest.get("sections") or []]}
    editions = [{"type": b.get("type", ""), "key": b.get("key", "")}
                for b in briefs if b.get("date") == latest.get("date")]
    return brief, quotes, editions


def _ledger_trend(stories):
    """What the home page's trends panel needs, without shipping the library:
    the last fourteen days of headlines, stories per day, and the cadence."""
    if not stories:
        return {"stories": [], "days": {}, "total": 0, "sources": 0, "cadence": None}
    last = max(s["d"] for s in stories)
    try:
        start = (datetime.strptime(last, "%Y-%m-%d") - timedelta(days=13)).strftime("%Y-%m-%d")
    except ValueError:
        start = ""
    days = collections.Counter(s["d"] for s in stories if s["d"])
    daily = sorted({s["d"] for s in stories if s["ed"] == "daily"})
    older = [s for s in stories if s["ed"] != "daily"]
    older_days = sorted({s["d"] for s in older})
    return {
        "stories": [{"h": s["h"], "src": s["src"], "d": s["d"]} for s in stories if s["d"] >= start],
        "days": dict(sorted(days.items())),
        "total": len(stories),
        "sources": len({s["src"].split("·")[0].strip() for s in stories if s["src"].strip()}),
        "cadence": {"dailyFirst": daily[0] if daily else "", "dailyN": len(daily),
                    "olderFirst": older_days[0] if older_days else "",
                    "olderLast": older_days[-1] if older_days else "", "olderN": len(older)},
    }


def _ledger_research(universe):
    """The theses as the home and research pages list them, and the record."""
    stocks = {s.get("ticker"): s for s in (universe.get("stocks") or []) if s.get("ticker")}
    views = _research_views(stocks, datetime.now(tz=timezone.utc).date())
    desks = _desk_titles()
    rows = []
    for v in views:
        desk = desks.get(v.get("sector") or "") or {}
        price = v.get("price") or {}
        current = v.get("current") or {}
        rows.append({
            "ticker": v["ticker"], "name": v.get("name") or v["ticker"],
            "direction": v.get("direction"), "status": v.get("status"),
            "conviction": v.get("conviction"), "kind": v.get("kind"),
            "written_on": v.get("written_on"), "review_by": v.get("review_by"),
            "entry_price": v.get("entry_price"), "target_price": v.get("target_price"),
            "if_wrong_price": current.get("if_wrong_price"),
            "desk": desk.get("desk", ""), "deskTitle": desk.get("title", ""),
            "last_close": ({"date": price["as_of"], "close": price["last"]}
                           if price.get("last") is not None else None),
        })
    return rows, _research_record(views)


# ---- the pages -----------------------------------------------------------------

def generate_home(briefs, universe, version):
    """Write docs/index.html: the brief's top stories, the screen of the day,
    research calls and recent trends."""
    stocks = universe.get("stocks") or []
    brief, quotes, _ = _ledger_brief(briefs)
    research, _ = _ledger_research(universe)
    screen = next(s for s in LEDGER_SCREENS if s["id"] == LEDGER_SCREEN_OF_DAY)
    sod = _ledger_screen([_stocks_data_row(s) for s in stocks], screen) if stocks else None
    cfg = dict(_ledger_common(universe), nonop=sorted(sectype.NON_OPERATING),
               brief=brief or {"sections": []}, quotes=quotes,
               trend=_ledger_trend(_ledger_story_rows(briefs)), sod=sod, research=research)
    html = render_ledger_page("home", "Apterreon, Daily Intelligence Brief", cfg, version,
                              description="A daily news brief, a stock screener covering every US listing, and written research on single companies.",
                              loading="Loading the brief", engine=True)
    (DOCS_DIR / "index.html").write_text(html, encoding="utf-8")


def generate_today(briefs, universe, version):
    """Write docs/today.html: the newest brief by section."""
    brief, _, editions = _ledger_brief(briefs)
    cfg = dict(_ledger_common(universe), nonop=sorted(sectype.NON_OPERATING),
               brief=brief or {"sections": []}, editions=editions)
    html = render_ledger_page("today", "Today's Brief, Apterreon", cfg, version,
                              description="The latest news brief, section by section.",
                              loading="Loading the brief")
    (DOCS_DIR / "today.html").write_text(html, encoding="utf-8")


def generate_stories(briefs, universe, version):
    """Write docs/stories.html: every story the brief has carried, searchable."""
    stories = _ledger_story_rows(briefs)
    order = []
    for b in briefs:
        for sec in b.get("sections", []) or []:
            if sec.get("name") and sec["name"] not in order:
                order.append(sec["name"])
    cfg = dict(_ledger_common(universe), nonop=sorted(sectype.NON_OPERATING),
               stories=stories, sectionOrder=order)
    html = render_ledger_page("stories", "Story Library, Apterreon", cfg, version,
                              description="Every story the brief has carried, searchable by headline, summary and source.",
                              loading=f"Loading {len(stories):,} stories")
    (DOCS_DIR / "stories.html").write_text(html, encoding="utf-8")


def _stocks_research():
    """The Stocks page's Research filter: each ticker with a written thesis, mapped to its current
    view's direction, and the analyst's watchlist. Read from theses/, the source write_thesis_views
    reads, so it does not depend on which of the two writers runs first."""
    views = {}
    notes_dir = THESES_DIR / "notes"
    if notes_dir.is_dir():
        for tdir in sorted(notes_dir.iterdir()):
            notes = sorted(tdir.glob("*.md")) if tdir.is_dir() else []
            if not notes:
                continue
            try:
                fm = _parse_front_matter(notes[-1].read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError):
                continue
            if fm:
                views[tdir.name] = _note_scalar(fm, "direction") or ""
    watch = []
    wl = THESES_DIR / "watchlist.txt"
    if wl.exists():
        for line in wl.read_text(encoding="utf-8").splitlines():
            t = line.split("#", 1)[0].strip().upper()
            if t and t not in watch:
                watch.append(t)
    return {"thesis": views, "watchlist": watch}


def generate_stocks_page(universe, version):
    """Write docs/stocks-data.json and docs/stocks.html, the grid screener."""
    stocks = universe.get("stocks", []) or []
    data_path = DOCS_DIR / "stocks-data.json"
    data_path.write_text(
        json.dumps([_stocks_data_row(s) for s in stocks], separators=(",", ":")),
        encoding="utf-8")
    print(f"stocks: wrote {data_path.name} "
          f"({data_path.stat().st_size / 1024 / 1024:.2f} MB, {len(stocks)} tickers).")
    cfg = dict(_ledger_common(universe), presets=LEDGER_SCREENS, data="stocks-data.json",
               dims={d: g["fields"] for d, g in SCORE_GROUPS_PY.items()},
               research=_stocks_research())
    html = render_ledger_page(
        "stocks", "Stocks, Apterreon", cfg, version,
        description="Every US listing and every figure we track, each compared with all companies.",
        loading=f"Loading {len(stocks):,} listings",
        script=_stock_page_script(cfg), engine=True)
    (DOCS_DIR / "stocks.html").write_text(html, encoding="utf-8")


def _tickers_with_files(directory):
    """Tickers that have a file in a per-ticker directory (the inverse of
    _news_filename)."""
    if not directory.is_dir():
        return set()
    out = set()
    for p in directory.glob("*.json"):
        stem = p.stem
        if stem.startswith("_") and stem[1:] in _WIN_RESERVED:
            stem = stem[1:]
        out.add(stem)
    return out


def generate_company_page(universe, version):
    """Write docs/company.html, one page for every ticker (company.html#NVDA).

    Runs after the thesis and company view writers, because it lists which
    tickers have those files: the page fetches only what exists, so a ticker
    without one costs no request."""
    stocks = universe.get("stocks") or []
    tickers = [s.get("ticker") for s in stocks if s.get("ticker")]
    prices = _tickers_with_files(PRICES_DIR)
    news = _tickers_with_files(NEWS_DIR)
    history = _tickers_with_files(HISTORY_VIEW_DIR)
    cfg = dict(_ledger_common(universe), data="stocks-data.json", have={
        "company": sorted(_tickers_with_files(COMPANY_VIEW_DIR)),
        "thesis": sorted(_tickers_with_files(THESIS_VIEW_DIR)),
        "noPrices": sorted(t for t in tickers if t not in prices),
        "noNews": sorted(t for t in tickers if t not in news),
        "noHistory": sorted(t for t in tickers if t not in history),
    })
    html = render_ledger_page(
        "company", "Company, Apterreon", cfg, version,
        description="One company's price history, scores, investment thesis if there is one, and how each figure compares with all companies.",
        loading=f"Loading {len(stocks):,} listings", script=_stock_page_script(cfg), engine=True)
    (DOCS_DIR / "company.html").write_text(html, encoding="utf-8")


def write_manifest():
    manifest = {
        "name": "Apterreon, Daily Intelligence Brief",
        "short_name": "Apterreon",
        "description": "Apterreon Daily Intelligence Brief. Explore what's out there.",
        "start_url": "./index.html",
        "display": "standalone",
        "background_color": "#0A0A0F",
        "theme_color": "#0A0A0F",
    }
    (DOCS_DIR / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")


# --- thesis views -------------------------------------------------------------
#
# theses/ is written by a scheduled Claude agent and is the source of truth. This
# renders the CURRENT view per ticker into docs/thesis/{TICKER}.json so the
# screener can show it without the browser parsing markdown.
#
# Derived, and regenerated from theses/ on every run. That direction matters: a
# copy under docs/ that drifted from the notes would be worse than no copy, and
# docs/ is force-pushed to gh-pages each run so it can never be authoritative.
THESES_DIR = REPO_ROOT / "theses"
THESIS_VIEW_DIR = DOCS_DIR / "thesis"
_THESIS_SCALARS = ("thesis_id", "kind", "written_on", "panel_date", "direction",
                   "conviction", "evidence_base", "falsifier_specific",
                   "variant_perception", "disconfirmation", "entry_price",
                   "horizon_days", "target_price", "review_by", "key_claim",
                   "falsifier", "slot")


def _parse_front_matter(text):
    """Minimal YAML front-matter reader: scalars, block lists, inline lists.

    Deliberately not a YAML parser. The notes are written to a fixed shape that
    validate.py enforces before anything is committed, so the surface this has to
    cover is small and a dependency would not earn its place."""
    if not text.startswith("---"):
        return None
    end = text.find("\n---", 3)
    if end == -1:
        return None
    out, key = {}, None
    for line in text[3:end].splitlines():
        if re.match(r"^\s*-\s+", line) and key:
            out.setdefault(key, [])
            if isinstance(out[key], list):
                out[key].append(line.strip()[1:].strip().strip('"'))
            continue
        m = re.match(r"^([A-Za-z_][\w]*):\s*(.*)$", line)
        if not m:
            continue
        key, val = m.group(1), m.group(2).strip()
        if val.startswith("[") and val.endswith("]"):
            inner = val[1:-1].strip()
            out[key] = [x.strip().strip('"') for x in inner.split(",") if x.strip()]
        elif val == "":
            out[key] = []
        else:
            out[key] = val.strip('"')
    return out


# --- note rendering -----------------------------------------------------------
#
# The note popup and the research library need every note on a name, not only
# the newest, and they need the body as well as the front-matter. The body is
# markdown written by an agent that reads untrusted filing text, so it is treated
# as untrusted too: everything is escaped first and only a small subset is turned
# back into markup. Rendering happens here, once, so the browser never parses
# markdown and there is a single renderer to audit.

# Named entities the notes type literally ("&minus;0.8%"). Escaping turns them
# into "&amp;minus;", which prints the entity name, so exactly these are mapped
# back to their characters. Any other entity stays escaped and prints as typed.
_NOTE_ENTITIES = {"minus": "\u2212", "mdash": "\u2014", "ndash": "\u2013",
                  "rarr": "\u2192", "larr": "\u2190", "middot": "\u00b7",
                  "nbsp": "\u00a0", "times": "\u00d7", "asymp": "\u2248"}
_NOTE_ENTITY_RAW = re.compile(r"&(" + "|".join(_NOTE_ENTITIES) + r");")
_NOTE_ENTITY_ESCAPED = re.compile(r"&amp;(" + "|".join(_NOTE_ENTITIES) + r");")
_MD_TABLE_SEP = re.compile(r"^\|?\s*:?-+:?\s*(?:\|\s*:?-+:?\s*)*\|?$")
# A condition the page can evaluate ends in "[check: FIELD OP NUMBER]". The
# minus sign may arrive as the entity, already decoded to U+2212 by then.
_CONDITION_CHECK = re.compile(
    r"\s*\[check:\s*([A-Za-z_][\w.]*)\s*(>=|<=|>|<)\s*"
    r"([-+\u2212]?(?:\d+(?:\.\d*)?|\.\d+))\s*\]\s*$")
_DOC_KIND_WORDS = {
    "business": ("annual report description of the business",
                 "annual report descriptions of the business"),
    "risk_factors": ("annual report section on risks", "annual report sections on risks"),
    "segment_note": ("note on its business segments", "notes on its business segments"),
    "earnings_release": ("results announcement", "results announcements"),
    "mdna": ("management discussion of results", "management discussions of results"),
}
_THESIS_NOTE_SUMMARY_KEYS = ("date", "kind", "direction", "conviction",
                             "target_price", "key_claim", "since_last_note",
                             "path")


def _decode_note_entities(text):
    """Plain text with the whitelisted entities turned into characters.

    For front-matter values, which the page escapes itself. Returns the input
    unchanged when it is not a string."""
    if not isinstance(text, str):
        return text
    return _NOTE_ENTITY_RAW.sub(lambda m: _NOTE_ENTITIES[m.group(1)], text)


def _md_bold(esc):
    # Both ends must touch a non-space, so a lone or unclosed "**" stays literal.
    return re.sub(r"\*\*(?=\S)(.+?)(?<=\S)\*\*", r"<strong>\1</strong>", esc)


def _md_inline(esc):
    """Inline markup over text that is ALREADY escaped: code, links, bold."""
    codes, links = [], []

    def _code(m):
        codes.append("<code>" + m.group(1) + "</code>")
        return "\x00" + str(len(codes) - 1) + "\x00"

    def _link(m):
        # The URL is escaped text, so a quote in it is &quot; and cannot close
        # the attribute. Only http and https reach this branch at all.
        links.append('<a href="' + m.group(2) + '" target="_blank" '
                     'rel="noopener noreferrer">' + _md_bold(m.group(1)) + "</a>")
        return "\x01" + str(len(links) - 1) + "\x01"

    out = re.sub(r"`([^`\n]+)`", _code, esc)
    out = re.sub(r"\[([^\[\]\n]+)\]\((https?://[^\s()<>]+)\)", _link, out)
    out = _md_bold(out)
    out = re.sub(r"\x01(\d+)\x01", lambda m: links[int(m.group(1))], out)
    out = re.sub(r"\x00(\d+)\x00", lambda m: codes[int(m.group(1))], out)
    return out


def _md_table_cells(line):
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return [c.strip() for c in s.split("|")]


def _md_table(rows):
    head, body = None, rows
    if len(rows) >= 2 and _MD_TABLE_SEP.match(rows[1].strip()):
        head, body = _md_table_cells(rows[0]), rows[2:]
    parts = ['<table class="md-table">']
    if head:
        parts.append("<thead><tr>" + "".join(
            "<th>" + _md_inline(c) + "</th>" for c in head) + "</tr></thead>")
    parts.append("<tbody>")
    for row in body:
        parts.append("<tr>" + "".join(
            "<td>" + _md_inline(c) + "</td>" for c in _md_table_cells(row)) + "</tr>")
    parts.append("</tbody></table>")
    return "".join(parts)


def _md_to_html(text):
    """Safe markdown subset to HTML.

    Escapes everything first, then allows only: paragraphs split on blank lines,
    "- " bullet lists (wrapped lines continue the item), "> " quoted lines,
    **bold**, `code`, [text](url) where the url is http or https, and pipe
    tables. A revision has to quote the claim it replaces word for word, and
    without the quote it ran into the analyst's own sentences. The entity
    whitelist in _NOTE_ENTITIES is restored as characters. Nothing else in the
    input can produce a tag or an attribute."""
    if not text:
        return ""
    src = str(text).replace("\r\n", "\n").replace("\r", "\n")
    # The inline pass uses these two as placeholders, so input cannot carry them.
    src = src.replace("\x00", "").replace("\x01", "")
    esc = _html.escape(src, quote=True)
    out, para, items, table, quote = [], [], [], [], []

    def flush():
        if para:
            out.append("<p>" + _md_inline(" ".join(para)) + "</p>")
            para.clear()
        if quote:
            out.append("<blockquote><p>" + _md_inline(" ".join(quote)) + "</p></blockquote>")
            quote.clear()
        if items:
            out.append("<ul>" + "".join(
                "<li>" + _md_inline(" ".join(i)) + "</li>" for i in items) + "</ul>")
            items.clear()
        if table:
            out.append(_md_table(table))
            table.clear()

    for raw in esc.split("\n"):
        line = raw.strip()
        if not line:
            flush()
            continue
        if line.startswith("|"):
            if not table:
                flush()
            table.append(line)
            continue
        if table:
            flush()
        # The text is already escaped, so a quoted line starts with the entity.
        # Only "> text", and only opening a block or continuing a quote: a wrapped
        # line of prose can begin with ">= 5 percent", and read as a quotation it
        # lost its sign and split the sentence.
        if (line == "&gt;" or line.startswith("&gt; ")) and (quote or not (para or items)):
            quote.append(line[4:].strip())
            continue
        if quote:
            flush()
        if line.startswith("- "):
            if para:
                flush()
            items.append([line[2:].strip()])
            continue
        if items:
            items[-1].append(line)
            continue
        para.append(line)
    flush()
    return _NOTE_ENTITY_ESCAPED.sub(lambda m: _NOTE_ENTITIES[m.group(1)], "".join(out))


def _note_scalar(fm, key):
    """A front-matter scalar as a stripped string, or None when absent or empty."""
    v = fm.get(key)
    if v is None or isinstance(v, list):
        return None
    v = str(v).strip()
    return v or None


def _note_int(fm, key):
    v = _note_scalar(fm, key)
    if v is None:
        return None
    try:
        return int(v)
    except ValueError:
        try:
            f = float(v)
        except ValueError:
            return None
        return int(f) if math.isfinite(f) and f.is_integer() else None


def _note_float(fm, key):
    v = _note_scalar(fm, key)
    if v is None:
        return None
    try:
        f = float(v.replace(",", "").lstrip("$"))
    except ValueError:
        return None
    return f if math.isfinite(f) else None


def _parse_condition(item):
    """One conditions item as {text, check}; check is None unless computable."""
    s = (_decode_note_entities(str(item or "")) or "").strip()
    m = _CONDITION_CHECK.search(s)
    if not m:
        return {"text": s, "check": None}
    try:
        value = float(m.group(3).replace("\u2212", "-"))
    except ValueError:
        return {"text": s, "check": None}
    return {"text": s[:m.start()].strip(),
            "check": {"field": m.group(1), "op": m.group(2), "value": value}}


def _note_body(text):
    """Everything after the closing front-matter fence."""
    if not text.startswith("---"):
        return text
    end = text.find("\n---", 3)
    if end == -1:
        return text
    nl = text.find("\n", end + 4)
    return "" if nl == -1 else text[nl + 1:]


def _note_sections(body):
    """[{title, html}] split on "## " headings. title is plain text, html is safe."""
    sections, title, buf = [], None, []
    for line in (body or "").split("\n"):
        m = re.match(r"^##\s+(.+?)\s*$", line)
        if m:
            chunk = "\n".join(buf).strip()
            if title is not None or chunk:
                sections.append({"title": _decode_note_entities(title or ""),
                                 "html": _md_to_html(chunk)})
            title, buf = m.group(1), []
            continue
        buf.append(line)
    chunk = "\n".join(buf).strip()
    if title is not None or chunk:
        sections.append({"title": _decode_note_entities(title or ""),
                         "html": _md_to_html(chunk)})
    return sections


def _read_filings_rows():
    """ticker -> every row in data/filings/*.csv, or None when there is no data.

    Shared by the company views and the thesis views so both count the same
    documents. None (no directory, no CSVs) is different from an empty dict:
    only when the data exists can a note say that nothing new was collected."""
    if not FILINGS_CSV_DIR.is_dir():
        return None
    paths = sorted(FILINGS_CSV_DIR.glob("*.csv"))
    if not paths:
        return None
    out = {}
    for path in paths:
        try:
            with path.open(encoding="utf-8", newline="") as fh:
                for row in csv.DictReader(fh):
                    tk = (row.get("ticker") or "").upper()
                    if tk:
                        out.setdefault(tk, []).append(row)
        except Exception as exc:
            print(f"filings: could not read {path.name} ({exc}).")
    return out


def _docs_between(rows, after, upto):
    """{doc_kind: count} of documents first collected in (after, upto].

    Collected means recorded_at, falling back to filed. A document re-recorded
    by a later run is counted once, at the first time it was collected."""
    if not after or not upto:
        return None
    dated = []
    for r in rows or []:
        when = (r.get("recorded_at") or "")[:10] or (r.get("filed") or "")[:10]
        if when:
            dated.append((when, r))
    dated.sort(key=lambda x: x[0])
    seen, counts = set(), {}
    for when, r in dated:
        key = (r.get("doc_kind"), r.get("accession"))
        if key in seen:
            continue
        seen.add(key)
        if after < when <= upto:
            kind = (r.get("doc_kind") or "").strip() or "document"
            counts[kind] = counts.get(kind, 0) + 1
    return counts


def _doc_kind_phrase(kind, n):
    one, many = _DOC_KIND_WORDS.get(kind) or (kind.replace("_", " "),
                                              kind.replace("_", " ") + "s")
    return f"{n} {one if n == 1 else many}"


def _and_list(items):
    """"a", "a and b", "a, b and c"."""
    items = list(items)
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


def _since_last_note(prev, cur, docs=None):
    """Plain sentences saying what changed from the previous note. The company page
    capitalises the first letter and adds the closing full stop."""
    if prev is None:
        return "this is the first note on this company"
    parts = []
    pd, cd = prev.get("direction"), cur.get("direction")
    if (pd or "") != (cd or ""):
        def say(d):
            word = _VIEW_WORDS.get(str(d or "").strip().lower())
            return f"\u201c{word}\u201d" if word else "no stated view"
        parts.append(f"the view changed from {say(pd)} to {say(cd)}")
    pc, cc = prev.get("conviction"), cur.get("conviction")
    if pc != cc:
        if pc is None:
            parts.append(f"conviction set at {cc} of 5")
        elif cc is None:
            parts.append(f"conviction no longer given (it was {pc} of 5)")
        else:
            parts.append(f"conviction {'raised' if cc > pc else 'lowered'} "
                         f"from {pc} to {cc} of 5")
    pt, ct = prev.get("target_price"), cur.get("target_price")
    if pt != ct:
        if pt is None:
            parts.append(f"target set at ${ct:,.2f}")
        elif ct is None:
            parts.append(f"target dropped (it was ${pt:,.2f})")
        else:
            parts.append(f"target {'raised' if ct > pt else 'lowered'} "
                         f"from ${pt:,.2f} to ${ct:,.2f}")
    # A reworded claim under an unchanged rating is what thesis drift looks
    # like, so "view unchanged" must not be printed over it.
    pk = " ".join((prev.get("key_claim") or "").split())
    ck = " ".join((cur.get("key_claim") or "").split())
    if pk != ck:
        parts.append("main claim added" if not pk else
                     "main claim dropped" if not ck else "main claim rewritten")
    sentence = _and_list(parts) if parts else "the view is unchanged"
    if docs is not None:
        total = sum(docs.values())
        if total:
            detail = _and_list(_doc_kind_phrase(k, n) for k, n in
                               sorted(docs.items(), key=lambda x: (-x[1], x[0])))
            sentence += (f". Since the previous note we collected {total} "
                         f"document{'s' if total != 1 else ''}: {detail}")
        else:
            sentence += ". No new documents have been collected since the previous note"
    return sentence


def _thesis_note_record(path, ticker):
    """One note as the popup reads it, or None when it has no front-matter."""
    try:
        text = path.read_text(encoding="utf-8")
    except Exception as exc:
        print(f"thesis: could not read {path.name} ({exc}); skipped.")
        return None
    fm = _parse_front_matter(text)
    if not fm:
        return None
    date = _note_scalar(fm, "written_on")
    if not date and re.match(r"^\d{4}-\d{2}-\d{2}", path.name):
        date = path.name[:10]
    conditions = fm.get("conditions")
    if isinstance(conditions, str):
        conditions = [conditions] if conditions.strip() else []
    caveats = fm.get("data_caveats")
    if isinstance(caveats, str):
        caveats = [caveats] if caveats.strip() else []
    rec = {
        "path": f"theses/notes/{ticker}/{path.name}",
        "date": date,
        "kind": _note_scalar(fm, "kind"),
        "direction": _note_scalar(fm, "direction"),
        "conviction": _note_int(fm, "conviction"),
        "evidence_base": _note_int(fm, "evidence_base"),
        "falsifier_specific": _note_int(fm, "falsifier_specific"),
        "variant_perception": _note_int(fm, "variant_perception"),
        "disconfirmation": _note_int(fm, "disconfirmation"),
        "entry_price": _note_float(fm, "entry_price"),
        "target_price": _note_float(fm, "target_price"),
        "if_wrong_price": _note_float(fm, "if_wrong_price"),
        "horizon_days": _note_int(fm, "horizon_days"),
        "review_by": _note_scalar(fm, "review_by"),
        "next_check": _note_scalar(fm, "next_check"),
        "key_claim": _decode_note_entities(_note_scalar(fm, "key_claim")),
        "falsifier": _decode_note_entities(_note_scalar(fm, "falsifier")),
        "add_if": _decode_note_entities(_note_scalar(fm, "add_if")),
        "conditions": [_parse_condition(c) for c in (conditions or [])
                       if str(c).strip()],
        "data_caveats": [_decode_note_entities(str(c)) for c in (caveats or [])
                         if str(c).strip()],
        "since_last_note": None,
        "sections": _note_sections(_note_body(text)),
    }
    return rec


def _thesis_notes_for(tdir, filings=None):
    """Every readable note for one ticker, NEWEST FIRST, each with since_last_note.

    The one place notes are read for both the thesis views and the research
    page, so the popup and the library cannot describe a note differently.
    filings is the _read_filings_rows() result; None leaves document counts out."""
    ticker = tdir.name
    recs = [r for r in (_thesis_note_record(p, ticker) for p in sorted(tdir.glob("*.md")))
            if r]
    rows = None if filings is None else (filings.get(ticker.upper()) or [])
    prev = None
    for rec in recs:            # oldest first, so each note sees the one before it
        docs = None
        if prev is not None and rows is not None:
            docs = _docs_between(rows, prev.get("date"), rec.get("date"))
        rec["since_last_note"] = _since_last_note(prev, rec, docs)
        prev = rec
    recs.reverse()
    return recs


def _thesis_note_summary(rec):
    """The subset of a note record the research library timeline needs."""
    return {k: rec.get(k) for k in _THESIS_NOTE_SUMMARY_KEYS}


def _last_close(ticker):
    """{date, close} of the newest close in docs/prices/{TICKER}.json, or None."""
    try:
        blob = json.loads((PRICES_DIR / _news_filename(ticker)).read_text(encoding="utf-8"))
        closes = [c for c in (blob.get("closes") or [])
                  if isinstance(c, (list, tuple)) and len(c) >= 2 and c[1] is not None]
    except Exception:
        return None
    if not closes:
        return None
    try:
        close = float(closes[-1][1])
    except (TypeError, ValueError):
        return None
    if not math.isfinite(close):
        return None
    return {"date": closes[-1][0], "close": close}


def write_thesis_views():
    """One JSON per ticker holding the current view, its history and every note.

    The top-level scalars are the newest note's front-matter and are read by the
    stocks page as they are. "notes" holds every note newest first, rendered
    for the note popup; "last_close" is the newest close the build could see."""
    notes_dir = THESES_DIR / "notes"
    if not notes_dir.is_dir():
        return 0
    events = []
    ev_path = THESES_DIR / "ledger" / "events.csv"
    if ev_path.exists():
        try:
            with ev_path.open(encoding="utf-8", newline="") as fh:
                events = list(csv.DictReader(fh))
        except Exception as exc:
            print(f"thesis: could not read events.csv ({exc}); history omitted.")

    filings = _read_filings_rows()
    THESIS_VIEW_DIR.mkdir(parents=True, exist_ok=True)
    written = 0
    for tdir in sorted(notes_dir.iterdir()):
        if not tdir.is_dir():
            continue
        ticker = tdir.name
        notes = sorted(tdir.glob("*.md"))
        if not notes:
            continue
        latest = notes[-1]          # filenames lead with the date, so this is current
        try:
            fm = _parse_front_matter(latest.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError) as exc:
            print(f"thesis: could not read {ticker}/{latest.name} "
                  f"({type(exc).__name__}: {exc}); skipped.")
            continue
        if not fm:
            print(f"thesis: {latest.name} has no front-matter; skipped.")
            continue
        hist = [e for e in events if e.get("ticker") == ticker]
        view = {k: fm.get(k) for k in _THESIS_SCALARS if fm.get(k) not in (None, "")}
        view["ticker"] = ticker
        view["data_caveats"] = fm.get("data_caveats") or []
        view["note_path"] = f"theses/notes/{ticker}/{latest.name}"
        view["note_count"] = len(notes)
        view["history"] = [{"date": e.get("date"), "kind": e.get("kind"),
                            "direction": e.get("direction"),
                            "conviction": e.get("conviction"),
                            "target_price": e.get("target_price"),
                            "prior_conviction": e.get("prior_conviction"),
                            "trigger": e.get("trigger")} for e in hist]
        # Surfaced on the card: a revision that kept direction and conviction while
        # swapping the claim is what thesis drift looks like.
        view["claim_changed_ever"] = any(e.get("claim_changed") == "yes" for e in hist)
        view["notes"] = _thesis_notes_for(tdir, filings)
        view["last_close"] = _last_close(ticker)
        (THESIS_VIEW_DIR / _news_filename(ticker)).write_text(
            json.dumps(view, separators=(",", ":")), encoding="utf-8")
        written += 1
    print(f"thesis: wrote {written} view files to docs/thesis/.")
    return written


# --- company views ----------------------------------------------------------
# Everything the filings and companyfacts passes collect lives in CSVs and text
# files that the screener cannot read: the browser would have to pull a whole
# month of filing rows to find one ticker's. So the same derived-view pattern
# used for theses applies here, one small JSON per covered ticker.
#
# Only tickers that actually have collected data get a file. A missing file is
# the normal case for most of the universe and the page treats it as such.
COMPANY_VIEW_DIR = DOCS_DIR / "company"
# Enough periods to show a cycle without making the file big. MPC needs 2020 in
# view for its current earnings to mean anything.
_COMPANY_MAX_PERIODS = 14
_COMPANY_MAX_FILINGS = 8


def _company_financials():
    """ticker -> annual and quarterly reported history, newest last."""
    out = {}
    if not FINANCIALS_CSV_DIR.is_dir():
        return out
    for path in sorted(FINANCIALS_CSV_DIR.glob("*.csv")):
        try:
            with path.open(encoding="utf-8", newline="") as fh:
                for row in csv.DictReader(fh):
                    tk = (row.get("ticker") or "").upper()
                    if tk:
                        # Keyed on both, not on period alone: every annual row
                        # carries the literal period "FY", so keying on it
                        # collapsed a decade of history into one row.
                        key = (row.get("period", ""), row.get("period_end", ""))
                        out.setdefault(tk, {})[key] = row
        except Exception as exc:
            print(f"company: could not read {path.name} ({exc}).")
    series = {}
    for tk, periods in out.items():
        rows = sorted(periods.values(), key=lambda r: r.get("period_end") or "")
        annual = [r for r in rows if (r.get("period") or "").startswith("FY")]
        quarterly = [r for r in rows if not (r.get("period") or "").startswith("FY")]
        series[tk] = {
            "annual": [_company_period(r) for r in annual[-_COMPANY_MAX_PERIODS:]],
            "quarterly": [_company_period(r) for r in quarterly[-_COMPANY_MAX_PERIODS:]],
        }
    return series


def _company_period(row):
    """One reported period, numbers as numbers so the page need not parse."""
    out = {"period": row.get("period"), "period_end": row.get("period_end")}
    for f in ("revenue", "gross_profit", "operating_income", "net_income",
              "eps_diluted", "ocf", "capex", "assets", "equity", "shares_diluted"):
        v = row.get(f)
        if v not in (None, ""):
            try:
                out[f] = float(v)
            except ValueError:
                pass
    # Margins are the reason to look at this series at all, and computing them
    # here keeps one definition rather than one per consumer.
    rev = out.get("revenue")
    if rev:
        for name, num in (("gross_margin", "gross_profit"),
                          ("operating_margin", "operating_income"),
                          ("net_margin", "net_income")):
            if out.get(num) is not None:
                out[name] = out[num] / rev
        if out.get("ocf") is not None and out.get("capex") is not None:
            out["fcf"] = out["ocf"] - abs(out["capex"])
    return out


def _company_filings():
    """ticker -> the most recent filing rows, newest first, text path kept."""
    out = _read_filings_rows() or {}
    trimmed = {}
    for tk, rows in out.items():
        rows.sort(key=lambda r: (r.get("filed") or "", r.get("accession") or ""),
                  reverse=True)
        seen, keep = set(), []
        for r in rows:
            key = (r.get("doc_kind"), r.get("accession"))
            if key in seen:
                continue
            seen.add(key)
            try:
                chars = int(r.get("text_chars") or 0)
            except ValueError:
                chars = 0
            # collected is the day the pipeline fetched it, which can be months after
            # it was filed. A note could only have read what was collected by then.
            keep.append({"filed": r.get("filed"), "form": r.get("form"),
                         "doc_kind": r.get("doc_kind"), "items": r.get("items"),
                         "accession": r.get("accession"),
                         "collected": (r.get("recorded_at") or "")[:10],
                         "text_path": r.get("text_path"), "text_chars": chars})
            if len(keep) >= _COMPANY_MAX_FILINGS:
                break
        trimmed[tk] = keep
    return trimmed


# Segment NAMES only, deliberately. The rendered R-file carries the numbers too,
# but they arrive as a flattened line of several tables sharing one caption, and
# a generic scraper over them produced confident nonsense: Apple's "segments"
# came out as the XBRL footnote references Topic 280 and SubTopic 10, JPM's
# included "ROE NM NM NM NM", and MPC's first table is segment adjusted EBITDA
# rather than revenue, so its shares would have been labelled wrong while looking
# right. Segment revenue is worth having, but the way to get it is the inline
# XBRL contexts on StatementBusinessSegmentsAxis, where each number arrives
# tagged with its segment member and its measure. Until that exists, the page
# shows the names and links the note rather than printing a made-up mix.
# Anchored on the segment noun rather than the sentence opener, because filers
# introduce the list five different ways: "We have three reportable segments:",
# "Our reportable segments consist of", "There are three reportable business
# segments \u2013", "the following five operating segments:".
_SEGMENT_ANCHOR = re.compile(
    r"\b(?:reportable|reporting|operating|business)\s+segments?\b\s*"
    r"(?:consist(?:ing|s)?\s+of|are\s+comprised\s+of|are|include[sd]?|:|"
    r"\u2013|\u2014|\u2010|-)\s*([^.]{4,240})", re.I)
_SEGMENT_COUNT_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
                        "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10}


def _segment_names(text):
    """Segment names from the note's own sentence, or [].

    "We have three reportable segments: Refining & Marketing, Midstream and
    Renewable Diesel." is prose, not a table, and parses without guessing."""
    flat = " ".join((text or "").split())
    m = _SEGMENT_ANCHOR.search(flat)
    if not m:
        return []
    tail = m.group(1)
    # Cut at the first clause that stops listing and starts describing.
    tail = re.split(r"\b(?:each|these|which|as\s+of|for\s+the|in\s+addition|"
                    r"with\s+the|our\s+reportable|the\s+following)\b|"
                    r"\s[\u2013\u2014]\s", tail, 1, re.I)[0]
    parts = re.split(r"\s*,\s*|\s+and\s+|\s*;\s*", tail)
    names = []
    for p in parts:
        p = re.sub(r"^(?:and|or)\s+", "", p.strip(" .;:-\u2013\u2014"), flags=re.I)
        # A segment name is a short proper noun phrase. Anything longer is the
        # sentence continuing past the list.
        if not p or len(p) > 48 or len(p) < 2:
            continue
        if not re.match(r"^[A-Z0-9]", p) or re.search(r"\b(?:segment|which|that)\b", p, re.I):
            continue
        if p in ("The", "the", "A", "a", "An", "an"):
            continue
        names.append(p)
    if len(names) < 2:
        return []
    # Cross-check against the stated count. A mismatch means the split ran past
    # the list, so the names are not trustworthy enough to show.
    cm = re.search(r"\b(one|two|three|four|five|six|seven|eight|nine|ten|\d+)\s+"
                   r"(?:(?:reportable|reporting|operating|business)\s+){1,2}"
                   r"segments?\b", flat, re.I)
    if cm:
        raw = cm.group(1).lower()
        stated = _SEGMENT_COUNT_WORDS.get(raw) or (int(raw) if raw.isdigit() else None)
        if stated and stated != len(names):
            return []
    return names[:10]


# The opening of Item 1, minus the heading and the boilerplate that precedes the
# actual description. Filers start it half a dozen ways ("Item 1. Business
# Company Background The Company designs...", "ITEM 1. BUSINESS In this report,
# the terms..."), so the heading and any "in this report the terms" preamble are
# dropped and the first sentences that actually describe the business are kept.
_BIZ_HEAD = re.compile(
    r"^\s*Items?\s+1\.?\s*[\u2014\u2013-]?\s*Business\.?\s*", re.I)
_BIZ_PREAMBLE = re.compile(
    r"^\s*(?:General|Overview|Introduction|Company Overview|Our Company|"
    r"Company Background|Business|Our Business)\b[\s.:\u2014\u2013-]*", re.I)
_BIZ_SKIP_SENTENCE = re.compile(
    r"\b(?:in this (?:report|Annual Report|Form 10-K)|unless the context|"
    r"references to|refer to|collectively|as used herein|incorporated (?:in|by)|"
    r"Financial Statements and Supplementary Data|Notes to Consolidated|"
    r"see Item\s+\d|Items?\s+\d[A-B]?\.)\b",
    re.I)
_BIZ_TARGET_CHARS = 520
# Trailing dots that do not end a sentence. Ordered longest first so that "U.S."
# is matched before "S.".
_BIZ_ABBREV = re.compile(
    r"\b(?:U\.S\.A?|N\.V|S\.A|L\.P|L\.L\.C|Inc|Corp|Co|Ltd|LLC|PLC|plc|"
    r"Mr|Mrs|Ms|Dr|St|No|Nos|vs|approx|Cir|Ass'n)\.", re.I)


def _business_excerpt(text):
    """First few sentences of Item 1 that actually describe the business."""
    flat = " ".join((text or "").split())
    if not flat:
        return ""
    flat = _BIZ_HEAD.sub("", flat)
    for _ in range(3):
        trimmed = _BIZ_PREAMBLE.sub("", flat)
        if trimmed == flat:
            break
        flat = trimmed
    # Split on sentence ends, keeping the terminator. Abbreviations inside a
    # company name would split early, so a fragment shorter than 30 characters
    # is glued back onto the one before it.
    # Protect abbreviations before splitting. "CF Industries Holdings, Inc. and
    # its subsidiaries" split at "Inc.", which cut a definitions sentence in
    # half so that only the first half was recognised as one and skipped.
    # Python has no variable-length lookbehind, so the dots are masked instead.
    guarded = _BIZ_ABBREV.sub(lambda m: m.group(0).replace(".", "\x00"), flat)
    parts, buf = [], ""
    for chunk in (c.replace("\x00", ".") for c in re.split(r"(?<=[.!?])\s+", guarded)):
        if buf and len(chunk) < 30:
            buf += " " + chunk
            continue
        if buf:
            parts.append(buf)
        buf = chunk
    if buf:
        parts.append(buf)

    out, skipped = [], 0
    for sent in parts:
        # Defined terms are not a description of the business. CF opens with
        # four such sentences, so skipping only the first left an excerpt that
        # explained which subsidiary "CF Industries" refers to and nothing else.
        if not out and skipped < 6 and _BIZ_SKIP_SENTENCE.search(sent):
            skipped += 1
            continue
        out.append(sent)
        if sum(len(x) for x in out) >= _BIZ_TARGET_CHARS:
            break
    excerpt = " ".join(out).strip()
    # A heading can sit inside the first kept sentence rather than at the very
    # start of the item, which is how KO kept "General" and YELP "Company
    # Overview". Strip again now that the sentence is chosen.
    for _ in range(3):
        trimmed = _BIZ_PREAMBLE.sub("", excerpt)
        if trimmed == excerpt:
            break
        excerpt = trimmed
    return excerpt.strip() if len(excerpt) >= 80 else ""


def write_company_views(stocks=None):
    """One JSON per ticker with reported history, filings held and segment mix.

    Regenerated every run from the CSVs, same as the thesis views: docs/ is
    force-pushed to gh-pages, so nothing under it can be authoritative."""
    fins = _company_financials()
    filings = _company_filings()
    tickers = set(fins) | set(filings)
    if not tickers:
        print("company: nothing collected yet, no view files written.")
        return 0

    # Peer share needs the panel's revenue and sub-industry, which the scoring
    # pass has already attached to the stock dicts.
    peers = {}
    if stocks:
        groups = {}
        for s in stocks:
            grp, rev = s.get("sub_industry"), s.get("ttm_revenue")
            if grp and isinstance(rev, (int, float)) and rev > 0:
                groups.setdefault(grp, []).append((s.get("ticker"), float(rev)))
        for grp, members in groups.items():
            if len(members) < 3:
                continue        # a "share" of two names says nothing
            total = sum(r for _t, r in members)
            ranked = sorted(members, key=lambda x: -x[1])
            for pos, (tk, rev) in enumerate(ranked, 1):
                peers[tk] = {"group": grp, "n": len(members), "rank": pos,
                             "share": rev / total if total else None,
                             "leader": ranked[0][0],
                             "leader_share": ranked[0][1] / total if total else None}

    COMPANY_VIEW_DIR.mkdir(parents=True, exist_ok=True)
    written = 0
    for tk in sorted(tickers):
        view = {"ticker": tk}
        if tk in fins:
            view["reported"] = fins[tk]
        rows = filings.get(tk) or []
        if rows:
            view["filings"] = rows
        if tk in peers:
            view["peer_share"] = peers[tk]
        biz_row = next((r for r in rows if r.get("doc_kind") == "business"), None)
        if biz_row and biz_row.get("text_path"):
            biz_file = FILINGS_CSV_DIR / biz_row["text_path"]
            if biz_file.exists():
                try:
                    excerpt = _business_excerpt(biz_file.read_text(encoding="utf-8"))
                except Exception:
                    excerpt = ""
                if excerpt:
                    view["business"] = {"excerpt": excerpt,
                                        "filed": biz_row.get("filed"),
                                        "text_path": biz_row.get("text_path"),
                                        "text_chars": biz_row.get("text_chars")}

        seg_row = next((r for r in rows if r.get("doc_kind") == "segment_note"), None)
        if seg_row and seg_row.get("text_path"):
            seg_file = FILINGS_CSV_DIR / seg_row["text_path"]
            if seg_file.exists():
                try:
                    names = _segment_names(seg_file.read_text(encoding="utf-8"))
                except Exception:
                    names = []
                if names:
                    view["segments"] = {"names": names,
                                        "as_of": seg_row.get("filed"),
                                        "form": seg_row.get("form"),
                                        "text_path": seg_row.get("text_path"),
                                        "text_chars": seg_row.get("text_chars")}
        (COMPANY_VIEW_DIR / _news_filename(tk)).write_text(
            json.dumps(view, separators=(",", ":")), encoding="utf-8")
        written += 1
    print(f"company: wrote {written} view files to docs/company/.")
    return written

# --- history views ------------------------------------------------------------
#
# The company page shows where a listing sits today. These files let it show how
# each tracked metric, and its standing against the universe, has moved since the
# panel began (2026-09-03). They are read from data/fundamentals/, the append-only
# daily panel, and regenerated whole on every site build, like every other file
# under docs/.
#
#   docs/history/TICKER.json   one per ticker in the current universe that has a
#                              panel row: {"t", "d": [dates], "v": {key: [values]},
#                              "stale": [date indices], "na": {key: 1 | [indices]},
#                              "c": [change_pct], "gap": [date indices],
#                              "pf": {date: close}}
#   docs/history/_universe.json {"d": [dates], "n": [operating rows],
#                              "m": {key: {"c": [centre], "s": [scale], "n": [count],
#                              "sd": [indices using mean and sd]}}}
#
# Values are the panel's, withheld where the site withholds them: every
# price-derived field on a row flagged price_stale (or whose price_date is older
# than its own date), and every field apply_security_types marks not_applicable
# for the row's security_type. A withheld value is null, so the page draws a gap.
#
# "c" is the panel's change_pct per date, null where the row is stale or flagged
# change_gap; "gap" lists the change_gap rows, so the page can say the day's
# change is unavailable rather than merely unrecorded.
#
# "pf" fills the price chart only. For a panel date from the ticker's first row
# on where the view has no price (a stale row, or no row), it holds the close
# stored for that exact date in docs/prices/TICKER.json, as the vendor reported
# it. A date without a stored close is left out: nothing is interpolated or
# carried forward, and no other metric is filled.
#
# The universe stats are web/zengine.js's, per date: operating listings only
# (security_type not in NON_OPERATING), log10 for market_cap and volume, centre
# the median and scale 1.4826 * MAD, or mean and sd where MAD is 0. The page
# computes z = clip((T(value) - c) / s, +/-5) for any date.
HISTORY_VIEW_DIR = DOCS_DIR / "history"
HISTORY_UNIVERSE_FILE = "_universe.json"
HISTORY_KEYS = ["price"] + [m["key"] for m in LEDGER_METRICS]
_HISTORY_KEY_SET = frozenset(HISTORY_KEYS)
_HISTORY_PRICE_FIELDS = frozenset(_PANEL_PRICE_FIELDS)


def _hist_round(v, sig=5):
    """A float to `sig` significant figures, as an int when that is exact."""
    if v == 0:
        return 0
    r = float(f"{v:.{sig}g}")
    return int(r) if r.is_integer() and abs(r) < 1e15 else r


def _history_na(cat, row_vals):
    """Keys that do not apply to a row of this security_type: the same rules
    apply_security_types uses (_NA_ALWAYS, and _NA_IF_ZERO on an exact 0)."""
    out = set(k for k in _NA_ALWAYS.get(cat, ()) if k in _HISTORY_KEY_SET)
    for k in _NA_IF_ZERO.get(cat, ()):
        if row_vals.get(k) == 0:
            out.add(k)
    return out


def _read_history_panel(paths):
    """{date: [(ticker, security_type, stale, {key: float|None}, na_keys)]}.

    csv.reader with column indices rather than DictReader: the panel is about
    60,000 rows a month and this runs on every build."""
    by_date = collections.defaultdict(list)
    for path in paths:
        with path.open(encoding="utf-8", newline="") as fh:
            rd = csv.reader(fh)
            try:
                header = next(rd)
            except StopIteration:
                continue
            col = {h: j for j, h in enumerate(header)}
            ci = [(k, col[k]) for k in HISTORY_KEYS if k in col]
            j_date, j_tk = col.get("date"), col.get("ticker")
            j_type, j_stale = col.get("security_type"), col.get("price_stale")
            j_pd, j_price = col.get("price_date"), col.get("price")
            j_chg, j_gap = col.get("change_pct"), col.get("change_gap")
            if j_date is None or j_tk is None:
                continue
            width = len(header)
            for row in rd:
                if len(row) < width:
                    row = row + [""] * (width - len(row))
                date, tk = row[j_date], row[j_tk]
                if not date or not tk:
                    continue
                vals = {}
                for k, j in ci:
                    s = row[j]
                    if s == "":
                        continue
                    try:
                        v = float(s)
                    except ValueError:
                        continue
                    if math.isfinite(v):
                        vals[k] = v
                cat = row[j_type] if j_type is not None else ""
                if not cat:
                    cat = sectype.classify_row(dict(zip(header, row)))
                stale = j_stale is not None and row[j_stale] not in ("", "0", "0.0")
                # The rule _panel_rows_for_session applies before writing: a price
                # from a series that ends before the row's own date is not that
                # day's. Rows written before the flag existed can still carry one.
                if not stale and j_pd is not None and j_price is not None:
                    pdate = row[j_pd]
                    stale = bool(pdate) and row[j_price] != "" and pdate[:10] < date
                if stale:
                    for k in _HISTORY_PRICE_FIELDS:
                        vals.pop(k, None)
                na = _history_na(cat, vals) if cat in _NA_ALWAYS or cat in _NA_IF_ZERO else ()
                for k in na:
                    vals.pop(k, None)
                # The day's change: withheld on a stale row like every price
                # field, and on a change_gap row, whose recorded figure spans two
                # sessions (the 2026-09-23 rows keep it, flagged, in the panel).
                gap = j_gap is not None and row[j_gap] not in ("", "0", "0.0")
                chg = None
                if not stale and not gap and j_chg is not None and row[j_chg] != "":
                    try:
                        chg = float(row[j_chg])
                    except ValueError:
                        chg = None
                    if chg is not None and not math.isfinite(chg):
                        chg = None
                by_date[date].append((tk, cat, stale, vals, na, chg, gap))
    return by_date


def _history_universe(by_date, dates):
    """Per date, per metric, zengine's centre and scale over the operating rows."""
    out = {"d": dates, "n": [], "m": {}}
    per = {m["key"]: {"c": [], "s": [], "n": []} for m in LEDGER_METRICS}
    sd_idx = {m["key"]: [] for m in LEDGER_METRICS}
    for di, d in enumerate(dates):
        rows = [r for r in by_date[d] if r[1] not in sectype.NON_OPERATING]
        out["n"].append(len(rows))
        for m in LEDGER_METRICS:
            k, log = m["key"], m["transform"] == "log10"
            xs = []
            for r in rows:
                v = r[3].get(k)
                if v is None:
                    continue
                if log:
                    if v <= 0:
                        continue
                    v = math.log10(v)
                xs.append(v)
            p = per[k]
            if not xs:
                p["c"].append(None); p["s"].append(None); p["n"].append(0)
                continue
            st = _ledger_stat(xs)
            ok = st["method"] != "none"
            p["c"].append(_hist_round(st["center"], 7) if ok else None)
            p["s"].append(_hist_round(st["scale"], 7) if ok else None)
            p["n"].append(st["n"])
            if st["method"] == "sd":
                sd_idx[k].append(di)
    for k, p in per.items():
        if sd_idx[k]:
            p["sd"] = sd_idx[k]
        out["m"][k] = p
    return out


def _history_price_fill(tk, dates, idx, prices, na_idx):
    """{date: close} for the price chart's empty days, from the stored series.

    `dates` is the panel calendar, `idx` the indices of the ticker's rows in it,
    `prices` the view's price per row (None where withheld). A day qualifies when
    it is on the calendar from the ticker's first row to the last date, has no
    price in the view, price applies to the row, and the stored series holds a
    close for that exact date. The close is copied as stored; a day without one
    stays empty. Only the price chart reads this: P/E and every other metric
    stay blank on those days."""
    if not idx:
        return {}
    have = {}
    for j, di in enumerate(idx):
        if prices is not None and prices[j] is not None:
            have[di] = True
    empty = [di for di in range(idx[0], len(dates)) if di not in have and di not in na_idx]
    if not empty:
        return {}
    try:
        blob = json.loads((PRICES_DIR / _news_filename(tk)).read_text(encoding="utf-8"))
    except Exception:
        return {}
    stored = {}
    for pair in blob.get("closes") or []:
        try:
            v = float(pair[1])
        except (TypeError, ValueError, IndexError):
            continue
        if math.isfinite(v) and v > 0:
            stored[str(pair[0])] = v
    return {dates[di]: _hist_round(stored[dates[di]], 6) for di in empty if dates[di] in stored}


def write_history_views(stocks=None):
    """docs/history/TICKER.json for every ticker in the current universe that the
    panel has rows for, and docs/history/_universe.json. Returns files written.

    Files left from an earlier build (restored from gh-pages) for tickers that
    are no longer in the universe are removed, so the page never offers one."""
    t0 = time.time()
    paths = sorted(FUNDAMENTALS_CSV_DIR.glob("????-??.csv"))
    if not paths:
        print("history: no panel files, no history written.")
        return 0
    by_date = _read_history_panel(paths)
    dates = sorted(by_date)
    if not dates:
        print("history: the panel has no rows, no history written.")
        return 0
    wanted = {s.get("ticker") for s in (stocks or []) if s.get("ticker")}

    per_tk = collections.defaultdict(list)       # ticker -> [(date index, row)]
    for di, d in enumerate(dates):
        for r in by_date[d]:
            if not wanted or r[0] in wanted:
                per_tk[r[0]].append((di, r))

    HISTORY_VIEW_DIR.mkdir(parents=True, exist_ok=True)
    written, keep, total, filled = 0, {HISTORY_UNIVERSE_FILE}, 0, 0
    for tk in sorted(per_tk):
        seq = per_tk[tk]
        seq.sort(key=lambda x: x[0])
        # One row per date: the panel's key is date+ticker; a duplicate would be
        # an earlier bug, and the later row is the one written last.
        dedup = {}
        for di, r in seq:
            dedup[di] = r
        idx = sorted(dedup)
        view = {"t": tk, "d": [dates[di] for di in idx], "v": {}}
        stale = [j for j, di in enumerate(idx) if dedup[di][2]]
        if stale:
            view["stale"] = stale
        na = {}
        for k in HISTORY_KEYS:
            sig = 6 if k == "price" else 5
            arr = []
            for di in idx:
                v = dedup[di][3].get(k)
                arr.append(None if v is None else _hist_round(v, sig))
            if any(v is not None for v in arr):
                view["v"][k] = arr
            hits = [j for j, di in enumerate(idx) if k in dedup[di][4]]
            if hits:
                na[k] = 1 if len(hits) == len(idx) else hits
        if na:
            view["na"] = na
        chg = [dedup[di][5] for di in idx]
        if any(v is not None for v in chg):
            view["c"] = [None if v is None else _hist_round(v, 5) for v in chg]
        gap = [j for j, di in enumerate(idx) if dedup[di][6]]
        if gap:
            view["gap"] = gap
        fill = _history_price_fill(tk, dates, idx, view["v"].get("price"),
                                   {di for di in idx if "price" in dedup[di][4]})
        if fill:
            view["pf"] = fill
            filled += len(fill)
        name = _news_filename(tk)
        body = json.dumps(view, separators=(",", ":"), allow_nan=False)
        (HISTORY_VIEW_DIR / name).write_text(body, encoding="utf-8")
        total += len(body)
        keep.add(name)
        written += 1

    uni = _history_universe(by_date, dates)
    body = json.dumps(uni, separators=(",", ":"), allow_nan=False)
    (HISTORY_VIEW_DIR / HISTORY_UNIVERSE_FILE).write_text(body, encoding="utf-8")
    total += len(body)
    removed = 0
    for p in HISTORY_VIEW_DIR.glob("*.json"):
        if p.name not in keep:
            p.unlink()
            removed += 1
    print(f"history: wrote {written} ticker files and {HISTORY_UNIVERSE_FILE} to docs/history/ "
          f"({len(dates)} panel dates, {dates[0]} to {dates[-1]}; {total / 1024 / 1024:.1f} MB; "
          f"{time.time() - t0:.1f}s" + (f"; {removed} stale files removed" if removed else "")
          + f"; {filled} price-chart days drawn from stored closes).")
    return written


# --- the research page ------------------------------------------------------
#
# The screen ranks 5,354 names by arithmetic. This page holds the part that is
# not arithmetic: what the analyst actually concluded about a handful of them,
# and whether those conclusions have been any good.
#
# Coverage is about four names a week against roughly a thousand eligible, so
# the notes are invisible from the screen unless you already know the ticker.
# That is what this page is for. It is built server-side from theses/, the same
# source write_thesis_views reads, so it cannot disagree with the note cards on
# the stocks page.
RESEARCH_STATUS_ORDER = {"due": 0, "open": 1, "watching": 2, "graded": 3}
# A direction that commits to something gets a prediction row and a grade.
# "watch" and "no view" deliberately do not: see append_prediction in events.py.
GRADEABLE_DIRECTIONS = ("long", "short", "avoid")


def _research_price(ticker, entry_price):
    """Last close and move since the note was written, or {}.

    Reads the same docs/prices/{TICKER}.json the screener charts use. Absent in
    a local run that has not fetched prices; the page renders a dash."""
    lc = _last_close(ticker)
    if not lc:
        return {}
    last = lc["close"]
    out = {"last": round(last, 2), "as_of": lc["date"]}
    try:
        entry = float(entry_price)
    except (TypeError, ValueError):
        return out
    if entry > 0:
        out["move"] = round(last / entry - 1.0, 4)
    return out


def _research_views(stocks_by_ticker, today):
    """One record per covered ticker, newest note wins."""
    notes_dir = THESES_DIR / "notes"
    if not notes_dir.is_dir():
        return []
    events = []
    ev_path = THESES_DIR / "ledger" / "events.csv"
    if ev_path.exists():
        with ev_path.open(encoding="utf-8", newline="") as fh:
            events = list(csv.DictReader(fh))
    preds, scored = [], set()
    for name, sink in (("predictions.csv", preds), ("scores.csv", None)):
        path = THESES_DIR / "ledger" / name
        if not path.exists():
            continue
        with path.open(encoding="utf-8", newline="") as fh:
            rows = list(csv.DictReader(fh))
        if sink is None:
            scored = {r.get("prediction_id") for r in rows}
        else:
            sink.extend(rows)
    pred_by_ticker = {}
    for p in preds:
        pred_by_ticker.setdefault(p.get("ticker"), []).append(p)
    filings = _read_filings_rows()

    out = []
    for tdir in sorted(notes_dir.iterdir()):
        if not tdir.is_dir():
            continue
        ticker = tdir.name
        notes = sorted(tdir.glob("*.md"))
        if not notes:
            continue
        latest = notes[-1]
        try:
            fm = _parse_front_matter(latest.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError) as exc:
            print(f"research: could not read {ticker}/{latest.name} "
                  f"({type(exc).__name__}: {exc}); skipped.")
            continue
        if not fm:
            continue
        direction = _note_scalar(fm, "direction") or ""
        mine = pred_by_ticker.get(ticker) or []
        open_preds = [p for p in mine if p.get("prediction_id") not in scored]
        if direction not in GRADEABLE_DIRECTIONS:
            status = "watching"
        elif mine and not open_preds:
            status = "graded"
        else:
            status = "open"
        review_by = _note_scalar(fm, "review_by") or ""
        days_to_review = None
        if review_by:
            try:
                d = (datetime.strptime(review_by, "%Y-%m-%d").date() - today).days
                days_to_review = d
                if d < 0 and status in ("open", "watching"):
                    status = "due"
            except ValueError:
                pass

        s = stocks_by_ticker.get(ticker) or {}
        sleeves = {k: s.get(k) for k in ("g", "v", "m", "q")
                   if isinstance(s.get(k), (int, float))}
        rec = {
            "ticker": ticker,
            "name": s.get("name") or ticker,
            "sector": s.get("sector") or "",
            "sub_industry": s.get("sub_industry") or "",
            "direction": direction or "no view",
            "status": status,
            "review_by": review_by,
            "days_to_review": days_to_review,
            "note_path": f"theses/notes/{ticker}/{latest.name}",
            "note_count": len(notes),
            "price": _research_price(ticker, fm.get("entry_price")),
            "sleeves": sleeves,
            "data_caveats": fm.get("data_caveats") or [],
        }
        for k in ("conviction", "evidence_base", "falsifier_specific",
                  "variant_perception", "disconfirmation", "horizon_days"):
            try:
                rec[k] = int(str(fm.get(k, "")).strip())
            except (TypeError, ValueError):
                rec[k] = None
        for k in ("entry_price", "target_price"):
            try:
                rec[k] = float(str(fm.get(k, "")).strip())
            except (TypeError, ValueError):
                rec[k] = None
        for k in ("kind", "written_on", "key_claim", "falsifier", "slot", "thesis_id"):
            rec[k] = _note_scalar(fm, k) or ""
        hist = [e for e in events if e.get("ticker") == ticker]
        rec["history"] = [{"date": e.get("date"), "kind": e.get("kind"),
                           "direction": e.get("direction"),
                           "conviction": e.get("conviction"),
                           "prior_direction": e.get("prior_direction"),
                           "prior_conviction": e.get("prior_conviction"),
                           "trigger": e.get("trigger")} for e in hist]
        rec["claim_changed_ever"] = any(e.get("claim_changed") == "yes" for e in hist)
        # Every note on the name, newest first, for the library timeline. Built by
        # the same helper as docs/thesis/{TICKER}.json so the two cannot drift.
        full_notes = _thesis_notes_for(tdir, filings)
        rec["notes"] = [_thesis_note_summary(n) for n in full_notes]
        # The current note whole, sections included: the page prints it in full.
        rec["current"] = full_notes[0] if full_notes else None
        # The screen fields any note's conditions test, so the research page can
        # hand the popup those values and it can print HOLDING or NOT MET there.
        rec["check_fields"] = sorted({
            str(c["check"]["field"]) for n in full_notes
            for c in (n.get("conditions") or [])
            if isinstance(c, dict) and isinstance(c.get("check"), dict)
            and c["check"].get("field")})
        # Equal weight, and labelled as such on the page. The screener's own
        # composite depends on slider weights the reader sets, so there is no
        # single number to quote here; the four sleeve percentiles are.
        if len(sleeves) == 4:
            rec["equal_weight"] = round(sum(sleeves.values()) / 4, 4)
        out.append(rec)
    out.sort(key=lambda r: (RESEARCH_STATUS_ORDER.get(r["status"], 9),
                            r.get("written_on") or ""), reverse=False)
    return out


def _research_record(views):
    """The track record, and an honest account of it when it is empty."""
    scores = []
    sc_path = THESES_DIR / "ledger" / "scores.csv"
    if sc_path.exists():
        with sc_path.open(encoding="utf-8", newline="") as fh:
            scores = list(csv.DictReader(fh))
    tiers = {}
    for v in views:
        c = v.get("conviction")
        if c is None:
            continue
        t = tiers.setdefault(c, {"conviction": c, "n": 0, "open": 0,
                                 "graded": 0, "watching": 0})
        t["n"] += 1
        t[{"due": "open"}.get(v["status"], v["status"])] = \
            t.get({"due": "open"}.get(v["status"], v["status"]), 0) + 1
    # Maturity is horizon_days from the note date, which is not review_by: the
    # review is a prompt to look again, the horizon is when the call is graded.
    maturities = []
    for v in views:
        if v["direction"] not in GRADEABLE_DIRECTIONS:
            continue
        if not v.get("written_on") or not v.get("horizon_days"):
            continue
        try:
            d = datetime.strptime(v["written_on"], "%Y-%m-%d").date()
        except ValueError:
            continue
        maturities.append((d + timedelta(days=v["horizon_days"])).isoformat())
    return {
        "notes": sum(v["note_count"] for v in views),
        "tickers": len(views),
        "open": sum(1 for v in views if v["status"] in ("open", "due")),
        "watching": sum(1 for v in views if v["status"] == "watching"),
        "graded": len(scores),
        "tiers": [tiers[k] for k in sorted(tiers)],
        "earliest_maturity": min(maturities) if maturities else "",
        "scored": bool(scores),
    }


_NOTE_REPO_URL = "https://github.com/CTLSmith5689/daily-intelligence-brief/blob/main/"


# The view in the words the notes themselves use (theses/PROMPTS.md, PLAIN WORDS).
# "Long" and "avoid" are the analyst's shorthand, not the reader's.
_VIEW_WORDS = {"long": "Own it", "short": "Bet against it", "avoid": "Stay away",
               "watch": "Keep watching", "no view": "No view"}
_STATUS_WORDS = {"open": "Open call", "watching": "Watching", "graded": "Checked",
                 "due": "Review overdue"}
# --- the model portfolios ---------------------------------------------------------
#
# portfolios.html has one card per book; book.html#ID shows one book in full. Both
# read portfolio.engine.site_data: the ledger in portfolio/ledger, the stored
# closes in docs/prices, the benchmark closes in docs/prices/_BENCHMARKS.json, the
# daily record in data/portfolio/nav.csv, and the rules candidate book, which is
# recomputed from the latest panel date on every run so the PM can compare it with
# what is held. Nothing here trades: after inception (portfolio/bin/seed.py),
# trading is the PM's job.
PORTFOLIO_DIR = REPO_ROOT / "portfolio"
PORTFOLIO_NAV_CSV = DATA_DIR / "portfolio" / "nav.csv"
# The PM draft that research.html used to show in full. Kept in the repository and
# linked from the Portfolios page under "Earlier draft".
PORTFOLIO_DRAFTS = (("PM-agent-draft.md", "The draft instructions for the portfolio manager"),
                    ("PM-decision-sample.md", "A sample decision on NVIDIA"))
# What the cards page leaves out of its data; book.html carries everything.
_PORTFOLIO_DETAIL_KEYS = ("trades", "decisions", "holdings", "candidate", "mandateHistory",
                          "unpriced", "scoring")

# --- the instructions the agents run on ---------------------------------------
#
# The claude.ai routines' prompts are mirrored as files (theses/routines/,
# portfolio/routines/) and the long instructions they point to are one section of
# theses/PROMPTS.md (the analyst) and of portfolio/PROMPTS.md (the PMs). Research
# and Portfolios print both, read from the repository
# at build time, so an edit to a file is on the site after the next run. The
# files are text anyone with push access can change, so they go through the same
# escape-first renderer as the notes.
PROMPTS_MD = REPO_ROOT / "theses" / "PROMPTS.md"
PM_PROMPTS_MD = REPO_ROOT / "portfolio" / "PROMPTS.md"
RESEARCH_ROUTINE = REPO_ROOT / "theses" / "routines" / "research-agent.md"
PORTFOLIO_ROUTINES_DIR = REPO_ROOT / "portfolio" / "routines"
PORTFOLIO_BRIEFS_DIR = REPO_ROOT / "portfolio" / "books"
# (routine file, who it is, the books it runs), in the order the page shows them.
PM_ROUTINES = (("style-pm.md", "Style PM", "The six style books and the Hedge Fund Strategy Model"),
               ("neural-pm.md", "Neural PM", "The Neural Model Portfolio, on its own"))
# (brief file, its name, the books it is for). The mapping itself is written in
# portfolio/PROMPTS.md; this only orders the page.
PM_BRIEFS = (("growth.md", "Growth brief", "lg-growth, mid-growth and sm-growth"),
             ("value.md", "Value brief", "lg-value, mid-value and sm-value"),
             ("hedge.md", "Hedge brief", "the hedge book"),
             ("neural.md", "Neural brief", "the neural book"))


def _doc_md_to_html(text):
    """Instruction files as HTML, through the same safe subset as the notes.

    _md_to_html has no headings, code blocks or numbered lists, and the prompts use
    all three. Those are handled here, escaped, and every other run of lines goes
    through _md_to_html unchanged. Headings become h5, as in a note's body; fenced
    and indented code keeps its line breaks and spacing."""
    if not text:
        return ""
    lines = str(text).replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out, prose = [], []
    code = None
    prev_blank = True

    def flush():
        if prose:
            out.append(_md_to_html("\n".join(prose)))
            prose.clear()

    def pre(block):
        out.append("<pre><code>" + _html.escape("\n".join(block), quote=False)
                   + "</code></pre>")

    i = 0
    while i < len(lines):
        line = lines[i]
        s = line.strip()
        if code is not None:
            if s.startswith("```"):
                pre(code)
                code = None
            else:
                code.append(line)
            i += 1
            continue
        if s.startswith("```"):
            flush()
            code = []
        elif re.match(r"^#{1,6}\s", s):
            flush()
            out.append("<h5>" + _md_inline(_html.escape(s.lstrip("#").strip())) + "</h5>")
        elif s == "---":
            flush()
        elif prev_blank and not prose and line.startswith("    "):
            # An indented block after a blank line is code, as in markdown.
            block = []
            while i < len(lines) and (lines[i].startswith("    ") or not lines[i].strip()):
                block.append(lines[i][4:])
                i += 1
            while block and not block[-1].strip():
                block.pop()
            pre(block)
            prev_blank = True
            continue
        elif re.match(r"^\d+\.\s", s):
            # A numbered item starts its own paragraph; its number stays as typed.
            flush()
            prose.append(s)
        elif not s:
            flush()
        else:
            prose.append(line)
        prev_blank = not s
        i += 1
    if code is not None:
        pre(code)
    flush()
    return "".join(out)


def _prompts_section(heading, path=None):
    """The body of one "## " section of a PROMPTS.md (theses/ unless `path` says
    otherwise), without its heading, or "" when the file or the section is missing."""
    try:
        text = Path(path or PROMPTS_MD).read_text(encoding="utf-8")
    except OSError:
        return ""
    lines = text.replace("\r\n", "\n").split("\n")
    try:
        start = next(i for i, l in enumerate(lines) if l.strip() == heading)
    except StopIteration:
        return ""
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")),
               len(lines))
    return "\n".join(lines[start + 1:end]).strip()


def _routine_doc(path):
    """A mirrored routine prompt: {"schedule", "html"} from a file whose header (a
    "Schedule:" line) ends at the first "---" line, or None when it is missing."""
    try:
        text = Path(path).read_text(encoding="utf-8").replace("\r\n", "\n")
    except OSError:
        return None
    head, sep, body = text.partition("\n---\n")
    if not sep:
        head, body = "", text
    m = re.search(r"^Schedule:\s*(.+)$", head, re.M)
    return {"schedule": m.group(1).strip() if m else "", "html": _doc_md_to_html(body.strip())}


def _rel(path):
    try:
        return Path(path).resolve().relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return Path(path).name


def _analyst_instructions():
    """What research.html prints under "How the analyst works"."""
    routine = _routine_doc(RESEARCH_ROUTINE)
    section = _prompts_section("## Agent 1: the analyst")
    if not routine and not section:
        return None
    return {"routine": routine, "routinePath": _rel(RESEARCH_ROUTINE),
            "prompt": _doc_md_to_html(section), "promptPath": _rel(PROMPTS_MD),
            "promptSection": "Agent 1: the analyst"}


DIRECTOR_MD = REPO_ROOT / "theses" / "DIRECTOR.md"
DIRECTOR_ROUTINE = REPO_ROOT / "theses" / "routines" / "research-director.md"
DIRECTOR_PLANS = REPO_ROOT / "theses" / "director"
DESKS_DIR = REPO_ROOT / "theses" / "desks"
THESES_CONFIG = REPO_ROOT / "theses" / "config.json"


def _desk_titles():
    """{sector: {"desk": slug, "title": name}} from the "desks" map in
    theses/config.json and each desk file's "# " heading, the same sources
    theses/bin/desks.py reads."""
    try:
        mapping = json.loads(THESES_CONFIG.read_text(encoding="utf-8")).get("desks") or {}
    except (OSError, ValueError):
        return {}
    out = {}
    for sector, slug in mapping.items():
        title = slug
        try:
            m = re.search(r"^#\s+(.+?)\s*$", (DESKS_DIR / f"{slug}.md").read_text(encoding="utf-8"), re.M)
            title = m.group(1) if m else slug
        except OSError:
            pass
        out[sector] = {"desk": slug, "title": title}
    return out


def _director_instructions():
    """What research.html prints under "How the director works"."""
    routine = _routine_doc(DIRECTOR_ROUTINE)
    try:
        text = DIRECTOR_MD.read_text(encoding="utf-8").replace("\r\n", "\n")
    except OSError:
        text = ""
    text = re.sub(r"\A\s*#[ \t]+[^\n]*\n", "", text).strip()
    if not routine and not text:
        return None
    return {"routine": routine, "routinePath": _rel(DIRECTOR_ROUTINE),
            "prompt": _doc_md_to_html(text), "promptPath": _rel(DIRECTOR_MD)}


def _plan_flow_map(s):
    m = re.fullmatch(r"\{(.*)\}", s.strip())
    if not m:
        return None
    parts, buf, quoted = [], [], False
    for ch in m.group(1):
        if ch == '"':
            quoted = not quoted
        if ch == "," and not quoted:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    parts.append("".join(buf))
    out = {}
    for p in parts:
        k, sep, v = p.partition(":")
        if sep:
            v = v.strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                v = v[1:-1]
            out[k.strip().lower()] = v.strip()
    return out


def _director_plan(today=None):
    """The plan for the current week, for research.html: its assignments and its
    body sections as safe HTML, or None when there is no plan.

    The current week's plan is the newest theses/director/{SUNDAY}.md whose Sunday
    is on or before today, so on a Sunday evening the page moves to the plan just
    written for the week ahead. It is shown as written; whether it passed
    director_check.py is recorded in each run's manifest, not here."""
    today = today or datetime.now(tz=EASTERN).date()
    if not DIRECTOR_PLANS.is_dir():
        return None
    plans = sorted(p for p in DIRECTOR_PLANS.glob("*.md")
                   if re.fullmatch(r"\d{4}-\d{2}-\d{2}\.md", p.name) and p.stem <= today.isoformat())
    if not plans:
        return None
    path = plans[-1]
    try:
        text = path.read_text(encoding="utf-8").replace("\r\n", "\n")
    except OSError:
        return None
    m = re.match(r"^---\n(.*?)\n---\n?(.*)$", text, re.S)
    head, body = (m.group(1), m.group(2)) if m else ("", text)
    week_of, assignments, in_list = "", [], False
    for line in head.splitlines():
        kv = re.match(r"^([A-Za-z_]\w*):\s*(.*)$", line)
        if kv:
            in_list = kv.group(1) == "assignments"
            if kv.group(1) == "week_of":
                week_of = kv.group(2).strip().strip("\"'")
            continue
        item = re.match(r"^\s*-\s+(.*)$", line)
        if item and in_list:
            a = _plan_flow_map(item.group(1))
            if a:
                assignments.append({k: a.get(k, "") for k in ("date", "ticker", "kind", "desk", "reason")})
    titles = {v["desk"]: v["title"] for v in _desk_titles().values()}
    for a in assignments:
        a["deskTitle"] = titles.get(a["desk"], a["desk"])
    sections = []
    for chunk in re.split(r"^(?=##[ \t]+[^#])", body, flags=re.M):
        h = re.match(r"^##[ \t]+(.+?)[ \t]*\n", chunk)
        if h:
            sections.append({"title": h.group(1).strip(),
                             "html": _doc_md_to_html(chunk[h.end():].strip())})
    return {"path": _rel(path), "weekOf": week_of, "assignments": assignments,
            "sections": sections}


def _pm_instructions():
    """What portfolios.html prints under "How the PMs work": one entry per PM
    routine, the portfolio/PROMPTS.md section they all follow, rendered once, and
    the book briefs in portfolio/books/."""
    pms = []
    for name, who, books in PM_ROUTINES:
        path = PORTFOLIO_ROUTINES_DIR / name
        doc = _routine_doc(path)
        if doc:
            pms.append(dict(doc, name=who, books=books, path=_rel(path)))
    briefs = []
    for name, title, books in PM_BRIEFS:
        path = PORTFOLIO_BRIEFS_DIR / name
        try:
            text = path.read_text(encoding="utf-8").replace("\r\n", "\n")
        except OSError:
            continue
        # The file's own "# Title" line is the summary on the page, so it is dropped.
        text = re.sub(r"\A\s*#[ \t]+[^\n]*\n", "", text)
        briefs.append({"name": title, "books": books, "path": _rel(path),
                       "html": _doc_md_to_html(text.strip())})
    section = _prompts_section("## Agent 2: the PM", PM_PROMPTS_MD)
    if not pms and not section and not briefs:
        return None
    return {"pms": pms, "briefs": briefs, "prompt": _doc_md_to_html(section),
            "promptPath": _rel(PM_PROMPTS_MD), "promptSection": "Agent 2: the PM"}


# --- the board -------------------------------------------------------------------
#
# portfolios.html shows the books as a board: one column per book, one card per
# holding. It is drawn here from portfolio.engine.site_data, so every figure on it
# comes from the ledger (trades.csv, written only through portfolio/bin/trade.py)
# and the stored closes. Nothing on it is typed in by a PM, and a holding with no
# stored close for the date shows its value as n/a rather than an estimate.

# (group label, book ids), left to right.
BOARD_GROUPS = (("Style growth", ("lg-growth", "mid-growth", "sm-growth")),
                ("Style value", ("lg-value", "mid-value", "sm-value")),
                ("Hedge", ("hedge",)),
                ("Neural", ("neural",)))
_MINUS = "−"
_MID = "\u00b7"
_MONTHS3 = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def _bd_date(s):
    try:
        d = datetime.strptime(str(s), "%Y-%m-%d")
    except ValueError:
        return _html.escape(str(s or ""))
    return f"{d.day} {_MONTHS3[d.month - 1]} {d.year}"


def _bd_ok(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _bd_pct(f, dp=1, sign=True):
    if not _bd_ok(f):
        return "n/a"
    x = f * 100
    body = f"{abs(x):,.{dp}f}%"
    if not sign:
        return (_MINUS if x < 0 and round(abs(x), dp) else "") + body
    return (_MINUS if x < 0 and round(abs(x), dp) else "+") + body


def _bd_cls(f):
    if not _bd_ok(f) or abs(f) < 0.00005:
        return ""
    return " ld-up" if f > 0 else " ld-dn"


def _bd_usd(v, dp=0):
    if not _bd_ok(v):
        return "n/a"
    return (_MINUS if v < 0 and round(abs(v), dp) else "") + f"${abs(v):,.{dp}f}"


def _bd_shares(v):
    if not _bd_ok(v):
        return "n/a"
    v = abs(v)
    return f"{v:,.0f}" if abs(v - round(v)) < 1e-9 else f"{v:,.2f}"


def _bd_kv(label, value, cls=""):
    return (f'<div class="ld-kb-kv"><dt>{_html.escape(label)}</dt>'
            f'<dd class="ld-num{cls}">{value}</dd></div>')


def _board_card(h, asof, priced=True):
    e = _html.escape
    tk = str(h.get("ticker") or "")
    short = h.get("side") == "short"
    cls = "ld-kb-card" + (" ld-kb-short" if short else "") + ("" if priced else " ld-kb-nopx")
    href = "company.html#" + _urlquote(tk, safe="")
    avg = h.get("avgCost")
    cost = _bd_usd(h.get("costBasis")) + (
        f'<span class="ld-kb-sub">{"sold at" if short else "at"} {_bd_usd(avg, 2)}</span>'
        if _bd_ok(avg) else "")
    if priced:
        stats = (_bd_kv("Shares", _bd_shares(h.get("shares")))
                 + _bd_kv("Cost" if not short else "Proceeds", cost)
                 + _bd_kv("Value", _bd_usd(h.get("value")))
                 + _bd_kv("Gain or loss", _bd_pct(h.get("ret")), _bd_cls(h.get("ret")))
                 + _bd_kv("Day", _bd_pct(h.get("dayChg")), _bd_cls(h.get("dayChg"))))
        weight = _bd_pct(h.get("weight"), 1, sign=False)
    else:
        stats = (_bd_kv("Shares", _bd_shares(h.get("shares")))
                 + _bd_kv("Cost" if not short else "Proceeds", cost)
                 + _bd_kv("Value", "n/a") + _bd_kv("Gain or loss", "n/a")
                 + _bd_kv("Day", "n/a"))
        weight = "n/a"
    why = ""
    if not priced:
        why = (f'<p class="ld-kb-why">No stored close for {_bd_date(asof)}.</p>'
               if h.get("why") == "no stored close" else
               '<p class="ld-kb-why">Stored prices were rebased since it was bought.</p>')
    memo = h.get("memo") or {}
    memo_html = (f'<a class="ld-kb-memo" href="{href}/thesis" title="The analyst’s note of '
                 f'{_bd_date(memo.get("date"))}">Memo</a>') if memo else ""
    sector = h.get("sector") or "Sector not known"
    return (f'<article class="{cls}">'
            f'<div class="ld-kb-ct"><a class="ld-kb-tk" href="{href}">{e(tk)}</a>'
            + ('<span class="ld-kb-tag">Short</span>' if short else "")
            + f'<span class="ld-kb-w ld-num" title="Weight">{weight}</span></div>'
            f'<p class="ld-kb-nm">{e(h.get("name") or tk)}</p>'
            f'<dl class="ld-kb-stats">{stats}</dl>{why}'
            f'<div class="ld-kb-cf"><span class="ld-kb-sec">{e(sector)}</span>{memo_html}</div>'
            "</article>")


def _board_column(b, coverage):
    e = _html.escape
    bid = str(b.get("id") or "")
    pm_book = b.get("kind") != "style"
    bench = str(b.get("benchmarkName") or b.get("benchmark") or "")
    bench_short = re.sub(r"\s*\(.*\)$", "", bench)
    kick = {"hedge": "Long and short", "neural": "Free hand"}.get(
        b.get("kind"), f'{(b.get("size") or "").capitalize()} cap {_MID} '
                       f'{(b.get("style") or "").capitalize()}')
    started = bool(b.get("inception"))
    rows = []
    if started:
        nav = b.get("nav")
        rows.append(_bd_kv("Value", _bd_usd(nav) if _bd_ok(nav) else "n/a (partial)"))
        rows.append(_bd_kv(f'Return since {_bd_date(b.get("inception"))}',
                           _bd_pct(b.get("ret"), 2), _bd_cls(b.get("ret"))))
        rows.append(_bd_kv(bench_short, _bd_pct(b.get("benchRet"), 2), _bd_cls(b.get("benchRet"))))
        if b.get("cash_benchmark"):
            rows.append(_bd_kv("Cash (Treasury bills)", _bd_pct(b.get("cashRet"), 2),
                               _bd_cls(b.get("cashRet"))))
        rows.append(_bd_kv("Cash", _bd_pct(b.get("cashWeight"), 1, sign=False)))
        rows.append(_bd_kv("Holdings", f'{int(b.get("holdingsCount") or 0):,}'))
        if pm_book:
            rows.append(_bd_kv("Gross, net", f'{_bd_pct(b.get("gross"), 0, sign=False)}, '
                                             f'{_bd_pct(b.get("net"), 0, sign=False)}'))
        else:
            shadow = (b.get("scoring") or {}).get("shadow") or {}
            va, act = shadow.get("valueAdded"), shadow.get("activeShare")
            rows.append(_bd_kv("PM value added", _bd_pct(va, 2) if _bd_ok(va) else "n/a",
                               _bd_cls(va)))
            rows.append(_bd_kv("Active share vs rules",
                               _bd_pct(act, 1, sign=False) if _bd_ok(act) else "n/a"))
    else:
        rows.append(_bd_kv("Value", "Not started"))
    pmd = b.get("lastPmDecision")
    rows.append(_bd_kv("PM decision", _bd_date(pmd.get("date")) if pmd else "None yet"))
    partial = ""
    if started and b.get("partial"):
        miss = [u.get("ticker") for u in b.get("unpriced") or []]
        partial = (f'<p class="ld-kb-partial">Partial: no usable close on '
                   f'{_bd_date(b.get("asof"))} for {e(", ".join(t for t in miss if t))}, so the '
                   "value is left blank. Weights and cash are shares of what could be priced.</p>")
    head = (f'<a class="ld-kb-head" href="book.html#{_urlquote(bid, safe="")}">'
            f'<span class="ld-kicker">{e(kick)}</span>'
            f'<h3 class="ld-kb-name" id="kb-{e(bid)}">{e(b.get("name") or bid)}</h3>'
            f'<span class="ld-kb-bench">Against the {e(bench)}</span>'
            f'<dl class="ld-kb-stats">{"".join(rows)}</dl></a>{partial}')
    holdings = list(b.get("holdings") or [])
    unpriced = list(b.get("unpriced") or [])
    cards = []
    for side, label in (("long", ""), ("short", "Short positions")):
        mine = ([_board_card(h, b.get("asof")) for h in holdings if h.get("side") == side]
                + [_board_card(u, b.get("asof"), priced=False)
                   for u in unpriced if u.get("side") == side])
        if mine and label:
            cards.append(f'<p class="ld-kb-div">{label}</p>')
        cards.extend(mine)
    if not started:
        need = int(((b.get("mandate") or {}).get("holdings_range") or [25])[0])
        body = ('<div class="ld-kb-empty"><p><b>Waiting for three years of history.</b></p>'
                f'<p>A company joins this box once its annual reports give three years of '
                f'figures; {int(coverage.get("with_growth") or 0):,} of '
                f'{int(coverage.get("sized") or 0):,} companies have them so far. The book starts '
                f'when its box holds at least {need}.</p></div>')
    elif not cards:
        body = ('<div class="ld-kb-empty"><p><b>Holds cash.</b> The PM builds this book on its '
                "next run.</p></div>")
    else:
        body = "".join(cards)
    return (f'<section class="ld-kb-col{" ld-kb-wait" if not started else ""}" '
            f'aria-labelledby="kb-{e(bid)}">{head}<div class="ld-kb-cards">{body}</div></section>')


def _portfolio_board_html(data):
    """The Portfolios board as HTML, from portfolio.engine.site_data (the full
    books, holdings included). Every string from the data is escaped."""
    books = {b.get("id"): b for b in data.get("books") or []}
    coverage = data.get("coverage") or {}
    groups = []
    for label, ids in BOARD_GROUPS:
        cols = "".join(_board_column(books[i], coverage) for i in ids if i in books)
        if cols:
            groups.append(f'<div class="ld-kb-grp"><p class="ld-kb-gh">{_html.escape(label)}</p>'
                          f'<div class="ld-kb-cols">{cols}</div></div>')
    return ('<div class="ld-kb" role="region" aria-label="The books, one column each" '
            f'tabindex="0"><div class="ld-kb-track">{"".join(groups)}</div></div>')


def _portfolio_site_data():
    return PF.site_data(ledger_dir=PORTFOLIO_DIR / "ledger", books_dir=PORTFOLIO_DIR / "books",
                        panel_dir=FUNDAMENTALS_CSV_DIR, prices=PF.PriceStore(PRICES_DIR),
                        benchmarks=PF.BenchmarkStore(PRICES_DIR), nav_csv=PORTFOLIO_NAV_CSV,
                        events=THESES_DIR / "ledger" / "events.csv", history=STYLE_HISTORY_CSV)


def record_portfolio_nav():
    """The daily run's only portfolio step: value every incepted book at each
    session's stored closes and append what is new to data/portfolio/nav.csv.
    It never trades. Returns the number of rows appended."""
    trades = PF.read_rows(PORTFOLIO_DIR / "ledger" / "trades.csv")
    if not trades:
        print("portfolios: no book has been incepted; nothing to value.")
        return 0
    n = PF.record_nav(trades, PF.panel_dates(FUNDAMENTALS_CSV_DIR), PF.PriceStore(PRICES_DIR),
                      PF.BenchmarkStore(PRICES_DIR), PORTFOLIO_NAV_CSV)
    print(f"portfolios: recorded {n} NAV row(s).")
    return n


# --- scoring and attribution -----------------------------------------------------
#
# The analyst's calls are marked every session and scored at their horizon by
# theses/bin/score.py; the PM is scored against a rules-only shadow of each style
# book, and every book's return is attributed, by portfolio/bin/score_pm.py. Both
# are arithmetic over stored closes, run by the daily run after the NAV, and write
# append-only files under data/ (the only tree this run commits besides docs/ and
# state/): data/scoring/scores.csv and marks.csv, and data/portfolio/
# shadow_trades.csv, shadow_nav.csv, decision_marks.csv and attribution.csv.
SCORING_DIR = DATA_DIR / "scoring"
SCORING_SCORES_CSV = SCORING_DIR / "scores.csv"
SCORING_MARKS_CSV = SCORING_DIR / "marks.csv"


def _scoring_modules():
    """theses/bin/score.py and portfolio/bin/score_pm.py. theses/bin is appended to
    the path, never prepended: it holds a queue.py."""
    tb = str(REPO_ROOT / "theses" / "bin")
    if tb not in _sys.path:
        _sys.path.append(tb)
    import score as score_mod
    from portfolio.bin import score_pm as score_pm_mod
    return score_mod, score_pm_mod


def _scoring_ctx(score_pm_mod):
    return score_pm_mod.Ctx(ledger_dir=PORTFOLIO_DIR / "ledger", prices=PF.PriceStore(PRICES_DIR),
                            benchmarks=PF.BenchmarkStore(PRICES_DIR), panel_dir=FUNDAMENTALS_CSV_DIR,
                            history=STYLE_HISTORY_CSV, events=THESES_DIR / "ledger" / "events.csv",
                            books_dir=PORTFOLIO_DIR / "books")


def run_scoring(today=None):
    """The daily run's scoring step, after the NAV: marks and final scores for the
    analyst's calls, then the shadow books, per-decision marks and attribution.
    Each half is wrapped so a failure in one cannot cost the other."""
    today = today or datetime.now(EASTERN).date().isoformat()
    score_mod, score_pm_mod = _scoring_modules()
    try:
        score_mod.run_daily(PRICES_DIR, FUNDAMENTALS_CSV_DIR, SCORING_SCORES_CSV, SCORING_MARKS_CSV,
                            today=today, events=PF.read_rows(THESES_DIR / "ledger" / "events.csv"),
                            predictions=PF.read_rows(THESES_DIR / "ledger" / "predictions.csv"),
                            root=THESES_DIR.parent)
    except Exception as exc:
        print(f"scoring: analyst calls not scored ({type(exc).__name__}: {exc}).")
    try:
        score_pm_mod.run_daily(_scoring_ctx(score_pm_mod), today=today,
                               data_dir=PORTFOLIO_NAV_CSV.parent)
    except Exception as exc:
        print(f"scoring: PM scoring not recorded ({type(exc).__name__}: {exc}).")


def _scorecard_data():
    score_mod, _ = _scoring_modules()
    calls = score_mod.load_calls(PF.read_rows(THESES_DIR / "ledger" / "events.csv"),
                                 PF.read_rows(THESES_DIR / "ledger" / "predictions.csv"),
                                 root=THESES_DIR.parent)
    panel = score_mod.Panel(FUNDAMENTALS_CSV_DIR)
    sector_of = {c["call_id"]: score_mod.call_sector(c, panel) for c in calls}
    out = score_mod.scorecard_data(calls, PF.read_rows(SCORING_SCORES_CSV),
                                   PF.read_rows(SCORING_MARKS_CSV), sector_of, _NOTE_REPO_URL)
    # The PM against the analyst: every trade against the analyst's rating, marked
    # by portfolio/bin/score_pm.py into data/portfolio/disagreements.csv.
    _, score_pm_mod = _scoring_modules()
    rows = PF.read_rows(PORTFOLIO_NAV_CSV.parent / "disagreements.csv")
    out["pmVsAnalyst"] = {"summary": score_pm_mod.disagreement_summary(rows),
                          "rows": [{k: r.get(k) for k in score_pm_mod.DISAGREEMENT_COLUMNS
                                    if k != "computed_at"} for r in rows]}
    return out


def _book_scoring():
    """{book id: summary} for the book pages and the board, or {} on any failure."""
    try:
        _, score_pm_mod = _scoring_modules()
        return score_pm_mod.site_summary(PORTFOLIO_NAV_CSV.parent)
    except Exception as exc:
        print(f"portfolios: scoring summary unavailable ({type(exc).__name__}: {exc}).")
        return {}


def generate_scorecard(universe, version=None):
    """Write docs/scorecard.html: the analyst's calls, open and scored, and the
    aggregates and calibration, every group with its count."""
    if version is None:
        version = _write_ledger_assets()
    sc = _scorecard_data()
    html = render_ledger_page("scorecard", "Scorecard, Apterreon",
                              dict(_ledger_common(universe), scorecard=sc), version,
                              description="How the analyst's calls have done against their "
                                          "sector funds, open and scored.",
                              loading="Loading the scorecard")
    (DOCS_DIR / "scorecard.html").write_text(html, encoding="utf-8")
    print(f"scorecard: wrote scorecard.html ({len(sc['open'])} open, {len(sc['scored'])} scored).")
    return len(sc["open"]) + len(sc["scored"])


def generate_portfolios(universe, version=None):
    """Write docs/portfolios.html (the cards) and docs/book.html (one book, by hash)."""
    if version is None:
        version = _write_ledger_assets()
    data = _portfolio_site_data()
    scoring = _book_scoring()
    for b in data["books"]:
        b["scoring"] = scoring.get(b.get("id")) or {}
    drafts = [{"label": label, "path": f"portfolio/drafts/{name}"}
              for name, label in PORTFOLIO_DRAFTS if (PORTFOLIO_DIR / "drafts" / name).exists()]
    common = _ledger_common(universe)
    cards = dict(data, books=[{k: v for k, v in b.items() if k not in _PORTFOLIO_DETAIL_KEYS}
                              for b in data["books"]])
    html = render_ledger_page("portfolios", "Portfolios, Apterreon",
                              dict(common, portfolios=cards, drafts=drafts,
                                   board=_portfolio_board_html(data),
                                   howPms=_pm_instructions()), version,
                              description="Eight paper model portfolios: six by size and "
                                          "style, a hedge fund strategy and a free hand.",
                              loading="Loading the portfolios")
    (DOCS_DIR / "portfolios.html").write_text(html, encoding="utf-8")
    html = render_ledger_page("book", "Model portfolio, Apterreon",
                              dict(common, portfolios=data), version,
                              description="One model portfolio: holdings, trades, decisions and "
                                          "the rules book to compare it with.",
                              loading="Loading the portfolio")
    (DOCS_DIR / "book.html").write_text(html, encoding="utf-8")
    live = sum(1 for b in data["books"] if b.get("inception"))
    print(f"portfolios: wrote portfolios.html and book.html ({live} incepted of "
          f"{len(data['books'])} books, panel {data['asof'] or 'none'}).")
    return live


def generate_research(universe, version=None):
    """Write docs/research.html: every thesis in one table, each row leading to
    its company page where the full note is, the record, and a link to the
    model portfolios (portfolios.html)."""
    if version is None:
        version = _write_ledger_assets()
    research, record = _ledger_research(universe)
    cfg = dict(_ledger_common(universe), nonop=sorted(sectype.NON_OPERATING),
               research=research, record=record, howAnalyst=_analyst_instructions(),
               howDirector=_director_instructions(), directorPlan=_director_plan())
    html = render_ledger_page("research", "Research, Apterreon", cfg, version,
                              description="Written views on single companies, each with a target price, a review date and what would prove it wrong.",
                              loading="Loading the theses")
    (DOCS_DIR / "research.html").write_text(html, encoding="utf-8")
    print(f"research: wrote research.html ({len(research)} names, "
          f"{record['open']} open, {record['graded']} graded).")
    return len(research)


def generate_site(briefs, universe=None):
    """Orchestrator. Generates the full multi-page static site under docs/.

    `universe` lets a caller supply one it already has. A publish run passes the
    committed cache so that rebuilding pages cannot trigger a 1,500-name refresh."""
    if universe is None:
        universe = get_or_generate_stocks_universe()

    version = _write_ledger_assets()
    generate_home(briefs, universe, version)
    generate_today(briefs, universe, version)
    generate_stories(briefs, universe, version)
    generate_stocks_page(universe, version)
    write_manifest()
    # Derived from theses/, which the scheduled analyst writes and which is the
    # source of truth. Regenerated every run so the published view cannot drift
    # from the notes.
    #
    # Each writer is wrapped: one odd note must not fail the run, because in
    # daily mode that would skip the data commit and lose the panel row.
    n_thesis = n_company = n_views = 0
    try:
        n_thesis = write_thesis_views()
    except Exception as exc:
        print(f"thesis: view writing failed ({type(exc).__name__}: {exc}); "
              f"the rest of the site is unaffected.")
    # Same shape, different author: these come from the filings and companyfacts
    # passes rather than from the analyst. The scored stock dicts carry
    # sub_industry and ttm_revenue, which is what peer share is computed from.
    try:
        n_company = write_company_views(universe.get("stocks") or [])
    except Exception as exc:
        print(f"company: view writing failed ({type(exc).__name__}: {exc}); "
              f"the rest of the site is unaffected.")
    # From the panel alone, so daily and publish runs write the same files.
    n_history = 0
    try:
        n_history = write_history_views(universe.get("stocks") or [])
    except Exception as exc:
        print(f"history: view writing failed ({type(exc).__name__}: {exc}); "
              f"the rest of the site is unaffected.")
    # After both writers: the page lists which tickers have their files.
    try:
        generate_company_page(universe, version)
    except Exception as exc:
        print(f"company: page generation failed ({type(exc).__name__}: {exc}); "
              f"the rest of the site is unaffected.")
    try:
        n_views = generate_research(universe, version)
    except Exception as exc:
        print(f"research: page generation failed ({type(exc).__name__}: {exc}); "
              f"the rest of the site is unaffected.")
    try:
        generate_portfolios(universe, version)
    except Exception as exc:
        print(f"portfolios: page generation failed ({type(exc).__name__}: {exc}); "
              f"the rest of the site is unaffected.")
    try:
        generate_scorecard(universe, version)
    except Exception as exc:
        print(f"scorecard: page generation failed ({type(exc).__name__}: {exc}); "
              f"the rest of the site is unaffected.")
    print("Wrote docs/index.html, today.html, stories.html, stocks.html, company.html, "
          f"research.html ({n_views} names), portfolios.html, book.html, scorecard.html, "
          f"manifest.json, assets/"
          + (f", {n_thesis} thesis views" if n_thesis else "")
          + (f", {n_company} company views" if n_company else "")
          + (f", {n_history} history views" if n_history else "") + ".")


def s3_publish_brief(brief_type, now_et, interactive_html, data=None, quotes=None, timestamp=None):
    """Write brief HTML + JSON sidecar, clean old ones, regenerate the multi-page site."""
    date_iso = now_et.strftime("%Y-%m-%d")
    s3_write_brief(brief_type, date_iso, interactive_html, data=data, quotes=quotes, timestamp=timestamp)
    s3_cleanup_old_briefs()
    briefs = s3_list_briefs()
    generate_site(briefs)


def build_sections_from_headlines(headlines):
    """Group headlines into the site's section structure, without an LLM.

    Returns the same shape the renderers already consume. summary, insight,
    the_edge and tomorrow_watch stay empty: every template guards on truthiness
    (`if edge_text:`, `if (edgeText)`), so those blocks are simply omitted rather
    than rendering as empty panels."""
    sections = []
    for section_name, categories in SECTIONS:
        stories = [{
            "headline": h.get("title", ""),
            "summary": "",
            "insight": "",
            "source": h.get("source", ""),
            "link": h.get("link", ""),
        } for h in headlines if h.get("category") in categories]
        if stories:
            sections.append({"name": section_name, "stories": stories})
    return {"sections": sections, "the_edge": "", "tomorrow_watch": ""}


# ── Lambda Handler ──────────────────────────────────────────────────────────

def lambda_handler(event, context):
    # Handle pin toggle requests (from API Gateway or Function URL)
    if event.get("action") == "pin":
        key = event.get("key", "")
        if key:
            new_state = s3_toggle_pin(key)
            briefs = s3_list_briefs()
            generate_site(briefs)
            return {"pinned": new_state, "key": key}
        return {"error": "No key provided"}

    # Modes:
    #   record   cheap, runs hourly. Quotes + headlines appended to the CSVs.
    #   daily    record, plus the fundamentals panel, the snapshot page and the
    #            site rebuild. Runs once a day.
    #   publish  rebuild the site from what is already committed. Fetches
    #            nothing, records nothing.
    # The split exists because docs/index.html is ~325KB and is rewritten in full
    # on every site rebuild; doing that hourly would bloat the repo for nothing,
    # while appending a few CSV rows hourly costs almost nothing.
    mode = event.get("mode") or event.get("brief_type") or "daily"
    if mode in ("morning", "midday", "evening"):
        mode = "daily"          # legacy edition names still dispatch a daily run
    if mode not in ("record", "daily", "publish"):
        return {"status": "error", "error": f"unknown mode {mode!r}"}

    now_et = datetime.now(EASTERN)
    observed_at = now_et.isoformat(timespec="seconds")
    date_iso = now_et.strftime("%Y-%m-%d")
    date_str = now_et.strftime("%A, %B %d")
    timestamp = now_et.strftime("%I:%M %p ET")

    # A publish run exists for one case: the analyst pushed a note and the page
    # should show it. Rebuilding is the whole job, so it fetches nothing and
    # records nothing.
    #
    # It must not be a daily run. A daily run appends the fundamentals panel row,
    # and a push arriving at 11:00 would stamp mid-session prices with today's
    # date. The panel is append-only, so that row would be permanent and every
    # return, volatility and drawdown computed over it would be wrong. Rebuilding
    # from committed data has no such hazard: docs/ is regenerated wholesale on
    # every run anyway and is force-pushed to gh-pages, so it is never a source
    # of truth for anything.
    if mode == "publish":
        # Read the cached universe rather than calling
        # get_or_generate_stocks_universe, which regenerates whenever the cache is
        # not from today (Eastern). On a push that means re-scraping the lists and
        # a 2,100-second yfinance pass to rebuild pages from numbers already on disk.
        #
        # The cache is not in git. The workflow restores it from the
        # universe-cache release asset before this runs, and fails the run itself
        # if the asset exists but cannot be fetched.
        cache_path = STATE_DIR / "stocks_universe.json"
        universe = None
        if cache_path.exists():
            try:
                universe = json.loads(cache_path.read_text(encoding="utf-8"))
            except Exception as exc:
                print(f"publish: universe cache unreadable ({exc}).")
        if not (universe or {}).get("stocks"):
            # Refuse rather than regenerate. Regenerating was the old fallback, and
            # in a push-triggered run it meant a cold rebuild of 40 minutes or more
            # that rewrites data/tickers.csv (which the Commit data step then
            # commits) and publishes a thin universe to the site. Failing leaves
            # the published site as it was and fires the alert.
            print("publish: no usable universe cache; refusing to rebuild the site without it.")
            return {"status": "failed", "mode": mode, "stocks": 0, "ok": False}
        # The cache may predate the label; the site must never publish without it.
        _relabel_cached_universe(universe["stocks"])
        generate_site(s3_list_briefs(), universe=universe)
        n_stocks = len((universe or {}).get("stocks") or [])
        print(f"publish: rebuilt the site from committed data ({n_stocks} tickers).")
        return {"status": "published", "mode": mode, "stocks": n_stocks,
                "ok": bool(n_stocks)}

    # A record run promotes itself when the day's panel row is still missing and
    # the session is over. The cron that was supposed to guarantee this is not
    # reliable: GitHub dropped the 22:23 slot on four consecutive days while
    # every hourly run succeeded, so the pipeline looked healthy and recorded
    # nothing. Whichever run happens to be the first one after the close does
    # the work now, and the rest see the row and stay cheap. The same mechanism
    # is the retry for a deferred panel (_panel_gate): a deferral writes no row,
    # so the next hourly run is promoted and tries again.
    if mode == "record" and now_et.weekday() < 5 and now_et.hour >= 17:
        if not panel_has_date(date_iso):
            print(f"mode: record run promoted to daily. {date_iso} has no panel row "
                  f"and the session is over ({timestamp}). The daily cron is not "
                  f"dependable enough to be the only thing that triggers this.")
            mode = "daily"

    # 1. Market data
    print("Fetching market data...")
    quotes = fetch_market_data()

    # 2. Headlines
    print(f"Fetching RSS headlines ({mode} run)...")
    headlines = fetch_rss_headlines(max_per_feed=MAX_PER_FEED, brief_type="morning")

    # 3. Record both to the append-only CSVs. This is the durable output; every
    #    later step only rebuilds views over data already committed here.
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    n_quotes = record_quotes(quotes, observed_at)
    n_heads = record_headlines(headlines, observed_at)

    if mode == "record":
        # A record run exists to append quotes and headlines. Seeing neither
        # means every source was down, or the append silently failed, and either
        # way the run did not do its job.
        return {"status": "recorded", "mode": mode, "quotes": n_quotes,
                "headlines_new": n_heads, "headlines_seen": len(headlines),
                "ok": bool(n_quotes or n_heads or headlines)}

    # ── daily only ──────────────────────────────────────────────────────────
    if not headlines:
        print("No headlines fetched; publishing the page anyway from market data.")

    # 4. Group headlines into the section shape the renderers expect. This used
    #    to be a Claude call; the project is pure aggregation now, so summary and
    #    insight stay empty and the templates omit those blocks.
    data = build_sections_from_headlines(headlines)

    # 5. Fundamentals panel: one row per ticker per day.
    #
    # Proof that today's session traded, from quotes already fetched above: an
    # Alpha Vantage quote carries the session its close belongs to. Yields are
    # excluded because they have no trading_day. No new source and no new key;
    # the project has no market calendar, and this is the cheapest stand-in for
    # one. False on a holiday, and also when Alpha Vantage is down, in which case
    # the price pass looks for the same proof in the series themselves.
    session_confirmed = any(q.get("trading_day") == date_iso
                            for q in quotes if not q.get("is_yield"))
    universe = get_or_generate_stocks_universe(session_confirmed=session_confirmed)
    stocks = (universe or {}).get("stocks") or []

    # Only record a panel row on days the market actually traded. The daily run
    # fires after the close, so a Saturday run would stamp Friday's closing prices
    # with Saturday's date, and Sunday would do it again: three identical rows for
    # one trading day, which silently corrupts any return, volatility or drawdown
    # computed over the panel.
    #
    # Weekends here. Exchange holidays are caught further down by _panel_gate
    # without a market calendar: no Alpha Vantage quote carries today's trading
    # day and no stored series has today's bar. Before that gate, holiday rows
    # repeated the prior close and were identifiable only via last_updated.
    panel_rows = 0
    panel_deferred = False
    panel_expected = now_et.weekday() < 5
    if now_et.weekday() >= 5:
        print(f"fundamentals: {date_iso} is a weekend, no trading day to record.")
    elif (universe or {}).get("stale"):
        # The universe fell back to cache, either because every source returned
        # nothing or because the partial-scrape ABORT fired. Its prices are the
        # prior session's. Writing them under today's date would append a
        # permanent flat day that record_fundamentals can never correct. Today
        # having no panel row is recoverable; today having a wrong one is not.
        # Deliberate, but not healthy: the universe scrape failed. Leave
        # panel_expected true so the run reports itself as having fallen short.
        print(f"fundamentals: universe is a cached fallback, not a live scrape for "
              f"{date_iso}; skipping the panel rather than recording stale prices "
              f"under today's date.")
    else:
        # A close from an older session must never be stamped with today's
        # date. On 2026-09-21 the promoted run priced at 20:34 ET, Yahoo had not
        # yet published the day's bar for 4,894 of 5,489 series, and the panel
        # recorded the Friday close under Monday for 4,584 rows; 09-10, 09-11
        # and 09-14 had the same fault. See _panel_gate for the three outcomes.
        verdict, lagging, dated = _panel_gate(stocks, date_iso, session_confirmed, now_et)
        if verdict == "holiday":
            # Deliberate and correct, so not a shortfall either.
            panel_expected = False
            print(f"fundamentals: no series has a {date_iso} bar and no quote confirms "
                  f"the session ({lagging}/{dated} lag); treating {date_iso} as an "
                  f"exchange holiday and recording nothing.")
        elif verdict == "defer":
            # Reported as fine, not failed. The next hourly run retries on its
            # own, and a failure email every hour for a vendor running late is
            # an alert that cries wolf. What would be a real failure, the day
            # never resolving, cannot happen quietly: at 23:xx ET the gate
            # writes regardless.
            panel_deferred = True
            print(f"fundamentals: deferring {date_iso}. {lagging} of {dated} priced "
                  f"tickers end before today's session, over the "
                  f"{_PANEL_LAG_DEFER_SHARE:.0%} limit; the next hourly run is "
                  f"promoted and retries, and from {_PANEL_DEFER_UNTIL_HOUR}:00 ET "
                  f"the row is written with those tickers flagged price_stale.")
        else:
            if lagging:
                print(f"fundamentals: {lagging} of {dated} priced tickers end before "
                      f"{date_iso}; recording them with price fields withheld and "
                      f"price_stale=1.")
            panel_rows = record_fundamentals(_panel_rows_for_session(stocks, date_iso),
                                             date_iso)

    # 5b. Earnings press releases (8-K item 2.02, exhibit EX-99.1).
    #
    # Deliberately after the panel and wrapped, because this is the one part of
    # the run that depends on a third party's index being up. The panel row is
    # the thing that cannot be backfilled; a missed filing is picked up by the
    # next run's four-day window. So nothing in here is allowed to cost the day
    # its row, or the site its rebuild.
    try:
        cik_to_ticker, _ticker_to_cik = {}, {}
        for _tkr, _cik in (fetch_edgar_ticker_cik_map() or {}).items():
            try:
                cik_to_ticker.setdefault(int(_cik), _tkr)
                # Both directions are kept. Two share classes share one CIK, so
                # inverting cik_to_ticker would lose GOOG behind GOOGL.
                _ticker_to_cik[_tkr] = int(_cik)
            except (TypeError, ValueError):
                continue
        _entries, _reported = collect_earnings_filings(cik_to_ticker,
                                                       days_back=FILINGS_DAYS_BACK)
        if _entries:
            record_filings(_entries, observed_at)
        # Reported history, for the companies that just reported. companyfacts is
        # ~4MB each, so fetching the universe would move 20GB. Tied to the earnings
        # event it costs one request per company per quarter and arrives exactly
        # when there is a new period to add.
        _fin = []
        for _t, _c in _reported.items():
            _fin.extend(fetch_financial_history(_c, _t))
        # 5c. The reading pack: the names the analyst is about to be handed, topped
        #     up with the documents the earnings path only collects going forward.
        #     Inside the same guard and after it, so a failure here costs the pack
        #     and nothing else.
        _pack_names = reading_pack_tickers(stocks)
        _pack = collect_reading_packs(_pack_names, _ticker_to_cik)
        if _pack:
            record_filings(_pack, observed_at)
        # Reported history rides on the earnings event too, so a pack name that has
        # not reported since collection began has no decade to read. Once it has
        # one, the earnings path adds each new period as it is reported.
        _have_fin = financials_tickers_held()
        for _t in _pack_names:
            if _t not in _reported and _t not in _have_fin and _ticker_to_cik.get(_t):
                _fin.extend(fetch_financial_history(_ticker_to_cik[_t], _t))
        if _fin:
            record_financials(_fin, observed_at)
    except Exception as exc:
        print(f"filings: collection failed ({type(exc).__name__}: {exc}); "
              f"the panel and the site are unaffected.")

    # 5d. Model portfolios: value each book at the stored closes. No trading; that
    #     is the PM's job. After the price refresh and the panel, before the site
    #     is rendered from the same record.
    try:
        record_portfolio_nav()
    except Exception as exc:
        print(f"portfolios: NAV not recorded ({type(exc).__name__}: {exc}); "
              f"the panel and the site are unaffected.")

    # 5e. Scoring: the analyst's calls marked and, at their horizon, scored; the PM
    #     against the rules-only shadow books; each book's return attributed. After
    #     the NAV, before the site, which publishes all of it on this run.
    try:
        run_scoring(date_iso)
    except Exception as exc:
        print(f"scoring: skipped ({type(exc).__name__}: {exc}); "
              f"the panel and the site are unaffected.")

    # 6. Publish the snapshot page and rebuild the site.
    title = f"Daily Brief · {date_str}"
    interactive_html = build_interactive_html(title, data, quotes, timestamp)
    s3_publish_brief("daily", now_et, interactive_html, data=data, quotes=quotes, timestamp=timestamp)

    # A daily run exists to add a row to the panel and refresh the universe.
    # It is allowed to add no row on a weekend, on a holiday, or when the panel
    # already has today; those set panel_rows to 0 deliberately and are reported
    # as fine. What is not fine is a weekday run that tried and got nothing.
    priced = sum(1 for s in stocks if s.get("price") is not None)
    # Writing no row is correct when the row is already there. The panel is
    # append-only and refuses a duplicate date, so record_fundamentals returns 0
    # both when it failed and when it deliberately declined, and the verdict
    # cannot tell those apart from the count alone. Asking the panel itself can:
    # a second daily run on a recorded day, a manual dispatch, or a retry are
    # all no-ops by design. Failing them would send a failure email for correct
    # behaviour, and an alert that cries wolf is how a channel gets ignored.
    panel_satisfied = panel_rows > 0 or panel_has_date(date_iso)
    # A deferral is a scheduled retry, not a failure (see the panel gate above).
    healthy = bool(stocks) and (panel_satisfied or panel_deferred or not panel_expected)
    return {"status": "published", "mode": mode, "stories": len(headlines),
            "quotes": len(quotes), "stocks": len(stocks),
            "panel_rows": panel_rows, "priced": priced,
            "panel_expected": panel_expected, "panel_satisfied": panel_satisfied,
            "panel_deferred": panel_deferred, "session_confirmed": session_confirmed,
            "ok": healthy}


if __name__ == "__main__":
    import sys
    # "record" for the hourly run, "daily" for the full one, "publish" to rebuild
    # the site without fetching or recording anything. The old edition names
    # (morning/midday/evening) still work and map to a daily run.
    mode = sys.argv[1] if len(sys.argv) > 1 else "daily"
    result = lambda_handler({"mode": mode}, None)
    print(json.dumps(result, indent=2))
    # The exit code is the only thing CI reads. Every silent outage this project
    # has had was a run that failed at its purpose and exited 0 anyway, so the
    # verdict the handler just computed decides the code.
    if result.get("status") == "error" or result.get("ok") is False:
        print(f"FAILED: the run completed but did not accomplish its purpose "
              f"({result.get('status')}). Exiting non-zero so this is visible.")
        sys.exit(1)
