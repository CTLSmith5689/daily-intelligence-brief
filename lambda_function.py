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
# Reported history, long format, one row per ticker per fiscal period. The panel
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
        if s.get("price") is None and s.get("market_cap") is None:
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


TICKERS_CSV = DATA_DIR / "tickers.csv"
TICKER_COLUMNS = ["ticker", "name", "sector", "sub_industry", "index",
                  "first_seen", "last_seen_in_index", "status", "dropped_on"]

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
    so the git diff shows exactly which names entered or left."""
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


# ── Multi-page site shell: shared CSS, JS, and render helpers ──────────────

SITE_CSS = """
*,*::before,*::after { box-sizing:border-box; margin:0; padding:0; }
:root {
  --bg-base:#0A0A0F; --bg-1:#11121A; --bg-2:#16171F;
  --border:rgba(255,255,255,0.06); --border-bright:rgba(255,255,255,0.12);
  --apt-red:#FF1F3D; --apt-red-deep:#CC0028; --apt-rose:#FF7A85; --apt-amber:#FFB347;
  --text-1:#FFFFFF; --text-2:#E2E5EC; --text-3:#9CA3AF; --text-4:#6B7280; --text-5:#3F4654;
  /* Theme-tunable surface tints (used by topnav, cards, table). Dark default.
     These were written as --surface-1:var(--surface-1), which is a self-reference.
     CSS treats that as invalid at computed-value time, so in dark mode all three
     resolved to nothing and every background:var(--surface-N) painted no fill at
     all. Verified in the browser: light returns rgba(245,241,232,0.80), dark
     returned empty. Eleven rules were affected, including .stk-row:hover, the
     chart plot grounds, .lib-controls and .edition-edge. Defined here against
     --bg-1 and --bg-2 with the same alphas the light theme uses. */
  --surface-1:rgba(17,18,26,0.80); --surface-2:rgba(17,18,26,0.94); --surface-3:rgba(22,23,31,0.96);
  --grid-line:rgba(255,255,255,0.06); --chart-axis:rgba(255,255,255,0.45); --chart-value:rgba(255,255,255,0.85);
  --plexus-opacity:0.55;
  --bg-glow-1:rgba(255,31,61,0.10); --bg-glow-2:rgba(204,0,40,0.07); --bg-glow-3:rgba(255,122,133,0.04);
}
:root[data-theme="light"] {
  --bg-base:#EDE8DC; --bg-1:#F5F1E8; --bg-2:#E4DED0;
  --border:rgba(23,20,15,0.14); --border-bright:rgba(23,20,15,0.30);
  /* Brand reds stay; soften apt-rose for light backgrounds */
  --apt-red:#FF4A1C; --apt-red-deep:#C4350F; --apt-rose:#E2725B; --apt-amber:#C77A18;
  --text-1:#17140F; --text-2:#3A342B; --text-3:#6B6152; --text-4:#A2988A; --text-5:#B8AF9E;
  --surface-1:rgba(245,241,232,0.80); --surface-2:rgba(245,241,232,0.94); --surface-3:rgba(237,232,220,0.96);
  --grid-line:rgba(20,18,14,0.08); --chart-axis:rgba(20,18,14,0.55); --chart-value:rgba(20,18,14,0.85);
  --plexus-opacity:0.18;
  --bg-glow-1:rgba(214,23,46,0.05); --bg-glow-2:rgba(161,9,33,0.04); --bg-glow-3:rgba(200,68,83,0.03);
}
/* Light-mode element-level fixups for spots that still use raw rgba() and would
   otherwise look washed out on a light background. */
:root[data-theme="light"] .stk-views-input,
:root[data-theme="light"] .stk-filter-input,
:root[data-theme="light"] .stk-filter-select,
:root[data-theme="light"] .stk-views-chip { background:#FFFFFF; }
:root[data-theme="light"] .stk-row:hover { background:rgba(20,18,14,0.05); }
:root[data-theme="light"] .stk-th:hover { color:var(--text-1); }
:root[data-theme="light"] .stk-views-del:hover { background:rgba(214,23,46,0.08); }
:root[data-theme="light"] .nws-sent-row, :root[data-theme="light"] .nws-item:hover { background:rgba(20,18,14,0.04); }
:root[data-theme="light"] .empty-state { color:var(--text-3); }
html { background:var(--bg-base); color:var(--text-1); font-family:'Inter',-apple-system,BlinkMacSystemFont,sans-serif; font-size:15px; -webkit-font-smoothing:antialiased; scroll-behavior:smooth; }
body { min-height:100vh; overflow-x:hidden; }
::-webkit-scrollbar { width:6px; }
::-webkit-scrollbar-thumb { background:var(--border-bright); border-radius:3px; }
a { color:inherit; text-decoration:none; }

body::before {
  content:''; position:fixed; inset:0; z-index:1; pointer-events:none;
  background:
    radial-gradient(800px 600px at 15% 20%, var(--bg-glow-1), transparent 60%),
    radial-gradient(900px 700px at 85% 80%, var(--bg-glow-2), transparent 60%),
    radial-gradient(1200px 800px at 50% 40%, var(--bg-glow-3), transparent 70%);
}
body::after {
  content:''; position:fixed; inset:0; z-index:2; pointer-events:none;
  background-image:radial-gradient(rgba(255,255,255,0.025) 1px, transparent 1px);
  background-size:3px 3px; opacity:0.5; mix-blend-mode:overlay;
}
:root[data-theme="light"] body::after {
  background-image:radial-gradient(rgba(20,18,14,0.04) 1px, transparent 1px);
  mix-blend-mode:multiply; opacity:0.4;
}
.topnav, .hero, .featured, .features, .feed, .lib, .footer, .destinations, .picks, .editions { position:relative; z-index:3; }

.topnav {
  position:sticky; top:16px; max-width:1200px; margin:16px auto 0; padding:10px 14px 10px 18px;
  display:flex; align-items:center; gap:14px;
  background:var(--surface-1);
  backdrop-filter:blur(24px) saturate(160%); -webkit-backdrop-filter:blur(24px) saturate(160%);
  border:1px solid var(--border); border-radius:18px;
}
.lockup { display:flex; align-items:center; gap:12px; }
.lockup-text { display:flex; flex-direction:column; line-height:1; }
.brand { font-family:'Space Grotesk',sans-serif; font-weight:800; font-size:14px; letter-spacing:4px; color:var(--text-1); text-transform:uppercase; }
.lockup-tagline { font-family:'Space Mono',monospace; font-size:9px; letter-spacing:2px; color:var(--apt-rose); text-transform:uppercase; margin-top:5px; }
.pulse-row { display:flex; align-items:center; gap:8px; margin-left:14px; padding-left:14px; border-left:1px solid var(--border); font-family:'Space Mono',monospace; font-size:10px; letter-spacing:2px; color:var(--text-3); text-transform:uppercase; }
.pulse-dot { width:6px; height:6px; border-radius:50%; background:#34D27A; box-shadow:0 0 12px rgba(52,210,122,0.7); animation:pulse 1.8s ease-out infinite; }
@keyframes pulse { 0%{box-shadow:0 0 0 0 rgba(52,210,122,0.55);} 70%{box-shadow:0 0 0 10px rgba(52,210,122,0);} 100%{box-shadow:0 0 0 0 rgba(52,210,122,0);} }

.nav { margin-left:auto; display:flex; gap:4px; align-items:center; }
.nav a { padding:8px 14px; font-size:13px; font-weight:500; color:var(--text-3); border-radius:10px; transition:all .2s; }
.nav a:hover { color:var(--text-1); background:rgba(255,255,255,0.04); }
.nav a.active { color:var(--text-1); background:rgba(255,31,61,0.10); }
.theme-toggle { margin-left:8px; padding:6px 10px; font-size:14px; line-height:1; color:var(--text-3); background:transparent; border:1px solid var(--border); border-radius:10px; cursor:pointer; transition:color .15s, border-color .15s, background .15s; }
.theme-toggle:hover { color:var(--text-1); border-color:var(--border-bright); background:rgba(255,255,255,0.04); }
:root[data-theme="light"] .theme-toggle:hover { background:rgba(20,18,14,0.05); }
.theme-toggle-icon { display:inline-block; }

.hero { max-width:1200px; margin:0 auto; padding:26px 0 20px;
  display:grid; grid-template-columns:minmax(0,1.25fr) minmax(0,1fr); gap:0 46px;
  align-items:end; border-bottom:1px solid var(--text-1); }
.hero-r { padding-bottom:4px; }
@media (max-width:900px) {
  .hero { grid-template-columns:1fr; gap:18px; padding:20px 0 16px; }
  .hero-r { padding-bottom:0; }
}
.eyebrow {
  display:inline-flex; align-items:center; gap:7px; padding:0; border-radius:0;
  background:none; border:none;
  font-family:'Space Mono',monospace; font-size:9px; letter-spacing:2.5px; color:var(--text-4);
  text-transform:uppercase; margin-bottom:10px;
}
.eyebrow .live-dot { width:6px; height:6px; border-radius:50%; background:#34D27A; }

/* The masthead voice: one serif line, black on cream, no gradient fill. The
   gradient was tuned for a dark ground and read as washed-out pink on this one. */
h1.hero-title {
  font-family:'Instrument Serif',Georgia,serif; font-weight:400; font-size:42px; line-height:1.05;
  letter-spacing:-0.3px; margin:0; max-width:20ch; color:var(--text-1);
}
h1.hero-title em { font-style:italic; color:var(--apt-red); }
@media (max-width:900px) { h1.hero-title { font-size:32px; max-width:none; } }
@keyframes fadeUp { to { opacity:1; transform:translateY(0); } }

.hero-sub { font-size:12.5px; line-height:1.65; color:var(--text-3); max-width:52ch;
  margin:0 0 14px; font-weight:400; }
.hero-actions { display:flex; gap:20px; flex-wrap:wrap; align-items:baseline; }
/* A ruled word, matching the screener's controls. The gradient pill with a
   glow belonged to the dark design and was the loudest thing on the page. */
.btn-primary {
  padding:0 0 3px; font-family:'Space Mono',monospace; font-size:11px; letter-spacing:2px;
  white-space:nowrap;
  text-transform:uppercase; background:none; color:var(--text-1);
  border:none; border-bottom:2px solid var(--apt-red); border-radius:0; cursor:pointer;
  display:inline-flex; align-items:center; gap:8px; text-decoration:none; box-shadow:none;
}
.btn-primary:hover { color:var(--apt-red); }
.btn-secondary {
  padding:0 0 3px; font-family:'Space Mono',monospace; font-size:11px; letter-spacing:2px;
  text-transform:uppercase; background:none; color:var(--text-3);
  border:none; border-bottom:1px solid var(--border-bright); border-radius:0;
  cursor:pointer; display:inline-flex; align-items:center; gap:7px; text-decoration:none;
}
.btn-secondary:hover { color:var(--text-1); border-bottom-color:var(--text-1); }

.featured { max-width:1200px; margin:0 auto; padding:32px 24px 64px; }
.featured-card {
  position:relative;
  background:var(--bg-1);
  border:1px solid var(--text-1); border-radius:0;
  padding:24px 26px; overflow:hidden;
}
/* The gradient border ring went with the dark design. A single hairline says
   the same thing here and does not fight the type. */
.feat-meta { display:flex; align-items:center; gap:10px; margin-bottom:18px; font-family:'Space Mono',monospace; font-size:11px; letter-spacing:2px; color:var(--text-3); text-transform:uppercase; flex-wrap:wrap; }
.feat-meta .tag { padding:0; border-radius:0; background:none; color:var(--apt-red); border:none; }
.feat-meta .dot { width:3px; height:3px; border-radius:50%; background:var(--text-4); }
.feat-kicker { font-family:'Space Mono',monospace; font-weight:400; font-size:10px;
  letter-spacing:3px; text-transform:uppercase; color:var(--text-4); margin-bottom:14px; }
.feat-body { font-size:15px; line-height:1.75; color:var(--text-2); max-width:74ch;
  margin-bottom:8px; font-weight:400; }
.feat-body::first-letter { font-family:'Instrument Serif',Georgia,serif; font-size:2.1em;
  font-weight:400; line-height:0.9; color:var(--text-1); float:left; padding:4px 8px 0 0; }
.feat-grid { display:grid; grid-template-columns:repeat(3, 1fr); gap:18px; margin-top:32px; }
.feat-stat { padding:0; background:none; border:none; border-top:1px solid var(--text-1);
  padding-top:12px; }

.fs-label { font-family:'Space Mono',monospace; font-size:10px; letter-spacing:2px; color:var(--text-4); text-transform:uppercase; margin-bottom:8px; }
.fs-val { font-family:'Instrument Serif',Georgia,serif; font-size:34px; font-weight:400;
  color:var(--text-1); letter-spacing:0; line-height:1; }
.fs-delta { font-family:'Space Mono',monospace; font-size:10px; letter-spacing:1px;
  color:var(--text-4); margin-top:6px; }
.feat-actions { margin-top:28px; display:flex; gap:14px; flex-wrap:wrap; align-items:center; }
.feat-actions .quiet { font-family:'Space Mono',monospace; font-size:11px; letter-spacing:2px; color:var(--text-3); text-transform:uppercase; border-bottom:1px solid var(--border); padding-bottom:2px; }

.themes-list { display:flex; flex-wrap:wrap; gap:8px; margin-top:20px; }
.theme-pill { padding:6px 12px; font-family:'Space Mono',monospace; font-size:10px; letter-spacing:1.5px; color:var(--apt-rose); text-transform:uppercase; background:rgba(255,31,61,0.06); border:1px solid rgba(255,31,61,0.20); border-radius:999px; }

.snapshot-list { list-style:none; padding:0; margin:0 0 4px 0; max-width:920px; }
.snapshot-list li { position:relative; padding:14px 0 14px 28px; border-top:1px solid var(--border); font-size:17px; line-height:1.55; color:var(--text-1); font-weight:400; letter-spacing:-0.005em; }
.snapshot-list li:first-child { border-top:none; padding-top:6px; }
.snapshot-list li::before { content:''; position:absolute; left:6px; top:24px; width:8px; height:8px; border-radius:50%; background:var(--apt-rose); box-shadow:0 0 12px rgba(255,31,61,0.35); }
.snapshot-list li:first-child::before { top:16px; }

.destinations { max-width:1200px; margin:0 auto; padding:24px 24px 64px; }
.destinations-h { display:flex; justify-content:space-between; align-items:end; margin-bottom:32px; flex-wrap:wrap; gap:18px; }
.destinations-h h2 { font-family:'Space Grotesk',sans-serif; font-weight:700; font-size:36px; letter-spacing:-0.02em; line-height:1.1; }
.destinations-h p { font-size:15px; color:var(--text-3); line-height:1.6; max-width:380px; }
.destinations-grid { display:grid; grid-template-columns:repeat(3, 1fr); gap:18px; }
@media (max-width:780px) { .destinations-grid { grid-template-columns:1fr; } }
.dest-card {
  display:block; padding:24px; background:var(--bg-1);
  border:1px solid var(--border); border-radius:0;
  transition:border-color .18s;
}
.dest-card:hover { border-color:var(--text-1); }
.dest-eyebrow { font-family:'Space Mono',monospace; font-size:9px; letter-spacing:2.5px;
  color:var(--text-4); text-transform:uppercase; margin-bottom:12px; }
.dest-title { font-family:'Instrument Serif',Georgia,serif; font-size:26px; font-weight:400;
  letter-spacing:-0.2px; color:var(--text-1); margin-bottom:9px; }
.dest-body { font-size:14px; line-height:1.55; color:var(--text-3); margin-bottom:18px; }
.dest-cta { font-family:'Space Mono',monospace; font-size:10px; letter-spacing:2px;
  color:var(--text-3); text-transform:uppercase; border-bottom:1px solid var(--apt-red);
  padding-bottom:2px; }

.features { max-width:1200px; margin:0 auto; padding:0 0 56px; }
.features-h { display:flex; justify-content:space-between; align-items:baseline; gap:26px;
  flex-wrap:wrap; margin:0 0 20px; padding:26px 0 12px; border-bottom:1px solid var(--text-1); }
.features-h h2 { font-family:'Instrument Serif',Georgia,serif; font-weight:400; font-size:30px;
  letter-spacing:-0.2px; line-height:1.08; max-width:18ch; color:var(--text-1); }
.features-h p { font-size:13px; color:var(--text-3); line-height:1.7; max-width:44ch; margin:0; }

.section-grid { display:grid; grid-template-columns:repeat(2, 1fr); gap:18px; }
@media (max-width:780px) { .section-grid { grid-template-columns:1fr; } }

/* A card here is a column of a newspaper, not a panel floating over a photo.
   The old rule hardcoded rgba(17,18,26,0.65) as its ground, which is why these
   stayed charcoal no matter which theme was chosen. */
.sec-card {
  position:relative; background:var(--bg-1);
  border:1px solid var(--border); padding:22px 24px 20px;
  transition:border-color .18s;
}
.sec-card:hover { border-color:var(--text-1); }
.sc-head { display:flex; align-items:baseline; gap:12px; margin-bottom:14px;
  padding-bottom:11px; border-bottom:1px solid var(--text-1); }
/* A numeral, not a badge. The section number reads as a folio mark. */
.sc-num {
  flex-shrink:0; font-family:'Instrument Serif',Georgia,serif; font-size:26px;
  line-height:1; color:var(--apt-red); min-width:26px;
}
.sc-titles { flex:1; min-width:0; }
.sc-eyebrow { font-family:'Space Mono',monospace; font-size:10px; letter-spacing:2px; color:var(--text-4); text-transform:uppercase; margin-bottom:4px; }
.sc-title { font-family:'Instrument Serif',Georgia,serif; font-size:24px; font-weight:400;
  letter-spacing:-0.2px; line-height:1.12; color:var(--text-1); }
.sc-count { font-family:'Space Mono',monospace; font-size:9px; letter-spacing:1.5px;
  color:var(--text-4); padding:0; background:none; }
.sc-list { display:flex; flex-direction:column; gap:0; }
.sc-item { padding:14px 0; border-top:1px solid var(--border); display:grid; grid-template-columns:1fr auto; gap:12px; align-items:start; transition:padding-left .15s; }
.sc-item:first-child { border-top:none; padding-top:4px; }
.sc-item:hover { padding-left:6px; }
.sc-item-headline { font-size:14px; font-weight:400; color:var(--text-1); line-height:1.5; }
.sc-item-source { font-family:'Space Mono',monospace; font-size:10px; letter-spacing:1.5px; color:var(--text-4); text-transform:uppercase; margin-top:6px; }
.sc-arrow { color:var(--text-4); font-size:18px; transition:color .15s, transform .15s; align-self:start; padding-top:2px; }
.sc-item:hover .sc-arrow { color:var(--apt-red); transform:translateX(3px); }
.sc-arrow { font-size:14px; }

.editions { max-width:1200px; margin:0 auto; padding:0 0 56px; }
.editions-h { display:flex; justify-content:space-between; align-items:baseline; gap:26px;
  flex-wrap:wrap; margin:0 0 22px; padding:26px 0 12px; border-bottom:1px solid var(--text-1); }
.editions-h h2 { font-family:'Instrument Serif',Georgia,serif; font-weight:400; font-size:32px;
  letter-spacing:-0.2px; line-height:1.08; color:var(--text-1); max-width:22ch; }
.editions-h p { font-size:13px; color:var(--text-3); line-height:1.7; max-width:44ch; margin:0; }
.edition-block { margin-bottom:48px; }
.edition-head { display:flex; align-items:baseline; gap:12px; margin-bottom:16px;
  padding-bottom:10px; border-bottom:1px solid var(--text-1); flex-wrap:wrap; }
.edition-name { font-family:'Instrument Serif',Georgia,serif; font-size:26px; font-weight:400;
  letter-spacing:-0.2px; color:var(--text-1); }
.edition-time { font-family:'Space Mono',monospace; font-size:11px; letter-spacing:2px; color:var(--text-4); text-transform:uppercase; }
.edition-link { margin-left:auto; font-family:'Space Mono',monospace; font-size:10px;
  letter-spacing:2px; color:var(--text-3); text-transform:uppercase;
  border-bottom:1px solid var(--apt-red); padding-bottom:2px; }
.edition-link:hover { color:var(--apt-red); }
.edition-edge { font-size:16px; line-height:1.65; color:var(--text-2); margin-bottom:24px; padding:18px 22px; background:var(--surface-1); border-left:3px solid var(--apt-red); border-radius:8px; }
.edition-empty { padding:30px; text-align:center; font-family:'Space Mono',monospace;
  font-size:11px; letter-spacing:2px; color:var(--text-4); text-transform:uppercase;
  background:none; border:1px dashed var(--border); border-radius:0; }

.lib { max-width:1200px; margin:0 auto; padding:0 0 56px; }
.lib.lib-wide { padding-top:36px; padding-bottom:24px; max-width:1480px; }
.lib.lib-wide .lib-h { margin-bottom:10px; }
.lib.lib-wide .lib-h h2 { font-size:32px; }
.lib.lib-wide { max-width:min(1640px, 96vw); }
.lib-h { display:flex; justify-content:space-between; align-items:baseline; gap:14px;
  flex-wrap:wrap; margin:0 0 16px; padding:26px 0 12px; border-bottom:1px solid var(--text-1); }
.lib-h h2 { font-family:'Instrument Serif',Georgia,serif; font-weight:400; font-size:32px;
  letter-spacing:-0.2px; line-height:1.08; color:var(--text-1); }
.lib-h .lib-count { font-family:'Space Mono',monospace; font-size:10px; letter-spacing:2px;
  color:var(--text-4); text-transform:uppercase; padding:0; border:none; }

.lib-controls { display:flex; flex-direction:column; gap:14px; margin-bottom:24px; padding:20px; background:var(--surface-1); backdrop-filter:blur(20px); -webkit-backdrop-filter:blur(20px); border:1px solid var(--border); border-radius:16px; }
.lib-search { display:flex; align-items:center; gap:10px; padding:0 0 5px; background:none;
  border:none; border-bottom:1px solid var(--text-1); max-width:420px; }
.lib-search:focus-within { border-bottom-color:var(--apt-red); }
.lib-search .icon { color:var(--text-4); font-size:12px; }
.lib-search input { flex:1; min-width:0; background:transparent; border:none; outline:none;
  font-family:'Space Mono',monospace; font-size:12px; color:var(--text-1); }
.lib-search input::placeholder { color:var(--text-4); }
.lib-search .clear-btn { background:transparent; border:none; cursor:pointer; padding:4px 8px; color:var(--text-3); font-family:'Space Mono',monospace; font-size:10px; letter-spacing:2px; text-transform:uppercase; transition:color .15s; }
.lib-search .clear-btn:hover { color:var(--text-1); }
.lib-search .clear-btn[hidden] { display:none; }

.lib-chips { display:flex; flex-wrap:wrap; gap:6px; align-items:center; }
.lib-chip-label { font-family:'Space Mono',monospace; font-size:9px; letter-spacing:2px; color:var(--text-4); text-transform:uppercase; margin-right:4px; }
/* Square, like the screener's. A pill and a rectangle on the same site read as
   two different products. */
.lib-chip {
  padding:4px 9px; font-family:'Space Mono',monospace; font-size:9px; letter-spacing:1.5px;
  color:var(--text-3); cursor:pointer; background:transparent;
  border:1px solid var(--border-bright); border-radius:0; text-transform:uppercase;
  user-select:none; transition:background .15s, color .15s, border-color .15s;
}
.lib-chip:hover { color:var(--text-1); border-color:var(--border-bright); }
.lib-chip.active { color:#FFF; background:rgba(255,31,61,0.18); border-color:var(--apt-red); }

.lib-list { display:flex; flex-direction:column; border-top:1px solid var(--text-1); }
.lib-item {
  background:none; padding:12px 4px; border-bottom:1px solid var(--border);
  display:grid; grid-template-columns:132px 1fr auto; gap:16px; align-items:baseline;
  transition:background .15s;
}
.lib-item:hover { background:var(--bg-1); }
/* A section name, not a badge. Set in the same mono as every other label on
   the site and left aligned so the column reads as a column. */
.lib-item .li-section {
  font-family:'Space Mono',monospace; font-size:9px; letter-spacing:1.5px;
  color:var(--text-4); text-transform:uppercase; padding:0;
  border:none; background:none; justify-self:start;
}
.lib-item:hover .li-section { color:var(--apt-red); }
.lib-item .li-headline { font-size:14px; color:var(--text-1); line-height:1.5; font-weight:400; }
.lib-item .li-meta { font-family:'Space Mono',monospace; font-size:9px; letter-spacing:1.5px; color:var(--text-4); text-transform:uppercase; margin-top:5px; }
.lib-item .li-src { font-family:'Space Mono',monospace; font-size:10px; letter-spacing:1.5px; color:var(--text-4); text-transform:uppercase; text-align:right; }
@media (max-width:680px) {
  .lib-item { grid-template-columns:1fr; gap:6px; padding:16px 18px; }
  .lib-item .li-src { text-align:left; }
}

.empty-state { padding:44px; text-align:center; font-family:'Space Mono',monospace; font-size:11px;
  letter-spacing:2px; color:var(--text-4); text-transform:uppercase; background:none;
  border:none; border-bottom:1px solid var(--border); }

/* Stocks page: filterable table */
.lib-sub { font-size:14px; color:var(--text-3); line-height:1.6; max-width:780px; margin-bottom:24px; }
.picks-meta { font-family:'Space Mono',monospace; font-size:11px; letter-spacing:2px; color:var(--text-4); text-transform:uppercase; }
/* Advanced filter panel */
.stk-filter-bar { display:flex; align-items:center; gap:10px; padding-top:6px; border-top:1px solid var(--border); margin-top:4px; flex-wrap:wrap; }
.stk-filter-toggle {
  display:inline-flex; align-items:center; gap:8px;
  padding:8px 14px; font-family:'Space Mono',monospace; font-size:11px; letter-spacing:1.5px;
  color:var(--text-2); cursor:pointer; background:rgba(255,31,61,0.06);
  border:1px solid rgba(255,31,61,0.20); border-radius:999px; text-transform:uppercase;
  transition:all .15s;
}
.stk-filter-toggle:hover { color:var(--text-1); border-color:rgba(255,31,61,0.45); }
.stk-filter-toggle.open { color:var(--text-1); background:rgba(255,31,61,0.12); border-color:rgba(255,31,61,0.45); }
.stk-filter-toggle-arrow { display:inline-block; font-size:14px; line-height:1; transition:transform .2s; }
.stk-filter-toggle.open .stk-filter-toggle-arrow { transform:rotate(90deg); }
.stk-filter-toggle-count { color:var(--apt-rose); font-weight:600; }
.stk-filter-reset {
  font-family:'Space Mono',monospace; font-size:10px; letter-spacing:1.5px;
  color:var(--text-3); background:transparent; border:none; cursor:pointer;
  text-transform:uppercase; padding:8px 4px;
}
.stk-filter-reset:hover { color:var(--apt-rose); }

.stk-filter-panel { display:flex; flex-direction:column; gap:10px; margin-top:6px; padding:14px 16px; background:var(--bg-1); border:1px solid var(--border); border-radius:12px; }
.stk-filter-cols { display:grid; grid-template-columns:1fr 1fr; gap:24px; }
@media (max-width:980px) { .stk-filter-cols { grid-template-columns:1fr; gap:18px; } }
.stk-filter-col { display:flex; flex-direction:column; gap:8px; }
.stk-filter-col-h { font-family:'Space Grotesk',sans-serif; font-size:13px; font-weight:700; letter-spacing:0.02em; color:var(--text-1); padding-bottom:8px; margin-bottom:4px; border-bottom:1px solid var(--border); display:flex; align-items:baseline; gap:10px; flex-wrap:wrap; }
.stk-filter-col-sub { font-family:'Space Mono',monospace; font-size:9px; letter-spacing:1.5px; color:var(--text-4); text-transform:uppercase; font-weight:400; }
.stk-filter-row { display:grid; grid-template-columns:130px 90px auto 90px 1fr; align-items:center; gap:8px; }

/* Dimension weight sliders (right column of filter panel) */
.stk-weight-row { display:grid; grid-template-columns:90px 1fr 50px; align-items:center; gap:12px; padding:6px 0; }
.stk-weight-label { font-family:'Space Mono',monospace; font-size:11px; letter-spacing:1px; color:var(--text-2); text-transform:uppercase; }
.stk-weight-slider {
  -webkit-appearance:none; appearance:none; height:4px; background:rgba(255,255,255,0.08);
  border-radius:2px; outline:none; cursor:pointer; width:100%;
}
.stk-weight-slider::-webkit-slider-thumb {
  -webkit-appearance:none; appearance:none; width:16px; height:16px; border-radius:50%;
  background:var(--apt-rose); border:2px solid var(--bg-base); cursor:pointer;
  box-shadow:0 0 0 1px var(--apt-rose), 0 0 8px rgba(255,31,61,0.3); transition:transform .15s;
}
.stk-weight-slider::-webkit-slider-thumb:hover { transform:scale(1.15); }
.stk-weight-slider::-moz-range-thumb {
  width:16px; height:16px; border-radius:50%; background:var(--apt-rose);
  border:2px solid var(--bg-base); cursor:pointer; box-shadow:0 0 8px rgba(255,31,61,0.3);
}
.stk-weight-val { font-family:'Space Mono',monospace; font-size:12px; color:var(--apt-rose); font-weight:600; text-align:right; }
.stk-weight-presets { display:flex; flex-wrap:wrap; gap:5px; align-items:center; padding-top:10px; margin-top:6px; border-top:1px solid var(--border); }
.stk-weight-presets-label { font-family:'Space Mono',monospace; font-size:9px; letter-spacing:2px; color:var(--text-4); text-transform:uppercase; margin-right:4px; }
.stk-filter-row-toggle { grid-template-columns:1fr; }
.stk-filter-label { font-family:'Space Mono',monospace; font-size:10px; letter-spacing:1.5px; color:var(--text-3); text-transform:uppercase; }
.stk-filter-input {
  padding:7px 10px; font-family:'Space Mono',monospace; font-size:12px;
  color:var(--text-1); background:var(--bg-1);
  border:1px solid var(--border); border-radius:8px; outline:none;
  transition:border-color .15s, box-shadow .15s; width:100%;
}
.stk-filter-input:focus { border-color:var(--apt-red); box-shadow:0 0 0 2px rgba(255,31,61,0.10); }
.stk-filter-input::placeholder { color:var(--text-5); }
.stk-filter-input.stk-bad { border-bottom-color:var(--apt-red); color:var(--apt-red); }
.stk-filter-sep { font-family:'Space Mono',monospace; font-size:10px; color:var(--text-4); text-align:center; }
.stk-filter-hint { font-family:'Space Mono',monospace; font-size:9px; letter-spacing:1px; color:var(--text-4); text-transform:uppercase; }
.stk-filter-stat { font-family:'Space Mono',monospace; font-size:9px; letter-spacing:0.5px; color:var(--text-4); text-transform:none; font-style:italic; }
.stk-filter-stat-empty { color:var(--text-5); }
.stk-filter-quicks { display:flex; gap:5px; flex-wrap:wrap; }
.stk-quick {
  padding:5px 10px; font-family:'Space Mono',monospace; font-size:9px; letter-spacing:1.2px;
  color:var(--text-3); cursor:pointer; background:transparent;
  border:1px solid var(--border); border-radius:6px; text-transform:uppercase; transition:all .15s;
}
.stk-quick:hover { color:var(--text-1); border-color:var(--border-bright); }
.stk-quick.active { color:#FFF; background:rgba(255,31,61,0.18); border-color:var(--apt-red); }
.stk-filter-checkbox { display:flex; align-items:center; gap:8px; font-family:'Space Mono',monospace; font-size:11px; color:var(--text-2); cursor:pointer; }
.stk-filter-checkbox input { accent-color:var(--apt-red); width:14px; height:14px; cursor:pointer; }

@media (max-width:780px) {
  .stk-filter-row { grid-template-columns:1fr 1fr; gap:6px 10px; }
  .stk-filter-row .stk-filter-label { grid-column:1 / -1; }
  .stk-filter-sep { display:none; }
  .stk-filter-hint { grid-column:1 / -1; }
  .stk-filter-quicks { grid-column:1 / -1; margin-top:4px; }
}

.stk-table { background:transparent; border:none; }
.stk-table::-webkit-scrollbar { width:8px; }
.stk-table::-webkit-scrollbar-thumb { background:var(--border-bright); border-radius:4px; }
/* An author display rule outranks the UA [hidden] rule, so these panels would
   stay visible when switched away from. Re-assert it for the view containers. */
.stk-chart[hidden], .stk-radar[hidden], .stk-table[hidden], .stk-hero[hidden] { display:none !important; }
.stk-chart { display:block; }
.stk-focus { display:flex; align-items:center; flex-wrap:nowrap; gap:7px; margin:0 0 12px;
  height:24px; overflow-x:auto; overflow-y:hidden; scrollbar-width:none; }
.stk-focus::-webkit-scrollbar { display:none; }
.stk-cmp-out { opacity:0.45; }
.stk-cmp-out i { filter:grayscale(1); }
.stk-focus-hint { font-family:'Space Mono',monospace; font-size:9px; letter-spacing:1.2px;
  text-transform:uppercase; color:var(--text-4); white-space:nowrap; overflow:hidden;
  text-overflow:ellipsis; }
.stk-focus-lab { font-family:'Space Mono',monospace; font-size:9px; letter-spacing:2px;
  text-transform:uppercase; color:var(--text-4); margin-right:3px; }
.stk-focus-clear { font-family:'Space Mono',monospace; font-size:9px; letter-spacing:1.5px;
  text-transform:uppercase; background:none; border:none; cursor:pointer; color:var(--text-4);
  border-bottom:1px solid transparent; padding:0 0 2px; margin-left:4px; }
.stk-focus-clear:hover { color:var(--apt-red); border-bottom-color:var(--apt-red); }
.stk-axis { font-family:'Space Mono',monospace; font-size:10px; letter-spacing:1px;
  color:var(--text-1); background:var(--bg-base); border:none;
  border-bottom:1px solid var(--text-1); padding:2px 2px 3px; cursor:pointer; max-width:180px; }
.stk-axis:focus { outline:none; border-bottom-color:var(--apt-red); }
.stk-axis-op { font-family:'Space Mono',monospace; font-size:9px; letter-spacing:1.5px;
  text-transform:uppercase; color:var(--text-4); }
.stk-axis-sep { width:1px; height:15px; background:var(--border-bright); margin:0 3px; }
.stk-lenses { display:flex; align-items:center; gap:6px; flex-wrap:wrap; margin-bottom:10px; }
.stk-lens { font-family:'Space Mono',monospace; font-size:10px; letter-spacing:1.5px; text-transform:uppercase;
  padding:5px 10px; cursor:pointer; border:1px solid var(--border-bright); background:transparent; color:var(--text-3); }
.stk-lens.active { background:var(--text-1); color:var(--bg-base); border-color:var(--text-1); }
.stk-chart-blurb { font-size:12px; color:var(--text-3); line-height:1.6; margin:0 0 12px; max-width:70ch; }
.stk-chart-plot { position:relative; border:1px solid var(--border); background:var(--surface-1); }
.stk-chart-plot canvas { display:block; width:100%; height:520px; }
.stk-chart-plot .ax { position:absolute; font-family:'Space Mono',monospace; font-size:10px;
  letter-spacing:2px; text-transform:uppercase; color:var(--text-3); pointer-events:none; }
.stk-chart-plot .tl { top:12px; left:14px; } .stk-chart-plot .tr { top:12px; right:14px; }
.stk-chart-plot .bl { bottom:12px; left:14px; } .stk-chart-plot .br { bottom:12px; right:14px; }
.stk-chart-tip { position:absolute; pointer-events:none; background:var(--bg-base); color:var(--text-1);
  border:1px solid var(--text-1); padding:6px 9px; font-family:'Space Mono',monospace; font-size:10px;
  line-height:1.5; white-space:nowrap; z-index:5; }
.stk-chart-foot { font-size:11px; color:var(--text-4); margin:10px 2px 0; }
.stk-result { margin-left:auto; font-family:'Space Mono',monospace; font-size:10px;
  letter-spacing:1.5px; color:var(--text-4); white-space:nowrap; }
.stk-about { font-family:'Space Mono',monospace; font-size:10px; letter-spacing:1.5px;
  text-transform:uppercase; background:none; border:none; cursor:pointer; color:var(--text-4);
  border-bottom:1px solid transparent; padding:0 0 2px; }
.stk-about:hover, .stk-about[aria-expanded="true"] { color:var(--text-1); border-bottom-color:var(--apt-red); }
.stk-railgroup { padding:11px 0; border-bottom:1px solid var(--border); }
.stk-railhead { font-family:'Space Mono',monospace; font-size:9px; letter-spacing:2.5px;
  text-transform:uppercase; color:var(--text-4); margin-bottom:8px; }
body.page-stocks .stk-railgroup .lib-chips { flex-wrap:wrap; gap:4px; }
body.page-stocks .stk-railgroup .lib-chip { font-size:8px; padding:3px 7px; letter-spacing:1px; }
.stk-hero { display:grid; grid-template-columns:minmax(0,1fr) minmax(0,1fr); gap:0;
  border-bottom:1px solid var(--text-1); margin-bottom:0; }
.stk-hero-l { padding:26px 34px 30px 0; }
.stk-hero-r { padding:26px 0 30px 34px; border-left:1px solid var(--text-1); }
.stk-hero-num { font-family:'Instrument Serif',Georgia,serif; font-weight:400; font-size:60px;
  line-height:1.02; letter-spacing:-0.5px; color:var(--text-1); margin:0 0 16px; max-width:12ch; }
.stk-hero-num .dot { color:var(--apt-red); }
.stk-hero-blurb { font-size:13px; line-height:1.75; color:var(--text-3); margin:0; max-width:48ch; }
.stk-hero-q { font-family:'Space Mono',monospace; font-size:12px; line-height:2.1;
  color:var(--text-3); margin:0 0 30px; max-width:56ch; }
.stk-q { font-family:inherit; font-size:inherit; background:none; border:none; padding:0 0 2px;
  cursor:pointer; color:var(--text-1); border-bottom:2px solid var(--apt-red); }
.stk-q:hover { color:var(--apt-red); }
.stk-q.on { color:var(--apt-red); }
.stk-hero-stats { display:flex; gap:40px; align-items:baseline; }
.stk-hero-stats .n { display:block; font-family:'Instrument Serif',Georgia,serif; font-size:34px;
  line-height:1; color:var(--text-1); }
.stk-hero-stats .n.hot { color:var(--apt-red); }
.stk-hero-stats .k { display:block; font-family:'Space Mono',monospace; font-size:9px;
  letter-spacing:2px; text-transform:uppercase; color:var(--text-4); margin-top:7px; }
@media (max-width:900px) {
  .stk-hero { grid-template-columns:1fr; }
  .stk-hero-l { padding-right:0; }
  .stk-hero-r { padding-left:0; border-left:none; border-top:1px solid var(--text-1); }
  .stk-hero-num { font-size:40px; }
}
.stk-masthead { display:flex; align-items:baseline; gap:12px; padding:0 0 10px; }
.stk-mast-name { font-family:'Instrument Serif',Georgia,serif; font-size:27px; line-height:1; color:var(--text-1); }
.stk-mast-sub { font-family:'Space Mono',monospace; font-size:10px; letter-spacing:3px;
  text-transform:uppercase; color:var(--text-4); }
.stk-sortrow { display:flex; align-items:center; gap:14px; padding:14px 4px 2px;
  font-family:'Space Mono',monospace; font-size:10px; letter-spacing:1.5px; text-transform:uppercase; }
.stk-sortlab { font-size:9px; letter-spacing:2px; color:var(--text-4); }
.stk-sort { background:none; border:none; padding:0 0 2px; cursor:pointer; color:var(--text-4);
  font-family:inherit; font-size:inherit; letter-spacing:inherit; text-transform:inherit;
  border-bottom:1px solid transparent; }
.stk-sort.active { color:var(--text-1); border-bottom-color:var(--apt-red); }
.stk-score { font-family:'Instrument Serif',Georgia,serif; font-size:21px; line-height:1; text-align:right; letter-spacing:0; }
.stk-views-switch { display:flex; border:1px solid var(--border-bright); flex-shrink:0; }
.stk-view-btn { padding:7px 15px; font-family:'Space Mono',monospace; font-size:10px; letter-spacing:2px;
  text-transform:uppercase; cursor:pointer; background:transparent; color:var(--text-3); border:none; }
.stk-view-btn + .stk-view-btn { border-left:1px solid var(--border-bright); }
.stk-view-btn.active { background:var(--text-1); color:var(--bg-base); }
.stk-map { display:flex; flex-direction:column; min-width:0; }
/* One band for all three columns, so their contents start on the same line.
   Two rows: a label, and a line of detail under it. */
.stk-col-h { display:flex; flex-direction:column; justify-content:flex-end; gap:4px;
  height:52px; padding-bottom:9px; margin-bottom:12px;
  border-bottom:1px solid var(--text-1); }
.stk-col-h-1 { display:flex; align-items:baseline; justify-content:space-between; gap:10px;
  font-family:'Space Mono',monospace; font-size:10px; letter-spacing:2px; text-transform:uppercase;
  color:var(--text-1); }
.stk-col-h-1 h3 { font-family:'Instrument Serif',Georgia,serif; font-size:20px; font-weight:400;
  letter-spacing:-0.2px; text-transform:none; color:var(--text-1); margin:0;
  overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.stk-col-h-2 { font-family:'Space Mono',monospace; font-size:9px; letter-spacing:1.1px;
  color:var(--text-4); overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.stk-map-spin { font-family:'Space Mono',monospace; font-size:9px; letter-spacing:1.5px;
  text-transform:uppercase; background:none; border:1px solid var(--border-bright); cursor:pointer;
  color:var(--text-3); padding:2px 7px; margin-left:9px; }
.stk-map-spin.active { background:var(--text-1); color:var(--bg-base); border-color:var(--text-1); }
/* One height for both plots, so the map and the rings end on the same line. */
.stk-radar { --pane-h:clamp(400px, calc(100vh - 290px), 780px); }
.stk-map-plot { position:relative; height:var(--pane-h);
  border:1px solid var(--border); background:var(--surface-1); }
.stk-map-plot canvas { position:absolute; inset:0; width:100%; height:100%; cursor:grab; }
.stk-map-foot { font-family:'Space Mono',monospace; font-size:9px; letter-spacing:1px;
  color:var(--text-4); margin:9px 0 0; }
.stk-radar { display:grid; grid-template-columns:repeat(3, minmax(0,1fr));
  grid-template-rows:auto auto auto; align-items:start; gap:0 22px;
  align-items:start; justify-content:start; }
.stk-radar > .stk-map { grid-column:1; grid-row:1 / span 3; }
.stk-radar > .stk-rings-h { grid-column:2; grid-row:1; }
.stk-radar > .stk-radar-plot { grid-column:2; grid-row:2; }
.stk-radar > .stk-radar-note { grid-column:2; grid-row:3; }
.stk-radar > .stk-radar-side { grid-column:3; grid-row:1 / span 3; }
.stk-radar-plot { position:relative; height:var(--pane-h); display:flex;
  align-items:center; justify-content:center;
  border:1px solid var(--border); background:var(--surface-1);
  padding:14px; max-width:720px; }
.stk-radar-plot svg { width:auto; height:100%; max-width:100%; display:block; }
.stk-radar-quads .q { position:absolute; font-family:'Space Mono',monospace; font-size:10px;
  letter-spacing:2px; text-transform:uppercase; color:var(--text-3); }
.stk-radar-quads .tl { top:14px; left:16px; } .stk-radar-quads .tr { top:14px; right:16px; }
.stk-radar-quads .bl { bottom:14px; left:16px; } .stk-radar-quads .br { bottom:14px; right:16px; }
.stk-radar-note { font-size:11px; color:var(--text-4);
  line-height:1.6; margin:10px 2px 0; }
.stk-cmps { display:flex; flex-wrap:wrap; gap:6px; margin:0 0 16px; }
.stk-cmp { display:inline-flex; align-items:center; gap:6px; padding:3px 6px 3px 7px;
  border:1px solid var(--border-bright); font-family:'Space Mono',monospace; font-size:10px;
  letter-spacing:1px; color:var(--text-1); }
.stk-cmp i { width:7px; height:7px; background:var(--cmp); flex-shrink:0; }
.stk-cmp-x { background:none; border:none; padding:0 0 0 2px; cursor:pointer; font-size:13px;
  line-height:1; color:var(--text-4); }
.stk-cmp-x:hover { color:var(--apt-red); }
/* In compare mode the percentile bar gives up its column to the extra values. */
.stk-radar-row.cmp { grid-template-columns:minmax(0,1fr) repeat(3, 34px); }
.stk-radar-fam-h.cmp { display:grid; grid-template-columns:minmax(0,1fr) repeat(3, 34px);
  align-items:baseline; }
.stk-radar-side h3 { font-family:'Instrument Serif',Georgia,serif; font-size:26px; font-weight:400;
  color:var(--text-1); margin:0 0 4px; }
.stk-radar-hint { font-size:11px; color:var(--text-4); line-height:1.6; margin:0 0 14px; }
.stk-radar-side .stk-cmps:empty { display:none; }
.stk-picklist { border-top:1px solid var(--text-1); overflow-y:auto;
  max-height:calc(var(--pane-h) + 30px); }
.stk-pick-h { position:sticky; top:0; background:var(--bg-base); z-index:1;
  font-family:'Space Mono',monospace; font-size:9px; letter-spacing:1.5px; text-transform:uppercase;
  color:var(--text-4); padding:7px 2px 6px; border-bottom:1px solid var(--border); }
.stk-pick { display:grid; grid-template-columns:52px minmax(0,1fr) 46px; gap:8px; align-items:baseline;
  width:100%; text-align:left; padding:7px 2px; background:none; border:none;
  border-bottom:1px solid var(--border); cursor:pointer; }
.stk-pick:hover { background:var(--bg-1); }
.stk-pick-tk { font-family:'Space Mono',monospace; font-size:10px; letter-spacing:0.5px;
  color:var(--text-1); }
.stk-pick-nm { font-size:11px; color:var(--text-3); overflow:hidden; text-overflow:ellipsis;
  white-space:nowrap; }
.stk-pick-sc { font-family:'Instrument Serif',Georgia,serif; font-size:14px; text-align:right; }
.stk-pick-empty { padding:24px 2px; font-family:'Space Mono',monospace; font-size:10px;
  letter-spacing:1.5px; text-transform:uppercase; color:var(--text-4); }
.stk-radar-fam { margin-top:16px; }
.stk-radar-fam-h { display:flex; justify-content:space-between; align-items:baseline;
  border-bottom:1px solid var(--border-bright); padding-bottom:5px; margin-bottom:7px; }
.stk-radar-val.na { cursor:help; }
.stk-radar-fam-h b { font-family:'Space Mono',monospace; font-size:10px; letter-spacing:2px;
  text-transform:uppercase; font-weight:400; color:var(--text-2); }
.stk-radar-row { display:grid; grid-template-columns:1fr 46px 26px; gap:8px; align-items:center;
  font-size:11px; color:var(--text-3); padding:2px 0; }
.stk-radar-foot { font-size:10px; line-height:1.5; color:var(--text-4); margin:8px 0 0;
  padding-top:7px; border-top:1px solid var(--border); }
.stk-radar-bar { height:2px; background:var(--border-bright); position:relative; }
.stk-radar-bar i { position:absolute; inset:0 auto 0 0; background:var(--apt-red); display:block; }
.stk-radar-val { font-family:'Space Mono',monospace; font-size:10px; color:var(--text-2); text-align:right; }
.stk-radar-val.na { color:var(--text-5); }
/* -- Screener shell, transcribed from the design ------------------------- */
body.scr-page { margin:0; background:var(--bg-base); color:var(--text-1);
  font-family:'Space Grotesk',system-ui,sans-serif; }
body.scr-page::before, body.scr-page::after { display:none !important; }
.scr { min-height:100vh; background:var(--bg-base); color:var(--text-1); }
.scr-top { position:sticky; top:0; z-index:60; display:flex; align-items:center;
  justify-content:space-between; gap:24px; height:56px; padding:0 32px;
  border-bottom:1px solid var(--text-1); background:var(--bg-base); }
.scr-brand { display:flex; align-items:baseline; gap:14px; }
.scr-mark { font-family:'Instrument Serif',Georgia,serif; font-size:23px; letter-spacing:-0.3px; }
.scr-sub { font-family:'Space Mono',monospace; font-size:10px; letter-spacing:2px;
  text-transform:uppercase; color:var(--text-3); }
.scr-nav { display:flex; align-items:center; gap:24px; font-family:'Space Mono',monospace;
  font-size:11px; letter-spacing:1.5px; text-transform:uppercase; }
.scr-nav a { color:var(--text-3); text-decoration:none; }
.scr-nav a:hover { color:var(--text-1); }
.scr-nav a.on { color:var(--text-1); border-bottom:2px solid var(--apt-red); padding-bottom:2px; }
.scr-body { display:flex; align-items:flex-start; }
.scr-rail { width:282px; flex-shrink:0; border-right:1px solid var(--text-1);
  background:var(--bg-1); align-self:stretch; }
.scr-rail-in { min-height:0; }
.scr-rail-h { position:sticky; top:0; z-index:2; background:var(--bg-1); display:flex;
  align-items:center; justify-content:space-between; height:53px; padding:0 20px;
  border-bottom:1px solid var(--text-1); font-family:'Space Mono',monospace; font-size:10px;
  letter-spacing:2.5px; text-transform:uppercase; }
.scr-reset { font-size:9px; letter-spacing:1.5px; color:var(--text-3); cursor:pointer; }
.scr-reset:hover { color:var(--apt-red); }
.scr-rail-lab { font-family:'Space Mono',monospace; font-size:10px; letter-spacing:2.5px;
  text-transform:uppercase; margin-bottom:11px; }
.stk-rg { border-bottom:1px solid var(--border); }
.stk-rg > summary { list-style:none; }
.stk-rg > summary::-webkit-details-marker { display:none; }
.stk-rg > summary:hover { background:var(--bg-base); }
.scr-check { display:flex; align-items:flex-start; gap:9px; margin-bottom:9px; cursor:pointer;
  font-size:12px; line-height:1.4; color:var(--text-2); }
.scr-saved > div { display:flex; justify-content:space-between; gap:10px; padding:6px 0;
  border-bottom:1px solid var(--border); font-size:12px; cursor:pointer; color:var(--text-2); }
.scr-saveline { display:flex; gap:6px; margin-top:10px; }
.scr-cov { margin-top:13px; }
.scr-cov-h { font-family:'Space Mono',monospace; font-size:9px; letter-spacing:1px;
  text-transform:uppercase; color:var(--text-3); margin-bottom:8px; }
.scr-cov-grid { display:grid; grid-template-columns:1fr 1fr; gap:6px 10px; }
.scr-cov-grid label { display:flex; align-items:center; justify-content:space-between; gap:6px;
  font-size:11px; color:var(--text-2); }
.scr-cov-grid input { width:42px; }
.scr-cov-note { font-size:11px; line-height:1.5; color:var(--text-4); margin:9px 0 0; }
.scr-main { flex:1; min-width:0; }
.scr-tool { position:sticky; top:56px; z-index:50; background:var(--bg-base); display:flex;
  align-items:center; gap:16px; height:53px; padding:0 32px;
  border-bottom:1px solid var(--text-1); }
.scr-find { position:relative; display:flex; align-items:center; gap:9px; flex:1 1 0; min-width:104px;
  max-width:290px; border-bottom:1px solid var(--text-1); padding-bottom:4px; }
.scr-find .ic { color:var(--text-4); font-size:12px; }
.scr-find input { flex:1; min-width:0; background:transparent; border:none; outline:none;
  font-family:'Space Mono',monospace; font-size:12px; color:var(--text-1); }
.scr-find .clear-btn { background:none; border:none; cursor:pointer; color:var(--text-4);
  font-size:15px; line-height:1; }
.scr-ac { position:absolute; top:100%; left:0; right:0; z-index:80; margin-top:5px;
  background:var(--bg-base); border:1px solid var(--text-1); max-height:292px; overflow-y:auto;
  box-shadow:0 8px 26px rgba(0,0,0,0.14); }
.scr-ac-item { display:flex; align-items:baseline; gap:10px; padding:8px 12px; cursor:pointer;
  border-bottom:1px solid var(--border); }
.scr-ac-item:last-child { border-bottom:none; }
.scr-ac-item.on { background:var(--bg-1); }
.scr-ac-tk { flex-shrink:0; min-width:54px; font-family:'Space Mono',monospace; font-size:11px;
  letter-spacing:0.5px; color:var(--text-1); }
.scr-ac-nm { flex:1; min-width:0; font-size:12px; color:var(--text-3); overflow:hidden;
  text-overflow:ellipsis; white-space:nowrap; }
.scr-ac-tag { flex-shrink:0; font-family:'Space Mono',monospace; font-size:8px; letter-spacing:1.5px;
  text-transform:uppercase; color:var(--text-4); }
.scr-ac-item.on .scr-ac-tag { color:var(--apt-red); }
.scr-ac-none { padding:10px 12px; font-size:12px; color:var(--text-4); }
.scr-note { margin-left:auto; flex-shrink:1; min-width:0; overflow:hidden;
  text-overflow:ellipsis; white-space:nowrap; font-family:'Space Mono',monospace;
  font-size:9px; letter-spacing:1px; color:var(--text-4); }
.scr-count { margin-left:14px; flex-shrink:0; white-space:nowrap;
  font-family:'Space Mono',monospace; font-size:10px; letter-spacing:1.5px; color:var(--text-4); }
.scr-pane { padding:0 32px 30px; }
.scr-page-body { padding:0 32px 40px; }
body.page-stocks .scr-page-body { padding:0; }
a.scr-brand { text-decoration:none; color:inherit; }
@media (max-width:900px) { .scr-page-body { padding:0 16px 30px; } }
/* Ultrawide: the rail stays pinned left, the working area stops growing. Left
   aligned rather than centred so the table keeps its edge against the rail. */
@media (min-width:1700px) {
  .scr-tool > *:last-child { margin-right:auto; }
  .scr-tool, .scr-pane, .scr-foot { max-width:1660px; }
}
.scr-foot { border-top:1px solid var(--text-1); padding:20px 32px; display:flex;
  justify-content:space-between; gap:16px; font-family:'Space Mono',monospace; font-size:10px;
  letter-spacing:1.5px; color:var(--text-4); }
@media (max-width:1200px) {
  .stk-radar { grid-template-columns:1fr; grid-template-rows:auto; }
  .stk-radar > .stk-map, .stk-radar > .stk-rings-h, .stk-radar > .stk-radar-plot,
  .stk-radar > .stk-radar-note, .stk-radar > .stk-radar-side {
    grid-column:1; grid-row:auto; }
  .stk-map { margin-bottom:26px; }
}
@media (max-width:900px) {
  .scr-body { display:block; }
  .scr-rail { width:auto; border-right:none; border-bottom:1px solid var(--text-1); }
  .scr-top, .scr-tool, .scr-pane, .scr-foot { padding-left:16px; padding-right:16px; }
}
/* The screener chassis. The design is flat: 1px rules, no rounded cards, no
   fills behind the toolbar, and the ticker set in a serif at reading size so
   the eye lands on the company before the numbers. */
body.page-stocks .lib.lib-wide { background:transparent; border:none; box-shadow:none; padding:0; }
body.page-stocks .lib-search { background:transparent; border:none; border-bottom:1px solid var(--border-bright);
  border-radius:0; box-shadow:none; }
body.page-stocks .lib-chip { background:transparent; border:1px solid var(--border-bright); border-radius:0;
  font-family:'Space Mono',monospace; font-size:9px; letter-spacing:1.5px; padding:4px 9px; }
body.page-stocks .lib-chip.active { background:var(--text-1); color:var(--bg-base); border-color:var(--text-1); }
body.page-stocks .lib-chip-label { font-family:'Space Mono',monospace; font-size:9px; letter-spacing:2px;
  color:var(--text-4); }
body.page-stocks .stk-sidebar { background:transparent; border:none; border-right:1px solid var(--border-bright);
  border-radius:0; box-shadow:none; padding-right:18px; }
body.page-stocks .stk-sidebar-h { font-family:'Space Mono',monospace; font-size:10px; letter-spacing:2.5px;
  text-transform:uppercase; color:var(--text-1); border-bottom:1px solid var(--text-1); padding-bottom:8px; }
body.page-stocks .stk-views-switch { border-color:var(--text-1); }
body.page-stocks .lib-h { display:none; }
.stk-toprow { display:flex; align-items:center; gap:14px; flex-wrap:nowrap;
  padding:0 0 9px; border-bottom:1px solid var(--text-1); background:transparent; }
.stk-toprow .lib-search { flex:1 1 260px; max-width:340px; background:transparent;
  border:none; border-bottom:1px solid var(--border-bright); border-radius:0; padding:3px 0; }
.stk-toprow .lib-chips { gap:5px; }
.stk-table { border:none; background:transparent; }
.stk-row { border-bottom:1px solid var(--border); background:transparent; align-items:center; }
.stk-row:hover { background:var(--surface-1); }
.stk-rank { font-family:'Space Mono',monospace; font-size:11px; color:var(--text-4); }
.stk-id { display:flex; align-items:baseline; gap:9px; min-width:0; }
.stk-tk { font-family:'Instrument Serif',Georgia,serif; font-size:19px; line-height:1.1; color:var(--text-1); }
.stk-nm { font-size:12px; color:var(--text-3); white-space:nowrap; overflow:hidden;
  text-overflow:ellipsis; min-width:0; }
.stk-factors { display:flex; gap:10px; align-items:center; padding-left:12px; }
.stk-th-fac { display:flex; gap:10px; padding-left:12px; }
.stk-th-fac > span { flex:1 1 0; min-width:0; text-align:center; overflow:hidden;
  text-overflow:ellipsis; white-space:nowrap; }
.stk-f { flex:1 1 0; min-width:0; display:block; text-align:center; }
.stk-f-t { display:block; height:5px; background:var(--border-bright); position:relative; }
/* The midline: a bar grows right of it for a positive z and left for a negative
   one, so zero is readable without a number. */
.stk-f-t::after { content:''; position:absolute; left:50%; top:-2px; bottom:-2px; width:1px;
  background:var(--border-bright); }
.stk-f-t em { position:absolute; top:0; bottom:0; display:block; }
.stk-f-t em.up { background:var(--apt-red); }
.stk-f-t em.dn { background:var(--text-4); }
.stk-f b { display:block; font-family:'Space Mono',monospace; font-size:8px; letter-spacing:1px;
  color:var(--text-5); font-weight:400; margin-top:3px; }
.stk-head { display:grid; grid-template-columns:26px minmax(0,1.5fr) minmax(0,0.8fr) 74px 68px minmax(0,2.1fr) 82px 74px; gap:10px; padding:8px 4px; border-bottom:1px solid var(--text-1); background:transparent; backdrop-filter:none; -webkit-backdrop-filter:none; font-family:'Space Mono',monospace; font-size:9px; letter-spacing:2px; color:var(--text-4); text-transform:uppercase; position:sticky; top:0; z-index:3; }
.stk-th { cursor:pointer; user-select:none; transition:color .15s; }
.stk-th:nth-child(n+4) { text-align:right; }
.stk-th:hover { color:var(--text-1); }
.stk-th.asc::after { content:' \\2191'; color:var(--apt-rose); margin-left:4px; }
.stk-th.desc::after { content:' \\2193'; color:var(--apt-rose); margin-left:4px; }
.stk-row { display:grid; grid-template-columns:26px minmax(0,1.5fr) minmax(0,0.8fr) 74px 68px minmax(0,2.1fr) 82px 74px; gap:10px; padding:9px 4px; cursor:pointer; align-items:center; }
.stk-row:hover { background:var(--bg-1); }
.stk-ticker { font-family:'Space Grotesk',sans-serif; font-size:14px; font-weight:700; color:var(--apt-rose); letter-spacing:0.02em; padding-top:1px; }
.stk-name { font-size:13px; color:var(--text-1); line-height:1.35; }
.stk-name .stk-sub { font-family:'Space Mono',monospace; font-size:9px; letter-spacing:1.5px; color:var(--text-4); text-transform:uppercase; margin-top:4px; font-weight:400; }
.stk-sector { font-family:'Space Mono',monospace; font-size:10px; letter-spacing:1.2px;
  color:var(--text-3); text-transform:uppercase; padding-top:1px;
  white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.stk-cap { font-family:'Space Mono',monospace; font-size:12px; color:var(--text-1); text-align:right; padding-top:1px; }
.stk-pct { font-family:'Space Mono',monospace; font-size:12px; text-align:right; padding-top:1px; }
.stk-pct.stk-pos { color:#34D27A; }
.stk-pct.stk-neg { color:var(--apt-red); }
.stk-pe { font-family:'Space Mono',monospace; font-size:12px; color:var(--text-3); text-align:right; padding-top:1px; }

.stk-score-pos { color:#34D27A; }
.stk-score-neg { color:var(--apt-red); }
.stk-score-neutral { color:var(--text-2); }
.stk-score-na { color:var(--text-5); }
.stk-date { font-family:'Space Mono',monospace; font-size:10px; letter-spacing:1px; color:var(--text-3); text-align:right; padding-top:3px; }
.stk-date-dim { color:var(--text-4); }
.stk-row { cursor:pointer; }
.stk-row .stk-ticker { transition:color .15s; }
.stk-row:hover .stk-ticker { color:#FFB347; }

/* Expand-on-click factor panel */
.stk-detail { padding:16px 0 22px 20px; background:transparent; border-top:1px solid var(--border); border-bottom:1px solid var(--border); border-left:2px solid var(--apt-red); animation:fpFadeIn .25s ease-out; }
/* One expanded company ran 2,418px in a 720px viewport: three and a half
   screens of scrolling, with every block full width and stacked because there
   was nothing telling them otherwise. The summary and the factor cards stay
   across the top, where they are read first and want the width. The four
   blocks below are evidence, they pair naturally by size, and side by side
   they take about a third off the height.
   align-items:start so a short card does not stretch to match a tall one. */
@media (min-width:1100px) {
  .stk-detail { display:grid; grid-template-columns:1fr 1fr; column-gap:18px; align-items:start; }
  .stk-detail > .sb-card,
  .stk-detail > .fp-grid,
  .stk-detail > .fp-meta-panel { grid-column:1 / -1; }
}
@keyframes fpFadeIn { from { opacity:0; transform:translateY(-4px); } to { opacity:1; transform:translateY(0); } }

/* Score breakdown card (sits above the 4 factor cards) */
.sb-card { padding:16px 18px; background:transparent; border:1px solid var(--border);  margin-bottom:14px; }
.sb-h { font-family:'Space Mono',monospace; font-size:10px; letter-spacing:2px; color:var(--text-3); text-transform:uppercase; margin-bottom:14px; padding-bottom:10px; border-bottom:1px solid var(--border); display:flex; align-items:baseline; gap:10px; }
.sb-h-sub { font-size:9px; letter-spacing:1.5px; color:var(--text-4); text-transform:none; font-style:italic; opacity:0.8; }
.sb-row { display:grid; grid-template-columns:90px 1fr 60px; align-items:center; gap:14px; padding:5px 0; }
.sb-label { font-family:'Space Mono',monospace; font-size:11px; letter-spacing:1px; color:var(--text-2); text-transform:capitalize; }
.sb-bar { position:relative; height:6px; background:var(--border);  overflow:hidden; }
.sb-bar-axis { position:absolute; left:50%; top:0; bottom:0; width:1px; background:var(--border); z-index:2; }
.sb-bar-fill { position:absolute; top:0; bottom:0;  z-index:1; transition:width .25s ease-out; }
.sb-bar-fill.sb-pos { background:linear-gradient(90deg, rgba(52,210,122,0.4), rgba(52,210,122,0.85)); }
.sb-bar-fill.sb-neg { background:linear-gradient(270deg, rgba(255,31,61,0.4), rgba(255,31,61,0.85)); }
.sb-val { font-family:'Space Mono',monospace; font-size:12px; text-align:right; font-weight:500; }
.sb-val-pos { color:#34D27A; }
.sb-val-neg { color:var(--apt-red); }
.sb-val-na { color:var(--text-5); }
.sb-comp-row { display:grid; grid-template-columns:90px 1fr; align-items:baseline; gap:14px; margin-top:14px; padding-top:14px; border-top:1px solid var(--border); }
.sb-comp-label { font-family:'Space Mono',monospace; font-size:11px; letter-spacing:2px; color:var(--text-3); text-transform:uppercase; }
.sb-comp { font-family:'Space Grotesk',sans-serif; font-size:32px; font-weight:800; letter-spacing:-0.02em; text-align:right; line-height:1; }
.sb-comp-pos { color:#34D27A; }
.sb-comp-neg { color:var(--apt-red); }
.sb-comp-na { color:var(--text-5); }

/* Benford's Law card (sits below the 4 factor cards in the expand panel) */
.bf-card { margin-top:14px; padding:16px 18px; background:transparent; border:1px solid var(--border);  }
.bf-grid { display:grid; grid-template-columns:1fr 1fr; gap:18px; }
@media (max-width:780px) { .bf-grid { grid-template-columns:1fr; } }
.bf-sub { display:flex; flex-direction:column; }
.bf-sub-h { font-family:'Space Mono',monospace; font-size:10px; letter-spacing:1.5px; color:var(--text-3); text-transform:uppercase; margin-bottom:10px; padding-bottom:8px; border-bottom:1px solid var(--border); display:flex; align-items:baseline; gap:8px; flex-wrap:wrap; }
.bf-sub-meta { font-family:'Space Mono',monospace; font-size:9px; color:var(--text-4); margin-left:auto; letter-spacing:1px; text-transform:none; }
.bf-sub-meta sup { font-size:7px; vertical-align:super; }
.bf-h { font-family:'Space Mono',monospace; font-size:10px; letter-spacing:2px; color:var(--text-3); text-transform:uppercase; margin-bottom:14px; padding-bottom:10px; border-bottom:1px solid var(--border); display:flex; align-items:baseline; gap:10px; flex-wrap:wrap; }
.bf-fit { font-size:9px; font-weight:600; padding:2px 8px;  letter-spacing:1.5px; }
.bf-fit-good { color:#34D27A; background:rgba(52,210,122,0.10); border:1px solid rgba(52,210,122,0.25); }
.bf-fit-fair { color:#FFB347; background:rgba(255,179,71,0.10); border:1px solid rgba(255,179,71,0.25); }
.bf-fit-poor { color:var(--apt-red); background:rgba(255,31,61,0.10); border:1px solid rgba(255,31,61,0.25); }
.bf-meta { font-family:'Space Mono',monospace; font-size:10px; color:var(--text-4); margin-left:auto; letter-spacing:1px; text-transform:none; }
.bf-meta sup { font-size:8px; vertical-align:super; }
.bf-row { display:grid; grid-template-columns:20px 1fr 52px 48px; align-items:center; gap:10px; padding:4px 0; }
.bf-d { font-family:'Space Grotesk',sans-serif; font-size:14px; font-weight:700; color:var(--text-2); text-align:center; }
.bf-bar { position:relative; height:8px; background:var(--border);  }
.bf-bar-fill { position:absolute; left:0; top:0; bottom:0;  transition:background .2s; }
.bf-marker { position:absolute; top:-3px; bottom:-3px; width:2px; background:var(--text-2); opacity:0.65; }
.bf-obs { font-family:'Space Mono',monospace; font-size:12px; text-align:right; font-weight:500; transition:color .2s; }
.bf-exp { font-family:'Space Mono',monospace; font-size:10px; color:var(--text-4); }
/* Per-row deviation severity: green if within 10% of expected, amber 10-25%, red >25% */
.bf-row-close .bf-bar-fill    { background:linear-gradient(90deg, rgba(52,210,122,0.45), rgba(52,210,122,0.85)); }
.bf-row-close .bf-obs         { color:#34D27A; }
.bf-row-moderate .bf-bar-fill { background:linear-gradient(90deg, rgba(255,179,71,0.45), rgba(255,179,71,0.90)); }
.bf-row-moderate .bf-obs      { color:#FFB347; }
.bf-row-far .bf-bar-fill      { background:linear-gradient(90deg, rgba(255,122,133,0.55), rgba(255,31,61,0.95)); }
.bf-row-far .bf-obs           { color:var(--apt-red); }
.bf-foot { margin-top:14px; padding-top:12px; border-top:1px solid var(--border); font-family:'Inter',sans-serif; font-size:11px; color:var(--text-4); line-height:1.5; }
.bf-empty { padding:18px; text-align:center; font-family:'Space Mono',monospace; font-size:11px; color:var(--text-4); text-transform:uppercase; }

/* Signals row: Neglect (Lynch) + Insider Movement (Seyhun) */
.sg-row-grid { display:grid; grid-template-columns:1fr 1fr; gap:16px; margin-top:14px; }
@media (max-width:780px) { .sg-row-grid { grid-template-columns:1fr; } }
.sg-card { padding:16px 18px; background:transparent; border:1px solid var(--border);  border-left-width:3px; border-left-color:var(--apt-rose); }
.sg-card-insider { border-left-color:#9B8CFF; }
.sg-card-stub { opacity:0.7; }
.sg-h { display:flex; justify-content:space-between; align-items:baseline; gap:10px; margin-bottom:12px; padding-bottom:8px; border-bottom:1px solid var(--border); flex-wrap:wrap; }
.sg-h-title { font-family:'Space Grotesk',sans-serif; font-size:13px; font-weight:700; color:var(--text-1); letter-spacing:0.02em; }
.sg-h-eyebrow { font-family:'Space Mono',monospace; font-size:9px; letter-spacing:1.5px; color:var(--text-4); text-transform:uppercase; font-weight:400; margin-left:6px; }
.sg-score-badge { font-family:'Space Mono',monospace; font-size:10px; letter-spacing:1px; padding:3px 9px;  text-transform:uppercase; }
.sg-score-badge.sg-pos { color:#34D27A; background:rgba(52,210,122,0.10); border:1px solid rgba(52,210,122,0.25); }
.sg-score-badge.sg-neg { color:var(--apt-rose); background:rgba(255,57,77,0.10); border:1px solid rgba(255,57,77,0.25); }
.sg-score-badge.sg-neutral { color:var(--text-2); background:var(--border); border:1px solid var(--border); }
.sg-score-badge.sg-na { color:var(--text-4); background:transparent; border:1px dashed var(--border); }
.sg-rows { display:flex; flex-direction:column; gap:8px; }
.sg-row { display:grid; grid-template-columns:1fr auto auto; gap:12px; align-items:center; font-size:12px; padding:4px 0; }
.sg-row-stub { color:var(--text-5); }
.sg-label { color:var(--text-3); font-family:'Space Mono',monospace; font-size:10px; letter-spacing:1px; text-transform:uppercase; }
.sg-val { font-family:'Space Mono',monospace; font-size:12px; color:var(--text-1); font-weight:500; text-align:right; min-width:80px; }
.sg-bar { display:inline-flex; gap:2px; align-items:center; }
.sg-bar-cell { display:inline-block; width:6px; height:8px;  background:var(--border); }
.sg-bar-cell-on { background:var(--apt-rose); }
.sg-card-insider .sg-bar-cell-on { background:#9B8CFF; }
.sg-bar-empty { font-family:'Space Mono',monospace; font-size:10px; color:var(--text-5); }
.sg-foot { margin-top:12px; padding-top:10px; border-top:1px dashed var(--border); font-size:10px; line-height:1.5; color:var(--text-4); }

/* Chart card per ticker (price + op margin, lazy-loaded on expand) */
.ch-card { margin-top:14px; padding:16px 18px; background:transparent; border:1px solid var(--border);  }
.ch-h { display:flex; align-items:baseline; justify-content:space-between; gap:14px; margin-bottom:14px; padding-bottom:10px; border-bottom:1px solid var(--border); flex-wrap:wrap; }
.ch-h-title { font-family:'Space Mono',monospace; font-size:10px; letter-spacing:2px; color:var(--text-3); text-transform:uppercase; }
.ch-tabs { display:flex; gap:4px; flex-wrap:wrap; }
.ch-tab { font-family:'Space Mono',monospace; font-size:9px; letter-spacing:1.5px; color:var(--text-3); background:transparent; border:1px solid var(--border);  padding:4px 10px; cursor:pointer; text-transform:uppercase; transition:color .15s, border-color .15s, background .15s; }
.ch-tab:hover { color:var(--text-1); border-color:var(--border-bright); }
.ch-tab.active { color:var(--text-1); background:var(--apt-rose); border-color:var(--apt-rose); }
.ch-grid { display:grid; grid-template-columns:1fr 1fr; gap:16px; }
@media (max-width:780px) { .ch-grid { grid-template-columns:1fr; } }
.ch-pane { display:flex; flex-direction:column; }
.ch-pane-h { font-family:'Space Grotesk',sans-serif; font-size:12px; font-weight:700; letter-spacing:0.02em; color:var(--text-2); margin-bottom:8px; padding-bottom:6px; border-bottom:1px solid var(--border); display:flex; justify-content:space-between; align-items:baseline; gap:10px; flex-wrap:wrap; }
.ch-pane-h-left { display:inline-flex; align-items:baseline; gap:10px; }
.ch-pane-price { font-family:'Space Mono',monospace; font-size:13px; font-weight:500; color:var(--text-1); letter-spacing:0; }
.ch-pane-meta { font-family:'Space Mono',monospace; font-size:9px; letter-spacing:1px; color:var(--text-4); text-transform:uppercase; }
.ch-pane-meta.ch-pos { color:#34D27A; }
.ch-pane-meta.ch-neg { color:var(--apt-red); }
.ch-canvas { width:100%; height:160px; display:block; }

/* News card per ticker (lazy-loaded on row expand) */
/* Thesis card. Sits above the score breakdown because the view is the point and
   the factors are the evidence for it. The falsifier is the only element wearing
   the brand accent: on this page the thing that could kill the thesis outranks
   the conclusion. */
.th-card { margin-top:14px; padding:16px 18px; border:1px solid var(--border); border-top:2px solid var(--text-1); }
.th-h { font-family:'Space Mono',monospace; font-size:10px; letter-spacing:2px; color:var(--text-3); text-transform:uppercase; margin-bottom:14px; padding-bottom:10px; border-bottom:1px solid var(--border); display:flex; align-items:baseline; gap:10px; flex-wrap:wrap; }
.th-h-date { color:var(--text-4); letter-spacing:1px; margin-left:auto; text-transform:none; }
.th-loading { font-size:9px; color:var(--text-5); text-transform:none; letter-spacing:1px; font-style:italic; }
.th-empty { font-size:12px; color:var(--text-3); line-height:1.6; }
.th-top { display:flex; align-items:baseline; gap:14px; flex-wrap:wrap; margin-bottom:12px; }
.th-dir { font-family:'Instrument Serif',Georgia,serif; font-size:26px; line-height:1; color:var(--text-1); }
.th-dir-noview { color:var(--text-3); }
.th-pips { display:inline-flex; gap:3px; align-items:center; }
.th-pip { width:10px; height:10px; border-radius:50%; border:1.5px solid var(--text-4); }
.th-pip-on { background:var(--apt-red); border-color:var(--apt-red); }
.th-nums { font-family:'Space Mono',monospace; font-size:11px; color:var(--text-3); display:flex; gap:0; flex-wrap:wrap; }
.th-nums span { padding-right:12px; margin-right:12px; border-right:1px solid var(--border); }
.th-nums span:last-child { border-right:0; margin-right:0; padding-right:0; }
.th-nums b { color:var(--text-1); font-weight:400; }
.th-claim { font-family:'Instrument Serif',Georgia,serif; font-size:19px; line-height:1.42; color:var(--text-1); margin:0 0 14px; max-width:62ch; }
.th-fals { border:1px solid var(--apt-red); border-left:3px solid var(--apt-red); padding:11px 14px; margin-bottom:14px; max-width:62ch; }
.th-fals-k { font-family:'Space Mono',monospace; font-size:9px; letter-spacing:2px; text-transform:uppercase; color:var(--apt-red-deep); display:block; margin-bottom:6px; }
.th-fals-t { font-size:13px; line-height:1.55; color:var(--text-2); }
.th-drift { border:1px solid var(--apt-amber); border-left:3px solid var(--apt-amber); padding:10px 13px; margin-bottom:14px; font-size:12px; line-height:1.55; color:var(--text-2); max-width:62ch; }
.th-drift b { color:var(--text-1); }
.th-cav { font-size:12px; color:var(--text-3); line-height:1.6; margin-bottom:14px; max-width:62ch; }
.th-cav-k { font-family:'Space Mono',monospace; font-size:9px; letter-spacing:2px; text-transform:uppercase; color:var(--text-4); display:block; margin-bottom:5px; }
.th-hist { border-top:1px solid var(--border); padding-top:11px; font-family:'Space Mono',monospace; font-size:10.5px; color:var(--text-3); }
.th-hist-row { display:flex; gap:10px; padding:3px 0; flex-wrap:wrap; }
.th-hist-row span:first-child { color:var(--text-4); min-width:82px; }
.th-note-link { font-family:'Space Mono',monospace; font-size:10px; color:var(--text-4); margin-top:10px; }
.th-note-link a { color:var(--text-3); }
.co-card { margin-top:14px; padding:16px 18px; border:1px solid var(--border); }
.co-h { font-family:'Space Mono',monospace; font-size:10px; letter-spacing:2px; color:var(--text-3); text-transform:uppercase; margin-bottom:14px; padding-bottom:10px; border-bottom:1px solid var(--border); display:flex; align-items:baseline; gap:10px; }
.co-h-src { color:var(--text-4); letter-spacing:1px; margin-left:auto; text-transform:none; }
.co-loading { font-size:9px; color:var(--text-5); text-transform:none; letter-spacing:1px; font-style:italic; }
.co-empty { font-size:12px; color:var(--text-3); line-height:1.6; max-width:62ch; }
.co-block { margin-bottom:18px; }
.co-block:last-child { margin-bottom:0; }
.co-k { font-family:'Space Mono',monospace; font-size:9px; letter-spacing:2px; text-transform:uppercase; color:var(--text-4); margin-bottom:8px; }
.co-note + .co-k, .co-chips + .co-k, .co-bar + .co-k { margin-top:16px; }
.co-note { font-size:11.5px; color:var(--text-3); line-height:1.55; margin-top:7px; max-width:62ch; }
.co-note a { color:var(--text-2); }
.co-chips { display:flex; flex-wrap:wrap; gap:6px; }
.co-chip { font-family:'Space Mono',monospace; font-size:11px; color:var(--text-2); border:1px solid var(--border-bright); padding:3px 9px; }
.co-bar { height:10px; background:var(--surface-3); border:1px solid var(--border); position:relative; }
.co-bar-fill { position:absolute; inset:0 auto 0 0; background:var(--apt-red); }
.co-srow { display:flex; align-items:center; gap:12px; padding:5px 0; flex-wrap:wrap; }
.co-srow-k { font-family:'Space Mono',monospace; font-size:10.5px; color:var(--text-3); min-width:124px; }
.co-spark { flex:0 0 auto; overflow:visible; }
.co-srow-v { font-family:'Space Mono',monospace; font-size:12px; color:var(--text-3); min-width:96px; font-variant-numeric:tabular-nums; }
.co-srow-v b { color:var(--text-1); font-weight:400; }
.co-srow-v i, .co-srow-from i { font-style:normal; color:var(--text-4); font-size:10px; }
.co-srow-from { font-family:'Space Mono',monospace; font-size:10.5px; color:var(--text-4); font-variant-numeric:tabular-nums; }
.co-files { display:flex; flex-direction:column; border-top:1px solid var(--border); }
.co-file { display:flex; align-items:baseline; gap:12px; padding:6px 0; border-bottom:1px solid var(--border); font-family:'Space Mono',monospace; font-size:11px; color:var(--text-3); text-decoration:none; }
.co-file:hover { color:var(--text-1); }
.co-file-d { color:var(--text-4); min-width:86px; }
.co-file-n { color:var(--text-2); flex:1 1 auto; }
.co-file-f { color:var(--text-4); min-width:44px; }
.co-file-c { color:var(--text-4); min-width:40px; text-align:right; font-variant-numeric:tabular-nums; }
@media (max-width:640px) { .co-srow-k { min-width:100%; } .co-srow-from { display:none; } }
.nws-card { margin-top:14px; padding:16px 18px; background:transparent; border:1px solid var(--border);  }
.nws-h { font-family:'Space Mono',monospace; font-size:10px; letter-spacing:2px; color:var(--text-3); text-transform:uppercase; margin-bottom:14px; padding-bottom:10px; border-bottom:1px solid var(--border); display:flex; align-items:baseline; gap:10px; }
.nws-loading { font-size:9px; color:var(--text-5); text-transform:none; letter-spacing:1px; font-style:italic; }
.nws-grid { display:grid; grid-template-columns:1fr 1fr 1fr; gap:18px; }
@media (max-width:780px) { .nws-grid { grid-template-columns:1fr; } }
.nws-col { display:flex; flex-direction:column; }
.nws-col-h { font-family:'Space Grotesk',sans-serif; font-size:13px; font-weight:700; letter-spacing:0.02em; color:var(--text-2); margin-bottom:10px; padding-bottom:8px; border-bottom:1px solid var(--border); display:flex; align-items:baseline; gap:8px; }
.nws-count { font-family:'Space Mono',monospace; font-size:9px; color:var(--apt-rose); letter-spacing:1px; }
.nws-item { display:block; padding:10px 0; border-top:1px solid var(--border); transition:padding-left .12s; text-decoration:none; }
.nws-item:first-of-type { border-top:none; padding-top:4px; }
.nws-item:hover { padding-left:6px; }
.nws-title { font-size:13px; line-height:1.4; color:var(--text-1); }
.nws-meta { font-family:'Space Mono',monospace; font-size:9px; letter-spacing:1px; color:var(--text-4); text-transform:uppercase; margin-top:5px; }
.nws-item:hover .nws-title { color:var(--apt-rose); }
.nws-empty { padding:14px 0; font-family:'Space Mono',monospace; font-size:10px; color:var(--text-5); text-transform:uppercase; text-align:center; }

/* Per-bucket sentiment header (Loughran-McDonald + VADER) */
.nws-sent-row { display:flex; gap:14px; padding:8px 10px; margin-bottom:8px; background:var(--bg-1); border:1px solid var(--border);  align-items:center; }
.nws-sent-cell { display:flex; align-items:baseline; gap:6px; flex:1; }
.nws-sent-label { font-family:'Space Mono',monospace; font-size:9px; letter-spacing:1.5px; color:var(--text-4); text-transform:uppercase; }
.nws-sent-val { font-family:'Space Mono',monospace; font-size:12px; font-weight:600; }
.nws-sent-pos { color:#34D27A; }
.nws-sent-neg { color:var(--apt-red); }
.nws-sent-neutral { color:var(--text-2); }
.nws-sent-na { color:var(--text-5); }

/* Stocks page top row: search + Index/Sector chips full-width above the wrap */
.stk-toprow { display:flex; flex-wrap:wrap; gap:10px 18px; align-items:center; padding:12px 16px; margin-bottom:12px; background:var(--surface-2); backdrop-filter:blur(20px); -webkit-backdrop-filter:blur(20px); border:1px solid var(--border); border-radius:14px; position:sticky; top:90px; z-index:4; }
.stk-toprow > .lib-search { flex:1 1 280px; min-width:220px; }

/* Stocks page sidebar layout: filters left, table right */
.stk-wrap { display:grid; grid-template-columns:300px 1fr; gap:20px; align-items:start; margin-top:6px; }
.stk-sidebar { position:sticky; top:210px; max-height:calc(100vh - 230px); overflow-y:auto; padding:18px 18px; background:var(--surface-1); backdrop-filter:blur(20px); -webkit-backdrop-filter:blur(20px); border:1px solid var(--border); border-radius:16px; display:flex; flex-direction:column; gap:14px; }
.stk-sidebar::-webkit-scrollbar { width:6px; }
.stk-sidebar::-webkit-scrollbar-thumb { background:var(--border-bright); border-radius:3px; }
.stk-sidebar-h { display:flex; align-items:baseline; justify-content:space-between; padding-bottom:10px; border-bottom:1px solid var(--border); font-family:'Space Grotesk',sans-serif; font-size:14px; font-weight:700; letter-spacing:0.04em; color:var(--text-1); text-transform:uppercase; }
.stk-sidebar-h .stk-filter-reset { padding:0; font-family:'Space Mono',monospace; font-size:9px; letter-spacing:1.5px; color:var(--text-3); background:transparent; border:none; cursor:pointer; text-transform:uppercase; }
.stk-sidebar-h .stk-filter-reset:hover { color:var(--apt-rose); }
.stk-main { min-width:0; }

/* Sidebar overrides for the existing filter HTML (drop toggle, always-visible panel, single-column inner stacking) */
.stk-sidebar .stk-filter-panel { display:flex; flex-direction:column; gap:8px; margin-top:0; padding:0; background:transparent; border:0; border-radius:0; }
.stk-sidebar .stk-filter-cols { display:flex; flex-direction:column; gap:14px; }
.stk-sidebar .stk-filter-col-h { font-family:'Space Mono',monospace; font-size:10px; letter-spacing:2px; color:var(--text-3); text-transform:uppercase; padding:8px 0; margin-bottom:6px; border-bottom:1px solid var(--border); display:flex; align-items:center; justify-content:space-between; cursor:pointer; list-style:none; user-select:none; }
.stk-sidebar .stk-filter-col-h::-webkit-details-marker { display:none; }
.stk-sidebar .stk-filter-col-h:hover { color:var(--text-1); }
.stk-section-caret { font-size:10px; color:var(--text-4); transition:transform .18s ease; }
.stk-sidebar details[open] > .stk-filter-col-h .stk-section-caret { transform:rotate(90deg); color:var(--apt-rose); }
.stk-sidebar .stk-filter-col-sub { font-family:'Space Mono',monospace; font-size:8px; letter-spacing:1px; color:var(--text-4); text-transform:uppercase; padding:2px 0 6px 0; display:block; }
.stk-sidebar .stk-filter-select { width:100%; padding:5px 6px; font-family:'Space Mono',monospace; font-size:10px; color:var(--text-1); background:var(--bg-1); border:1px solid var(--border); border-radius:6px; }
.stk-sidebar .stk-filter-select:focus { outline:none; border-color:var(--apt-rose); }

/* Saved Views */
.stk-views { padding:10px 0 12px 0; border-bottom:1px solid var(--border); margin-bottom:4px; }
.stk-views-h { font-family:'Space Mono',monospace; font-size:9px; letter-spacing:2px; color:var(--text-3); text-transform:uppercase; margin-bottom:8px; }
.stk-views-row { display:flex; gap:6px; margin-bottom:8px; }
.stk-views-input { flex:1; min-width:0; padding:5px 8px; font-family:'Space Mono',monospace; font-size:10px; color:var(--text-1); background:var(--bg-1); border:1px solid var(--border); border-radius:6px; }
.stk-views-input:focus { outline:none; border-color:var(--apt-rose); }
.stk-views-list { display:flex; flex-wrap:wrap; gap:4px; }
.stk-views-empty { font-size:9px; color:var(--text-5); font-style:italic; }
.stk-views-chip { display:inline-flex; align-items:stretch; background:var(--bg-1); border:1px solid var(--border); border-radius:999px; overflow:hidden; }
.stk-views-chip:hover { border-color:var(--apt-rose); }
.stk-views-load { padding:4px 10px; font-family:'Space Mono',monospace; font-size:9px; letter-spacing:1px; color:var(--text-2); background:transparent; border:0; cursor:pointer; text-transform:uppercase; }
.stk-views-load:hover { color:var(--text-1); }
.stk-views-del { padding:4px 8px; font-size:11px; line-height:1; color:var(--text-4); background:transparent; border:0; border-left:1px solid var(--border); cursor:pointer; }
.stk-views-del:hover { color:var(--apt-red); background:rgba(255,57,77,0.08); }
.stk-sidebar .stk-filter-row { display:grid; grid-template-columns:1fr 1fr; column-gap:6px; row-gap:4px; padding:5px 0; align-items:center; }
.stk-sidebar .stk-filter-row > .stk-filter-label { grid-column:1 / -1; font-size:9px; }
.stk-sidebar .stk-filter-row > input.stk-filter-input[data-bound="min"] { grid-column:1; }
.stk-sidebar .stk-filter-row > input.stk-filter-input[data-bound="max"] { grid-column:2; }
.stk-sidebar .stk-filter-row > .stk-filter-sep { display:none; }
.stk-sidebar .stk-filter-row > .stk-filter-hint { grid-column:1 / -1; font-size:8px; }
.stk-sidebar .stk-filter-row > .stk-filter-stat { grid-column:1 / -1; font-size:9px; padding-top:2px; }
.stk-overlay-sub { font-family:'Space Mono',monospace; font-size:9px; letter-spacing:1.5px; color:var(--apt-rose); text-transform:uppercase; padding:10px 0 4px 0; margin-top:4px; border-top:1px dashed var(--border); }
.stk-overlay-sub:first-of-type { border-top:0; padding-top:6px; margin-top:0; }

/* Data Hygiene section: collapsible, sits above Saved Views */
.stk-hygiene { padding:0 0 10px 0; margin-bottom:6px; border-bottom:1px solid var(--border); }
.stk-hygiene > summary.stk-filter-col-h { padding:8px 0 8px 0; }
.stk-cov-row { display:grid; grid-template-columns:1fr 56px auto; align-items:center; gap:8px; padding:4px 0; }
.stk-cov-label { font-family:'Space Mono',monospace; font-size:10px; letter-spacing:1px; color:var(--text-2); text-transform:uppercase; }
.stk-cov-input { padding:4px 6px; font-family:'Space Mono',monospace; font-size:11px; color:var(--text-1); background:var(--bg-1); border:1px solid var(--border); border-radius:6px; text-align:center; }
:root[data-theme="light"] .stk-cov-input { background:#FFFFFF; }
.stk-cov-input:focus { outline:none; border-color:var(--apt-rose); }
.stk-cov-of { font-family:'Space Mono',monospace; font-size:10px; color:var(--text-4); }
.stk-sidebar .stk-filter-row > .stk-filter-quicks { grid-column:1 / -1; margin-top:2px; }
.stk-sidebar .stk-quick { padding:4px 7px; font-size:9px; }
.stk-sidebar .stk-weight-row { grid-template-columns:62px 1fr 38px; column-gap:8px; }
.stk-sidebar .stk-weight-label { font-size:10px; }
.stk-sidebar .stk-weight-val { font-size:11px; }
.stk-sidebar .lib-chip { padding:5px 10px; font-size:9px; }
.stk-sidebar .lib-chip-label { font-size:8px; }
.stk-sidebar .lib-chips { gap:4px; }
.stk-sidebar .stk-weight-presets { padding-top:8px; margin-top:4px; }
.stk-sidebar .stk-weight-presets-label { font-size:8px; }
.stk-filter-toggle-count { font-family:'Space Mono',monospace; font-size:9px; letter-spacing:1.5px; color:var(--apt-rose); text-transform:uppercase; padding:2px 0; }

/* The screener scrolls the page itself. The block that used to live here pinned
   the body to 100vh with overflow:hidden so that only an inner pane moved, and
   styled a chassis (.lib-wide, .stk-wrap, .stk-sidebar, .stk-toprow, .stk-main)
   that the design port removed. All of it is deleted rather than overridden:
   an overridden viewport lock is one equal-specificity rule away from coming
   back, and when it does the page stops responding to the wheel while still
   answering scrollTop, which is close to undebuggable from the outside. */
body.page-stocks .picks-meta, body.page-stocks .footer { display:none; }
body.scr-page .stk-head { top:109px; }   /* clears the 56px bar + 53px toolbar */

@media (max-width:1080px) {
  .stk-wrap { grid-template-columns:1fr; }
  .stk-sidebar { position:static; max-height:none; overflow:visible; }
}
/* auto-fit, not a fixed four. Adding the Risk group made five cards, and a
   four-column track left the fifth stranded on a row of its own with four
   empty cells beside it. Sized so the full-width panel takes all five across
   and narrower viewports step down on their own, which is what the two fixed
   breakpoints underneath were doing by hand. */
.fp-grid { display:grid; grid-template-columns:repeat(auto-fit, minmax(170px, 1fr)); gap:14px; }
.fp-card { padding:14px 16px; background:transparent; border:1px solid var(--border);  }
.fp-card-h { font-family:'Space Grotesk',sans-serif; font-size:13px; font-weight:700; letter-spacing:0.02em; color:var(--text-1); margin-bottom:10px; padding-bottom:8px; border-bottom:1px solid var(--border); }
.fp-card-toggle { display:flex; align-items:center; justify-content:space-between; width:100%; background:transparent; border:0; cursor:pointer; color:var(--text-1); font-family:'Space Grotesk',sans-serif; font-size:13px; font-weight:700; letter-spacing:0.02em; text-align:left; padding:0 0 8px 0; margin-bottom:10px; border-bottom:1px solid var(--border); transition:color .15s; }
.fp-card-toggle:hover { color:var(--apt-rose); }
.fp-card-caret { font-size:10px; color:var(--text-4); transition:transform .18s ease, color .15s; }
.fp-card.fp-card-active .fp-card-caret { transform:rotate(90deg); color:var(--apt-rose); }
.fp-card.fp-card-active { border-color:var(--apt-rose); box-shadow:0 0 0 1px rgba(255,57,77,0.18); }

/* Standalone methodology card. Slides in below the 4 factor cards when one is clicked. */
.fp-meta-panel { margin-top:14px; }
.fp-meta-status { font-family:'Space Mono',monospace; font-size:10px; color:var(--text-4);
  border:1px solid var(--border); padding:1px 5px; white-space:nowrap; cursor:help; }
.fp-meta-formula { font-family:'Space Mono',monospace; font-size:10.5px; color:var(--text-2);
  background:var(--surface-1); padding:1px 4px; margin-right:5px; }
.fp-meta-refresh { color:var(--text-4); }
.fp-meta-card { padding:16px 18px; background:transparent; border:1px solid var(--border);  border-left-width:3px; }
.fp-meta-growth   { border-left-color:#34D27A; }
.fp-meta-value    { border-left-color:#9B8CFF; }
.fp-meta-momentum { border-left-color:#FFB347; }
.fp-meta-quality  { border-left-color:#67B7FF; }
.fp-meta-card-h { display:flex; align-items:baseline; gap:14px; margin-bottom:14px; padding-bottom:10px; border-bottom:1px solid var(--border); }
.fp-meta-card-eyebrow { font-family:'Space Mono',monospace; font-size:9px; letter-spacing:2px; color:var(--text-4); text-transform:uppercase; }
.fp-meta-card-title { font-family:'Space Grotesk',sans-serif; font-size:13px; font-weight:700; color:var(--text-1); letter-spacing:0.02em; flex:1; }
.fp-meta-card-close { background:transparent; border:0; color:var(--text-3); font-size:18px; line-height:1; cursor:pointer; padding:0 4px; transition:color .15s; }
.fp-meta-card-close:hover { color:var(--apt-rose); }
.fp-meta-table { width:100%; border-collapse:collapse; font-size:11px; }
.fp-meta-table th { font-family:'Space Mono',monospace; font-size:9px; letter-spacing:1px; color:var(--text-4); text-transform:uppercase; text-align:left; padding:4px 8px 6px 0; border-bottom:1px solid var(--border); font-weight:500; }
.fp-meta-table td { padding:6px 8px 6px 0; border-bottom:1px solid var(--border); vertical-align:top; line-height:1.4; }
.fp-meta-table tr:last-child td { border-bottom:0; }
.fp-meta-label { color:var(--text-1); font-weight:500; white-space:nowrap; }
.fp-meta-src { font-family:'Space Mono',monospace; font-size:10px; color:var(--text-3); white-space:nowrap; }
.fp-meta-src[data-src="edgar"] { color:#67B7FF; }
.fp-meta-src[data-src="yfinance"] { color:#9B8CFF; }
.fp-meta-src[data-src="derived"] { color:var(--text-3); }
.fp-meta-asof { font-family:'Space Mono',monospace; font-size:10px; color:var(--text-3); white-space:nowrap; }
.fp-meta-method { color:var(--text-2); font-size:11px; }
.fp-row { display:flex; justify-content:space-between; align-items:baseline; padding:5px 0; font-size:12px; }
.fp-label { color:var(--text-3); font-family:'Space Mono',monospace; font-size:10px; letter-spacing:1px; text-transform:uppercase; }
.fp-val { font-family:'Space Mono',monospace; font-size:12px; color:var(--text-1); font-weight:500; }
.fp-row-na .fp-val { color:var(--text-5); }
@media (max-width:780px) {
  .stk-head, .stk-row { grid-template-columns:55px 1fr 70px 60px 60px; gap:8px; padding:12px 14px; }
  .stk-th[data-sort="sector"], .stk-row .stk-sector { display:none; }
  .stk-th[data-sort="earnings_date"], .stk-row .stk-date:not(.stk-date-dim) { display:none; }
  .stk-th[data-sort="last_updated"], .stk-row .stk-date.stk-date-dim { display:none; }
  .stk-detail { padding:12px 14px 18px; }
  .stk-filter-row { grid-template-columns:1fr 1fr; gap:6px 10px; }
}

.footer { max-width:1200px; margin:64px auto 0; padding:32px 24px 48px; border-top:1px solid var(--border); display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:14px; }
.footer .brand-foot { font-family:'Space Grotesk',sans-serif; font-size:12px; font-weight:800; letter-spacing:5px; color:var(--text-3); text-transform:uppercase; }
.footer .meta { font-family:'Space Mono',monospace; font-size:11px; color:var(--text-4); letter-spacing:1px; }

@media (max-width:760px) {
  h1.hero-title { font-size:48px; }
  .feat-body { font-size:16px; }
  .featured-card { padding:32px 24px; }
  .feat-grid { grid-template-columns:1fr; }
}
/* --- research page -------------------------------------------------------
   Its own vocabulary (rs-*) rather than reusing th-*, because the thesis card
   on the screener is a summary inside a row and these are the primary objects
   on their own page. Both draw from the same tokens, so they read as siblings. */
body.page-research .scr-page-body { max-width:1060px; margin:0 auto; padding:0 22px 72px; }
.rs-head { padding:44px 0 26px; border-bottom:1px solid var(--border); }
.rs-h1 { font-family:'Instrument Serif',Georgia,serif; font-size:54px; line-height:1; color:var(--text-1); font-weight:400; }
.rs-lede { margin-top:16px; font-size:14.5px; line-height:1.65; color:var(--text-3); max-width:66ch; }
.rs-h2 { font-family:'Space Mono',monospace; font-size:10px; letter-spacing:2.5px; text-transform:uppercase; color:var(--text-4); font-weight:400; }
.rs-h2row { display:flex; align-items:baseline; gap:12px; margin-bottom:14px; }
.rs-count { font-family:'Space Mono',monospace; font-size:10px; color:var(--text-5); letter-spacing:1px; margin-left:auto; }
.rs-k { font-family:'Space Mono',monospace; font-size:9px; letter-spacing:2px; text-transform:uppercase; color:var(--text-4); display:block; margin-bottom:6px; }
.rs-note { font-size:12.5px; line-height:1.65; color:var(--text-3); max-width:66ch; margin-top:12px; }
.rs-note b { color:var(--text-2); font-weight:400; }
.rs-note-loud { border-left:2px solid var(--apt-amber); padding-left:12px; color:var(--text-2); }
.rs-empty { font-size:13px; color:var(--text-3); padding:26px 0; }

.rs-record { padding:30px 0; border-bottom:1px solid var(--border); }
.rs-tiles { display:grid; grid-template-columns:repeat(auto-fit,minmax(132px,1fr)); gap:1px; background:var(--border); border:1px solid var(--border); margin:14px 0 20px; }
.rs-tile { background:var(--bg-base); padding:16px 18px; display:flex; flex-direction:column; gap:5px; }
.rs-tile-v { font-family:'Instrument Serif',Georgia,serif; font-size:34px; line-height:1; color:var(--text-1); font-variant-numeric:tabular-nums; }
.rs-tile-k { font-family:'Space Mono',monospace; font-size:9px; letter-spacing:1.5px; text-transform:uppercase; color:var(--text-4); }
.rs-tiers { width:100%; border-collapse:collapse; font-family:'Space Mono',monospace; font-size:11.5px; font-variant-numeric:tabular-nums; }
.rs-tiers th { text-align:left; font-weight:400; font-size:9px; letter-spacing:1.5px; text-transform:uppercase; color:var(--text-4); padding:7px 10px 7px 0; border-bottom:1px solid var(--border); }
.rs-tiers td { padding:7px 10px 7px 0; border-bottom:1px solid var(--border); color:var(--text-2); }
.rs-td-na { color:var(--text-5); }

.rs-views { padding:30px 0 0; }
.rs-controls { display:flex; flex-wrap:wrap; gap:22px; align-items:flex-end; padding-bottom:20px; border-bottom:1px solid var(--border); }
.rs-group { display:flex; flex-direction:column; }
.rs-group .rs-k { margin-bottom:7px; }
.rs-chips { display:flex; flex-wrap:wrap; gap:5px; }
.rs-chip { font-family:'Space Mono',monospace; font-size:11px; color:var(--text-3); background:transparent; border:1px solid var(--border-bright); padding:5px 11px; cursor:pointer; }
.rs-chip:hover { color:var(--text-1); }
.rs-chip.on { color:var(--text-1); border-color:var(--apt-red); }
.rs-chip i { font-style:normal; color:var(--text-5); margin-left:4px; }
.rs-select { font-family:'Space Mono',monospace; font-size:11px; color:var(--text-2); background:var(--bg-base); border:1px solid var(--border-bright); padding:5px 9px; }

.rs-list { display:flex; flex-direction:column; gap:1px; background:var(--border); border:1px solid var(--border); border-top:0; }
.rs-card { background:var(--bg-base); padding:22px 24px; }
.rs-top { display:flex; align-items:baseline; gap:11px; flex-wrap:wrap; margin-bottom:12px; }
.rs-tk { font-family:'Space Mono',monospace; font-size:15px; letter-spacing:1px; color:var(--text-1); text-decoration:none; border-bottom:1px solid var(--border-bright); }
.rs-tk:hover { border-bottom-color:var(--apt-red); }
.rs-nm { font-size:12.5px; color:var(--text-3); }
.rs-status { font-family:'Space Mono',monospace; font-size:9px; letter-spacing:1.5px; text-transform:uppercase; padding:3px 8px; border:1px solid var(--border-bright); color:var(--text-4); margin-left:auto; }
.rs-status-due { color:var(--apt-amber); border-color:var(--apt-amber); }
.rs-status-open { color:var(--text-2); border-color:var(--text-4); }
.rs-status-graded { color:var(--apt-red); border-color:var(--apt-red); }
.rs-dirline { display:flex; align-items:center; gap:16px; flex-wrap:wrap; margin-bottom:12px; }
.rs-dir { font-family:'Instrument Serif',Georgia,serif; font-size:28px; line-height:1; color:var(--text-1); }
.rs-dir-no-view, .rs-dir-watch { color:var(--text-4); }
.rs-pips { display:inline-flex; gap:3px; align-items:center; }
.rs-pip { width:9px; height:9px; border-radius:50%; border:1.5px solid var(--text-5); }
.rs-pip-on { background:var(--apt-red); border-color:var(--apt-red); }
.rs-comps { display:flex; gap:6px; flex-wrap:wrap; }
.rs-comp { font-family:'Space Mono',monospace; font-size:9.5px; letter-spacing:0.5px; color:var(--text-5); border:1px solid var(--border); padding:3px 7px; }
.rs-comp b { color:var(--text-4); font-weight:400; }
.rs-comp i { font-style:normal; color:var(--text-5); }
.rs-comp-on { color:var(--text-3); border-color:var(--border-bright); }
.rs-comp-on b { color:var(--text-1); }
.rs-claim { font-family:'Instrument Serif',Georgia,serif; font-size:19px; line-height:1.45; color:var(--text-1); max-width:62ch; margin-bottom:14px; }
.rs-claim-none { color:var(--text-4); font-size:16px; }
.rs-drift { border:1px solid var(--apt-amber); border-left:3px solid var(--apt-amber); padding:9px 12px; margin-bottom:13px; font-size:12px; line-height:1.55; color:var(--text-2); max-width:62ch; }
.rs-nums { font-family:'Space Mono',monospace; font-size:11px; color:var(--text-3); display:flex; flex-wrap:wrap; margin-bottom:14px; font-variant-numeric:tabular-nums; }
.rs-nums span { padding-right:12px; margin-right:12px; border-right:1px solid var(--border); }
.rs-nums span:last-child { border-right:0; margin-right:0; padding-right:0; }
.rs-nums b { color:var(--text-1); font-weight:400; }
.rs-move { font-style:normal; }
.rs-up { color:var(--apt-red); }
.rs-down { color:var(--text-4); }
.rs-fals { border:1px solid var(--apt-red); border-left:3px solid var(--apt-red); padding:11px 14px; margin-bottom:14px; max-width:62ch; }
.rs-fals .rs-k { color:var(--apt-red-deep); margin-bottom:5px; }
.rs-fals div { font-size:12.5px; line-height:1.55; color:var(--text-2); }
.rs-screen { margin-bottom:14px; }
.rs-sleeves { display:flex; gap:16px; flex-wrap:wrap; align-items:center; }
.rs-sl { display:inline-flex; align-items:center; gap:7px; font-family:'Space Mono',monospace; font-size:10px; color:var(--text-4); }
.rs-sl i { font-style:normal; color:var(--text-4); width:10px; }
.rs-sl b { color:var(--text-2); font-weight:400; font-variant-numeric:tabular-nums; min-width:38px; text-align:right; }
.rs-sl-track { width:88px; height:7px; background:var(--surface-3); border:1px solid var(--border); position:relative; }
.rs-sl-zero { position:absolute; top:-2px; bottom:-2px; left:50%; width:1px; background:var(--text-5); }
.rs-sl-fill { position:absolute; top:0; bottom:0; background:var(--apt-red); }
.rs-sl-neg { background:var(--text-4); }
.rs-links { display:flex; align-items:baseline; gap:14px; flex-wrap:wrap; padding-top:12px; border-top:1px solid var(--border); font-family:'Space Mono',monospace; font-size:10px; }
.rs-links a { color:var(--text-2); }
.rs-meta { color:var(--text-5); margin-left:auto; letter-spacing:0.5px; }
@media (max-width:700px) {
  .rs-h1 { font-size:38px; }
  .rs-card { padding:18px 15px; }
  .rs-status { margin-left:0; }
  .rs-meta { margin-left:0; width:100%; }
}
"""


STORIES_JS_TEMPLATE = """
(function() {
  const ALL_STORIES = __ALL_STORIES_JSON__;
  const SECTIONS = __SECTIONS_JSON__;
  const listEl = document.getElementById('lib-list');
  const searchEl = document.getElementById('lib-search');
  const clearEl = document.getElementById('lib-clear');
  const chipsEl = document.getElementById('lib-chips');
  const countEl = document.getElementById('lib-count');
  if (!listEl) return;

  SECTIONS.forEach(name => {
    const c = document.createElement('span');
    c.className = 'lib-chip';
    c.dataset.section = name;
    c.textContent = name;
    chipsEl.appendChild(c);
  });

  let activeSection = '';
  let query = '';

  function escapeHtml(s) {
    return String(s == null ? '' : s)
      .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
  }

  function fmt(date) {
    if (!date) return '';
    try {
      const d = new Date(date + 'T12:00:00');
      return d.toLocaleDateString('en-US', { month:'short', day:'numeric' }).toUpperCase();
    } catch (e) { return date; }
  }

  function render() {
    const q = query.toLowerCase();
    const filtered = ALL_STORIES.filter(s => {
      if (activeSection && s.section !== activeSection) return false;
      if (!q) return true;
      return ((s.headline||'')+' '+(s.summary||'')+' '+(s.source||'')+' '+(s.section||'')).toLowerCase().includes(q);
    });
    countEl.textContent = filtered.length === ALL_STORIES.length
      ? String(ALL_STORIES.length).padStart(2,'0') + ' stories'
      : String(filtered.length).padStart(2,'0') + ' of ' + String(ALL_STORIES.length).padStart(2,'0') + ' stories';
    if (filtered.length === 0) {
      listEl.innerHTML = '<div class="empty-state">No stories match. Adjust filters or clear the search.</div>';
      return;
    }
    listEl.innerHTML = filtered.map(s => {
      const link = escapeHtml(s.link || s.brief_url || '#');
      return '<a class="lib-item" href="'+link+'" target="_blank" rel="noopener">'
        + '<span class="li-section">'+escapeHtml(s.section||'')+'</span>'
        + '<span><span class="li-headline">'+escapeHtml(s.headline||'')+'</span>'
        +   '<span class="li-meta">'+fmt(s.date)+' &middot; '+escapeHtml((s.edition||'').toUpperCase())+'</span></span>'
        + '<span class="li-src">'+escapeHtml(s.source||'')+'</span>'
      + '</a>';
    }).join('');
  }

  searchEl.addEventListener('input', () => {
    query = searchEl.value.trim();
    clearEl.hidden = !query;
    render();
  });
  clearEl.addEventListener('click', () => {
    searchEl.value = ''; query = ''; clearEl.hidden = true; searchEl.focus(); render();
  });
  chipsEl.addEventListener('click', e => {
    const chip = e.target.closest('.lib-chip');
    if (!chip) return;
    activeSection = chip.dataset.section || '';
    chipsEl.querySelectorAll('.lib-chip').forEach(c => c.classList.toggle('active', c === chip));
    render();
  });

  render();
})();
"""


RESEARCH_JS_TEMPLATE = """
(function() {
  // The cards are already in the DOM, server-rendered. This only hides and
  // reorders them, so the page is readable with scripting off.
  var list = document.getElementById('rs-list');
  if (!list) return;
  var cards = Array.prototype.slice.call(list.querySelectorAll('.rs-card'));
  var state = { dir: 'all', status: 'all', sort: 'status' };

  function count(sel, val) {
    return cards.filter(function(c) { return c.dataset[sel] === val; }).length;
  }

  var SORTS = {
    status:   function(a, b) { return (ORDER[a.dataset.status] - ORDER[b.dataset.status])
                                      || b.dataset.written.localeCompare(a.dataset.written); },
    written:  function(a, b) { return b.dataset.written.localeCompare(a.dataset.written); },
    review:   function(a, b) { return (a.dataset.review || '9999').localeCompare(b.dataset.review || '9999'); },
    convict:  function(a, b) { return (+b.dataset.conv) - (+a.dataset.conv); },
    move:     function(a, b) { return (+b.dataset.move) - (+a.dataset.move); }
  };
  var ORDER = { due: 0, open: 1, watching: 2, graded: 3 };

  function apply() {
    var shown = 0;
    cards.forEach(function(c) {
      var ok = (state.dir === 'all' || c.dataset.dir === state.dir)
            && (state.status === 'all' || c.dataset.status === state.status);
      c.hidden = !ok;
      if (ok) shown++;
    });
    var sorted = cards.slice().sort(SORTS[state.sort] || SORTS.status);
    sorted.forEach(function(c) { list.appendChild(c); });
    var n = document.getElementById('rs-count');
    if (n) n.textContent = shown + (shown === 1 ? ' name' : ' names');
    var empty = document.getElementById('rs-noresult');
    if (empty) empty.hidden = shown > 0;
  }

  document.querySelectorAll('[data-filter]').forEach(function(btn) {
    btn.addEventListener('click', function() {
      var group = btn.dataset.filter, val = btn.dataset.value;
      state[group] = val;
      document.querySelectorAll('[data-filter="' + group + '"]').forEach(function(b) {
        b.classList.toggle('on', b === btn);
        b.setAttribute('aria-pressed', b === btn ? 'true' : 'false');
      });
      apply();
    });
  });
  var sortEl = document.getElementById('rs-sort');
  if (sortEl) sortEl.addEventListener('change', function() {
    state.sort = sortEl.value; apply();
  });
  apply();
})();
"""


STOCKS_JS_TEMPLATE = """
(function() {
  // The universe is fetched rather than inlined. Inlined, it made stocks.html
  // 5.8 MB of which ~93% was data the parser had to chew through before first
  // paint, on every load, uncacheable separately from the markup.
  let ALL = [];
  const SECTORS = __SECTORS_JSON__;
  const INDEXES = __INDEXES_JSON__;
  const DATA_URL = __DATA_URL__;
  // Order of the positional `pct` array on each stock, matching SCORE_FIELDS in
  // lambda_function.py. Index into it rather than looking up by name.
  const PCT_ORDER = __PCT_FIELDS_JSON__;
  // Generated from FIELD_METHODS in lambda_function.py. The methodology panel
  // reads this rather than the strings sitting beside each row below, so a
  // formula and its description are the same object.
  const FIELD_METHODS = __FIELD_METHODS_JSON__;
  const MIN_COHORT = __MIN_COHORT_JSON__;
  const FIELD_STATUS = __FIELD_STATUS_JSON__;
  const REFRESH_CLASSES = __REFRESH_CLASSES_JSON__;
  const pctOf = (s, field) => {
    if (!s.pct) return null;
    const i = PCT_ORDER.indexOf(field);
    return i < 0 ? null : s.pct[i];
  };
  const listEl = document.getElementById('stk-list');
  const searchEl = document.getElementById('stk-search');
  const clearEl = document.getElementById('stk-clear');
  const sectorChipsEl = document.getElementById('stk-sector-chips');
  const indexChipsEl = document.getElementById('stk-index-chips');
  const countEl = document.getElementById('stk-count');
  if (!listEl) return;

  SECTORS.forEach(name => {
    const c = document.createElement('span');
    c.className = 'lib-chip';
    c.dataset.sector = name;
    c.textContent = name;
    sectorChipsEl.appendChild(c);
  });
  INDEXES.forEach(name => {
    const c = document.createElement('span');
    c.className = 'lib-chip';
    c.dataset.index = name;
    c.textContent = name;
    indexChipsEl.appendChild(c);
  });

  let activeSector = '';
  let activeIndex = '';
  let query = '';
  let sortKey = 'market_cap';
  let sortDir = -1;

  // Range filter state. Values are stored in DATA UNITS (decimal fractions for
  // percent fields, raw dollars for market cap). UI inputs use HUMAN UNITS
  // (e.g. "10" for 10%, "5B" for $5B); parseFilterInput translates.
  // Percent-coded fields are listed here so we can convert correctly.
  // change_pct is deliberately absent: it is the one field stored already in
  // percent (-1.17 means -1.17%), so scaling it by 100 here would have labelled
  // a colour legend -117% and read a filter of "2" as 0.02 against a value of
  // -1.17. Everything in this set is stored as a fraction.
  const PCT_FIELDS = new Set([
    'revenue_growth_yoy', 'high52w_proximity', 'roe_ttm', 'fcf_yield',
    'eps_growth_yoy', 'return_1m', 'return_12_2',
    'rel_strength_sp500', 'volume_trend',
    'gross_margin', 'operating_margin', 'gross_margin_trend',
    'revenue_acceleration', 'fcf_growth_yoy', 'accruals_ratio',
    'inst_ownership', 'insider_ownership',
    'volatility_1y', 'max_drawdown_1y',
  ]);
  // Market-cap-coded fields use 1B / 300M / 5T suffixes
  const CAP_FIELDS = new Set(['market_cap', 'volume', 'insider_net_buy_90d']);

  const TIER_RANGES = {
    micro: { min: 0,            max: 300e6 },
    small: { min: 300e6,        max: 2e9 },
    mid:   { min: 2e9,          max: 10e9 },
    large: { min: 10e9,         max: 200e9 },
    mega:  { min: 200e9,        max: null },
  };

  const filters = {};      // {field: {min, max}} in DATA UNITS
  let onlyEnriched = false;

  function parseFilterInput(raw, field) {
    if (raw == null) return null;
    let s = String(raw).trim().toUpperCase();
    // Thousands separators, spaces and a leading currency symbol are all things
    // a reader types, and parseFloat stops dead at the first of them. The field
    // suggests "10,000" itself, so refusing to read it back was indefensible.
    s = s.replace(/[$, ]/g, '');
    if (!s) return null;
    let mult = 1;
    if (s.endsWith('T'))      { mult = 1e12; s = s.slice(0, -1); }
    else if (s.endsWith('B')) { mult = 1e9;  s = s.slice(0, -1); }
    else if (s.endsWith('M')) { mult = 1e6;  s = s.slice(0, -1); }
    else if (s.endsWith('K')) { mult = 1e3;  s = s.slice(0, -1); }
    else if (s.endsWith('%')) { mult = 0.01; s = s.slice(0, -1); }
    const n = parseFloat(s);
    if (isNaN(n)) return null;
    let val = n * mult;
    // For percent-coded fields, treat bare numbers as percent (10 -> 0.10)
    if (PCT_FIELDS.has(field) && mult === 1) val = val / 100;
    // For money fields, a bare number is millions, which is the unit the labels
    // now state. Anything carrying an explicit suffix already means what it says.
    else if (CAP_FIELDS.has(field) && mult === 1) val = val * 1e6;
    return val;
  }

  function activeFilterCount() {
    let c = 0;
    for (const f of Object.values(filters)) {
      if (f.min != null) c++;
      if (f.max != null) c++;
    }
    if (onlyEnriched) c++;
    if (typeof benfordFilter !== 'undefined' && benfordFilter) c++;
    if (typeof coverageMin !== 'undefined') {
      for (const dim of ['Growth','Value','Momentum','Quality']) {
        if (coverageMin[dim] > 0) c++;
      }
    }
    return c;
  }

  // Benford 1st-digit fit gate (Overlays section). Categorical, not a range.
  let benfordFilter = '';

  // Data Hygiene: minimum non-null factor count required per dimension.
  // 0 = no filter; up to 5 = require all five factors in that dimension to be present.
  const coverageMin = { Growth: 0, Value: 0, Momentum: 0, Quality: 0 };

  function dimensionCoverage(s, dim) {
    // Counts non-null, finite values across the SCORE_GROUPS fields for this dimension.
    const fields = (SCORE_GROUPS[dim] && SCORE_GROUPS[dim].fields) || [];
    let n = 0;
    for (const f of fields) {
      const v = s[f];
      if (v != null && isFinite(v)) n += 1;
    }
    return n;
  }

  function passesFilters(s) {
    if (onlyEnriched && s.market_cap == null) return false;
    for (const [field, range] of Object.entries(filters)) {
      const v = s[field];
      if (range.min != null) {
        if (v == null || v < range.min) return false;
      }
      if (range.max != null) {
        if (v == null || v > range.max) return false;
      }
    }
    if (benfordFilter) {
      const fit = (s.benford && s.benford.fit) || '';
      if (benfordFilter === 'good'  && fit !== 'good') return false;
      if (benfordFilter === 'fair'  && fit !== 'fair' && fit !== 'good') return false;
      if (benfordFilter === 'poor'  && fit !== 'poor') return false;
    }
    for (const dim of ['Growth', 'Value', 'Momentum', 'Quality']) {
      if (coverageMin[dim] > 0 && dimensionCoverage(s, dim) < coverageMin[dim]) return false;
    }
    return true;
  }

  function escapeHtml(s) {
    return String(s == null ? '' : s)
      .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
  }
  function fmtCap(n) {
    if (n == null || isNaN(n)) return '—';
    if (n >= 1e12) return (n/1e12).toFixed(2) + 'T';
    if (n >= 1e9)  return (n/1e9).toFixed(2)  + 'B';
    if (n >= 1e6)  return (n/1e6).toFixed(0)  + 'M';
    return String(n);
  }
  function fmtPct(n) {
    if (n == null || isNaN(n)) return '—';
    const sign = n >= 0 ? '+' : '';
    return sign + Number(n).toFixed(2) + '%';
  }
  function fmtNum(n, d) {
    if (n == null || isNaN(n)) return '—';
    return Number(n).toFixed(d == null ? 0 : d);
  }
  function fmtDate(iso) {
    if (!iso) return '—';
    try {
      const d = new Date(isoDatePart(iso) + 'T12:00:00Z');
      if (isNaN(d.getTime())) return '—';
      return d.toLocaleDateString('en-US', { month:'short', day:'numeric', timeZone:'UTC' }).toUpperCase();
    } catch (e) { return '—'; }
  }
  // Accepts either a bare date or a full ISO timestamp. Appending a time to a
  // string that already carries one yields "...+00:00T12:00:00Z", which parses
  // to NaN, which rendered as an em dash on every row that used a real
  // timestamp rather than a date.
  function isoDatePart(v) {
    const s = String(v || '');
    const t = s.indexOf('T');
    return t > 0 ? s.slice(0, t) : s;
  }
  function fmtDateMDY(iso) {
    if (!iso) return '—';
    try {
      const d = new Date(isoDatePart(iso) + 'T12:00:00Z');
      if (isNaN(d.getTime())) return '—';
      const mm = String(d.getUTCMonth() + 1).padStart(2, '0');
      const dd = String(d.getUTCDate()).padStart(2, '0');
      const yy = String(d.getUTCFullYear()).slice(-2);
      return mm + '/' + dd + '/' + yy;
    } catch (e) { return '—'; }
  }
  function fmtPctRaw(n, d) {
    // Decimal fraction (0.083) -> "8.3%". For factor values stored as decimals.
    if (n == null || isNaN(n)) return '—';
    return (Number(n) * 100).toFixed(d == null ? 1 : d) + '%';
  }
  function fmtRatio(n, d) {
    // Already a ratio (e.g. P/B 1.1). Just round.
    if (n == null || isNaN(n)) return '—';
    return Number(n).toFixed(d == null ? 2 : d);
  }

  // Factor groups for the expand panel: 5/5/5/5 layout matching Dark Matter playbook.
  // Sources: yfinance for Tier-1/2 fields, SEC EDGAR for the 5 quarterly-trend fields
  // (Revenue Acceleration, Gross Margin Trend, FCF Growth YoY, Earnings Consistency,
  // Op Margin Stability). Each row carries source + methodology metadata so the card
  // can reveal where each number comes from.
  const FACTOR_GROUPS = [
    {
      title: 'Growth',
      rows: [
        { label: 'Revenue Growth YoY',   key: 'revenue_growth_yoy',   type: 'pct',   source: 'yfinance', method: 'TTM revenue vs prior TTM. Yahoo .info["revenueGrowth"].' },
        { label: 'EPS Growth YoY (GAAP)', key: 'eps_growth_yoy',      type: 'pct',   source: 'yfinance', method: 'TTM EPS vs prior TTM. Yahoo .info["earningsGrowth"].' },
        { label: 'Revenue Acceleration', key: 'revenue_acceleration', type: 'pct',   source: 'edgar',    method: 'Change in YoY growth quarter-over-quarter. (Q[n] vs Q[n-4]) growth minus (Q[n-1] vs Q[n-5]) growth, from XBRL Revenues.' },
        { label: 'Gross Margin Trend',   key: 'gross_margin_trend',   type: 'pct',   source: 'edgar',    method: 'Current quarter (GrossProfit / Revenues) minus same quarter prior year. Reflects most recent 10-Q.' },
        { label: 'FCF Growth YoY',       key: 'fcf_growth_yoy',       type: 'pct',   source: 'edgar',    method: 'TTM free cash flow current vs prior TTM. FCF = NetCashProvidedByOperatingActivities minus CapEx, summed over last 4 quarters.' },
      ],
    },
    {
      title: 'Value',
      rows: [
        { label: 'P/E (Trailing)', key: 'pe',         type: 'ratio', source: 'yfinance', method: 'Price / TTM EPS. Yahoo .info["trailingPE"].' },
        { label: 'EV/EBITDA',      key: 'ev_ebitda',  type: 'ratio', source: 'yfinance', method: 'Enterprise value / TTM EBITDA. Yahoo .info["enterpriseToEbitda"].' },
        { label: 'EV/Revenue',     key: 'ev_revenue', type: 'ratio', source: 'yfinance', method: 'Enterprise value / TTM revenue. Yahoo .info["enterpriseToRevenue"].' },
        { label: 'Price/Book',     key: 'price_book', type: 'ratio', source: 'yfinance', method: 'Price / book value per share. Yahoo .info["priceToBook"].' },
        { label: 'FCF Yield',      key: 'fcf_yield',  type: 'pct',   source: 'yfinance', method: 'TTM free cash flow / market cap. Computed from Yahoo .info["freeCashflow"] and marketCap.' },
      ],
    },
    {
      title: 'Momentum',
      rows: [
        { label: '12-2 Month Return',   key: 'return_12_2',        type: 'pct', source: 'derived',  method: 'Close 21 trading days ago against the close 252 trading days ago, from the stored daily history. Skipping the most recent month is what makes it 12-2 (Jegadeesh-Titman); needs 200 trading days.' },
        { label: '1-Month Return',      key: 'return_1m',          type: 'pct', source: 'derived',  method: 'Latest close against the close 21 trading days earlier, from the stored daily history.' },
        { label: '52W High Proximity',  key: 'high52w_proximity',  type: 'pct', source: 'derived',  method: '(Price minus 52W high) / 52W high. Always less than or equal to zero.' },
        { label: 'Rel Strength vs S&P', key: 'rel_strength_sp500', type: 'pct', source: 'derived',  method: 'Stock 12-2 return minus SPY 12-2 return.' },
        { label: 'Volume Trend',        key: 'volume_trend',       type: 'pct', source: 'derived',  method: 'Recent average volume divided by longer-term average volume, minus 1.' },
      ],
    },
    {
      title: 'Quality',
      rows: [
        { label: 'ROE (TTM)',           key: 'roe_ttm',             type: 'pct',   source: 'yfinance', method: 'TTM net income / shareholders equity. Yahoo .info["returnOnEquity"].' },
        { label: 'Earnings Consistency', key: 'earnings_consistency', type: 'ratio', source: 'edgar',   method: '1 / (1 + coefficient of variation) of last 8 quarters EPS. Range 0 to 1, higher means steadier.' },
        { label: 'Net Debt/EBITDA',     key: 'net_debt_ebitda',     type: 'ratio', source: 'yfinance', method: '(Total debt minus cash) / TTM EBITDA. Lower is better; negative means net cash.' },
        { label: 'Op Margin Stability', key: 'op_margin_stability', type: 'ratio', source: 'edgar',    method: 'Standard deviation of quarterly operating margins over last 8 quarters. Lower means more stable.' },
        { label: 'Accruals Ratio',      key: 'accruals_ratio',      type: 'pct',   source: 'edgar',    method: 'Sloan accruals: (TTM net income minus TTM operating cash flow) / average total assets, from XBRL. High accruals mean earnings are not backed by cash.' },
      ],
    },
    {
      title: 'Risk',
      rows: [
        { label: 'Volatility (1y)',     key: 'volatility_1y',       type: 'pct',   source: 'derived',  method: 'Annualized standard deviation of daily returns over the stored year of closes: sd(daily) x sqrt(252).' },
        { label: 'Beta vs S&P 500',     key: 'beta_1y',             type: 'ratio', source: 'derived',  method: 'Slope of daily returns against the S&P 500 (^GSPC), matched on the days both traded. 1.00 moves with the index; below zero means it moved against it over this window.' },
        { label: 'Sharpe (1y)',         key: 'sharpe_1y',           type: 'ratio', source: 'derived',  method: 'Mean daily return in excess of the 13-week Treasury bill (^IRX), over its own standard deviation, annualized by sqrt(252).' },
        { label: 'Max Drawdown (1y)',   key: 'max_drawdown_1y',     type: 'pct',   source: 'derived',  method: 'Worst peak-to-trough fall across the stored year, as a negative number.' },
      ],
    },
  ];

  const SOURCE_LABEL = {
    yfinance:      'Yahoo Finance quote summary',
    edgar:         'SEC EDGAR XBRL',
    derived:       'Computed here',
    price_history: 'Stored daily closes and volumes',
    market_series: 'Treasury bill (^IRX) and S&P 500 (^GSPC)',
    form4:         'SEC EDGAR Form 4',
    news:          'Google News RSS',
    index:         'Index constituent tables',
  };

  // Decide which "as of" timestamp to surface for a given row's source. yfinance
  // and derived fields share the row last_updated; EDGAR fields use edgar_updated
  // (refreshed weekly). Honest about field-level vintage even though we don't yet
  // stamp every individual field independently.
  function fieldAsOf(s, source) {
    if (source === 'edgar') return s.edgar_updated || null;
    return s.last_updated || null;
  }

  function fmtFactor(val, type) {
    if (type === 'pct') return fmtPctRaw(val, 2);
    if (type === 'ratio') return fmtRatio(val, 2);
    return fmtNum(val, 2);
  }

  // ── Score breakdown: peer-relative z-scores per dimension ──────
  // Benchmark = the stock's own sector peers within the universe.
  // Higher score = better-than-peers on that dimension.
  // Fields where a LOWER value is better (P/E, leverage, accruals) are inverted.
  const SCORE_GROUPS = {
    Growth:   { fields: ['revenue_growth_yoy', 'eps_growth_yoy', 'revenue_acceleration', 'gross_margin_trend', 'fcf_growth_yoy'], invert: [] },
    Value:    { fields: ['pe', 'ev_ebitda', 'ev_revenue', 'price_book', 'fcf_yield'], invert: ['pe', 'ev_ebitda', 'ev_revenue', 'price_book'] },
    Momentum: { fields: ['return_12_2', 'return_1m', 'high52w_proximity', 'rel_strength_sp500', 'volume_trend'], invert: [] },
    Quality:  { fields: ['roe_ttm', 'earnings_consistency', 'net_debt_ebitda', 'op_margin_stability', 'accruals_ratio'], invert: ['net_debt_ebitda', 'op_margin_stability', 'accruals_ratio'] },
  };

  // The server already computed these, under gates this cannot reproduce: at
  // least two fields per dimension, a real sector cohort of at least five, and a
  // population sd over that cohort. Recomputing them here with one field and an
  // "Unknown" bucket produced confident scores for companies the server had
  // deliberately refused to score.
  const DIM_KEY = { Growth: 'g', Value: 'v', Momentum: 'm', Quality: 'q' };

  function scoreDimension(s, groupKey) {
    const v = s[DIM_KEY[groupKey]];
    return (v == null || !isFinite(v)) ? null : v;
  }

  // Per-dimension weights for the composite. Range 0 to 2, default 1.0 (equal).
  // Mutated by the slider event handlers; render() reads on every paint.
  const weights = { Growth: 1, Value: 1, Momentum: 1, Quality: 1 };

  function computeComposite(s) {
    // scorable means the server got three of the four dimensions. Below that a
    // composite is one or two numbers wearing the costume of four, and it ranked
    // the least-measured companies at the top of the list.
    if (!s.scorable) return null;
    const dims = ['Growth', 'Value', 'Momentum', 'Quality'];
    let weightedSum = 0;
    let totalWeight = 0;
    for (const d of dims) {
      const w = weights[d];
      if (w <= 0) continue;
      const score = scoreDimension(s, d);
      if (score == null) continue;
      weightedSum += score * w;
      totalWeight += w;
    }
    return totalWeight > 0 ? weightedSum / totalWeight : null;
  }

  function fmtScore(n) {
    if (n == null || isNaN(n)) return '—';
    const sign = n >= 0 ? '+' : '';
    return sign + Number(n).toFixed(2);
  }
  function scoreClass(n) {
    if (n == null) return 'stk-score-na';
    if (Math.abs(n) < 0.1) return 'stk-score-neutral';
    return n >= 0 ? 'stk-score-pos' : 'stk-score-neg';
  }

  function buildScoreBreakdown(s) {
    const dims = ['Growth', 'Value', 'Momentum', 'Quality'];
    const scores = dims.map(d => ({ name: d, val: scoreDimension(s, d) }));
    const composite = computeComposite(s);

    const rows = scores.map(d => {
      if (d.val == null) {
        return '<div class="sb-row"><span class="sb-label">'+d.name+'</span><div class="sb-bar"><div class="sb-bar-axis"></div></div><span class="sb-val sb-val-na">—</span></div>';
      }
      const pct = Math.min(100, Math.abs(d.val) / 3 * 50);  // 50% = max half-bar at z=±3
      const isPos = d.val >= 0;
      const fill = '<div class="sb-bar-fill ' + (isPos ? 'sb-pos' : 'sb-neg') + '" style="' + (isPos ? 'left:50%' : 'right:50%') + '; width:' + pct.toFixed(1) + '%"></div>';
      const valStr = (isPos ? '+' : '') + d.val.toFixed(2);
      const valClass = isPos ? 'sb-val sb-val-pos' : 'sb-val sb-val-neg';
      return '<div class="sb-row"><span class="sb-label">'+d.name+'</span><div class="sb-bar"><div class="sb-bar-axis"></div>'+fill+'</div><span class="'+valClass+'">'+valStr+'</span></div>';
    }).join('');

    let compositeStr, compClass;
    if (composite == null) {
      compositeStr = '—'; compClass = 'sb-comp-na';
    } else {
      compositeStr = (composite >= 0 ? '+' : '') + composite.toFixed(2);
      compClass = composite >= 0 ? 'sb-comp-pos' : 'sb-comp-neg';
    }
    return '<div class="sb-card"><div class="sb-h">Score Breakdown <span class="sb-h-sub">vs Sector Peers</span></div>'
      + rows
      + '<div class="sb-comp-row"><span class="sb-comp-label">vs Bmk</span><span class="sb-comp ' + compClass + '">' + compositeStr + '</span></div>'
      + '</div>';
  }

  function buildBenfordCard(s) {
    const b = s.benford;
    if (!b || !b.observed) {
      return '<div class="bf-card"><div class="bf-h">Benford\\'s Law <span class="bf-meta">no data</span></div><div class="bf-empty">Not enough EDGAR facts for this company to fit Benford reliably (need 30+ USD-denominated values).</div></div>';
    }
    const EXP_D1 = [30.1, 17.6, 12.5, 9.7, 7.9, 6.7, 5.8, 5.1, 4.6];
    // Second-digit Benford expected percentages (digits 0-9)
    const EXP_D2 = [12.0, 11.4, 10.9, 10.4, 10.0, 9.7, 9.3, 9.0, 8.8, 8.5];

    function benfordSeverity(obs, exp) {
      const relDev = Math.abs(obs - exp) / exp;
      if (relDev < 0.10) return 'close';
      if (relDev < 0.25) return 'moderate';
      return 'far';
    }

    function buildSubcard(title, fitLabel, fitClass, chi, mad, n, observed, expected, startDigit, scale) {
      const rows = observed.map((obs, i) => {
        const exp = expected[i];
        const obsW = Math.min(100, obs / scale * 100);
        const expPos = Math.min(100, exp / scale * 100);
        const sev = benfordSeverity(obs, exp);
        return '<div class="bf-row bf-row-' + sev + '">'
          + '<span class="bf-d">' + (startDigit + i) + '</span>'
          + '<div class="bf-bar">'
          +   '<div class="bf-bar-fill" style="width:' + obsW.toFixed(1) + '%"></div>'
          +   '<div class="bf-marker" style="left:' + expPos.toFixed(1) + '%" title="Benford expected ' + exp.toFixed(1) + '%"></div>'
          + '</div>'
          + '<span class="bf-obs">' + obs.toFixed(1) + '%</span>'
          + '<span class="bf-exp">' + exp.toFixed(1) + '%</span>'
        + '</div>';
      }).join('');
      return '<div class="bf-sub">'
        + '<div class="bf-sub-h">' + title
        +   ' <span class="bf-fit ' + fitClass + '">' + fitLabel + '</span>'
        +   ' <span class="bf-sub-meta">'
        +     'MAD ' + (mad == null ? '—' : Number(mad).toFixed(4))
        +     '  &middot;  <span title="Chi-square scales with sample size. At n in the '
        +     'thousands it rejects conformity for any deviation at all, so it is shown '
        +     'for reference while the verdict above uses MAD instead.">'
        +     '&chi;<sup>2</sup> ' + chi + '</span>'
        +     '  &middot;  n=' + n.toLocaleString() + '</span>'
        + '</div>'
        + rows
      + '</div>';
    }

    const d1Card = buildSubcard(
      'First Digit',
      String(b.fit).toUpperCase() + ' FIT',
      'bf-fit-' + b.fit,
      b.chi_sq,
      b.mad,
      b.n,
      b.observed,
      EXP_D1,
      1,
      35
    );
    let d2Card;
    if (b.observed_d2) {
      d2Card = buildSubcard(
        'Second Digit',
        String(b.fit_d2).toUpperCase() + ' FIT',
        'bf-fit-' + b.fit_d2,
        b.chi_sq_d2,
        b.mad_d2,
        b.n_d2,
        b.observed_d2,
        EXP_D2,
        0,
        14
      );
    } else {
      d2Card = '<div class="bf-sub"><div class="bf-sub-h">Second Digit <span class="bf-sub-meta">insufficient data</span></div><div class="bf-empty">Need values &ge; 10 with at least 30 samples.</div></div>';
    }

    return '<div class="bf-card">'
      + '<div class="bf-h">Benford\\'s Law</div>'
      + '<div class="bf-grid">' + d1Card + d2Card + '</div>'
      + '<div class="bf-foot">Digit-frequency of every USD value reported in this company\\'s XBRL filings, compared to the Benford distribution. Vertical marker shows the expected percentage; bar shows observed. Second-digit Benford is harder to game than first-digit because most manipulators only fudge the leading digit. A poor fit can flag reporting anomalies but is not by itself evidence of irregularity.</div>'
    + '</div>';
  }

  function buildDetail(s) {
    const groups = FACTOR_GROUPS.map(g => {
      const items = g.rows.map(r => {
        const val = s[r.key];
        const cls = (val == null || isNaN(val)) ? 'fp-row fp-row-na' : 'fp-row';
        return '<div class="'+cls+'"><span class="fp-label">'+escapeHtml(r.label)+'</span><span class="fp-val">'+fmtFactor(val, r.type)+'</span></div>';
      }).join('');
      return '<div class="fp-card" data-dim="'+g.title+'">'
        + '<button type="button" class="fp-card-h fp-card-toggle" data-dim="'+g.title+'" aria-expanded="false">'
        +   '<span>'+g.title+'</span>'
        +   '<span class="fp-card-caret">&#9656;</span>'
        + '</button>'
        + items
        + '</div>';
    }).join('');
    const scoreCard = buildScoreBreakdown(s);
    const benfordCard = buildBenfordCard(s);
    const chartCard = buildChartCard(s);
    // Methodology panel: empty placeholder. Populated when a card header is clicked.
    const metaPanel = '<div class="fp-meta-panel" id="fp-meta-' + escapeHtml(s.ticker) + '" data-ticker="' + escapeHtml(s.ticker) + '" hidden></div>';
    const signalsRow = buildSignalsRow(s);
    // News card is a placeholder; populated lazily on expand via fetchNewsFor.
    const newsCard = '<div class="nws-card" id="nws-' + escapeHtml(s.ticker) + '">'
      + '<div class="nws-h">News <span class="nws-loading">loading…</span></div>'
      + '</div>';
    // Thesis placeholder, populated lazily by fetchThesisFor. First in the detail:
    // the view is the point and the factor cards are the evidence for it.
    const thesisCard = '<div class="th-card" id="th-' + escapeHtml(s.ticker) + '">'
      + '<div class="th-h">Thesis <span class="th-loading">loading…</span></div>'
      + '</div>';
    // Reported facts, populated lazily by fetchCompanyFor. Sits under the thesis
    // and above the computed factors: filings are what the view is argued from.
    const companyCard = '<div class="co-card" id="co-' + escapeHtml(s.ticker) + '">'
      + '<div class="co-h">Reported <span class="co-loading">loading…</span></div>'
      + '</div>';
    return '<div class="stk-detail">' + thesisCard + companyCard + scoreCard + '<div class="fp-grid">'+groups+'</div>' + metaPanel + chartCard + signalsRow + newsCard + benfordCard + '</div>';
  }

  // ── Signals row: Neglect (Lynch) on the left, Insider Movement (Seyhun) on the right ──

  function neglectLabel(score) {
    if (score == null) return { text: 'N/A', cls: 'sg-na' };
    if (score >= 0.65) return { text: 'NEGLECTED', cls: 'sg-pos' };
    if (score >= 0.40) return { text: 'AVERAGE',   cls: 'sg-neutral' };
    return                       { text: 'CROWDED',   cls: 'sg-neg' };
  }

  function fmtMiniBar(componentScore) {
    // 0..1 fill, rendered with 10 cells.
    if (componentScore == null) return '<span class="sg-bar sg-bar-empty">—</span>';
    const filled = Math.round(componentScore * 10);
    let bar = '<span class="sg-bar">';
    for (let i = 0; i < 10; i++) {
      bar += '<span class="sg-bar-cell' + (i < filled ? ' sg-bar-cell-on' : '') + '"></span>';
    }
    bar += '</span>';
    return bar;
  }

  function buildSignalsRow(s) {
    // ── Neglect (Lynch) ──
    const score = s.neglect_score;
    const lab = neglectLabel(score);
    // Recompute the three component sub-scores for display (so we can show a bar
    // per input). Same formulas as compute_neglect_score on the backend.
    const aRaw = s.analyst_count;
    const iRaw = s.inst_ownership;
    const nRaw = s.news_count_7d;
    const aSub = (aRaw != null) ? (1 - Math.min(aRaw, 30) / 30) : null;
    const iSub = (iRaw != null) ? (1 - Math.min(iRaw, 0.5) / 0.5) : null;
    const nSub = (nRaw != null) ? (1 - Math.min(nRaw, 20) / 20) : null;
    const neglectCard =
        '<div class="sg-card sg-card-neglect">'
      + '<div class="sg-h">'
      +   '<span class="sg-h-title">Neglect <span class="sg-h-eyebrow">Peter Lynch</span></span>'
      +   '<span class="sg-score-badge ' + lab.cls + '">'
      +     (score != null ? score.toFixed(2) : '—') + ' &middot; ' + lab.text
      +   '</span>'
      + '</div>'
      + '<div class="sg-rows">'
      +   '<div class="sg-row">'
      +     '<span class="sg-label">Analyst Coverage</span>'
      +     '<span class="sg-val">' + (aRaw != null ? aRaw + ' analysts' : '—') + '</span>'
      +     fmtMiniBar(aSub)
      +   '</div>'
      +   '<div class="sg-row">'
      +     '<span class="sg-label">Institutional Holdings</span>'
      +     '<span class="sg-val">' + (iRaw != null ? (iRaw * 100).toFixed(0) + '%' : '—') + '</span>'
      +     fmtMiniBar(iSub)
      +   '</div>'
      +   '<div class="sg-row">'
      +     '<span class="sg-label">News Headlines (7d)</span>'
      +     '<span class="sg-val">' + (nRaw != null ? nRaw : '—') + '</span>'
      +     fmtMiniBar(nSub)
      +   '</div>'
      + '</div>'
      + '<div class="sg-foot">Composite of three normalized 0-to-1 components. Higher means less Wall Street attention. Bars show how much each input contributes to the score.</div>'
      + '</div>';

    // ── Insider Movement (Seyhun) ──
    // Pulls SEC Form 4 transactions from the last 90 days.
    const inNetBuy = s.insider_net_buy_90d;
    const inBuyers = s.insider_buyer_count_90d;
    const inSellers = s.insider_seller_count_90d;
    const inCluster = s.insider_cluster_max_30d;
    const inClusterScore = s.insider_cluster_score;
    const inTxCount = s.insider_tx_count_90d;
    const inHasData = (inNetBuy != null) || (inTxCount != null && inTxCount > 0);

    let inLabel;
    if (!inHasData) {
      inLabel = { text: 'NO ACTIVITY', cls: 'sg-na' };
    } else if (inClusterScore != null && inClusterScore >= 0.6) {
      inLabel = { text: 'CLUSTER BUY', cls: 'sg-pos' };
    } else if (inNetBuy != null && inNetBuy > 0) {
      inLabel = { text: 'NET BUYING', cls: 'sg-pos' };
    } else if (inNetBuy != null && inNetBuy < 0) {
      inLabel = { text: 'NET SELLING', cls: 'sg-neg' };
    } else {
      inLabel = { text: 'MIXED', cls: 'sg-neutral' };
    }

    function fmtUsdShort(v) {
      if (v == null) return '—';
      const a = Math.abs(v);
      const sign = v < 0 ? '-' : (v > 0 ? '+' : '');
      if (a >= 1e9) return sign + '$' + (a / 1e9).toFixed(2) + 'B';
      if (a >= 1e6) return sign + '$' + (a / 1e6).toFixed(2) + 'M';
      if (a >= 1e3) return sign + '$' + (a / 1e3).toFixed(0) + 'K';
      return sign + '$' + a.toFixed(0);
    }

    const insiderCard =
        '<div class="sg-card sg-card-insider' + (inHasData ? '' : ' sg-card-stub') + '">'
      + '<div class="sg-h">'
      +   '<span class="sg-h-title">Insider Movement <span class="sg-h-eyebrow">Nejat Seyhun</span></span>'
      +   '<span class="sg-score-badge ' + inLabel.cls + '">' + inLabel.text + '</span>'
      + '</div>'
      + '<div class="sg-rows">'
      +   '<div class="sg-row"><span class="sg-label">Net Buying (90d)</span><span class="sg-val">' + fmtUsdShort(inNetBuy) + '</span></div>'
      +   '<div class="sg-row"><span class="sg-label">Buyers / Sellers</span><span class="sg-val">'
      +     (inBuyers != null ? inBuyers : '0') + ' / ' + (inSellers != null ? inSellers : '0')
      +   '</span></div>'
      +   '<div class="sg-row"><span class="sg-label">Cluster (max 30d)</span><span class="sg-val">'
      +     (inCluster != null ? inCluster + ' buyers' : '—')
      +   '</span>'
      +   (inClusterScore != null ? fmtMiniBar(inClusterScore) : fmtMiniBar(null))
      +   '</div>'
      +   (s.insider_ownership != null
          ? '<div class="sg-row"><span class="sg-label">Insider Ownership</span><span class="sg-val">' + (s.insider_ownership * 100).toFixed(1) + '%</span></div>'
          : '')
      + '</div>'
      + '<div class="sg-foot">Open-market purchases (Form 4 code P) and sales (S) over the last 90 days. Cluster signal: max distinct buyers in any 30-day window. Seyhun: 3+ buyers clustered in one month is the strongest forward signal.</div>'
      + '</div>';

    return '<div class="sg-row-grid">' + neglectCard + insiderCard + '</div>';
  }

  // Track which dimension's metadata is currently shown per ticker (or null).
  const metaOpenByTicker = {};

  function buildMetaPanelHTML(s, dimTitle) {
    const group = FACTOR_GROUPS.find(g => g.title === dimTitle);
    if (!group) return '';
    const statuses = s.status || {};
    const rows = group.rows.map(r => {
      // The registry wins where it has an entry. The inline strings stay as the
      // fallback for fields not yet migrated, and every one of those is a field
      // whose description nothing verifies.
      const m = FIELD_METHODS[r.key] || null;
      const src = m ? m.source : r.source;
      // A registry entry names the timestamp that actually governs it, rather
      // than inferring one from the source bucket.
      const asOf = m && m.asof ? (s[m.asof] || fieldAsOf(s, src)) : fieldAsOf(s, src);
      const asOfTxt = asOf ? fmtDateMDY(asOf) : 'n/a';
      const code = statuses[r.key];
      const statusTxt = code
        ? '<span class="fp-meta-status" title="' + escapeHtml(FIELD_STATUS[code] || '') + '">'
            + escapeHtml(code.replace(/_/g, ' ')) + '</span>'
        : (s[r.key] == null ? '<span class="fp-meta-status">not reported</span>' : '');
      const refreshTxt = m && REFRESH_CLASSES[m.refresh]
        ? ' <span class="fp-meta-refresh">' + escapeHtml(REFRESH_CLASSES[m.refresh]) + '.</span>'
        : '';
      const method = m
        ? '<code class="fp-meta-formula">' + escapeHtml(m.formula) + '</code> '
            + escapeHtml(m.note) + refreshTxt
        : escapeHtml(r.method);
      return '<tr>'
        + '<td class="fp-meta-label">'+escapeHtml(r.label)+'</td>'
        + '<td class="fp-meta-src" data-src="'+src+'">'+escapeHtml(SOURCE_LABEL[src] || src)+'</td>'
        + '<td class="fp-meta-asof">'+asOfTxt+'</td>'
        + '<td class="fp-meta-status-cell">'+statusTxt+'</td>'
        + '<td class="fp-meta-method">'+method+'</td>'
        + '</tr>';
    }).join('');
    return '<div class="fp-meta-card fp-meta-' + dimTitle.toLowerCase() + '">'
      + '<div class="fp-meta-card-h">'
      +   '<span class="fp-meta-card-eyebrow">Methodology</span>'
      +   '<span class="fp-meta-card-title">' + escapeHtml(dimTitle) + ' factors, sources and as-of dates</span>'
      +   '<button type="button" class="fp-meta-card-close" aria-label="Close">&times;</button>'
      + '</div>'
      + '<table class="fp-meta-table"><thead><tr>'
      +   '<th>Metric</th><th>Source</th><th>As of</th><th>Status</th><th>Method</th>'
      + '</tr></thead><tbody>' + rows + '</tbody></table>'
      + '</div>';
  }

  function renderMetaPanelFor(ticker) {
    const panel = document.getElementById('fp-meta-' + ticker);
    if (!panel) return;
    const stock = ALL.find(s => s.ticker === ticker);
    if (!stock) return;
    const open = metaOpenByTicker[ticker];
    // Update card header active state to reflect what's open.
    const detail = panel.closest('.stk-detail');
    if (detail) {
      detail.querySelectorAll('.fp-card').forEach(card => {
        card.classList.toggle('fp-card-active', card.dataset.dim === open);
      });
      detail.querySelectorAll('.fp-card-toggle').forEach(btn => {
        btn.setAttribute('aria-expanded', btn.dataset.dim === open ? 'true' : 'false');
      });
    }
    if (!open) {
      panel.hidden = true;
      panel.innerHTML = '';
      return;
    }
    panel.hidden = false;
    panel.innerHTML = buildMetaPanelHTML(stock, open);
  }

  // ── Chart card: price history + operating margin history ─────────────
  // Time-range buttons filter the visible window. Op margin is quarterly
  // so anything finer than QTD just shows the latest quarter.
  const CHART_RANGES = [
    { id: 'd',   label: 'Day',   priceDays: 2,   qtrCount: 1 },
    { id: 'w',   label: 'Week',  priceDays: 5,   qtrCount: 1 },
    { id: 'm',   label: 'Month', priceDays: 22,  qtrCount: 1 },
    { id: 'qtd', label: 'QTD',   priceDays: 'qtd', qtrCount: 1 },
    { id: 'ytd', label: 'YTD',   priceDays: 'ytd', qtrCount: 4 },
    { id: '2q',  label: '2 QTR', priceDays: 126, qtrCount: 2 },
    { id: '3q',  label: '3 QTR', priceDays: 189, qtrCount: 3 },
    { id: '4q',  label: '4 QTR', priceDays: 252, qtrCount: 4 },
  ];
  const DEFAULT_RANGE = 'm';

  function buildChartCard(s) {
    const tabs = CHART_RANGES.map(r =>
      '<button type="button" class="ch-tab' + (r.id === DEFAULT_RANGE ? ' active' : '') + '" data-range="' + r.id + '">' + r.label + '</button>'
    ).join('');
    return '<div class="ch-card" id="ch-' + escapeHtml(s.ticker) + '" data-ticker="' + escapeHtml(s.ticker) + '" data-range="' + DEFAULT_RANGE + '">'
      + '<div class="ch-h"><span class="ch-h-title">Charts</span><div class="ch-tabs">' + tabs + '</div></div>'
      + '<div class="ch-grid">'
      +   '<div class="ch-pane">'
      +     '<div class="ch-pane-h">'
      +       '<span class="ch-pane-h-left">Price'
      +         '<span class="ch-pane-price" id="ch-price-now-' + escapeHtml(s.ticker) + '">' + (s.price != null ? '$' + Number(s.price).toFixed(2) : '—') + '</span>'
      +       '</span>'
      +       '<span class="ch-pane-meta" id="ch-price-meta-' + escapeHtml(s.ticker) + '">loading…</span>'
      +     '</div>'
      +     '<canvas class="ch-canvas" id="ch-price-' + escapeHtml(s.ticker) + '"></canvas>'
      +   '</div>'
      +   '<div class="ch-pane">'
      +     '<div class="ch-pane-h">Operating Margin <span class="ch-pane-meta" id="ch-opm-meta-' + escapeHtml(s.ticker) + '">' + (s.op_margin_history && s.op_margin_history.length ? 'EDGAR XBRL' : 'no XBRL data') + '</span></div>'
      +     '<canvas class="ch-canvas" id="ch-opm-' + escapeHtml(s.ticker) + '"></canvas>'
      +   '</div>'
      + '</div>'
      + '</div>';
  }

  // Lazy-loaded price series cache, keyed by ticker.
  const priceCache = {};

  function fetchPricesFor(ticker) {
    if (priceCache[ticker]) {
      renderChartsFor(ticker);
      return;
    }
    fetch('./prices/' + encodeURIComponent(newsFilename(ticker)), { cache: 'no-store' })
      .then(r => r.ok ? r.json() : null)
      .then(payload => {
        priceCache[ticker] = (payload && Array.isArray(payload.closes)) ? payload.closes : [];
        renderChartsFor(ticker);
      })
      .catch(() => { priceCache[ticker] = []; renderChartsFor(ticker); });
  }

  function rangeSlicePrices(closes, rangeId) {
    if (!closes.length) return [];
    const cfg = CHART_RANGES.find(r => r.id === rangeId) || CHART_RANGES[2];
    if (cfg.priceDays === 'ytd') {
      const yr = new Date().getUTCFullYear();
      return closes.filter(c => c[0].slice(0, 4) === String(yr));
    }
    if (cfg.priceDays === 'qtd') {
      const now = new Date();
      const qStart = new Date(Date.UTC(now.getUTCFullYear(), Math.floor(now.getUTCMonth() / 3) * 3, 1));
      const cutoff = qStart.toISOString().slice(0, 10);
      return closes.filter(c => c[0] >= cutoff);
    }
    return closes.slice(-cfg.priceDays);
  }

  function rangeSliceMargins(history, rangeId) {
    if (!history || !history.length) return [];
    const cfg = CHART_RANGES.find(r => r.id === rangeId) || CHART_RANGES[2];
    return history.slice(0, cfg.qtrCount).reverse();
  }

  // Read theme-aware chart colors from CSS vars so charts repaint correctly
  // when the user toggles light/dark mode.
  function chartColors() {
    const cs = getComputedStyle(document.documentElement);
    return {
      grid:   (cs.getPropertyValue('--grid-line')   || '').trim() || 'rgba(255,255,255,0.06)',
      axis:   (cs.getPropertyValue('--chart-axis')  || '').trim() || 'rgba(255,255,255,0.45)',
      value:  (cs.getPropertyValue('--chart-value') || '').trim() || 'rgba(255,255,255,0.85)',
    };
  }

  function drawLineChart(canvas, series, labelFmt, lineColor) {
    const dpr = window.devicePixelRatio || 1;
    const cssW = canvas.clientWidth;
    const cssH = canvas.clientHeight || 140;
    canvas.width = cssW * dpr;
    canvas.height = cssH * dpr;
    const ctx = canvas.getContext('2d');
    ctx.scale(dpr, dpr);
    ctx.clearRect(0, 0, cssW, cssH);
    const tc = chartColors();
    if (!series.length) {
      ctx.fillStyle = tc.axis;
      ctx.font = '11px "DM Mono", monospace';
      ctx.fillText('No data in this range.', 8, cssH / 2);
      return;
    }
    const padL = 36, padR = 8, padT = 8, padB = 18;
    const w = cssW - padL - padR;
    const h = cssH - padT - padB;
    const vals = series.map(s => s[1]);
    let minV = Math.min.apply(null, vals);
    let maxV = Math.max.apply(null, vals);
    if (minV === maxV) { minV -= 1; maxV += 1; }
    const span = maxV - minV;
    const x = i => padL + (series.length === 1 ? w / 2 : (i / (series.length - 1)) * w);
    const y = v => padT + h - ((v - minV) / span) * h;
    // Grid lines (3 horizontal)
    ctx.strokeStyle = tc.grid;
    ctx.lineWidth = 1;
    for (let i = 0; i <= 3; i++) {
      const yy = padT + (i / 3) * h;
      ctx.beginPath(); ctx.moveTo(padL, yy); ctx.lineTo(padL + w, yy); ctx.stroke();
    }
    // Y-axis labels
    ctx.fillStyle = tc.axis;
    ctx.font = '9px "DM Mono", monospace';
    ctx.textAlign = 'right';
    for (let i = 0; i <= 3; i++) {
      const v = maxV - (i / 3) * span;
      ctx.fillText(labelFmt(v), padL - 4, padT + (i / 3) * h + 3);
    }
    // X-axis labels (first + last)
    ctx.textAlign = 'left';
    ctx.fillText(series[0][0], padL, padT + h + 12);
    ctx.textAlign = 'right';
    ctx.fillText(series[series.length - 1][0], padL + w, padT + h + 12);
    // Area + line
    ctx.beginPath();
    ctx.moveTo(x(0), y(series[0][1]));
    for (let i = 1; i < series.length; i++) ctx.lineTo(x(i), y(series[i][1]));
    ctx.strokeStyle = lineColor;
    ctx.lineWidth = 1.5;
    ctx.stroke();
    // Last value dot
    const lastIdx = series.length - 1;
    ctx.fillStyle = lineColor;
    ctx.beginPath();
    ctx.arc(x(lastIdx), y(series[lastIdx][1]), 2.5, 0, 2 * Math.PI);
    ctx.fill();
  }

  function drawBarChart(canvas, series, labelFmt, posColor, negColor) {
    const dpr = window.devicePixelRatio || 1;
    const cssW = canvas.clientWidth;
    const cssH = canvas.clientHeight || 140;
    canvas.width = cssW * dpr;
    canvas.height = cssH * dpr;
    const ctx = canvas.getContext('2d');
    ctx.scale(dpr, dpr);
    ctx.clearRect(0, 0, cssW, cssH);
    const tc = chartColors();
    if (!series.length) {
      ctx.fillStyle = tc.axis;
      ctx.font = '11px "DM Mono", monospace';
      ctx.fillText('No XBRL data for this range.', 8, cssH / 2);
      return;
    }
    // Top padding leaves room for the value label that sits above each bar.
    const padL = 36, padR = 12, padT = 18, padB = 26;
    const w = cssW - padL - padR;
    const h = cssH - padT - padB;
    const vals = series.map(s => s[1]);
    let minV = Math.min(0, Math.min.apply(null, vals));
    let maxV = Math.max(0, Math.max.apply(null, vals));
    // Add ~10% headroom so the bar top + label don't touch the chart top.
    const range0 = maxV - minV;
    if (range0 < 0.005) { maxV += 0.005; minV -= 0.005; }
    else { maxV += range0 * 0.1; minV -= range0 * 0.05; }
    const span = maxV - minV;
    const yZero = padT + h - ((0 - minV) / span) * h;
    const slot = w / series.length;
    // Cap bar width so a single-quarter view doesn't span the whole pane.
    const barW = Math.min(56, Math.max(8, slot * 0.55));
    // Y-axis labels + grid
    ctx.fillStyle = tc.axis;
    ctx.font = '9px "DM Mono", monospace';
    ctx.textAlign = 'right';
    for (let i = 0; i <= 3; i++) {
      const v = maxV - (i / 3) * span;
      const yy = padT + (i / 3) * h;
      ctx.fillText(labelFmt(v), padL - 4, yy + 3);
      ctx.strokeStyle = tc.grid;
      ctx.beginPath(); ctx.moveTo(padL, yy); ctx.lineTo(padL + w, yy); ctx.stroke();
    }
    // Bars
    series.forEach((p, i) => {
      const cx = padL + slot * i + slot / 2;
      const yv = padT + h - ((p[1] - minV) / span) * h;
      const top = Math.min(yv, yZero);
      const barH = Math.abs(yv - yZero);
      ctx.fillStyle = p[1] >= 0 ? posColor : negColor;
      ctx.fillRect(cx - barW / 2, top, barW, Math.max(1, barH));
      // Value label: above the bar normally; if it would clip the top of the
      // chart, render inside the bar with contrasting text.
      ctx.font = '9px "DM Mono", monospace';
      ctx.textAlign = 'center';
      const labelText = (p[1] * 100).toFixed(1) + '%';
      const labelY = top - 4;
      if (labelY < padT + 4) {
        ctx.fillStyle = 'rgba(0,0,0,0.85)';
        ctx.fillText(labelText, cx, top + 11);
      } else {
        ctx.fillStyle = tc.value;
        ctx.fillText(labelText, cx, labelY);
      }
      // Date label
      ctx.fillStyle = tc.axis;
      ctx.fillText(p[0].slice(2, 7).replace('-', '/'), cx, padT + h + 14);
    });
  }

  function renderChartsFor(ticker) {
    const card = document.getElementById('ch-' + ticker);
    if (!card) return;
    const range = card.dataset.range || DEFAULT_RANGE;
    const stock = ALL.find(s => s.ticker === ticker);
    if (!stock) return;

    // Price pane
    const priceCanvas = document.getElementById('ch-price-' + ticker);
    const priceMeta   = document.getElementById('ch-price-meta-' + ticker);
    const priceNowEl  = document.getElementById('ch-price-now-' + ticker);
    const closes = priceCache[ticker];
    if (priceCanvas && closes !== undefined) {
      const slice = rangeSlicePrices(closes, range);
      drawLineChart(priceCanvas, slice, v => '$' + v.toFixed(0), '#FFB347');
      // Surface the latest close from the price file. Falls back to .price
      // (yfinance live) when the file is empty.
      if (priceNowEl) {
        let shown = null, asOf = null;
        if (closes && closes.length) {
          shown = closes[closes.length - 1][1];
          asOf  = closes[closes.length - 1][0];
        } else if (stock.price != null) {
          shown = stock.price;
        }
        priceNowEl.textContent = shown != null
          ? '$' + Number(shown).toFixed(2) + (asOf ? ' as of ' + asOf : '')
          : '—';
      }
      if (priceMeta) {
        if (slice.length) {
          const first = slice[0][1], last = slice[slice.length - 1][1];
          const chg = ((last - first) / first) * 100;
          const sign = chg >= 0 ? '+' : '';
          priceMeta.textContent = sign + chg.toFixed(2) + '% over window';
          priceMeta.className = 'ch-pane-meta ' + (chg >= 0 ? 'ch-pos' : 'ch-neg');
        } else {
          priceMeta.textContent = closes && closes.length ? 'no prices in this window' : 'no price file';
          priceMeta.className = 'ch-pane-meta';
        }
      }
    }

    // Op margin pane
    const opmCanvas = document.getElementById('ch-opm-' + ticker);
    if (opmCanvas) {
      const margins = rangeSliceMargins(stock.op_margin_history, range);
      const series = margins.map(m => [m.end, m.margin]);
      drawBarChart(opmCanvas, series, v => (v * 100).toFixed(0) + '%', '#34D27A', '#E84B4B');
    }
  }

  // ── News: lazy fetch per ticker ─────────────────────────────────
  const newsCache = {};

  // Mirror the Python sanitizer so JS hits the same on-disk filename when the
  // ticker collides with a Windows reserved device name (CON, PRN, AUX, etc).
  const WIN_RESERVED = new Set(['CON','PRN','AUX','NUL','COM1','COM2','COM3','COM4','COM5','COM6','COM7','COM8','COM9','LPT1','LPT2','LPT3','LPT4','LPT5','LPT6','LPT7','LPT8','LPT9']);
  function newsFilename(ticker) {
    const t = (ticker || '').toUpperCase();
    return WIN_RESERVED.has(t) ? '_' + t + '.json' : t + '.json';
  }

  const thesisCache = {};

  // ── Reported: what the filings say, as opposed to what the panel computes ──
  // The panel holds five dates of derived ratios. This holds a decade of what
  // the company actually filed, plus the documents it was taken from. Loaded
  // lazily from docs/company/{TICKER}.json, which most tickers do not have.

  const companyCache = {};
  const CO_REPO = 'https://github.com/CTLSmith5689/daily-intelligence-brief/blob/main/data/filings/';

  function fetchCompanyFor(ticker) {
    if (companyCache[ticker] !== undefined) { renderCompany(ticker, companyCache[ticker]); return; }
    fetch('./company/' + encodeURIComponent(newsFilename(ticker)), { cache: 'no-store' })
      .then(r => r.ok ? r.json() : null)
      .then(v => { companyCache[ticker] = v || null; renderCompany(ticker, companyCache[ticker]); })
      .catch(() => { companyCache[ticker] = null; renderCompany(ticker, null); });
  }

  // One series, drawn to one scale. The zero line appears only when the series
  // actually crosses zero, which for operating margin and EPS it often does and
  // for revenue it never does.
  function coSpark(vals, w, h) {
    const pts = vals.filter(v => v != null && isFinite(v));
    if (pts.length < 2) return '';
    let lo = Math.min.apply(null, pts), hi = Math.max.apply(null, pts);
    if (lo === hi) { lo -= 1; hi += 1; }
    const crosses = lo < 0 && hi > 0;
    if (crosses) { const m = Math.max(Math.abs(lo), Math.abs(hi)); lo = -m; hi = m; }
    const pad = (hi - lo) * 0.12;
    lo -= pad; hi += pad;
    const x = i => 3 + (i / (vals.length - 1)) * (w - 6);
    const y = v => h - 4 - ((v - lo) / (hi - lo)) * (h - 8);
    // A missing year breaks the line rather than being bridged. MPC has no
    // tagged revenue for 2016 and 2017, and joining 2015 to 2018 would draw two
    // years of figures the filings do not report.
    let d = '', open = false;
    vals.forEach((v, i) => {
      if (v == null || !isFinite(v)) { open = false; return; }
      d += (open ? ' L' : ' M') + x(i).toFixed(1) + ' ' + y(v).toFixed(1);
      open = true;
    });
    d = d.trim();
    let last = null;
    for (let i = vals.length - 1; i >= 0; i--) { if (vals[i] != null && isFinite(vals[i])) { last = i; break; } }
    const zero = crosses
      ? '<line x1="3" y1="' + y(0).toFixed(1) + '" x2="' + (w - 3) + '" y2="' + y(0).toFixed(1)
        + '" stroke="var(--grid-line)" stroke-width="1"/>'
      : '';
    let dots = '';
    vals.forEach((v, i) => {
      if (v == null || !isFinite(v)) return;
      const prev = i > 0 && vals[i - 1] != null && isFinite(vals[i - 1]);
      const next = i < vals.length - 1 && vals[i + 1] != null && isFinite(vals[i + 1]);
      if (!prev && !next) {
        dots += '<circle cx="' + x(i).toFixed(1) + '" cy="' + y(v).toFixed(1)
             + '" r="2" fill="var(--apt-red)"/>';
      }
    });
    const dot = dots + (last == null ? ''
      : '<circle cx="' + x(last).toFixed(1) + '" cy="' + y(vals[last]).toFixed(1)
        + '" r="2.6" fill="var(--apt-red)"/>');
    return '<svg class="co-spark" viewBox="0 0 ' + w + ' ' + h + '" width="' + w + '" height="' + h
      + '" aria-hidden="true">' + zero
      + '<path d="' + d + '" fill="none" stroke="var(--apt-red)" stroke-width="2" '
      + 'stroke-linejoin="round" stroke-linecap="round"/>' + dot + '</svg>';
  }

  function coMoney(v) {
    if (v == null || !isFinite(v)) return '--';
    const a = Math.abs(v);
    if (a >= 1e12) return (v / 1e12).toFixed(2) + 'T';
    if (a >= 1e9) return (v / 1e9).toFixed(1) + 'B';
    if (a >= 1e6) return (v / 1e6).toFixed(0) + 'M';
    return v.toFixed(0);
  }

  function coSeriesRow(label, rows, key, fmt) {
    const vals = rows.map(r => (r[key] == null ? null : r[key]));
    if (!vals.some(v => v != null && isFinite(v))) return '';
    let firstI = vals.findIndex(v => v != null && isFinite(v));
    let lastI = -1;
    for (let i = vals.length - 1; i >= 0; i--) { if (vals[i] != null && isFinite(vals[i])) { lastI = i; break; } }
    const yr = i => (rows[i].period_end || '').slice(0, 4);
    return '<div class="co-srow">'
      + '<span class="co-srow-k">' + escapeHtml(label) + '</span>'
      + coSpark(vals, 188, 30)
      + '<span class="co-srow-v"><b>' + fmt(vals[lastI]) + '</b> <i>' + escapeHtml(yr(lastI)) + '</i></span>'
      + '<span class="co-srow-from">from ' + fmt(vals[firstI]) + ' <i>' + escapeHtml(yr(firstI)) + '</i></span>'
      + '</div>';
  }

  function renderCompany(ticker, v) {
    const el = document.getElementById('co-' + ticker);
    if (!el) return;
    const head = kicker => '<div class="co-h"><span>Reported</span>'
      + '<span class="co-h-src">' + kicker + '</span></div>';
    if (!v) {
      el.innerHTML = head('SEC EDGAR')
        + '<div class="co-empty">Nothing collected from EDGAR for this ticker yet. Filings are '
        + 'harvested when a company reports, so a name that has not reported since collection '
        + 'started will be empty.</div>';
      return;
    }

    let html = head('SEC EDGAR &middot; XBRL companyfacts');

    // What it reports: segment names and share of its sub-industry. Both are
    // statements about structure rather than about price.
    const seg = v.segments, ps = v.peer_share;
    if (seg || ps) {
      html += '<div class="co-block">';
      if (seg && seg.names && seg.names.length) {
        html += '<div class="co-k">Reportable segments</div><div class="co-chips">'
          + seg.names.map(n => '<span class="co-chip">' + escapeHtml(n) + '</span>').join('')
          + '</div>'
          + '<div class="co-note">Named in the ' + escapeHtml(seg.form || 'filing')
          + ' segment note. Revenue by segment is not collected: the rendered note flattens '
          + 'several tables into one block and parsing it produced wrong numbers, so only the '
          + 'names are shown.'
          + (seg.text_path ? ' <a href="' + CO_REPO + escapeHtml(seg.text_path)
              + '" target="_blank" rel="noopener">Read the note</a>.' : '')
          + '</div>';
      }
      if (ps && ps.share != null) {
        const pct = (ps.share * 100);
        html += '<div class="co-k">Share of sub-industry revenue</div>'
          + '<div class="co-bar"><span class="co-bar-fill" style="width:'
          + Math.max(1, Math.min(100, pct)).toFixed(1) + '%"></span></div>'
          + '<div class="co-note">' + pct.toFixed(1) + '% of trailing revenue across '
          + ps.n + ' names in ' + escapeHtml(ps.group) + ', ranked ' + ps.rank + ' of ' + ps.n
          + (ps.rank === 1 ? '.' : '. Largest is ' + escapeHtml(ps.leader) + ' at '
              + (ps.leader_share * 100).toFixed(1) + '%.')
          + ' Revenue share is not market share: the peer group is a GICS label, not a market.'
          + '</div>';
      }
      html += '</div>';
    }

    // Reported history. Annual, because a quarterly series of a seasonal
    // business shows the season rather than the trend.
    const ann = (v.reported && v.reported.annual) || [];
    if (ann.length >= 2) {
      const rows = coSeriesRow('Revenue', ann, 'revenue', coMoney)
        + coSeriesRow('Diluted EPS', ann, 'eps_diluted', x => (x == null ? '--' : x.toFixed(2)))
        + coSeriesRow('Operating margin', ann, 'operating_margin',
            x => (x == null ? '--' : (x * 100).toFixed(1) + '%'))
        + coSeriesRow('Free cash flow', ann, 'fcf', coMoney);
      if (rows) {
        html += '<div class="co-block"><div class="co-k">As reported, annual &middot; '
          + ann.length + ' years to ' + escapeHtml((ann[ann.length - 1].period_end || '').slice(0, 4))
          + '</div>' + rows
          + '<div class="co-note">Taken from XBRL companyfacts, which reports consolidated '
          + 'figures only. Revenue before 2018 is tagged differently by many filers, so an early '
          + 'gap in the line is a tagging change rather than a year without revenue.</div></div>';
      }
    }

    const f = v.filings || [];
    if (f.length) {
      html += '<div class="co-block"><div class="co-k">Documents held</div><div class="co-files">'
        + f.map(r => '<a class="co-file" href="' + CO_REPO + escapeHtml(r.text_path || '')
            + '" target="_blank" rel="noopener">'
            + '<span class="co-file-d">' + escapeHtml(r.filed || '') + '</span>'
            + '<span class="co-file-n">' + escapeHtml((r.doc_kind || '').replace(/_/g, ' ')) + '</span>'
            + '<span class="co-file-f">' + escapeHtml(r.form || '') + '</span>'
            + '<span class="co-file-c">' + Math.round((r.text_chars || 0) / 1000) + 'k</span>'
            + '</a>').join('')
        + '</div><div class="co-note">These are the documents the thesis agent reads. A 42k '
        + 'character item is truncated at that length.</div></div>';
    }
    el.innerHTML = html;
  }

  function fetchThesisFor(ticker) {
    if (thesisCache[ticker] !== undefined) { renderThesis(ticker, thesisCache[ticker]); return; }
    fetch('./thesis/' + encodeURIComponent(newsFilename(ticker)), { cache: 'no-store' })
      .then(r => r.ok ? r.json() : null)
      .then(v => { thesisCache[ticker] = v || null; renderThesis(ticker, thesisCache[ticker]); })
      .catch(() => { thesisCache[ticker] = null; renderThesis(ticker, null); });
  }

  function renderThesis(ticker, v) {
    const el = document.getElementById('th-' + ticker);
    if (!el) return;
    if (!v) {
      el.innerHTML = '<div class="th-h">Thesis</div>'
        + '<div class="th-empty">No thesis written for this ticker. Coverage is deliberately '
        + 'partial: the screen surfaces a few names a week out of roughly a thousand eligible, '
        + 'so most names will never have one.</div>';
      return;
    }
    const conv = parseInt(v.conviction, 10);
    let pips = '<span class="th-pips">';
    for (let i = 1; i <= 5; i++) pips += '<span class="th-pip' + (i <= conv ? ' th-pip-on' : '') + '"></span>';
    pips += '</span>';

    const noView = (v.direction || '') === 'no view';
    const nums = [];
    if (v.target_price) nums.push('<span>target <b>' + escapeHtml(v.target_price) + '</b></span>');
    if (v.entry_price) nums.push('<span>at <b>' + escapeHtml(v.entry_price) + '</b></span>');
    if (v.horizon_days) nums.push('<span><b>' + escapeHtml(v.horizon_days) + 'd</b></span>');
    if (v.review_by) nums.push('<span>review <b>' + escapeHtml(v.review_by) + '</b></span>');

    let html = '<div class="th-h">Thesis <span class="th-h-date">' + escapeHtml(v.kind || '')
      + ' &middot; ' + escapeHtml(v.written_on || '')
      + (v.note_count > 1 ? ' &middot; ' + v.note_count + ' notes' : '') + '</span></div>';

    html += '<div class="th-top"><span class="th-dir' + (noView ? ' th-dir-noview' : '') + '">'
      + escapeHtml(v.direction || '?') + '</span>' + pips
      + '<span class="th-nums">' + nums.join('') + '</span></div>';

    if (v.claim_changed_ever) {
      html += '<div class="th-drift"><b>This thesis has been revised with direction and '
        + 'conviction unchanged.</b> That is the shape thesis drift takes: the position '
        + 'survives while the reasoning is replaced. Read the history before trusting the '
        + 'current view.</div>';
    }
    if (v.key_claim) html += '<p class="th-claim">' + escapeHtml(v.key_claim) + '</p>';
    if (v.falsifier) {
      html += '<div class="th-fals"><span class="th-fals-k">What would prove this wrong</span>'
        + '<div class="th-fals-t">' + escapeHtml(v.falsifier) + '</div></div>';
    }
    const cav = Array.isArray(v.data_caveats) ? v.data_caveats : [];
    if (cav.length) {
      html += '<div class="th-cav"><span class="th-cav-k">What the analyst could not know ('
        + cav.length + ')</span>' + cav.map(escapeHtml).join(' &middot; ') + '</div>';
    }
    const hist = Array.isArray(v.history) ? v.history.slice().reverse() : [];
    if (hist.length > 1) {
      html += '<div class="th-hist">';
      hist.forEach(function (h) {
        let c = escapeHtml(h.conviction || '');
        if (h.prior_conviction && h.prior_conviction !== h.conviction) {
          c = escapeHtml(h.prior_conviction) + '&rarr;' + c;
        }
        html += '<div class="th-hist-row"><span>' + escapeHtml(h.date || '') + '</span>'
          + '<span>' + escapeHtml(h.kind || '') + '</span>'
          + '<span>' + escapeHtml(h.direction || '') + ' ' + c + '</span>'
          + '<span>' + escapeHtml(h.trigger || '') + '</span></div>';
      });
      html += '</div>';
    }
    if (v.note_path) {
      html += '<div class="th-note-link"><a href="https://github.com/CTLSmith5689/'
        + 'daily-intelligence-brief/blob/main/' + escapeHtml(v.note_path) + '" target="_blank" '
        + 'rel="noopener">' + escapeHtml(v.note_path) + '</a></div>';
    }
    el.innerHTML = html;
  }

  function fetchNewsFor(ticker) {
    if (newsCache[ticker]) {
      renderNews(ticker, newsCache[ticker]);
      return;
    }
    fetch('./news/' + encodeURIComponent(newsFilename(ticker)), { cache: 'no-store' })
      .then(r => r.ok ? r.json() : [])
      .then(items => {
        newsCache[ticker] = Array.isArray(items) ? items : [];
        renderNews(ticker, newsCache[ticker]);
      })
      .catch(() => renderNews(ticker, []));
  }

  function renderNews(ticker, items) {
    const el = document.getElementById('nws-' + ticker);
    if (!el) return;
    if (!items.length) {
      el.innerHTML = '<div class="nws-h">News</div>'
        + '<div class="nws-empty">No recent news pulled for this ticker. The next morning workflow run will retry.</div>';
      return;
    }
    const now = Math.floor(Date.now() / 1000);
    const DAY = 24 * 3600;
    const today = items.filter(i => i.ts && (now - i.ts) < DAY);
    const week  = items.filter(i => i.ts && (now - i.ts) >= DAY && (now - i.ts) < 7 * DAY);
    const month = items.filter(i => i.ts && (now - i.ts) >= 7 * DAY && (now - i.ts) < 31 * DAY);

    function avgScore(bucket, key) {
      const vals = bucket.map(i => i[key]).filter(v => typeof v === 'number');
      if (!vals.length) return null;
      return vals.reduce((a, b) => a + b, 0) / vals.length;
    }
    function fmtSent(v) {
      if (v == null) return '—';
      const s = v >= 0 ? '+' : '';
      return s + v.toFixed(2);
    }
    function sentClass(v) {
      if (v == null) return 'nws-sent-na';
      if (v <= -0.10) return 'nws-sent-neg';
      if (v >= 0.10) return 'nws-sent-pos';
      return 'nws-sent-neutral';
    }
    function sentimentRow(bucket) {
      const lm = avgScore(bucket, 'lm');
      const vd = avgScore(bucket, 'vader');
      return '<div class="nws-sent-row">'
        + '<div class="nws-sent-cell" title="Loughran-McDonald financial dictionary, '
        +   'averaged over the headlines in this group that contain any dictionary term. '
        +   'Three quarters of headlines contain none and are not counted, so this often '
        +   'rests on two or three of them and reads closer to a direction than a magnitude. '
        +   'A dash means nothing in the group was scorable.">'
        +   '<span class="nws-sent-label">LM</span> <span class="nws-sent-val ' + sentClass(lm) + '">' + fmtSent(lm) + '</span></div>'
        + '<div class="nws-sent-cell"><span class="nws-sent-label">VADER</span> <span class="nws-sent-val ' + sentClass(vd) + '">' + fmtSent(vd) + '</span></div>'
      + '</div>';
    }

    function bucketHTML(label, bucket) {
      if (!bucket.length) {
        return '<div class="nws-col"><div class="nws-col-h">' + label + '</div>'
          + sentimentRow(bucket)
          + '<div class="nws-empty">—</div></div>';
      }
      const links = bucket.slice(0, 6).map(i =>
        '<a class="nws-item" href="' + escapeHtml(i.link) + '" target="_blank" rel="noopener">'
        + '<div class="nws-title">' + escapeHtml(i.title) + '</div>'
        + '<div class="nws-meta">' + escapeHtml(i.source || '') + '</div>'
        + '</a>'
      ).join('');
      return '<div class="nws-col"><div class="nws-col-h">' + label
        + ' <span class="nws-count">' + bucket.length + '</span></div>'
        + sentimentRow(bucket)
        + links + '</div>';
    }

    el.innerHTML = '<div class="nws-h">News</div>'
      + '<div class="nws-grid">'
      + bucketHTML('Today',       today)
      + bucketHTML('Last 7 days', week)
      + bucketHTML('Last month',  month)
      + '</div>';
  }

  let expanded = new Set();

  // Theme toggle hook: re-render any open charts after a theme switch since
  // canvas colors are read from CSS vars at draw time.
  window.__aptRedrawCharts = function() {
    expanded.forEach(t => {
      if (priceCache[t] !== undefined) renderChartsFor(t);
    });
  };

  // Shared so the chart plots exactly what the list shows: one filter, three views.
  function currentFiltered() {
    const q = query.toLowerCase();
    return ALL.filter(s => {
      if (activeSector && s.sector !== activeSector) return false;
      if (activeIndex && s.index !== activeIndex) return false;
      if (!passesFilters(s)) return false;
      if (queryValueCheap && !(s.v != null && s.v > 0)) return false;
      if (!q) return true;
      return ((s.ticker||'')+' '+(s.name||'')+' '+(s.sector||'')+' '+(s.sub_industry||'')).toLowerCase().includes(q);
    });
  }

  function rowHtml(s, idx) {
      const chgClass = (s.change_pct != null && Number(s.change_pct) < 0) ? 'stk-neg' : 'stk-pos';
      const isOpen = expanded.has(s.ticker);
      const arrow = isOpen ? '▾' : '▸';
      const detailHtml = isOpen ? buildDetail(s) : '';
      const composite = computeComposite(s);
      // Four factor bars, centred on zero: a bar grows right of the midline for
      // a positive z-score and left for a negative one, so the eye reads the
      // shape of a company's profile without reading four numbers.
      const bars = [['G', s.g], ['V', s.v], ['M', s.m], ['Q', s.q]].map(function(p) {
        const z = p[1];
        const t = p[0] === 'G' ? 'Growth' : p[0] === 'V' ? 'Value' : p[0] === 'M' ? 'Momentum' : 'Quality';
        if (z == null) return '<span class="stk-f" title="' + t + ': no reading"><i class="stk-f-t"></i></span>';
        const mag = Math.min(Math.abs(z) / 1.5, 1) * 50;
        const style = z >= 0 ? 'left:50%;width:' + mag + '%' : 'right:50%;width:' + mag + '%';
        return '<span class="stk-f" title="' + t + ': ' + z.toFixed(2) + '"><i class="stk-f-t"><em class="' +
               (z >= 0 ? 'up' : 'dn') + '" style="' + style + '"></em></i></span>';
      }).join('');
      return '<div class="stk-row" data-ticker="'+escapeHtml(s.ticker)+'">'
        + '<div class="stk-rank">'+String(idx + 1).padStart(2, '0')+'</div>'
        + '<div class="stk-id"><span class="stk-tk">'+escapeHtml(s.ticker||'')+'</span>'
          + '<span class="stk-nm">'+escapeHtml(s.name||'')+'</span></div>'
        + '<div class="stk-sector" title="'+escapeHtml(s.sector||'')+'">'+escapeHtml(s.sector||'')+'</div>'
        + '<div class="stk-cap">'+fmtCap(s.market_cap)+'</div>'
        + '<div class="stk-pct '+chgClass+'">'+fmtPct(s.change_pct)+'</div>'
        + '<div class="stk-factors">'+bars+'</div>'
        + '<div class="stk-score '+scoreClass(composite)+'">'+fmtScore(composite)+'</div>'
        + '<div class="stk-date">'+fmtDateMDY(s.earnings_date)+'</div>'
      + '</div>'
      + detailHtml;
  }

  // The universe is 5,000+ names. Painting every one produced a 200,000px
  // document, which is past the size a compositing layer will paint correctly:
  // layout stayed right while the screen went blank or drew shifted by a few
  // hundred pixels. The old page hid this behind an inner overflow container
  // that capped the painted area; now that the page itself scrolls, the DOM has
  // to be the thing that stays small. Render a window and extend it on scroll.
  const PAGE_ROWS = 120;
  const EXTEND_MARGIN = 900;   // px of runway below the fold to keep filled
  let windowRows = [];
  let shownCount = 0;

  function redrawExpanded() {
    if (!expanded.size) return;
    requestAnimationFrame(() => {
      // Only redraw tickers the window has actually painted. An expanded row
      // that sits beyond the rendered range has no canvas to draw into.
      const painted = new Set(Array.from(listEl.querySelectorAll('.stk-row'))
        .map(r => r.dataset.ticker));
      expanded.forEach(t => {
        if (!painted.has(t)) return;
        if (priceCache[t] !== undefined) renderChartsFor(t);
        else fetchPricesFor(t);
        if (metaOpenByTicker[t]) renderMetaPanelFor(t);
      });
    });
  }

  function paintMore() {
    const next = windowRows.slice(shownCount, shownCount + PAGE_ROWS);
    if (!next.length) return false;
    listEl.insertAdjacentHTML('beforeend',
      next.map((s, i) => rowHtml(s, shownCount + i)).join(''));
    shownCount += next.length;
    return true;
  }

  // Keep painting while the bottom of the list sits inside the runway, so a
  // tall viewport or a short result set fills in one pass. The guard stops a
  // pathological layout from looping forever.
  function fillWindow() {
    // A hidden .stk-table has no client rects, so the runway test below would
    // read bottom = 0 forever and paint the entire universe into a list that is
    // not on screen. Nothing to fill until it can measure itself.
    if (!listEl.getClientRects().length) return;
    let guard = 0, grew = false;
    while (shownCount < windowRows.length && guard++ < 60) {
      // Always paint the first page. The runway test below compares against
      // window.innerHeight, and a viewport that measures zero, which happens in
      // a collapsed pane and in some embedded contexts, makes it true on the
      // first iteration and leaves the list completely empty with no error.
      if (shownCount > 0 &&
          listEl.getBoundingClientRect().bottom > window.innerHeight + EXTEND_MARGIN) break;
      if (!paintMore()) break;
      grew = true;
    }
    if (grew) redrawExpanded();
  }

  let extendQueued = false;
  function onScrollExtend() {
    if (extendQueued || shownCount >= windowRows.length) return;
    extendQueued = true;
    requestAnimationFrame(() => { extendQueued = false; fillWindow(); });
  }
  window.addEventListener('scroll', onScrollExtend, { passive: true });
  window.addEventListener('resize', onScrollExtend, { passive: true });

  // Canvases are sized from clientWidth at draw time, so a resize leaves the
  // bitmap stretched into the new box and, worse, leaves the hit list in the
  // old coordinate frame: clicking a visible dot selects a different company.
  // Debounced, because a drag-resize would otherwise redraw thousands of dots
  // per frame.
  let resizeRedrawT = null;
  function redrawActiveView() {
    clearTimeout(resizeRedrawT);
    resizeRedrawT = setTimeout(function() {
      if (currentView === 'chart') drawChart();
      else if (currentView === 'radar') { drawMap(); renderRadar(); }
    }, 120);
  }
  window.addEventListener('resize', redrawActiveView, { passive: true });

  // A window resize is not the only thing that changes a canvas box: the filter
  // rail reflowing, the focus bar wrapping to a second line and devtools docking
  // all do it while firing no resize event. Observing the elements catches every
  // case, and fires once on observe so the first paint is covered too.
  if (window.ResizeObserver) {
    const ro = new ResizeObserver(redrawActiveView);
    ['stk-map-canvas', 'stk-chart-canvas'].forEach(function(id) {
      const el = document.getElementById(id);
      if (el) ro.observe(el);
    });
  }

  function render() {
    syncFilterCount();
    let filtered = currentFiltered();
    filtered.sort((a, b) => {
      let av, bv;
      if (sortKey === '__score__') {
        av = computeComposite(a); bv = computeComposite(b);
      } else {
        av = a[sortKey]; bv = b[sortKey];
      }
      // Ties and absent values fall back to ticker. Returning 0 for two nulls
      // left most of the universe in arrival order once the composite became
      // gated on scorable, so clicking Score appeared to do nothing at all.
      if (av == null && bv == null) {
        return String(a.ticker || '').localeCompare(String(b.ticker || ''));
      }
      if (av == null) return 1;
      if (bv == null) return -1;
      if (typeof av === 'string') return av.localeCompare(bv) * sortDir;
      if (av === bv) return String(a.ticker || '').localeCompare(String(b.ticker || ''));
      return (av - bv) * sortDir;
    });
    renderHero(filtered);
    const scoredVisible = filtered.reduce(function(n, s) { return n + (s.scorable ? 1 : 0); }, 0);
    const scoreNote = document.getElementById('stk-score-note');
    if (scoreNote) {
      scoreNote.textContent = scoredVisible === filtered.length
        ? ''
        : scoredVisible.toLocaleString() + ' of ' + filtered.length.toLocaleString() +
          ' scored · the rest lack data on at least two of the four factors';
    }
    countEl.textContent = filtered.length === ALL.length
      ? String(ALL.length) + ' stocks'
      : String(filtered.length) + ' of ' + String(ALL.length) + ' stocks';
    if (filtered.length === 0) {
      listEl.innerHTML = '<div class="empty-state">No matches. Adjust filters or clear search.</div>';
      // The chart and the radar have to be told about an empty result too. This
      // returned early, so at the one moment the reader most needs to see that
      // nothing matched, both kept drawing the last set that did.
      windowRows = []; shownCount = 0;
      tetraInvalidate();
      if (currentView === 'chart') drawChart();
      else if (currentView === 'radar') { drawMap(); renderRadar(); }
      return;
    }
    windowRows = filtered;
    shownCount = 0;
    // Emptying the list collapses the document, and the first layout read in
    // fillWindow makes the browser clamp the scroll offset to the top. Put the
    // reader back where they were, then refill: the restored offset may sit
    // below what one page of rows covers.
    const keepY = window.scrollY;
    listEl.innerHTML = '';
    fillWindow();
    if (keepY && window.scrollY !== keepY) {
      window.scrollTo({ top: keepY, behavior: 'instant' });
      fillWindow();
    }
    redrawExpanded();
    // The chart and the radar read the same filtered set, so they have to be
    // redrawn when it changes. Without this they kept whatever they were
    // showing when the view was last opened, which reads as the filters simply
    // not applying to them.
    tetraInvalidate();
    renderFocusBar();
    if (currentView === 'chart') drawChart();
    else if (currentView === 'radar') { drawMap(); renderRadar(); }
  }

  // Card-level toggles inside the expanded panel reveal source + methodology
  // in a single methodology card sandwiched between the 4 factor cards and
  // the charts. Chart time-range tabs and methodology card toggles are
  // handled before the row-toggle handler so they don't collapse the whole row.
  listEl.addEventListener('click', e => {
    const closeBtn = e.target.closest('.fp-meta-card-close');
    if (closeBtn) {
      const detail = closeBtn.closest('.stk-detail');
      const panel = detail && detail.querySelector('.fp-meta-panel');
      const ticker = panel && panel.dataset.ticker;
      if (ticker) {
        metaOpenByTicker[ticker] = null;
        renderMetaPanelFor(ticker);
      }
      return;
    }
    const cardToggle = e.target.closest('.fp-card-toggle');
    if (cardToggle) {
      const dim = cardToggle.dataset.dim;
      const detail = cardToggle.closest('.stk-detail');
      const panel = detail && detail.querySelector('.fp-meta-panel');
      const ticker = panel && panel.dataset.ticker;
      if (ticker && dim) {
        metaOpenByTicker[ticker] = (metaOpenByTicker[ticker] === dim) ? null : dim;
        renderMetaPanelFor(ticker);
      }
      return;
    }
    const chTab = e.target.closest('.ch-tab');
    if (chTab) {
      const card = chTab.closest('.ch-card');
      if (card) {
        const ticker = card.dataset.ticker;
        const range = chTab.dataset.range;
        card.dataset.range = range;
        card.querySelectorAll('.ch-tab').forEach(t => t.classList.toggle('active', t === chTab));
        renderChartsFor(ticker);
      }
      return;
    }
    const row = e.target.closest('.stk-row');
    if (!row) return;
    const t = row.dataset.ticker;
    if (!t) return;
    const wasOpen = expanded.has(t);
    if (wasOpen) expanded.delete(t); else expanded.add(t);
    render();
    if (!wasOpen) {
      fetchThesisFor(t);
      fetchCompanyFor(t);
      fetchNewsFor(t);
      // Defer to next frame so the canvas elements exist in the DOM.
      requestAnimationFrame(() => fetchPricesFor(t));
    }
  });

  searchEl.addEventListener('input', () => {
    query = searchEl.value.trim();
    clearEl.hidden = !query;
    render();
  });
  clearEl.addEventListener('click', () => {
    searchEl.value = ''; query = ''; clearEl.hidden = true; searchEl.focus(); render();
  });
  sectorChipsEl.addEventListener('click', e => {
    const chip = e.target.closest('.lib-chip');
    if (!chip) return;
    activeSector = chip.dataset.sector || '';
    sectorChipsEl.querySelectorAll('.lib-chip').forEach(c => c.classList.toggle('active', c === chip));
    render();
  });
  indexChipsEl.addEventListener('click', e => {
    const chip = e.target.closest('.lib-chip');
    if (!chip) return;
    activeIndex = chip.dataset.index || '';
    indexChipsEl.querySelectorAll('.lib-chip').forEach(c => c.classList.toggle('active', c === chip));
    render();
  });
  // The pill row and the column headers are two controls over one piece of
  // state. They each used to write it and repaint only themselves, so they
  // disagreed the moment either was touched, and disagreed on first paint.
  function syncSortControls() {
    document.querySelectorAll('.stk-th').forEach(function(h) {
      h.classList.remove('asc', 'desc');
    });
    const th = document.querySelector('.stk-th[data-sort="' + sortKey + '"]');
    if (th) th.classList.add(sortDir > 0 ? 'asc' : 'desc');
    document.querySelectorAll('.stk-sort').forEach(function(b) {
      b.classList.toggle('active', b.dataset.sortby === sortKey);
    });
  }

  function setSort(key) {
    if (sortKey === key) sortDir = -sortDir;
    // String columns ascending; numbers, scores and dates descending, so the
    // first click always shows the most interesting end first.
    else { sortKey = key; sortDir = (key === 'ticker' || key === 'name' || key === 'sector') ? 1 : -1; }
    syncSortControls();
    render();
  }

  document.querySelectorAll('.stk-th[data-sort]').forEach(th => {
    th.addEventListener('click', () => setSort(th.dataset.sort));
  });

  // ── Advanced filter panel ───────────────────────────────────────
  const filterPanel = document.getElementById('stk-filter-panel');
  const filterToggle = document.getElementById('stk-filter-toggle');
  const filterCountEl = document.getElementById('stk-filter-count');
  const filterReset = document.getElementById('stk-filter-reset')
    || document.getElementById('stk-reset');
  const onlyEnrichedEl = document.getElementById('stk-only-enriched');

  function syncFilterCount() {
    const c = activeFilterCount();
    // The old single "(n active)" badge is gone; the design counts per section
    // instead, in the red numeral beside each group heading. Both lookups stay
    // null-tolerant so a future markup change degrades to a missing count
    // rather than a dead filter panel.
    if (filterCountEl) {
      filterCountEl.hidden = c === 0;
      filterCountEl.textContent = '(' + c + ' active)';
    }
    if (filterReset) {
      filterReset.hidden = c === 0 && !activeSector && !activeIndex && !query;
    }
    syncSectionCounts();
  }

  // Each rail group reports how many of its own bounds are set. Derived from
  // the DOM rather than a server-side key map, so the two cannot drift.
  function syncSectionCounts() {
    document.querySelectorAll('.stk-rg').forEach(function(grp) {
      const badge = grp.querySelector('[data-setcount]');
      if (!badge) return;
      let n = 0;
      grp.querySelectorAll('.stk-filter-input').forEach(function(inp) {
        // Count what actually filters, and mark what does not, so a value the
        // parser rejected cannot masquerade as an applied bound.
        const rawV = String(inp.value || '').trim();
        const okV = rawV !== '' && parseFilterInput(rawV, inp.dataset.filter) != null;
        inp.classList.toggle('stk-bad', rawV !== '' && !okV);
        if (okV) n++;
      });
      badge.textContent = n ? String(n) : '';
    });
  }

  if (filterToggle && filterPanel) {
    filterToggle.addEventListener('click', () => {
      const open = filterPanel.hasAttribute('hidden');
      if (open) {
        filterPanel.removeAttribute('hidden');
        filterToggle.setAttribute('aria-expanded', 'true');
        filterToggle.classList.add('open');
      } else {
        filterPanel.setAttribute('hidden', '');
        filterToggle.setAttribute('aria-expanded', 'false');
        filterToggle.classList.remove('open');
      }
    });
  }

  document.querySelectorAll('.stk-filter-input').forEach(inp => {
    inp.addEventListener('input', () => {
      const field = inp.dataset.filter;
      const bound = inp.dataset.bound;  // 'min' or 'max'
      if (!filters[field]) filters[field] = { min: null, max: null };
      filters[field][bound] = parseFilterInput(inp.value, field);
      syncFilterCount();
      render();
    });
  });

  document.querySelectorAll('.stk-quick').forEach(btn => {
    btn.addEventListener('click', () => {
      const tier = btn.dataset.tier;
      const range = TIER_RANGES[tier];
      if (!range) return;
      filters.market_cap = { min: range.min, max: range.max };
      // Reflect in inputs
      const minInp = document.querySelector('.stk-filter-input[data-filter="market_cap"][data-bound="min"]');
      const maxInp = document.querySelector('.stk-filter-input[data-filter="market_cap"][data-bound="max"]');
      function fmtCapInput(v) {
        if (v == null) return '';
        if (v >= 1e9) return (v / 1e9) + 'B';
        if (v >= 1e6) return (v / 1e6) + 'M';
        return String(v);
      }
      if (minInp) minInp.value = fmtCapInput(range.min);
      if (maxInp) maxInp.value = fmtCapInput(range.max);
      // Visual highlight
      document.querySelectorAll('.stk-quick').forEach(b => b.classList.toggle('active', b === btn));
      syncFilterCount();
      render();
    });
  });

  if (onlyEnrichedEl) {
    onlyEnrichedEl.addEventListener('change', () => {
      onlyEnriched = onlyEnrichedEl.checked;
      syncFilterCount();
      render();
    });
  }

  if (filterReset) {
    filterReset.addEventListener('click', () => {
      for (const k of Object.keys(filters)) delete filters[k];
      onlyEnriched = false;
      if (onlyEnrichedEl) onlyEnrichedEl.checked = false;
      benfordFilter = '';
      const benSel = document.getElementById('stk-benford-fit');
      if (benSel) benSel.value = '';
      ['Growth','Value','Momentum','Quality'].forEach(dim => { coverageMin[dim] = 0; });
      document.querySelectorAll('.stk-cov-input').forEach(inp => { inp.value = '0'; });
      document.querySelectorAll('.stk-filter-input').forEach(i => i.value = '');
      document.querySelectorAll('.stk-quick').forEach(b => b.classList.remove('active'));
      // Also reset weights to balanced
      ['Growth', 'Value', 'Momentum', 'Quality'].forEach(d => { weights[d] = 1; });
      document.querySelectorAll('.stk-weight-slider').forEach(sl => {
        sl.value = '1';
        const lbl = document.getElementById('stk-w-' + sl.dataset.weight);
        if (lbl) lbl.innerHTML = '1.0&times;';
      });
      // The chips and the search are refinements too. Leaving them set made
      // Reset look broken: the count moved but the universe did not.
      activeSector = '';
      activeIndex = '';
      query = '';
      if (searchEl) searchEl.value = '';
      if (clearEl) clearEl.hidden = true;
      document.querySelectorAll('#stk-sector-chips .lib-chip').forEach(function(c) {
        c.classList.toggle('active', !c.dataset.sector);
      });
      document.querySelectorAll('#stk-index-chips .lib-chip').forEach(function(c) {
        c.classList.toggle('active', !c.dataset.index);
      });
      syncFilterCount();
      render();
    });
  }

  // ── Dimension weight sliders ────────────────────────────────────
  document.querySelectorAll('.stk-weight-slider').forEach(sl => {
    sl.addEventListener('input', () => {
      const dim = sl.dataset.weight;
      const v = parseFloat(sl.value);
      weights[dim] = isNaN(v) ? 0 : v;
      const lbl = document.getElementById('stk-w-' + dim);
      if (lbl) lbl.innerHTML = v.toFixed(1) + '&times;';
      // Composite changes -> Score column + sort if currently on score -> re-render.
      render();
    });
  });

  const WEIGHT_PRESETS = {
    balanced: { Growth: 1,   Value: 1,   Momentum: 1,   Quality: 1   },
    value:    { Growth: 0.5, Value: 1.7, Momentum: 0.5, Quality: 1.3 },
    growth:   { Growth: 1.7, Value: 0.5, Momentum: 1.0, Quality: 0.8 },
    quality:  { Growth: 0.7, Value: 0.7, Momentum: 0.6, Quality: 2.0 },
    momentum: { Growth: 0.8, Value: 0.5, Momentum: 1.8, Quality: 0.9 },
  };
  document.querySelectorAll('.stk-quick[data-preset]').forEach(btn => {
    btn.addEventListener('click', () => {
      const preset = WEIGHT_PRESETS[btn.dataset.preset];
      if (!preset) return;
      for (const [d, w] of Object.entries(preset)) {
        weights[d] = w;
        const sl = document.querySelector('.stk-weight-slider[data-weight="' + d + '"]');
        if (sl) sl.value = String(w);
        const lbl = document.getElementById('stk-w-' + d);
        if (lbl) lbl.innerHTML = w.toFixed(1) + '&times;';
      }
      document.querySelectorAll('.stk-quick[data-preset]').forEach(b => b.classList.toggle('active', b === btn));
      render();
    });
  });

  // ── Benford 1st-digit fit overlay filter ────────────────────────
  const benfordSelect = document.getElementById('stk-benford-fit');
  if (benfordSelect) {
    benfordSelect.addEventListener('change', () => {
      benfordFilter = benfordSelect.value;
      syncFilterCount();
      render();
    });
  }

  // ── Data Hygiene: per-dimension factor coverage thresholds ───────
  document.querySelectorAll('.stk-cov-input').forEach(inp => {
    inp.addEventListener('input', () => {
      const dim = inp.dataset.cov;
      let v = parseInt(inp.value, 10);
      if (!isFinite(v) || v < 0) v = 0;
      if (v > 5) v = 5;
      coverageMin[dim] = v;
      syncFilterCount();
      render();
    });
  });

  // ── Saved Views (localStorage) ──────────────────────────────────
  const SAVED_VIEWS_KEY = 'apt-stocks-saved-views-v1';
  const viewsListEl = document.getElementById('stk-views-list');
  const viewsInputEl = document.getElementById('stk-views-name');
  const viewsSaveBtn = document.getElementById('stk-views-save');

  function loadSavedViews() {
    try { return JSON.parse(localStorage.getItem(SAVED_VIEWS_KEY) || '[]'); } catch (e) { return []; }
  }
  function persistSavedViews(views) {
    try { localStorage.setItem(SAVED_VIEWS_KEY, JSON.stringify(views)); } catch (e) {}
  }
  function captureCurrentView() {
    return {
      query, activeSector, activeIndex,
      filters: JSON.parse(JSON.stringify(filters)),
      onlyEnriched, benfordFilter,
      weights: { Growth: weights.Growth, Value: weights.Value, Momentum: weights.Momentum, Quality: weights.Quality },
      coverageMin: { Growth: coverageMin.Growth, Value: coverageMin.Value, Momentum: coverageMin.Momentum, Quality: coverageMin.Quality },
      sortKey, sortDir,
    };
  }
  function applyView(v) {
    if (!v) return;
    // Search
    query = v.query || '';
    if (searchEl) { searchEl.value = query; clearEl.hidden = !query; }
    // Sector + Index chips
    activeSector = v.activeSector || '';
    activeIndex = v.activeIndex || '';
    sectorChipsEl && sectorChipsEl.querySelectorAll('.lib-chip').forEach(c =>
      c.classList.toggle('active', (c.dataset.sector || '') === activeSector));
    indexChipsEl && indexChipsEl.querySelectorAll('.lib-chip').forEach(c =>
      c.classList.toggle('active', (c.dataset.index || '') === activeIndex));
    // Range filters
    for (const k of Object.keys(filters)) delete filters[k];
    Object.assign(filters, v.filters || {});
    document.querySelectorAll('.stk-filter-input').forEach(inp => {
      const f = inp.dataset.filter, b = inp.dataset.bound;
      const range = filters[f];
      const dataVal = range ? range[b] : null;
      if (dataVal == null) { inp.value = ''; return; }
      // Convert back from data units to display units for percent fields
      // toPrecision(12) collapses the binary noise a round-trip through
      // decimal introduces (7.000000000000001 back to 7) without quantising a
      // value the reader actually typed.
      if (PCT_FIELDS.has(f)) inp.value = String(Number((dataVal * 100).toPrecision(12)));
      // Millions, bare, matching what the field asks for. Writing "300M" here
      // would read as 300 million millions against a label saying $M.
      else if (CAP_FIELDS.has(f)) inp.value = String(Number((dataVal / 1e6).toPrecision(12)));
      else inp.value = String(dataVal);
    });
    // Toggles + select
    onlyEnriched = !!v.onlyEnriched;
    if (onlyEnrichedEl) onlyEnrichedEl.checked = onlyEnriched;
    benfordFilter = v.benfordFilter || '';
    if (benfordSelect) benfordSelect.value = benfordFilter;
    // Coverage thresholds (Data Hygiene)
    ['Growth','Value','Momentum','Quality'].forEach(d => {
      const c = (v.coverageMin && v.coverageMin[d] != null) ? v.coverageMin[d] : 0;
      coverageMin[d] = c;
      const inp = document.querySelector('.stk-cov-input[data-cov="' + d + '"]');
      if (inp) inp.value = String(c);
    });
    // Weights
    if (v.weights) {
      ['Growth','Value','Momentum','Quality'].forEach(d => {
        const w = (v.weights[d] != null) ? v.weights[d] : 1;
        weights[d] = w;
        const sl = document.querySelector('.stk-weight-slider[data-weight="' + d + '"]');
        if (sl) sl.value = String(w);
        const lbl = document.getElementById('stk-w-' + d);
        if (lbl) lbl.innerHTML = w.toFixed(1) + '&times;';
      });
    }
    // Sort
    if (v.sortKey) sortKey = v.sortKey;
    if (typeof v.sortDir === 'number') sortDir = v.sortDir;
    document.querySelectorAll('.stk-th').forEach(th => {
      th.classList.remove('asc', 'desc');
      if (th.dataset.sort === sortKey) th.classList.add(sortDir > 0 ? 'asc' : 'desc');
    });
    syncFilterCount();
    render();
  }
  function renderSavedViews() {
    if (!viewsListEl) return;
    const views = loadSavedViews();
    if (!views.length) {
      viewsListEl.innerHTML = '<span class="stk-views-empty">No saved views yet. Set up filters then save.</span>';
      return;
    }
    viewsListEl.innerHTML = views.map((v, i) =>
      '<span class="stk-views-chip" data-idx="' + i + '">'
      + '<button type="button" class="stk-views-load" data-idx="' + i + '">' + escapeHtml(v.name || 'unnamed') + '</button>'
      + '<button type="button" class="stk-views-del" data-idx="' + i + '" title="Delete">&times;</button>'
      + '</span>'
    ).join('');
  }
  if (viewsSaveBtn && viewsInputEl) {
    viewsSaveBtn.addEventListener('click', () => {
      const name = (viewsInputEl.value || '').trim();
      if (!name) { viewsInputEl.focus(); return; }
      const views = loadSavedViews();
      // Replace by name if it already exists
      const existing = views.findIndex(v => v.name === name);
      const view = Object.assign({ name }, captureCurrentView());
      if (existing >= 0) views[existing] = view;
      else views.push(view);
      persistSavedViews(views);
      viewsInputEl.value = '';
      renderSavedViews();
    });
    viewsInputEl.addEventListener('keydown', e => { if (e.key === 'Enter') viewsSaveBtn.click(); });
  }
  if (viewsListEl) {
    viewsListEl.addEventListener('click', e => {
      const loadBtn = e.target.closest('.stk-views-load');
      const delBtn  = e.target.closest('.stk-views-del');
      if (loadBtn) {
        const idx = parseInt(loadBtn.dataset.idx, 10);
        const views = loadSavedViews();
        if (views[idx]) applyView(views[idx]);
      } else if (delBtn) {
        const idx = parseInt(delBtn.dataset.idx, 10);
        const views = loadSavedViews();
        views.splice(idx, 1);
        persistSavedViews(views);
        renderSavedViews();
      }
    });
  }
  renderSavedViews();

  // ── Universe range hints under each filter input ───────────────
  // For each .stk-filter-stat slot, compute min / max / count of the named
  // field across the universe and render a small "Universe: X to Y across N
  // names" line so users know what values exist before they type.
  function fmtCapShort(v) {
    if (v == null || !isFinite(v)) return '—';
    const a = Math.abs(v);
    if (a >= 1e12) return (v / 1e12).toFixed(1) + 'T';
    if (a >= 1e9)  return (v / 1e9).toFixed(1)  + 'B';
    if (a >= 1e6)  return (v / 1e6).toFixed(1)  + 'M';
    if (a >= 1e3)  return (v / 1e3).toFixed(0)  + 'K';
    return v.toFixed(0);
  }
  function fmtStatVal(v, type) {
    if (v == null || !isFinite(v)) return '—';
    if (type === 'pct')   return (v >= 0 ? '+' : '') + (v * 100).toFixed(1) + '%';
    if (type === 'cap') {
      const sign = v < 0 ? '-' : '';
      return sign + '$' + fmtCapShort(Math.abs(v));
    }
    if (type === 'score') return (v >= 0 ? '+' : '') + v.toFixed(2);
    if (type === 'int')   return String(Math.round(v));
    return v.toFixed(2);
  }
  function populateRangeStats() {
    const slots = document.querySelectorAll('.stk-filter-stat[data-stat-for]');
    slots.forEach(el => {
      const field = el.dataset.statFor;
      const type  = el.dataset.statType || 'ratio';
      let mn = Infinity, mx = -Infinity, n = 0;
      for (const s of ALL) {
        const v = s[field];
        if (v == null || !isFinite(v)) continue;
        if (v < mn) mn = v;
        if (v > mx) mx = v;
        n += 1;
      }
      if (n === 0) {
        el.textContent = 'no data yet';
        el.classList.add('stk-filter-stat-empty');
        return;
      }
      el.textContent = 'Universe: ' + fmtStatVal(mn, type) + ' to ' + fmtStatVal(mx, type) + ' across ' + n.toLocaleString() + ' names';
    });
  }
  // -- Radar ---------------------------------------------------------------
  // Every spoke is a real percentile against sector peers, from the `pct` array
  // the pipeline computes. The design this is ported from generated these
  // numbers by hashing the ticker, which produced a pleasing shape and meant
  // nothing; the shape here is only as complete as the data behind it.
  const RADAR_EXTRA = [
    { key: 'news_vader_avg',  label: 'Headline tone' },
    { key: 'news_count_7d',   label: 'News volume 7d' },
    { key: 'neglect_score',   label: 'Neglect' },
  ];
  const RADAR_FAMS = [
    { name: 'Momentum', quad: 2, fields: ['return_12_2','return_1m','high52w_proximity','rel_strength_sp500','volume_trend'] },
    { name: 'Growth',   quad: 3, fields: ['revenue_growth_yoy','eps_growth_yoy','revenue_acceleration','gross_margin_trend','fcf_growth_yoy'] },
    { name: 'Quality',  quad: 0, fields: ['roe_ttm','earnings_consistency','net_debt_ebitda','op_margin_stability','accruals_ratio'] },
    { name: 'Value',    quad: 1, fields: ['pe','ev_ebitda','ev_revenue','price_book','fcf_yield'] },
  ];
  const SPOKE_LABELS = {
    return_12_2:'12-2 return', return_1m:'1-month return', high52w_proximity:'52w high proximity',
    rel_strength_sp500:'Rel strength vs S&P', volume_trend:'Volume trend',
    revenue_growth_yoy:'Revenue growth YoY', eps_growth_yoy:'EPS growth YoY',
    revenue_acceleration:'Revenue acceleration', gross_margin_trend:'Gross margin trend',
    fcf_growth_yoy:'FCF growth YoY', roe_ttm:'ROE (TTM)', earnings_consistency:'Earnings consistency',
    net_debt_ebitda:'Net debt/EBITDA', op_margin_stability:'Op margin stability',
    accruals_ratio:'Accruals ratio', pe:'P/E trailing', ev_ebitda:'EV/EBITDA',
    ev_revenue:'EV/Revenue', price_book:'Price/Book', fcf_yield:'FCF yield',
  };
  // Spokes in drawing order: quadrant by quadrant, clockwise from the top.
  const SPOKES = [].concat.apply([], RADAR_FAMS
    .slice().sort(function(a,b){ return a.quad - b.quad; })
    .map(function(f){ return f.fields.map(function(k){ return { fam: f.name, key: k }; }); }));

  // Up to three companies overlaid at once. More than three and the polygons
  // stop being readable against each other, which is the whole point of the view.
  const RADAR_MAX = 3;     // polygons the radar can overlay and stay readable
  const FOCUS_MAX = 6;     // companies the focus can hold, one per distinct colour
  const RADAR_COLORS = ['var(--apt-red)', '#2E6F8E', '#B4832B',
                        '#4C7A5A', '#8A5A9B', '#A8562F'];
  // The single selection. radarTickers keeps its name because every radar call
  // site already reads it; it is the focus list now, and the radar takes the
  // first RADAR_MAX of it.
  let radarTickers = [];

  function focusIndexOf(t) { return radarTickers.indexOf(t); }

  // Canvas takes CSS colours but not CSS variables, and fails silently on the
  // difference. Resolve against the root so the palette stays theme-aware
  // rather than hardcoding a hex that would be wrong in one of the two themes.
  function cssColor(c) {
    if (!c || c.slice(0, 4) !== 'var(') return c;
    const name = c.slice(4, -1).trim();
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim() || '#FF4A1C';
  }
  function focusColor(t) {
    const i = focusIndexOf(t);
    return i < 0 ? null : RADAR_COLORS[i % RADAR_COLORS.length];
  }

  function radarAdd(t) {
    if (!t || radarTickers.indexOf(t) !== -1) return false;
    if (radarTickers.length >= FOCUS_MAX) radarTickers.shift();
    radarTickers.push(t);
    return true;
  }

  // Plain click replaces the selection, the way clicking a row in any list
  // does. A modifier extends it, and clicking a company already in the set
  // removes it, so the same gesture undoes itself.
  function focusToggle(t, additive) {
    if (!t) return;
    if (!additive) {
      radarTickers = (radarTickers.length === 1 && radarTickers[0] === t) ? [] : [t];
    } else {
      const i = radarTickers.indexOf(t);
      if (i >= 0) radarTickers.splice(i, 1);
      else {
        if (radarTickers.length >= FOCUS_MAX) radarTickers.shift();
        radarTickers.push(t);
      }
    }
    renderFocusBar();
    if (currentView === 'chart') drawChart();
    else if (currentView === 'radar') { drawMap(); renderRadar(); }
  }

  // Every path that changes the focus or the filtered set repaints through
  // here. Four call sites used to decide this individually and two of them
  // forgot the map, which sits directly beside the control that changed.
  function refreshFocusViews() {
    renderFocusBar();
    if (currentView === 'chart') drawChart();
    else if (currentView === 'radar') { drawMap(); renderRadar(); }
  }

  function renderFocusBar() {
    const bar = document.getElementById('stk-focus');
    if (!bar) return;
    if (!radarTickers.length) {
      bar.innerHTML = '<span class="stk-focus-hint">Click a company to focus it, ctrl-click to add more</span>';
      return;
    }
    const byTicker = {};
    for (const s of ALL) byTicker[s.ticker] = s;
    // A filter can exclude something already focused. Dropping it silently
    // would lose the reader's selection; drawing it as though nothing happened
    // would explain neither the missing dot nor the surviving radar polygon.
    const shown = new Set();
    try { for (const s of currentFiltered()) shown.add(s.ticker); } catch (e) {}
    bar.hidden = false;
    bar.innerHTML =
      '<span class="stk-focus-lab">Focus</span>' +
      radarTickers.map(function(t, i) {
        const s = byTicker[t] || { ticker: t, name: '' };
        const out = shown.size && !shown.has(t);
        return '<span class="stk-cmp' + (out ? ' stk-cmp-out' : '') + '"' +
          (out ? ' title="Outside the current filters"' : '') +
          ' style="--cmp:' + RADAR_COLORS[i % RADAR_COLORS.length] + '">' +
          '<i></i>' + escapeHtml(s.ticker) +
          '<button type="button" class="stk-cmp-x" data-drop="' + escapeHtml(t) +
          '" aria-label="Remove ' + escapeHtml(t) + '">\u00d7</button></span>';
      }).join('') +
      '<button type="button" class="stk-focus-clear" id="stk-focus-clear">Clear</button>';
  }

  function drawRadar(picks) {
    const svg = document.getElementById('stk-radar-svg');
    const note = document.getElementById('stk-radar-note');
    if (!svg) return;
    const CX = 310, CY = 310, R = 232;
    const n = SPOKES.length;
    const ang = function(i) { return (i / n) * Math.PI * 2 - Math.PI / 2; };
    const at = function(i, frac) {
      const a = ang(i), r = R * frac;
      return [CX + Math.cos(a) * r, CY + Math.sin(a) * r];
    };
    let out = '';
    // Rings at 25 / 50 / 75; the 50 ring is dashed because it is the median.
    [0.25, 0.5, 0.75, 1].forEach(function(f) {
      const pts = SPOKES.map(function(_, i) { return at(i, f).map(Math.round).join(','); }).join(' ');
      out += '<polygon points="' + pts + '" fill="none" stroke="var(--border-bright)" stroke-width="1"' +
             (f === 0.5 ? ' stroke-dasharray="3 4"' : ' opacity="0.5"') + '/>';
    });
    SPOKES.forEach(function(sp, i) {
      const p = at(i, 1);
      out += '<line x1="' + CX + '" y1="' + CY + '" x2="' + Math.round(p[0]) + '" y2="' + Math.round(p[1]) +
             '" stroke="var(--border)" stroke-width="1"/>';
      const lp = at(i, 1.1), a = ang(i);
      const anchor = Math.abs(Math.cos(a)) < 0.25 ? 'middle' : (Math.cos(a) > 0 ? 'start' : 'end');
      out += '<text x="' + Math.round(lp[0]) + '" y="' + Math.round(lp[1]) + '" text-anchor="' + anchor +
             '" dominant-baseline="middle" font-size="10" fill="var(--text-4)" ' +
             'font-family="var(--font-ui, inherit)">' + escapeHtml(SPOKE_LABELS[sp.key] || sp.key) + '</text>';
    });
    // One polygon per company. Absent spokes break the path rather than being
    // drawn as zero: "we have no reading" and "it scores nothing" are different
    // claims. Later picks draw on top, with fill opacity low enough that an
    // overlap still reads as two shapes.
    const counts = [];
    picks.forEach(function(s, ci) {
      const col = RADAR_COLORS[ci % RADAR_COLORS.length];
      const have = [];
      SPOKES.forEach(function(sp, i) {
        const v = pctOf(s, sp.key);
        if (v != null) have.push({ i: i, p: at(i, Math.max(0.04, v / 100)) });
      });
      counts.push({ ticker: s.ticker, n: have.length });
      if (have.length < 3) return;
      const pts = have.map(function(h) { return h.p.map(Math.round).join(','); }).join(' ');
      out += '<polygon points="' + pts + '" fill="' + col + '" fill-opacity="' +
             (picks.length > 1 ? '0.10' : '0.18') + '" stroke="' + col +
             '" stroke-width="2" stroke-linejoin="round"/>';
      have.forEach(function(h) {
        out += '<circle cx="' + Math.round(h.p[0]) + '" cy="' + Math.round(h.p[1]) +
               '" r="2.5" fill="' + col + '"/>';
      });
    });
    svg.innerHTML = out;
    if (note) {
      if (picks.length === 1) {
        const s = picks[0], k = counts[0].n;
        note.textContent = k + ' of ' + n + ' inputs available. Each spoke is a percentile ' +
          'against ' + (s.sector || 'sector') + ' peers; the dashed ring is the median. ' +
          (k < n ? 'Gaps are inputs this company has no data for, not zero scores.' : '');
      } else {
        note.textContent = 'Each spoke is a percentile against that company\u2019s own sector peers, ' +
          'so the shapes are comparable even across sectors. Coverage: ' +
          counts.map(function(c) { return c.ticker + ' ' + c.n + '/' + n; }).join(', ') + '.';
      }
    }
    return counts;
  }

  // Chip removal is delegated: the chips are rebuilt on every render, so a
  // listener bound to each one would leak.
  (function wireRadarChips() {
    const focusBar = document.getElementById('stk-focus');
    if (focusBar && !focusBar.dataset.wired) {
      focusBar.dataset.wired = '1';
      focusBar.addEventListener('click', function(e) {
        if (e.target.id === 'stk-focus-clear') {
          radarTickers = [];
          refreshFocusViews();
          return;
        }
        const x = e.target.closest('[data-drop]');
        if (!x) return;
        radarTickers = radarTickers.filter(function(t) { return t !== x.dataset.drop; });
        refreshFocusViews();
      });
    }
    const host = document.getElementById('stk-radar-chips');
    if (!host) return;
    host.addEventListener('click', function(e) {
      const btn = e.target.closest('.stk-cmp-x');
      if (!btn) return;
      const t = btn.dataset.drop;
      radarTickers = radarTickers.filter(function(x) { return x !== t; });
      // The bar is the legend for these colours, and the map is what they key.
      // Repainting one without the other left the removed company highlighted on
      // the plot beside this very control.
      refreshFocusViews();
    });
  })();

  function renderRadar() {
    const side = document.getElementById('stk-radar-breakdown');
    const title = document.getElementById('stk-radar-title');
    const hint = document.getElementById('stk-radar-hint');
    const svg = document.getElementById('stk-radar-svg');
    if (!side) return;
    // The focus can hold more than the radar can legibly overlay.
    const picks = radarTickers.slice(0, RADAR_MAX)
      .map(function(t) { return ALL.find(function(x) { return x.ticker === t; }); })
      .filter(Boolean);
    const chipsEl = document.getElementById('stk-radar-chips');

    const listEl2 = document.getElementById('stk-radar-picklist');
    if (!picks.length) {
      if (svg) svg.innerHTML = '';
      if (chipsEl) chipsEl.innerHTML = '';
      if (title) title.textContent = 'Pick a company';
      if (hint) hint.textContent = 'Click one on the map, or take one from here. '
        + 'Up to ' + RADAR_MAX + ' compare on the same rings.';
      side.innerHTML = '';
      renderPickList();
      // The rings are still worth drawing empty: an outline says the shape a
      // company will take, where a blank rectangle says nothing at all.
      drawRadar([]);
      return;
    }
    if (listEl2) { listEl2.hidden = true; listEl2.innerHTML = ''; }

    if (title) {
      title.textContent = picks.length === 1
        ? picks[0].ticker + ' against its sector'
        : picks.map(function(s) { return s.ticker; }).join(' vs ');
    }
    if (hint) {
      hint.textContent = picks.length === 1
        ? (picks[0].name || '') + (picks[0].sector ? ' \u00b7 ' + picks[0].sector
            : ' \u00b7 no sector, so no peer group')
        : 'Add up to ' + RADAR_MAX + '. Each is ranked inside its own sector.';
    }
    if (chipsEl) {
      chipsEl.innerHTML = picks.map(function(s, i) {
        return '<span class="stk-cmp" style="--cmp:' + RADAR_COLORS[i % RADAR_COLORS.length] + '">' +
          '<i></i>' + escapeHtml(s.ticker) +
          '<button type="button" class="stk-cmp-x" data-drop="' + escapeHtml(s.ticker) +
          '" aria-label="Remove ' + escapeHtml(s.ticker) + '">\u00d7</button></span>';
      }).join('');
    }

    drawRadar(picks);

    // Breakdown: one value column per company, colour-keyed to its polygon. The
    // percentile bar only earns its width when there is a single company.
    const solo = picks.length === 1;
    side.innerHTML = RADAR_FAMS.map(function(f) {
      const famKey = { Growth: 'g', Value: 'v', Momentum: 'm', Quality: 'q' }[f.name];
      const heads = picks.map(function(s, i) {
        return '<span class="stk-radar-val" style="color:' +
          RADAR_COLORS[i % RADAR_COLORS.length] + '">' + fmtScore(s[famKey]) + '</span>';
      }).join('');
      const rows = f.fields.map(function(k) {
        const vals = picks.map(function(s, i) {
          const v = pctOf(s, k);
          return '<span class="stk-radar-val' + (v == null ? ' na' : '') + '"' +
            (v == null ? ' title="' + escapeHtml(naReason(s, k)) + '"' : '') +
            (solo ? '' : ' style="color:' + RADAR_COLORS[i % RADAR_COLORS.length] + '"') + '>' +
            (v == null ? '\u2014' : v) + '</span>';
        }).join('');
        const bar = solo
          ? '<span class="stk-radar-bar"><i style="width:' +
            (pctOf(picks[0], k) == null ? 0 : pctOf(picks[0], k)) + '%"></i></span>'
          : '';
        return '<div class="stk-radar-row' + (solo ? '' : ' cmp') + '">' +
          '<span>' + escapeHtml(SPOKE_LABELS[k] || k) + '</span>' + bar + vals + '</div>';
      }).join('');
      return '<div class="stk-radar-fam"><div class="stk-radar-fam-h' + (solo ? '' : ' cmp') + '">' +
        '<b>' + f.name + '</b>' + heads + '</div>' + rows + '</div>';
    }).join('') + radarExtraBlock(picks, solo);
  }

  // Sentiment and coverage, ranked against sector peers like everything above,
  // but kept in their own block rather than given a spoke. A fifth spoke would
  // read as a fifth factor, and headline tone is not one: it is not in any
  // dimension and not in the composite. Separating it says so without a
  // footnote.
  // A compact version of the list view, scoped to the filters, so the third
  // column is a way to choose rather than a paragraph telling you to choose
  // somewhere else. Capped: this is a picker, not the list view.
  const PICKLIST_MAX = 220;

  function renderPickList() {
    const el = document.getElementById('stk-radar-picklist');
    if (!el) return;
    let rows;
    try { rows = currentFiltered(); } catch (e) { rows = []; }
    // Score first, then ticker. Two unscored companies used to compare equal,
    // which left them in arrival order while the header claimed they were ranked.
    const withScore = rows.map(function(s) { return { s: s, c: computeComposite(s) }; });
    withScore.sort(function(a, b) {
      if (a.c == null && b.c == null) {
        return String(a.s.ticker || '').localeCompare(String(b.s.ticker || ''));
      }
      if (a.c == null) return 1;
      if (b.c == null) return -1;
      return b.c - a.c;
    });
    const scoredCount = withScore.reduce(function(n, r) { return n + (r.c != null ? 1 : 0); }, 0);
    rows = withScore.map(function(r) { return r.s; });
    const shown = rows.slice(0, PICKLIST_MAX);
    if (!shown.length) {
      el.hidden = false;
      el.innerHTML = '<div class="stk-pick-empty">Nothing matches the current filters.</div>';
      return;
    }
    el.hidden = false;
    el.innerHTML =
      '<div class="stk-pick-h">' + rows.length.toLocaleString() + ' matching' +
      (scoredCount === 0
        ? ', none scored, by ticker'
        : (rows.length > PICKLIST_MAX
            ? ', top ' + PICKLIST_MAX + ' of ' + scoredCount.toLocaleString() + ' scored'
            : ', ' + scoredCount.toLocaleString() + ' scored')) + '</div>' +
      shown.map(function(s) {
        const c = computeComposite(s);
        return '<button type="button" class="stk-pick" data-pick="' + escapeHtml(s.ticker) + '">' +
          '<span class="stk-pick-tk">' + escapeHtml(s.ticker) + '</span>' +
          '<span class="stk-pick-nm">' + escapeHtml(s.name || '') + '</span>' +
          '<span class="stk-pick-sc ' + scoreClass(c) + '">' + fmtScore(c) + '</span>' +
          '</button>';
      }).join('');
  }

  (function wirePickList() {
    const el = document.getElementById('stk-radar-picklist');
    if (!el) return;
    el.addEventListener('click', function(e) {
      const b = e.target.closest('[data-pick]');
      if (!b) return;
      focusToggle(b.dataset.pick, e.ctrlKey || e.metaKey || e.shiftKey);
    });
  })();

  // A dash in these panels had no explanation, and it covers three different
  // situations that a reader cannot tell apart: the company does not report the
  // line, the value exists but too few sector peers report it to rank against,
  // or a fetch has not reached it yet. Annaly is a mortgage REIT with no
  // meaningful EBITDA and no capital expenditure line, so its blanks are
  // correct and permanent; Micron's single blank is a fetch that needs one more
  // quarter. Those deserve different words.
  function naReason(s, key) {
    const code = (s.status || {})[key];
    if (code && FIELD_STATUS[code]) return FIELD_STATUS[code];
    const m = FIELD_METHODS[key];
    if (s[key] == null) {
      if (m && (m.source === 'edgar' || m.source === 'form4')) {
        // No apostrophe here on purpose. This is a Python string that becomes
        // JavaScript, and Python consumes the backslash in an escaped quote
        // before the browser ever sees it, leaving a bare quote that closes the
        // string early and breaks the entire script block. Wording around it is
        // safer than escaping twice and hoping the next edit preserves it.
        return 'Not reported in the filings for this company. Financial and property '
             + 'companies often do not file the line this needs, in which case the '
             + 'blank is permanent rather than pending.';
      }
      return 'No value from the source for this company.';
    }
    return 'Measured, but fewer than ' + MIN_COHORT + ' sector peers report it, so a '
         + 'percentile would claim precision the sample cannot support. The value '
         + 'itself is in the factor card above.';
  }

  function radarExtraBlock(picks, solo) {
    const rows = RADAR_EXTRA.map(function(x) {
      const vals = picks.map(function(s, i) {
        const v = pctOf(s, x.key);
        return '<span class="stk-radar-val' + (v == null ? ' na' : '') + '"' +
          (v == null ? ' title="' + escapeHtml(naReason(s, x.key)) + '"' : '') +
          (solo ? '' : ' style="color:' + RADAR_COLORS[i % RADAR_COLORS.length] + '"') + '>' +
          (v == null ? '—' : v) + '</span>';
      }).join('');
      const p0 = pctOf(picks[0], x.key);
      const bar = solo
        ? '<span class="stk-radar-bar"><i style="width:' + (p0 == null ? 0 : p0) + '%"></i></span>'
        : '';
      return '<div class="stk-radar-row' + (solo ? '' : ' cmp') + '">' +
        '<span>' + escapeHtml(x.label) + '</span>' + bar + vals + '</div>';
    }).join('');
    return '<div class="stk-radar-fam"><div class="stk-radar-fam-h' + (solo ? '' : ' cmp') + '">' +
      '<b>Coverage</b></div>' + rows +
      '<p class="stk-radar-foot">Percentiles against sector peers, the same as above. ' +
      'These are shown, not scored: none of them feeds a factor or the composite. ' +
      'Hover a dash to see why it is blank.</p></div>';
  }

  // -- Chart ---------------------------------------------------------------
  // Canvas, not SVG: an unfiltered view is 5,336 points, and that many DOM
  // nodes costs far more than it buys when each one is a 3px dot.
  // Everything worth plotting, grouped the way the rail groups its filters.
  // log marks quantities that span orders of magnitude and are meaningless on a
  // linear axis: market cap runs from millions to trillions in one column.
  const AXIS_FIELDS = [
    { k:'__score__',          label:'Composite score',      g:'Score' },
    { k:'g',                  label:'Growth score',         g:'Score' },
    { k:'v',                  label:'Value score',          g:'Score' },
    { k:'m',                  label:'Momentum score',       g:'Score' },
    { k:'q',                  label:'Quality score',        g:'Score' },

    { k:'market_cap',         label:'Market cap',           g:'Size',  log:true },
    { k:'price',              label:'Price',                g:'Size',  log:true },
    { k:'volume',             label:'Volume',               g:'Size',  log:true },
    { k:'change_pct',         label:'1 day move',           g:'Size' },

    { k:'pe',                 label:'P/E trailing',         g:'Value' },
    { k:'ev_ebitda',          label:'EV/EBITDA',            g:'Value' },
    { k:'ev_revenue',         label:'EV/Revenue',           g:'Value' },
    { k:'price_book',         label:'Price/Book',           g:'Value' },
    { k:'fcf_yield',          label:'FCF yield',            g:'Value' },

    { k:'revenue_growth_yoy', label:'Revenue growth YoY',   g:'Growth' },
    { k:'eps_growth_yoy',     label:'EPS growth YoY',       g:'Growth' },
    { k:'revenue_acceleration', label:'Revenue acceleration', g:'Growth' },
    { k:'fcf_growth_yoy',     label:'FCF growth YoY',       g:'Growth' },

    { k:'roe_ttm',            label:'ROE (TTM)',            g:'Quality' },
    { k:'gross_margin',       label:'Gross margin',         g:'Quality' },
    { k:'operating_margin',   label:'Operating margin',     g:'Quality' },
    { k:'net_debt_ebitda',    label:'Net debt/EBITDA',      g:'Quality' },
    { k:'earnings_consistency', label:'Earnings consistency', g:'Quality' },
    { k:'accruals_ratio',     label:'Accruals ratio',       g:'Quality' },

    { k:'return_12_2',        label:'12-2 return',          g:'Momentum' },
    { k:'return_1m',          label:'1 month return',       g:'Momentum' },
    { k:'return_52w',         label:'52 week return',       g:'Momentum' },
    { k:'high52w_proximity',  label:'52w high proximity',   g:'Momentum' },
    { k:'rel_strength_sp500', label:'Rel strength vs S&P',  g:'Momentum' },
    { k:'volume_trend',       label:'Volume trend',         g:'Momentum' },

    { k:'volatility_1y',      label:'Volatility 1y',        g:'Risk' },
    { k:'beta_1y',            label:'Beta vs S&P',          g:'Risk' },
    { k:'sharpe_1y',          label:'Sharpe 1y',            g:'Risk' },
    { k:'max_drawdown_1y',    label:'Max drawdown 1y',      g:'Risk' },

    { k:'neglect_score',      label:'Neglect',              g:'Coverage' },
    { k:'analyst_count',      label:'Analyst count',        g:'Coverage' },
    { k:'inst_ownership',     label:'Institutional %',      g:'Coverage' },
    { k:'insider_ownership',  label:'Insider %',            g:'Coverage' },
    { k:'news_count_7d',      label:'News volume 7d',       g:'Coverage' },
    { k:'news_vader_avg',     label:'Headline tone',        g:'Coverage' },
  ];
  const AXIS_BY_KEY = {};
  for (const f of AXIS_FIELDS) AXIS_BY_KEY[f.k] = f;

  // One-click starting points, which is all the old lenses ever were.
  const PRESETS = [
    { label:'Compounders',      x:'q',              y:'g',  c:'' },
    { label:'Cheap and moving', x:'v',              y:'m',  c:'' },
    { label:'Sentiment',        x:'news_vader_avg', y:'m',  c:'' },
    { label:'Risk vs reward',   x:'volatility_1y',  y:'sharpe_1y', c:'beta_1y' },
    { label:'Neglect',          x:'neglect_score',  y:'q',  c:'market_cap' },
    { label:'Size vs score',    x:'market_cap',     y:'__score__', c:'neglect_score' },
  ];

  let axisX = 'q', axisY = 'g', axisC = '';
  let pairsMode = false;

  function axisLabel(k) {
    const f = AXIS_BY_KEY[k];
    return f ? f.label : k;
  }

  // A log axis needs a positive value, so non-positive readings drop out rather
  // than being clamped into a lie about where they sit.
  function axisValue(s, k) {
    const v = (k === '__score__') ? computeComposite(s) : s[k];
    if (v == null || !isFinite(v)) return null;
    const f = AXIS_BY_KEY[k];
    if (f && f.log) return v > 0 ? Math.log10(v) : null;
    return v;
  }


  // ---- 3D factor space -------------------------------------------------
  // A regular tetrahedron centred on the origin. The four unit vectors sum to
  // zero, so a company that scores evenly across the factors lands in the
  // middle and a lopsided one is pushed toward whichever vertices it earns.
  const TETRA_V = {
    g: [ 1,  1,  1],
    v: [ 1, -1, -1],
    m: [-1,  1, -1],
    q: [-1, -1,  1],
  };
  const TETRA_ORDER = ['g', 'v', 'm', 'q'];
  const TETRA_LABEL = { g: 'Growth', v: 'Value', m: 'Momentum', q: 'Quality' };
  const TETRA_EDGES = [['g','v'],['g','m'],['g','q'],['v','m'],['v','q'],['m','q']];
  const RT3 = Math.sqrt(3);
  let yaw = 0.62, pitch = -0.32, spinRAF = null, suppressNextClick = false;
  // Honour the OS setting rather than offering motion this reader has already
  // said they do not want.
  const REDUCED_MOTION = window.matchMedia &&
    window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  let spin = false;
  let tetraPts = null, tetraRef = 1, tetraMatch = 0;
  function tetraInvalidate() { tetraPts = null; }

  function tetraPos(s) {
    const w = { g: s.g || 0, v: s.v || 0, m: s.m || 0, q: s.q || 0 };
    let x = 0, y = 0, z = 0;
    for (const k of TETRA_ORDER) {
      const V = TETRA_V[k];
      x += w[k] * V[0]; y += w[k] * V[1]; z += w[k] * V[2];
    }
    return [x / RT3, y / RT3, z / RT3];
  }

  // Yaw about the vertical, then pitch, then a weak perspective divide. The
  // perspective is deliberately mild: enough that the near face reads as nearer,
  // not so much that it distorts the cloud into saying something untrue.
  function project3(p, w, h, scale) {
    const cy = Math.cos(yaw), sy = Math.sin(yaw);
    const cp = Math.cos(pitch), sp = Math.sin(pitch);
    const x1 = p[0] * cy + p[2] * sy;
    const z1 = -p[0] * sy + p[2] * cy;
    const y2 = p[1] * cp - z1 * sp;
    const z2 = p[1] * sp + z1 * cp;
    const d = 4.6, k = d / (d - z2 * 0.5);
    return { x: w / 2 + x1 * scale * k, y: h / 2 - y2 * scale * k, z: z2, k: k };
  }

  function startSpin() {
    if (spinRAF || !spin || currentView !== 'radar') return;
    spinRAF = requestAnimationFrame(function step() {
      spinRAF = null;
      if (!spin || currentView !== 'radar') return;
      yaw += 0.0030;
      drawMap();
      spinRAF = requestAnimationFrame(step);
    });
  }
  function stopSpin() {
    if (spinRAF) { cancelAnimationFrame(spinRAF); spinRAF = null; }
  }

  function chartPoints() {
    const out = [];
    for (const s of currentFiltered()) {
      // The paired view is a different shape of chart: two panels, each
      // deriving its own pair of coordinates, so it takes whole rows.
      if (pairsMode) {
        if (!s.scorable) continue;
        out.push({ s: s });
        continue;
      }
      const x = axisValue(s, axisX), y = axisValue(s, axisY);
      if (x == null || y == null) continue;
      out.push({ s: s, x: x, y: y, c: axisC ? axisValue(s, axisC) : undefined });
    }
    return out;
  }

  let chartHit = [];

  const DENSITY_ABOVE = 420;   // beyond this, dots stop resolving into anything
  const CELL = 13;

  // proj is [{x, y, s}] in canvas pixels. Hits are pushed for every point even
  // in density mode, so hovering still names the nearest company rather than
  // only the ones drawn on top.
  // Focused points, drawn on top of whatever cloud style was used, in their
  // focus colour with the ticker beside them.
  // True only if something in the focus is actually plotted here.
  function focusDrawn(proj) {
    if (!radarTickers.length) return false;
    for (const q of proj) if (focusIndexOf(q.s.ticker) >= 0) return true;
    return false;
  }

  // Focused companies with no coordinates on the active lens, so the reader is
  // told rather than left wondering why a selection has no highlight.
  function focusMissing(proj) {
    if (!radarTickers.length) return [];
    const drawn = new Set();
    for (const q of proj) if (focusIndexOf(q.s.ticker) >= 0) drawn.add(q.s.ticker);
    return radarTickers.filter(function(t) { return !drawn.has(t); });
  }

  // The sentence the footers append. Selecting a company and then switching to
  // a lens it has no value for makes its highlight disappear with no
  // explanation, which reads as the focus having been lost rather than the
  // company having no data on these axes.
  function focusMissingNote(proj) {
    const gone = focusMissing(proj);
    if (!gone.length) return '';
    const names = gone.length > 4
      ? gone.slice(0, 4).join(', ') + ' and ' + (gone.length - 4) + ' more'
      : gone.join(', ');
    return ' \u00b7 ' + (gone.length === 1 ? 'focused company not on these axes: '
                                          : 'focused companies not on these axes: ') + names;
  }

  function paintFocused(ctx, proj) {
    if (!radarTickers.length) return;
    ctx.font = "10px 'Space Mono', monospace";
    for (const q of proj) {
      const col = cssColor(focusColor(q.s.ticker));
      if (!col) continue;
      ctx.beginPath(); ctx.arc(q.x, q.y, 5.5, 0, Math.PI * 2);
      ctx.fillStyle = col; ctx.globalAlpha = 1; ctx.fill();
      ctx.lineWidth = 1.5; ctx.strokeStyle = col;
      ctx.beginPath(); ctx.arc(q.x, q.y, 9, 0, Math.PI * 2); ctx.stroke();
      ctx.fillStyle = col;
      ctx.fillText(q.s.ticker, q.x + 12, q.y + 3.5);
    }
    ctx.globalAlpha = 1;
  }

  // Colour runs cool to warm across the plotted range, by rank rather than by
  // value, so one outlier cannot flatten everyone else into a single shade.
  function colourRamp(t) {
    const stops = [[46,111,142], [122,150,140], [196,166,96], [200,110,60], [190,60,45]];
    const p = Math.max(0, Math.min(0.999, t)) * (stops.length - 1);
    const i = Math.floor(p), f = p - i;
    const a = stops[i], b = stops[Math.min(i + 1, stops.length - 1)];
    return 'rgb(' + Math.round(a[0] + (b[0] - a[0]) * f) + ',' +
                    Math.round(a[1] + (b[1] - a[1]) * f) + ',' +
                    Math.round(a[2] + (b[2] - a[2]) * f) + ')';
  }

  function rankColours(proj) {
    const vals = proj.map(function(p) { return p.c; })
                     .filter(function(v) { return v != null && isFinite(v); })
                     .sort(function(a, b) { return a - b; });
    if (vals.length < 2) return null;
    return function(v) {
      if (v == null || !isFinite(v)) return null;
      let lo = 0, hi = vals.length;
      while (lo < hi) { const mid = (lo + hi) >> 1; if (vals[mid] <= v) lo = mid + 1; else hi = mid; }
      return colourRamp(lo / vals.length);
    };
  }

  // Axis values reach the chart already log10'd where the field asks for it,
  // so a legend printing them raw would label market cap 6.0 to 12.5 rather
  // than $1M to $3T. Undo the transform for display, then format in the
  // field's own units.
  function fmtAxisValue(v, k) {
    if (v == null || !isFinite(v)) return '—';
    const f = AXIS_BY_KEY[k] || {};
    const raw = f.log ? Math.pow(10, v) : v;
    if (CAP_FIELDS.has(k)) return fmtCapShort(raw);
    if (PCT_FIELDS.has(k)) return (raw * 100).toFixed(1) + '%';
    return Math.abs(raw) >= 1000 ? fmtCapShort(raw) : raw.toFixed(2);
  }

  // A ramp with no key is decoration. This says which end is which, in the
  // units of the field itself, and names the outline used for missing values.
  function drawColourLegend(ctx, box, label, lo, hi, missing, ink, dim) {
    const W = 104, H = 7;
    const x = box.x + box.w - W - 14, y = box.y + 16;
    for (let i = 0; i < W; i++) {
      ctx.fillStyle = colourRamp(i / (W - 1));
      ctx.globalAlpha = 0.85;
      ctx.fillRect(x + i, y, 1, H);
    }
    ctx.globalAlpha = 1;
    ctx.font = "9px 'Space Mono', monospace";
    ctx.fillStyle = dim;
    ctx.textAlign = 'left';
    ctx.fillText(label.toUpperCase(), x, y - 4);
    ctx.fillText(lo, x, y + H + 9);
    ctx.textAlign = 'right';
    ctx.fillText(hi, x + W, y + H + 9);
    if (missing) {
      ctx.beginPath(); ctx.arc(x + 4, y + H + 20, 3.2, 0, Math.PI * 2);
      ctx.strokeStyle = ink; ctx.lineWidth = 1; ctx.globalAlpha = 0.5; ctx.stroke();
      ctx.globalAlpha = 1;
      ctx.fillStyle = dim;
      ctx.textAlign = 'left';
      ctx.fillText(missing.toLocaleString() + ' no value', x + 11, y + H + 23);
    }
    ctx.textAlign = 'left';
  }

  function paintCloud(ctx, proj, ink, hits, capMaxIn) {
    if (!proj.length) return;
    const capMax = capMaxIn ||
      Math.max.apply(null, proj.map(function(p) { return p.s.market_cap || 0; })) || 1;
    // With a selection actually visible here, the rest of the cloud is context.
    // Keyed on what is drawn, not on the list: a focus that this lens cannot
    // place would otherwise dim everything to highlight nothing.
    const ctxDim = focusDrawn(proj) ? 0.45 : 1;
    // A colour field is information the density shading cannot carry, so when
    // one is chosen the cloud goes back to dots whatever its size.
    const colourOf = proj.length && proj[0].c !== undefined ? rankColours(proj) : null;

    if (colourOf || proj.length <= DENSITY_ABOVE) {
      for (const q of proj) {
        const r = 2 + Math.sqrt((q.s.market_cap || 0) / capMax) * 9;
        const col = colourOf ? colourOf(q.c) : null;
        ctx.beginPath(); ctx.arc(q.x, q.y, r, 0, Math.PI * 2);
        if (colourOf && !col) {
          // No value on the colour axis. Filling it with ink put it at the dark
          // end of a ramp whose dark end means "low", so a company we know
          // nothing about looked like a company scoring badly. An outline says
          // absent, which is what it is.
          ctx.strokeStyle = ink; ctx.lineWidth = 1;
          ctx.globalAlpha = 0.35 * ctxDim; ctx.stroke();
        } else {
          ctx.fillStyle = col || ink;
          ctx.globalAlpha = (col ? 0.55 : 0.30) * ctxDim; ctx.fill();
        }
        hits.push({ x: q.x, y: q.y, r: Math.max(r, 4), s: q.s });
      }
      ctx.globalAlpha = 1;
      paintFocused(ctx, proj);
      return;
    }

    const bins = new Map();
    for (const q of proj) {
      const cxi = Math.floor(q.x / CELL), cyi = Math.floor(q.y / CELL);
      const key = cxi + ':' + cyi;
      let b = bins.get(key);
      if (!b) { b = { n: 0, x: cxi * CELL, y: cyi * CELL }; bins.set(key, b); }
      b.n++;
      hits.push({ x: q.x, y: q.y, r: 5, s: q.s });
    }
    let maxN = 0;
    bins.forEach(function(b) { if (b.n > maxN) maxN = b.n; });
    // Square root, not linear: a couple of very dense cells in the middle would
    // otherwise flatten every sparser cell to the same near-invisible tint, and
    // the sparse tail is where the interesting companies are.
    ctx.fillStyle = ink;
    bins.forEach(function(b) {
      ctx.globalAlpha = (0.06 + Math.sqrt(b.n / maxN) * 0.60) * ctxDim;
      ctx.fillRect(b.x, b.y, CELL - 1, CELL - 1);
    });
    ctx.globalAlpha = 1;

    // The biggest names back on top, so the shading has landmarks in it.
    const top = proj.slice().sort(function(a, b) {
      return (b.s.market_cap || 0) - (a.s.market_cap || 0);
    }).slice(0, 70);
    for (const q of top) {
      const r = 2.5 + Math.sqrt((q.s.market_cap || 0) / capMax) * 7;
      ctx.beginPath(); ctx.arc(q.x, q.y, r, 0, Math.PI * 2);
      ctx.fillStyle = ink; ctx.globalAlpha = 0.62 * ctxDim; ctx.fill();
    }
    ctx.globalAlpha = 1;
    paintFocused(ctx, proj);
  }

  // One 2D panel inside an arbitrary box, so the single-lens view and the
  // paired view can share every line of it.
  function drawPanel(ctx, rows, box, cfg, ink, line, dim, hits) {
    const pad = cfg.pad == null ? 46 : cfg.pad;
    const pts = [];
    for (const s of rows) {
      const x = cfg.gx(s), y = cfg.gy(s);
      if (x == null || y == null || !isFinite(x) || !isFinite(y)) continue;
      pts.push({ s: cfg.row ? cfg.row(s) : s, x: x, y: y, c: cfg.gc ? cfg.gc(s) : undefined });
    }
    if (cfg.title) {
      ctx.fillStyle = dim;
      ctx.font = "10px 'Space Mono', monospace";
      ctx.fillText(cfg.title.toUpperCase(), box.x + pad, box.y + 16);
    }
    if (!pts.length) {
      ctx.fillStyle = dim;
      ctx.font = "12px 'Space Mono', monospace";
      ctx.fillText('Nothing to plot here.', box.x + pad, box.y + box.h / 2);
      return 0;
    }
    let x0 = Math.min.apply(null, pts.map(function(p) { return p.x; }));
    let x1 = Math.max.apply(null, pts.map(function(p) { return p.x; }));
    let y0 = Math.min.apply(null, pts.map(function(p) { return p.y; }));
    let y1 = Math.max.apply(null, pts.map(function(p) { return p.y; }));
    if (cfg.symmetric) {
      const m = Math.max(Math.abs(x0), Math.abs(x1), Math.abs(y0), Math.abs(y1)) || 1;
      x0 = -m; x1 = m; y0 = -m; y1 = m;
    }
    if (x1 === x0) x1 = x0 + 1;
    if (y1 === y0) y1 = y0 + 1;
    const sx = function(v) { return box.x + pad + (v - x0) / (x1 - x0) * (box.w - pad * 2); };
    const sy = function(v) { return box.y + box.h - pad - (v - y0) / (y1 - y0) * (box.h - pad * 2); };
    const cx = cfg.symmetric ? sx(0) : sx((x0 + x1) / 2);
    const cy = cfg.symmetric ? sy(0) : sy((y0 + y1) / 2);
    ctx.strokeStyle = line; ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(cx, box.y + pad * 0.4); ctx.lineTo(cx, box.y + box.h - pad * 0.4);
    ctx.moveTo(box.x + pad * 0.4, cy); ctx.lineTo(box.x + box.w - pad * 0.4, cy);
    ctx.stroke();

    paintCloud(ctx, pts.map(function(p) {
      return { x: sx(p.x), y: sy(p.y), s: p.s, c: p.c };
    }), ink, hits, cfg.capMax);

    // Axis names sit on the axes they describe, not in the corners, so a panel
    // half the width of the plot is still self-explanatory.
    if (cfg.xl || cfg.yl) {
      ctx.fillStyle = dim;
      ctx.font = "9px 'Space Mono', monospace";
      if (cfg.xl) {
        ctx.textAlign = 'right';
        ctx.fillText(cfg.xl.toUpperCase() + ' \u2192', box.x + box.w - pad, box.y + box.h - pad * 0.45);
        ctx.textAlign = 'start';
      }
      if (cfg.yl) {
        ctx.save();
        ctx.translate(box.x + pad * 0.55, box.y + pad);
        ctx.rotate(-Math.PI / 2);
        ctx.textAlign = 'left';
        ctx.fillText('\u2190 ' + cfg.yl.toUpperCase(), 0, 0);
        ctx.restore();
        ctx.textAlign = 'start';
      }
    }
    return pts.length;
  }

  let mapHit = [];

  // The map is the radar's selection surface, so it keeps its own hit list and
  // its own cache rather than borrowing the chart's.
  function drawMap() {
    const cv = document.getElementById('stk-map-canvas');
    if (!cv || !cv.clientWidth) return;
    if (tetraPts === null) {
      tetraPts = ALL.filter(function(s) { return s.scorable; })
                    .map(function(s) { return { s: s, p: tetraPos(s) }; });
      tetraMatch = currentFiltered().length;
      const ds = tetraPts.map(function(p) {
        return Math.hypot(p.p[0], p.p[1], p.p[2]);
      }).sort(function(a, b) { return a - b; });
      const p97 = ds.length ? ds[Math.min(ds.length - 1, Math.floor(ds.length * 0.97))] : 1;
      tetraRef = Math.max(p97, Math.sqrt(3));
    }
    // The map shows what the filters allow, like every other surface.
    const allowed = new Set();
    for (const s of currentFiltered()) allowed.add(s.ticker);
    const pts = tetraPts.filter(function(p) { return allowed.has(p.s.ticker); });
    mapHit = [];
    // Stamp the geometry the hits were computed against. A click arriving after
    // the box changed but before a redraw would otherwise hit-test against the
    // wrong frame and confidently select the wrong company.
    mapHit.w = cv.clientWidth;
    mapHit.h = cv.clientHeight;
    drawTetra(cv, pts, tetraRef, mapHit);
    const foot = document.getElementById('stk-map-foot');
    if (foot) {
      foot.textContent = pts.length.toLocaleString() + ' of ' +
        tetraMatch.toLocaleString() + ' matching companies placed \u00b7 ' +
        'needs 3 of 4 dimensions \u00b7 dot size: market cap';
    }
  }

  function drawTetra(cv, pts, ref, hits) {
    const dpr = window.devicePixelRatio || 1;
    const w = cv.clientWidth, h = cv.clientHeight;
    cv.width = Math.round(w * dpr); cv.height = Math.round(h * dpr);
    const ctx = cv.getContext('2d');
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);
    const css = getComputedStyle(document.documentElement);
    const ink = css.getPropertyValue('--text-1').trim() || '#17140F';
    const line = css.getPropertyValue('--border-bright').trim() || 'rgba(0,0,0,.18)';
    const dim = css.getPropertyValue('--text-4').trim() || '#8A8272';
    if (!pts.length) {
      ctx.fillStyle = dim;
      ctx.font = "11px 'Space Mono', monospace";
      ctx.fillText('Nothing here matches the current filters.', 24, h / 2);
      return;
    }
    // One scale for the whole cloud, fixed while it turns, so the shape you are
    // looking at stays the same shape.
    const vertMax = Math.sqrt(3);
    const scale = (Math.min(w, h) / 2 - 34) / ref;

    // The cage first, so the dots read as sitting inside it.
    const vp = {};
    for (const k of TETRA_ORDER) {
      vp[k] = project3(TETRA_V[k].map(function(c) { return c / RT3 * vertMax; }), w, h, scale);
    }
    ctx.strokeStyle = ink; ctx.lineWidth = 1;
    for (const e of TETRA_EDGES) {
      const a = vp[e[0]], b = vp[e[1]];
      // Edges running behind the cloud are dashed, the ones in front solid.
      // Alpha alone did not carry the depth against 2,858 overlapping dots, and
      // a frame of reference you cannot see is not one.
      const behind = (a.z + b.z) / 2 < 0;
      ctx.setLineDash(behind ? [3, 4] : []);
      ctx.globalAlpha = behind ? 0.28 : 0.55;
      ctx.beginPath(); ctx.moveTo(a.x, a.y); ctx.lineTo(b.x, b.y); ctx.stroke();
    }
    ctx.setLineDash([]); ctx.globalAlpha = 1;

    // The origin, where a company that scores evenly on all four sits. Without
    // it "near the middle" has nothing to be near.
    const o = project3([0, 0, 0], w, h, scale);
    ctx.strokeStyle = ink; ctx.globalAlpha = 0.35;
    ctx.beginPath();
    ctx.moveTo(o.x - 4, o.y); ctx.lineTo(o.x + 4, o.y);
    ctx.moveTo(o.x, o.y - 4); ctx.lineTo(o.x, o.y + 4);
    ctx.stroke(); ctx.globalAlpha = 1;

    // Painter's algorithm: far dots first so near ones overlap them correctly.
    const proj = pts.map(function(p) {
      const q = project3(p.p, w, h, scale);
      q.s = p.s; return q;
    }).sort(function(a, b) { return a.z - b.z; });

    const capMax = Math.max.apply(null, pts.map(function(p) { return p.s.market_cap || 0; })) || 1;
    const tetraDim = focusDrawn(proj) ? 0.45 : 1;
    for (const q of proj) {
      const cap = q.s.market_cap || 0;
      const r = (2 + Math.sqrt(cap / capMax) * 9) * q.k;
      // Near dots are more opaque. Depth is the only thing separating an
      // overlapping pair, so it has to be visible without being loud.
      const t = Math.max(0, Math.min(1, (q.z + vertMax) / (vertMax * 2)));
      ctx.beginPath(); ctx.arc(q.x, q.y, Math.max(r, 1), 0, Math.PI * 2);
      ctx.fillStyle = ink;
      ctx.globalAlpha = (0.14 + t * 0.30) * tetraDim;
      ctx.fill();
      hits.push({ x: q.x, y: q.y, r: Math.max(r, 4), s: q.s });
    }
    ctx.globalAlpha = 1;
    // After the cage and the cloud, so a selected company is never buried.
    paintFocused(ctx, proj);

    // Vertex labels last, on top of everything.
    ctx.font = "10px 'Space Mono', monospace";
    ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
    for (const k of TETRA_ORDER) {
      const a = vp[k];
      // Push the label along the ray from the centre, so it clears the cloud at
      // any rotation instead of landing on top of it whenever a vertex points up.
      let ox = a.x - w / 2, oy = a.y - h / 2;
      const len = Math.hypot(ox, oy) || 1;
      ox = ox / len * 15; oy = oy / len * 15;
      ctx.fillStyle = a.z < 0 ? dim : ink;
      ctx.fillText(TETRA_LABEL[k].toUpperCase(), a.x + ox, a.y + oy);
      ctx.globalAlpha = a.z < 0 ? 0.4 : 1;
      ctx.beginPath(); ctx.arc(a.x, a.y, 2.5, 0, Math.PI * 2);
      ctx.fill();
      ctx.globalAlpha = 1;
    }
    ctx.textAlign = 'start'; ctx.textBaseline = 'alphabetic';

  }

  const PAIR_PANELS = [
    { x:'q', y:'g', xl:'Quality', yl:'Growth',   title:'Compounders' },
    { x:'v', y:'m', xl:'Value',   yl:'Momentum', title:'Cheap and moving' },
  ];

  function drawPairs(cv) {
    const rows = chartPoints().map(function(p) { return p.s; });
    const dpr = window.devicePixelRatio || 1;
    const w = cv.clientWidth, h = cv.clientHeight;
    cv.width = Math.round(w * dpr); cv.height = Math.round(h * dpr);
    const ctx = cv.getContext('2d');
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);
    const css = getComputedStyle(document.documentElement);
    const ink = css.getPropertyValue('--text-1').trim() || '#17140F';
    const line = css.getPropertyValue('--border').trim() || 'rgba(0,0,0,.1)';
    const dim = css.getPropertyValue('--text-4').trim() || '#888';
    chartHit = [];
    const foot = document.getElementById('stk-chart-foot');
    if (!rows.length) {
      ctx.fillStyle = dim;
      ctx.font = "12px 'Space Mono', monospace";
      ctx.fillText('No companies are scored on enough dimensions to place.', 46, h / 2);
      if (foot) foot.textContent = '0 plotted' + focusMissingNote([]);
      return;
    }
    // One cap scale across both panels, so a dot the same size means the same
    // company on the left as on the right.
    const capMax = Math.max.apply(null, rows.map(function(s) { return s.market_cap || 0; })) || 1;
    const halfW = Math.floor(w / 2);
    // Each panel drops the rows missing its own two factors, so they plot
    // different counts. Report the larger, and say which, rather than silently
    // reporting whichever happened to be drawn last.
    const counts = [];
    PAIR_PANELS.forEach(function(cfg, i) {
      const box = { x: i * halfW, y: 0, w: halfW, h: h };
      counts.push(drawPanel(ctx, rows, box, {
        gx: function(s) { return s[cfg.x]; },
        gy: function(s) { return s[cfg.y]; },
        xl: cfg.xl, yl: cfg.yl, title: cfg.title,
        symmetric: true, capMax: capMax, pad: 40,
      }, ink, line, dim, chartHit));
    });
    const n = Math.max.apply(null, counts);
    // A hairline between the two, so they read as two charts and not one wide
    // one with a gap in the middle.
    ctx.strokeStyle = line; ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(halfW, 18); ctx.lineTo(halfW, h - 18); ctx.stroke();
    if (foot) {
      foot.textContent = (counts[0] === counts[1]
          ? n.toLocaleString() + ' plotted'
          : counts[0].toLocaleString() + ' left, ' + counts[1].toLocaleString() + ' right') +
        ' of ' + currentFiltered().length.toLocaleString() + ' matching \u00b7 ' +
        'only companies scored on 3 of 4 dimensions can be placed \u00b7 ' +
        (n > DENSITY_ABOVE
          ? 'shaded by how many companies fall in each cell, largest 70 drawn on top'
          : 'dot size: market cap') +
        focusMissingNote(chartPoints());
    }
    const plot = cv.parentElement;
    if (plot) ['tl','tr','bl','br'].forEach(function(c) {
      const el = plot.querySelector('.ax.' + c);
      if (el) el.textContent = '';
    });
  }

  function drawChart() {
    const cv = document.getElementById('stk-chart-canvas');
    if (!cv) return;
    if (pairsMode) { drawPairs(cv); return; }
    const pts = chartPoints();
    const dpr = window.devicePixelRatio || 1;
    const w = cv.clientWidth, h = cv.clientHeight;
    cv.width = Math.round(w * dpr); cv.height = Math.round(h * dpr);
    const ctx = cv.getContext('2d');
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);
    const css = getComputedStyle(document.documentElement);
    const ink = css.getPropertyValue('--text-1').trim() || '#17140F';
    const line = css.getPropertyValue('--border').trim() || 'rgba(0,0,0,.1)';
    const dim = css.getPropertyValue('--text-4').trim() || '#888';
    chartHit = [];
    if (!pts.length) {
      ctx.fillStyle = dim;
      ctx.font = "12px 'Space Mono', monospace";
      ctx.fillText('No company has both ' + axisLabel(axisY).toLowerCase() +
        ' and ' + axisLabel(axisX).toLowerCase() + '.', 46, h / 2);
      document.getElementById('stk-chart-foot').textContent =
        '0 plotted' + focusMissingNote([]);
      return;
    }
    // The points are already projected for this lens, so the panel just reads
    // them back off; the shared renderer is what brings the density work here.
    drawPanel(ctx, pts, { x: 0, y: 0, w: w, h: h }, {
      gx: function(p) { return p.x; },
      gy: function(p) { return p.y; },
      // These rows are already {s, x, y} wrappers, so hand drawPanel the stock
      // inside. Unwrapping after the fact fixed the hit targets but left every
      // dot sized off an undefined market cap, which is to say all the same size.
      row: function(p) { return p.s; },
      gc: function(p) { return p.c; },
      xZero: !!(AXIS_BY_KEY[axisX] || {}).zero,
      yZero: !!(AXIS_BY_KEY[axisY] || {}).zero,
      xl: axisLabel(axisX), yl: axisLabel(axisY),
    }, ink, line, dim, chartHit);

    // The key goes on after the cloud so nothing is painted over it.
    let colourGaps = 0;
    if (axisC) {
      const cv2 = pts.map(function(p) { return p.c; })
                     .filter(function(v) { return v != null && isFinite(v); })
                     .sort(function(a, b) { return a - b; });
      colourGaps = pts.length - cv2.length;
      if (cv2.length >= 2) {
        drawColourLegend(ctx, { x: 0, y: 0, w: w, h: h }, axisLabel(axisC),
                         fmtAxisValue(cv2[0], axisC), fmtAxisValue(cv2[cv2.length - 1], axisC),
                         colourGaps, ink, dim);
      }
    }
    document.getElementById('stk-chart-foot').textContent =
      pts.length.toLocaleString() + ' plotted of ' + currentFiltered().length.toLocaleString() +
      ' matching \u00b7 dot size: market cap' +
      (axisC ? ' \u00b7 colour: ' + axisLabel(axisC).toLowerCase() + ', cool to warm by rank' +
               (colourGaps ? ', ' + colourGaps.toLocaleString() + ' with no value drawn as outlines' : '')
             : (pts.length > DENSITY_ABOVE
                ? ' \u00b7 shaded by how many companies fall in each cell, largest 70 on top'
                : '')) +
      (((AXIS_BY_KEY[axisX] || {}).log || (AXIS_BY_KEY[axisY] || {}).log)
        ? ' \u00b7 log scale where the range demands it' : '') +
      focusMissingNote(pts);
  }

  function axisOptions(sel) {
    let out = '', group = '';
    for (const f of AXIS_FIELDS) {
      if (f.g !== group) {
        if (group) out += '</optgroup>';
        group = f.g;
        out += '<optgroup label="' + escapeHtml(group) + '">';
      }
      out += '<option value="' + f.k + '"' + (f.k === sel ? ' selected' : '') + '>' +
             escapeHtml(f.label) + '</option>';
    }
    return out + (group ? '</optgroup>' : '');
  }

  function renderLenses() {
    const el = document.getElementById('stk-lenses');
    if (!el) return;
    el.innerHTML =
      '<span class="lib-chip-label">Plot</span>' +
      '<select class="stk-axis" id="stk-axis-y" aria-label="Vertical axis">' + axisOptions(axisY) + '</select>' +
      '<span class="stk-axis-op">against</span>' +
      '<select class="stk-axis" id="stk-axis-x" aria-label="Horizontal axis">' + axisOptions(axisX) + '</select>' +
      '<span class="stk-axis-op">colour by</span>' +
      '<select class="stk-axis" id="stk-axis-c" aria-label="Colour">' +
        '<option value=""' + (axisC ? '' : ' selected') + '>nothing</option>' +
        axisOptions(axisC) + '</select>' +
      '<span class="stk-axis-sep"></span>' +
      PRESETS.map(function(p, i) {
        const on = !pairsMode && p.x === axisX && p.y === axisY && (p.c || '') === axisC;
        return '<button type="button" class="stk-lens' + (on ? ' active' : '') +
               '" data-preset="' + i + '">' + escapeHtml(p.label) + '</button>';
      }).join('') +
      '<button type="button" class="stk-lens' + (pairsMode ? ' active' : '') +
      '" data-pairs="1">Four factors</button>';

    const bind = function(id, set) {
      const s = document.getElementById(id);
      if (s) s.addEventListener('change', function() { set(s.value); pairsMode = false; redrawAxes(); });
    };
    bind('stk-axis-y', function(v) { axisY = v; });
    bind('stk-axis-x', function(v) { axisX = v; });
    bind('stk-axis-c', function(v) { axisC = v; });

    el.querySelectorAll('[data-preset]').forEach(function(b) {
      b.addEventListener('click', function() {
        const p = PRESETS[Number(b.dataset.preset)];
        axisX = p.x; axisY = p.y; axisC = p.c || ''; pairsMode = false;
        redrawAxes();
      });
    });
    const pb = el.querySelector('[data-pairs]');
    if (pb) pb.addEventListener('click', function() { pairsMode = true; redrawAxes(); });

    const blurb = document.getElementById('stk-chart-blurb');
    if (blurb) {
      blurb.textContent = pairsMode
        ? 'All four factors, two plots, every one on a position axis. Left is the compounder view, right is the value view.'
        : axisLabel(axisY) + ' against ' + axisLabel(axisX) +
          (axisC ? ', coloured by ' + axisLabel(axisC).toLowerCase() : '') +
          '. Dot size is market cap throughout.';
    }
  }

  function redrawAxes() {
    renderLenses();
    drawChart();
  }

  (function wireMapSpin() {
    const btn = document.getElementById('stk-spin');
    if (!btn) return;
    if (REDUCED_MOTION) { btn.hidden = true; return; }
    btn.addEventListener('click', function() {
      spin = !spin;
      btn.classList.toggle('active', spin);
      if (spin) startSpin(); else stopSpin();
    });
  })();

  (function wireMapClick() {
    const cv = document.getElementById('stk-map-canvas');
    if (!cv) return;
    cv.addEventListener('click', function(e) {
      if (suppressNextClick) { suppressNextClick = false; return; }
      // Refuse to hit-test a stale frame. Selecting the wrong company is worse
      // than selecting none, because nothing about it looks wrong.
      if (mapHit.w !== cv.clientWidth || mapHit.h !== cv.clientHeight) {
        drawMap();
        return;
      }
      const b = cv.getBoundingClientRect();
      const mx = e.clientX - b.left, my = e.clientY - b.top;
      let best = null, bd = 1e9;
      for (const h of mapHit) {
        const d = (h.x - mx) * (h.x - mx) + (h.y - my) * (h.y - my);
        if (d < bd && d < (h.r + 7) * (h.r + 7)) { bd = d; best = h; }
      }
      if (!best) return;
      focusToggle(best.s.ticker, e.ctrlKey || e.metaKey || e.shiftKey);
    });
    cv.addEventListener('mousemove', function(e) {
      const b = cv.getBoundingClientRect();
      const mx = e.clientX - b.left, my = e.clientY - b.top;
      let best = null, bd = 1e9;
      for (const h of mapHit) {
        const d = (h.x - mx) * (h.x - mx) + (h.y - my) * (h.y - my);
        if (d < bd && d < (h.r + 6) * (h.r + 6)) { bd = d; best = h; }
      }
      cv.style.cursor = best ? 'pointer' : 'grab';
    });
  })();

  (function wireTetraDrag() {
    const cv = document.getElementById('stk-map-canvas');
    if (!cv) return;
    let dragging = false, lastX = 0, lastY = 0, startX = 0, startY = 0, moved = 0;
    cv.addEventListener('pointerdown', function(e) {
      // Primary button only: a right or middle drag should neither rotate the
      // shape nor arm a flag that the click after it will never clear.
      if (e.button !== 0) return;
      // Any press starts clean, so a stale arm from an abandoned gesture or an
      // earlier lens cannot swallow this selection.
      suppressNextClick = false;
      dragging = true; moved = 0;
      startX = lastX = e.clientX; startY = lastY = e.clientY;
      stopSpin();
      cv.setPointerCapture(e.pointerId);
      cv.style.cursor = 'grabbing';
    });
    cv.addEventListener('pointermove', function(e) {
      if (!dragging) return;
      const dx = e.clientX - lastX, dy = e.clientY - lastY;
      lastX = e.clientX; lastY = e.clientY;
      // Furthest the pointer ever got from where it was pressed, not how far
      // it travelled. Path length punished a tremor that ended on the pixel it
      // started on, while peak displacement still catches a turn that loops
      // back to its origin.
      moved = Math.max(moved, Math.hypot(e.clientX - startX, e.clientY - startY));
      yaw += dx * 0.008;
      // Clamped so the cloud can never be turned past vertical, where the
      // vertex labels would invert and the shape stops being readable.
      pitch = Math.max(-1.35, Math.min(1.35, pitch + dy * 0.008));
      drawMap();
    });
    function release(e) {
      if (!dragging) return;
      dragging = false;
      cv.style.cursor = 'grab';
      try { cv.releasePointerCapture(e.pointerId); } catch (err) {}
      // A deliberate turn parks the shape where the reader put it. Spin, if it
      // was on, stays off until they ask for it again.
      if (moved >= 4) {
        suppressNextClick = true;
        spin = false;
        const b = document.getElementById('stk-spin');
        if (b) b.classList.remove('active');
      } else if (spin) { startSpin(); }
    }
    cv.addEventListener('pointerup', release);
    // A cancelled pointer never produces a click, so arming the suppression
    // there would swallow the next real one.
    cv.addEventListener('pointercancel', function(e) {
      dragging = false;
      cv.style.cursor = 'grab';
      try { cv.releasePointerCapture(e.pointerId); } catch (err) {}
    });
  })();

  (function wireChartTip() {
    const cv = document.getElementById('stk-chart-canvas');
    const tip = document.getElementById('stk-chart-tip');
    if (!cv || !tip) return;
    cv.addEventListener('mousemove', function(e) {
      const b = cv.getBoundingClientRect();
      const mx = e.clientX - b.left, my = e.clientY - b.top;
      let best = null, bd = 1e9;
      for (const h of chartHit) {
        const d = (h.x - mx) * (h.x - mx) + (h.y - my) * (h.y - my);
        if (d < bd && d < (h.r + 6) * (h.r + 6)) { bd = d; best = h; }
      }
      if (!best) { tip.hidden = true; return; }
      tip.hidden = false;
      tip.style.left = Math.min(b.width - 170, best.x + 10) + 'px';
      tip.style.top = Math.max(0, best.y - 34) + 'px';
      tip.innerHTML = escapeHtml(best.s.ticker) + '<br>' + escapeHtml(best.s.name || '');
    });
    cv.addEventListener('mouseleave', function() { tip.hidden = true; });

    cv.addEventListener('click', function(e) {
      // A rotation ends with a click event too. Ignore the one that follows a
      // drag, or turning the 3D map would reselect whatever ended up under the
      // cursor.
      if (suppressNextClick) { suppressNextClick = false; return; }
      const b = cv.getBoundingClientRect();
      const mx = e.clientX - b.left, my = e.clientY - b.top;
      let best = null, bd = 1e9;
      for (const h of chartHit) {
        const d = (h.x - mx) * (h.x - mx) + (h.y - my) * (h.y - my);
        if (d < bd && d < (h.r + 7) * (h.r + 7)) { bd = d; best = h; }
      }
      if (!best) return;
      focusToggle(best.s.ticker, e.ctrlKey || e.metaKey || e.shiftKey);
    });
  })();

  let currentView = 'list';

  function setView(v) {
    currentView = v;
    const list = document.querySelector('.stk-table');
    const radar = document.getElementById('stk-radar');
    const chart = document.getElementById('stk-chart');
    if (list) list.hidden = (v !== 'list');
    if (radar) radar.hidden = (v !== 'radar');
    if (chart) chart.hidden = (v !== 'chart');
    // render() may have run while the list was hidden, which empties it and
    // leaves shownCount at 0. Paint it now that it has a box to measure.
    if (v === 'list') { fillWindow(); redrawExpanded(); }
    if (v === 'chart') { renderLenses(); renderFocusBar(); drawChart(); }
    if (v !== 'radar') stopSpin();
    document.querySelectorAll('.stk-view-btn').forEach(function(b) {
      b.classList.toggle('active', b.dataset.view === v);
    });
    if (v === 'radar') { renderRadar(); drawMap(); startSpin(); }
  }
  document.querySelectorAll('.stk-view-btn').forEach(function(b) {
    b.addEventListener('click', function() { setView(b.dataset.view); });
  });

  (function wireAbout() {
    const btn = document.getElementById('stk-about-btn');
    const hero = document.getElementById('stk-hero');
    if (!btn || !hero) return;
    btn.addEventListener('click', function() {
      const open = hero.hidden;
      hero.hidden = !open;
      btn.setAttribute('aria-expanded', String(open));
    });
  })();

  document.querySelectorAll('.stk-sort').forEach(function(b) {
    b.addEventListener('click', function() { setSort(b.dataset.sortby); });
  });
  syncSortControls();

  // The headline count is spelled out, which is the design's whole tone: a
  // number you read rather than parse. Only up to the tens of thousands, which
  // is far past any plausible universe size.
  const ONES = ['zero','one','two','three','four','five','six','seven','eight','nine','ten',
    'eleven','twelve','thirteen','fourteen','fifteen','sixteen','seventeen','eighteen','nineteen'];
  const TENS = ['','','twenty','thirty','forty','fifty','sixty','seventy','eighty','ninety'];
  function spell(n) {
    if (n == null) return '';
    if (n < 20) return ONES[n];
    if (n < 100) return TENS[Math.floor(n/10)] + (n % 10 ? '-' + ONES[n % 10] : '');
    if (n < 1000) return ONES[Math.floor(n/100)] + ' hundred' + (n % 100 ? ' ' + spell(n % 100) : '');
    return spell(Math.floor(n/1000)) + ' thousand' + (n % 1000 ? ' ' + spell(n % 1000) : '');
  }
  function cap1(s) { return s ? s.charAt(0).toUpperCase() + s.slice(1) : s; }

  function renderHero(filtered) {
    const numEl = document.getElementById('stk-hero-num');
    if (numEl) {
      numEl.innerHTML = escapeHtml(cap1(spell(filtered.length))) + '<span class="dot">.</span>';
    }
    const scored = filtered.filter(function(s) { return s.scorable; });
    const comps = scored.map(computeComposite).filter(function(v) { return v != null; }).sort(function(a,b){return a-b;});
    const median = comps.length ? comps[Math.floor(comps.length / 2)] : null;
    const set = function(id, v) { const e = document.getElementById(id); if (e) e.textContent = v; };
    set('stk-stat-match', filtered.length.toLocaleString());
    set('stk-stat-scored', scored.length.toLocaleString());
    set('stk-stat-median', median == null ? '\u2014' : fmtScore(median));
  }

  // The query sentence is a real control, not decoration: each underlined term
  // drives the same filter state the sidebar does.
  const QUERY_TERMS = {
    cap:     { off:'any size',   on:'large-cap',  apply(on) { filters['market_cap'] = on ? {min:1e10, max:null} : {min:null,max:null}; } },
    value:   { off:'any price',  on:'cheap',      apply(on) { queryValueCheap = on; } },
    sector:  { off:'any sector', on:'any sector', apply() {} },
    neglect: { off:'anyone',     on:'ignoring',   apply(on) { filters['neglect_score'] = on ? {min:0.5, max:null} : {min:null,max:null}; } },
  };
  let queryValueCheap = false;
  document.querySelectorAll('.stk-q').forEach(function(b) {
    const term = QUERY_TERMS[b.dataset.q];
    if (!term) return;
    b.textContent = term.off;
    b.addEventListener('click', function() {
      const on = !b.classList.contains('on');
      b.classList.toggle('on', on);
      b.textContent = on ? term.on : term.off;
      term.apply(on);
      render();
    });
  });

  // ---- Ticker picker --------------------------------------------------
  // The search box used to add any exact ticker match on every keystroke, so
  // typing AAPL added AA, then AAP, then AAPL: three companies on the radar
  // when the reader asked for one, and the two they did not want silently
  // consuming the three-slot limit. Selection is explicit now. The box offers
  // matches and nothing is added until one is picked.
  const acEl = document.getElementById('stk-ac');
  let acItems = [], acIdx = -1;

  function acClose() {
    acItems = []; acIdx = -1;
    if (acEl) { acEl.hidden = true; acEl.innerHTML = ''; }
    if (searchEl) searchEl.setAttribute('aria-expanded', 'false');
  }

  // Exact ticker, then ticker prefix, then name prefix, then name anywhere,
  // each tier by descending size, so the obvious answer is always first.
  function acMatches(raw) {
    const q = String(raw || '').trim().toUpperCase();
    if (!q) return [];
    const hits = [];
    for (let i = 0; i < ALL.length; i++) {
      const s = ALL[i];
      const tk = String(s.ticker || '').toUpperCase();
      const nm = String(s.name || '').toUpperCase();
      let rank = -1;
      if (tk === q) rank = 0;
      else if (tk.indexOf(q) === 0) rank = 1;
      else if (nm.indexOf(q) === 0) rank = 2;
      else if (nm.indexOf(q) !== -1) rank = 3;
      if (rank >= 0) hits.push([rank, s]);
    }
    hits.sort(function(a, b) {
      return a[0] - b[0] || (b[1].market_cap || 0) - (a[1].market_cap || 0);
    });
    return hits.slice(0, 8).map(function(p) { return p[1]; });
  }

  function acPaint() {
    if (!acEl) return;
    if (!acItems.length) {
      const typed = searchEl && searchEl.value.trim();
      if (!typed) { acClose(); return; }
      acEl.innerHTML = '<div class="scr-ac-none">No company matches that.</div>';
      acEl.hidden = false;
      if (searchEl) searchEl.setAttribute('aria-expanded', 'true');
      return;
    }
    acEl.innerHTML = acItems.map(function(s, i) {
      const already = radarTickers.indexOf(s.ticker) !== -1;
      return '<div class="scr-ac-item' + (i === acIdx ? ' on' : '') + '" role="option"' +
             ' data-ac="' + escapeHtml(s.ticker) + '">' +
             '<span class="scr-ac-tk">' + escapeHtml(s.ticker) + '</span>' +
             '<span class="scr-ac-nm">' + escapeHtml(s.name || '') + '</span>' +
             '<span class="scr-ac-tag">' + (already ? 'On radar' : 'Add') + '</span>' +
             '</div>';
    }).join('');
    acEl.hidden = false;
    if (searchEl) searchEl.setAttribute('aria-expanded', 'true');
  }

  function acPick(ticker) {
    if (!ticker) return;
    // Picking is what puts a company on the radar, and it switches to that view
    // so the result of the click is visible rather than filed away behind a tab.
    // Navigate on every pick, new or not. A ticker already on the radar is the
    // one the reader is asking to see, and staying put while clearing the box
    // made the click read as broken.
    radarAdd(ticker);
    renderFocusBar();
    setView('radar');
    if (searchEl) searchEl.value = '';
    if (clearEl) clearEl.hidden = true;
    query = '';
    acClose();
    render();
    if (searchEl) searchEl.focus();
  }

  if (searchEl) {
    searchEl.addEventListener('input', function() {
      acItems = acMatches(searchEl.value);
      acIdx = acItems.length ? 0 : -1;
      acPaint();
    });
    searchEl.addEventListener('keydown', function(e) {
      if (e.key === 'Escape') { acClose(); return; }
      if (!acItems.length) return;
      if (e.key === 'ArrowDown') {
        e.preventDefault(); acIdx = (acIdx + 1) % acItems.length; acPaint();
      } else if (e.key === 'ArrowUp') {
        e.preventDefault(); acIdx = (acIdx - 1 + acItems.length) % acItems.length; acPaint();
      } else if (e.key === 'Enter') {
        e.preventDefault();
        const pick = acItems[acIdx] || acItems[0];
        if (pick) acPick(pick.ticker);
      }
    });
    // A plain blur handler would fire before a click on the list registers.
    searchEl.addEventListener('blur', function() { setTimeout(acClose, 150); });
  }
  if (acEl) {
    // mousedown, not click: the input blurs first and would close the list out
    // from under the pointer.
    acEl.addEventListener('mousedown', function(e) {
      const row = e.target.closest('[data-ac]');
      if (!row) return;
      e.preventDefault();
      acPick(row.dataset.ac);
    });
  }

  function boot(data) {
    ALL = Array.isArray(data) ? data : [];
    populateRangeStats();
    render();
  }

  if (listEl) {
    listEl.innerHTML = '<div class="empty-state">Loading the universe...</div>';
  }
  fetch(DATA_URL, { cache: 'no-cache' })
    .then(function(r) {
      if (!r.ok) throw new Error('HTTP ' + r.status);
      return r.json();
    })
    .then(boot)
    .catch(function(err) {
      // Say what happened. A silently empty screener looks like "no matches",
      // which is a very different claim from "the data failed to load".
      console.error('stocks: could not load ' + DATA_URL, err);
      if (listEl) {
        listEl.innerHTML = '<div class="empty-state">Could not load the stock universe (' +
          String(err.message || err) + '). Reload to retry.</div>';
      }
    });
})();
"""


THEME_JS = """
// Theme toggle. Persists to localStorage; the head pre-script applies any saved
// value before stylesheets render, so this only has to handle the click.
(function(){
  const root = document.documentElement;
  const btn = document.getElementById('apt-theme-toggle');
  function syncIcon() {
    const t = root.getAttribute('data-theme') || 'light';
    const ic = btn && btn.querySelector('.theme-toggle-icon');
    if (ic) ic.innerHTML = t === 'light' ? '&#9790;' : '&#9788;';  // moon : sun
  }
  syncIcon();
  if (btn) {
    btn.addEventListener('click', () => {
      const next = (root.getAttribute('data-theme') === 'light') ? 'dark' : 'light';
      root.setAttribute('data-theme', next);
      try { localStorage.setItem('apt-theme-v2', next); } catch(e) {}
      syncIcon();
      // Charts read their colours from the computed palette at draw time, so a
      // theme change has to redraw them or they keep the old ink.
      if (window.__aptRedrawCharts) window.__aptRedrawCharts();
    });
  }
})();
"""


def render_topnav(active=""):
    """The masthead, shared by every page including the screener.

    active is one of 'home', 'today', 'stories', 'stocks'. The subtitle beside
    the wordmark names the section, which is what a tagline repeated on every
    page could never do."""
    SUBTITLE = {
        "home":    "Explore what&rsquo;s out there.",
        "today":   "Today",
        "stories": "Stories",
        "stocks":  "The Screen",
        "research": "Research",
    }
    sub = SUBTITLE.get(active, "Explore what&rsquo;s out there.")

    def link(href, label, key):
        on = ' class="on"' if active == key else ''
        return f'<a href="./{href}"{on}>{label}</a>'

    return (
        '<div class="scr-top">'
        '<a class="scr-brand" href="./index.html">'
        '<span class="scr-mark">Apterreon</span>'
        f'<span class="scr-sub">{sub}</span>'
        '</a>'
        '<div class="scr-nav">'
        + link("index.html", "Home", "home")
        + link("today.html", "Today", "today")
        + link("stories.html", "Stories", "stories")
        + link("stocks.html", "Stocks", "stocks")
        + link("research.html", "Research", "research")
        + '<button type="button" id="apt-theme-toggle" class="theme-toggle"'
          ' aria-label="Toggle light/dark theme" title="Toggle light/dark">'
          '<span class="theme-toggle-icon">&#9788;</span></button>'
        '</div></div>'
    )


def render_footer(meta=""):
    """Matching footer: two mono lines on a hairline rule, no rounded panel."""
    right = meta or "Daily Intelligence Brief &middot; generated by Apterreon &middot; GitHub Pages"
    return (
        '<div class="scr-foot">'
        '<span>Apterreon &middot; Explore what&rsquo;s out there.</span>'
        f'<span>{right}</span>'
        '</div>'
    )


def render_page(title, body_html, active_nav="", extra_scripts=""):
    """Wrap body content in the shared site shell (head, plexus canvas, topnav, body, footer, scripts)."""
    topnav = render_topnav(active_nav)
    footer = render_footer()
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="color-scheme" content="dark light">
<meta name="theme-color" content="#EDE8DC">
<link rel="manifest" href="manifest.json">
<title>{title}</title>
<script>
// Apply persisted theme before stylesheet renders to avoid a flash.
(function(){{
  try {{
    // The screener design is a light design, so light is the default. Anyone
    // who used the site before the redesign has a stale 'dark' in storage from
    // when dark WAS the default, and would otherwise land on a palette that was
    // never tuned for it. Migrate once: the pre-redesign key is ignored, and a
    // v2 key records only a deliberate choice made since.
    var t = localStorage.getItem('apt-theme-v2');
    document.documentElement.setAttribute('data-theme', t === 'dark' ? 'dark' : 'light');
  }} catch (e) {{}}
}})();
</script>
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Instrument+Serif:ital@0;1&family=Space+Grotesk:wght@400;500;700&family=Space+Mono:wght@400;700&display=swap" rel="stylesheet">
<style>
{SITE_CSS}
</style>
</head>
<body class="page-{active_nav or 'home'} scr-page">

{topnav}

<div class="scr-page-body">
{body_html}
</div>

{footer}

<script>
{THEME_JS}
{extra_scripts}
</script>
</body>
</html>
"""


# ── Recent Trends: cached Claude generation across past brief days ──────────

def get_or_generate_recent_trends(briefs):
    """Formerly a daily Claude call summarizing the last ~10 days into bullets and
    themes. The project no longer uses an LLM, and there is no non-LLM way to
    synthesize prose, so this returns empty and generate_home omits the block."""
    return {"date": datetime.now(EASTERN).strftime("%Y-%m-%d"),
            "snapshot": [], "themes": []}


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
    "market_series": "13-week Treasury bill (^IRX) and S&P 500 (^GSPC), stored once per run",
    "edgar": "SEC EDGAR XBRL company facts",
    "form4": "SEC EDGAR Form 4 filings",
    "yfinance": "Yahoo Finance quote summary, one request per ticker",
    "news": "Google News RSS, one search per ticker",
    "index": "Wikipedia index constituent tables and the NASDAQ Trader directory",
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
    "source_error": "The source was reachable but the fetch or parse failed.",
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
    "change_pct": {
        "label": "1-Day Move", "units": "percent", "source": "price_history",
        "refresh": "daily", "asof": "prices_updated",
        "formula": "(closes[-1] / closes[-2] - 1) * 100",
        "note": "Close-to-close, one session. Stored in percent, not as a "
                "fraction, which is why it is the one percentage field not "
                "scaled by 100 for display.",
    },
    "return_1m": {
        "label": "1-Month Return", "units": "fraction", "source": "price_history",
        "refresh": "daily", "asof": "prices_updated",
        "formula": "closes[-1] / closes[-22] - 1",
        "note": "Simple holding-period return over 21 trading days, which is one "
                "calendar month of sessions.",
    },
    "return_12_2": {
        "label": "12-2 Month Return", "units": "fraction", "source": "price_history",
        "refresh": "daily", "asof": "prices_updated",
        "formula": "closes[-22] / closes[-253] - 1",
        "note": "Jegadeesh-Titman momentum: twelve months of return ending one "
                "month ago. Skipping the most recent month is the point, because "
                "that is where short-term reversal lives. Needs 200 sessions.",
    },
    "return_52w": {
        "label": "52-Week Return", "units": "fraction", "source": "price_history",
        "refresh": "daily", "asof": "prices_updated",
        "formula": "closes[-1] / closes[-253] - 1",
        "note": "Simple holding-period return over the stored year.",
    },
    "high52w_proximity": {
        "label": "52-Week High Proximity", "units": "fraction", "source": "price_history",
        "refresh": "daily", "asof": "prices_updated",
        "formula": "closes[-1] / max(closes) - 1",
        "note": "Distance below the highest close of the year, as a negative "
                "fraction; 0 means at the high. Measured on closes, so it sits "
                "slightly above a version measured on intraday highs.",
    },
    "rel_strength_sp500": {
        "label": "Relative Strength vs S&P 500", "units": "fraction",
        "source": "price_history", "refresh": "daily", "asof": "prices_updated",
        "formula": "return_52w(stock) - return_52w(^GSPC)",
        "note": "Difference of the two 52-week returns over the same trading "
                "days, which is the usual construction. Not a ratio and not a "
                "regression; beta_1y is the regression.",
    },
    "volume": {
        "label": "Volume", "units": "shares", "source": "price_history",
        "refresh": "daily", "asof": "prices_updated",
        "formula": "volumes[-1]",
        "note": "Shares traded in the most recent session.",
    },
    "volume_trend": {
        "label": "Volume Trend", "units": "fraction", "source": "price_history",
        "refresh": "daily", "asof": "prices_updated",
        "formula": "mean(volumes[-10:]) / mean(volumes[-63:]) - 1",
        "note": "Ten-session average against three-month average. Positive means "
                "trading has picked up. Matches the ratio Yahoo's own "
                "averageDailyVolume10Day over averageVolume expresses.",
    },
    "volatility_1y": {
        "label": "Volatility (1y)", "units": "fraction", "source": "price_history",
        "refresh": "daily", "asof": "prices_updated",
        "formula": "stdev(daily returns) * sqrt(252)",
        "note": "Annualized standard deviation of simple daily returns, sample "
                "standard deviation, over the stored year.",
    },
    "beta_1y": {
        "label": "Beta vs S&P 500", "units": "ratio", "source": "market_series",
        "refresh": "daily", "asof": "prices_updated",
        "formula": "cov(r_stock, r_index) / var(r_index)",
        "note": "Ordinary least squares slope of daily returns against the S&P "
                "500, matched on the sessions both actually traded. 1.00 moves "
                "with the index.",
    },
    "sharpe_1y": {
        "label": "Sharpe (1y)", "units": "ratio", "source": "market_series",
        "refresh": "daily", "asof": "prices_updated",
        "formula": "mean(r - rf) / stdev(r - rf) * sqrt(252)",
        "note": "Daily excess return over the 13-week Treasury bill, annualized. "
                "Withheld rather than assuming a zero rate when the rate series "
                "is unavailable, since that would inflate every Sharpe by roughly "
                "the level of short rates.",
    },
    "max_drawdown_1y": {
        "label": "Max Drawdown (1y)", "units": "fraction", "source": "price_history",
        "refresh": "daily", "asof": "prices_updated",
        "formula": "min(close / running_max(close) - 1)",
        "note": "Worst peak-to-trough fall across the stored year, on closes, as "
                "a negative fraction.",
    },
    "market_cap": {
        "label": "Market Cap", "units": "USD", "source": "edgar",
        "refresh": "daily", "asof": "prices_updated",
        "formula": "price * shares_outstanding",
        "note": "Cover-page shares outstanding from the latest filing, times the "
                "latest close. Not the weighted-average count, which describes a "
                "period rather than a moment and understates a company mid-buyback. "
                "Matches the vendor to 0.0% across the filers checked.",
    },
    "pe": {
        "label": "P/E (Trailing)", "units": "ratio", "source": "edgar",
        "refresh": "quarterly", "asof": "fiscal_period_end",
        "formula": "price / sum(last 4 quarters of diluted EPS)",
        "note": "Diluted, not basic, because that is the share count an outside "
                "holder is actually diluted by. Undefined and withheld when "
                "trailing EPS is zero or negative.",
    },
    "price_book": {
        "label": "Price/Book", "units": "ratio", "source": "edgar",
        "refresh": "quarterly", "asof": "fiscal_period_end",
        "formula": "market_cap / stockholders_equity",
        "note": "Parent-company equity. The including-noncontrolling-interests "
                "variant counts equity common holders have no claim on.",
    },
    "roe_ttm": {
        "label": "ROE (TTM)", "units": "fraction", "source": "edgar",
        "refresh": "quarterly", "asof": "fiscal_period_end",
        "formula": "ttm_net_income / mean(equity_now, equity_a_year_ago)",
        "note": "Average equity over the same window as the earnings, not the "
                "closing balance, because the denominator moves through the year.",
    },
    "gross_margin": {
        "label": "Gross Margin", "units": "fraction", "source": "edgar",
        "refresh": "quarterly", "asof": "fiscal_period_end",
        "formula": "ttm_gross_profit / ttm_revenue",
        "note": "Both trailing twelve months, from the same four quarters.",
    },
    "operating_margin": {
        "label": "Operating Margin", "units": "fraction", "source": "edgar",
        "refresh": "quarterly", "asof": "fiscal_period_end",
        "formula": "ttm_operating_income / ttm_revenue",
        "note": "Both trailing twelve months, from the same four quarters.",
    },
    "fcf_yield": {
        "label": "FCF Yield", "units": "fraction", "source": "edgar",
        "refresh": "quarterly", "asof": "fiscal_period_end",
        "formula": "(ttm_operating_cash_flow - ttm_capex) / market_cap",
        "note": "Capital expenditure is a positive outflow in the cash-flow "
                "statement, so it is subtracted by magnitude. This deliberately "
                "does not match the vendor's freeCashflow, which implies about "
                "$16bn for Microsoft against roughly $70bn of actual free cash "
                "flow; ours reconstructs from the filed statements.",
    },
    "revenue_growth_yoy": {
        "label": "Revenue Growth YoY", "units": "fraction", "source": "edgar",
        "refresh": "quarterly", "asof": "fiscal_period_end",
        "formula": "ttm_revenue / prior_ttm_revenue - 1",
        "note": "Trailing twelve months against the twelve before it, which is "
                "the smoother and more usual construction for a screen. The "
                "vendor's revenueGrowth compares a single quarter with the "
                "year-ago quarter, so the two agree only when growth is steady.",
    },
    "eps_growth_yoy": {
        "label": "EPS Growth YoY", "units": "fraction", "source": "edgar",
        "refresh": "quarterly", "asof": "fiscal_period_end",
        "formula": "ttm_diluted_eps / prior_ttm_diluted_eps - 1",
        "note": "Trailing twelve months against the twelve before it, on diluted "
                "EPS. Same difference from the vendor as revenue growth: theirs "
                "is a single quarter, so it can carry the opposite sign.",
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


def derive_from_price_history(stocks):
    """Everything the stored daily series can answer, without a network call.

    enrich_with_prices downloads a year of closes and volumes per ticker in
    bulk, one request per ~200 tickers. Yahoo's per-ticker quote summary, which
    is one request each and the slowest pass in the run, was answering several
    questions the bulk data already contains. Where both can answer, this wins:
    it is complete for every ticker with a stored series rather than for
    whichever ones a budgeted pass happened to reach.

    Sets price, change_pct, return_1m, return_12_2, return_52w,
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

    counts = collections.Counter()
    missing = short = stale = 0
    for s in stocks:
        status = s.get("status") or {}
        try:
            blob = json.loads((PRICES_DIR / _news_filename(s["ticker"]))
                              .read_text(encoding="utf-8"))
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
        counts["price"] += 1

        if px[-2] > 0:
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
          f"({missing} no history, {short} too short, {stale} stale).")
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
_BENCHMARK_SYMBOL = "^GSPC"      # S&P 500
_TRADING_DAYS = 252
# Enough of a year to annualize honestly. Below this the numbers are noise
# wearing an annual label.
_MIN_RISK_OBS = 120


def enrich_with_market_series(max_age_hours=24):
    """Store the risk-free rate and the benchmark beside the ticker histories.

    Two symbols, one file, same 24h cache as the per-ticker prices. Returns True
    when a usable series is on disk afterwards."""
    PRICES_DIR.mkdir(parents=True, exist_ok=True)
    path = PRICES_DIR / MARKET_FILE
    if path.exists():
        try:
            cached = json.loads(path.read_text(encoding="utf-8"))
            age = _age_hours_from_iso(cached.get("updated"))
            if age is not None and age <= max_age_hours and cached.get("risk_free"):
                print(f"market: risk-free and benchmark are {age:.1f}h old, reusing.")
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
    bench = series(_BENCHMARK_SYMBOL)
    if not rf and not bench:
        print("market: both series empty, keeping whatever is already on disk.")
        return path.exists()
    payload = {"updated": datetime.now(timezone.utc).isoformat(),
               "risk_free_symbol": _RISK_FREE_SYMBOL, "risk_free": rf,
               "benchmark_symbol": _BENCHMARK_SYMBOL, "benchmark": bench}
    path.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    print(f"market: stored {len(rf)} risk-free and {len(bench)} benchmark observations "
          f"({_RISK_FREE_SYMBOL} latest {rf[-1][1] if rf else 'n/a'}%).")
    return True


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
        if _finite(price) and _finite(eps) and eps > 0:
            put(s, "pe", price / eps, -500, 1000)
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
            if _finite(gp):
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
            for f in ("pe", "price_book", "roe_ttm", "gross_margin", "operating_margin",
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
_PRICE_RECHECK_FLOOR_H = 6


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


def enrich_with_prices(stocks, max_age_hours=24, batch_size=200):
    """Fetch ~1y daily closes and volumes per ticker via the yf.download bulk
    endpoint and write docs/prices/{TICKER}.json. The bulk endpoint is dramatically faster than
    per-ticker .history() (one HTTP per batch instead of one per ticker), and is
    much friendlier to Yahoo's rate limiter. 24h cache per file so the midday/
    evening runs are no-ops. Stored shape: {"updated": iso, "closes": [[date, close], ...]}.
    Skipped silently if yfinance is missing."""
    if not stocks:
        return 0
    try:
        import yfinance as yf
    except ImportError:
        print("prices: yfinance not installed, skipping.")
        return 0
    PRICES_DIR.mkdir(parents=True, exist_ok=True)

    expected_close = _last_expected_session()

    def needs_fetch(ticker):
        f = PRICES_DIR / _news_filename(ticker)
        if not f.exists():
            return True
        # Freshness comes from the "updated" field the writer stores in the file,
        # not from the mtime, which CI resets on every checkout.
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            return True
        age = _age_hours_from_iso(data.get("updated"))
        if age is None or age > max_age_hours:
            return True
        # How recently we asked is the wrong question. The panel takes its price
        # from the last close in this file, so what matters is whether that close
        # is the latest session.
        #
        # On a 24h window alone it never was. The file refreshed around 00:23 UTC
        # and the panel was written around 23:21 UTC, roughly 23 hours later and
        # just inside the window, so the fetch was skipped and the panel recorded
        # a close one session old. Every run. On 2026-09-11, 4,703 of 5,340
        # tickers carried an identical price across three consecutive panel
        # dates, and prices_updated advanced anyway because it stamps the attempt
        # rather than a new value. price, change_pct, market_cap, pe and
        # high52w_proximity all inherited it.
        #
        # The recheck floor bounds the cost: exchange holidays are not detected,
        # so on a holiday expected_close names a session that never happens and
        # this would otherwise refetch the whole universe on every run.
        if age > _PRICE_RECHECK_FLOOR_H:
            closes = data.get("closes") or []
            last_close = closes[-1][0] if closes and isinstance(closes[-1], list) else None
            if last_close and last_close < expected_close:
                return True
        return False

    todo = [s for s in stocks if needs_fetch(s["ticker"])]
    skipped = len(stocks) - len(todo)
    if not todo:
        print(f"prices: all {len(stocks)} ticker files within {max_age_hours}h, skipping fetch.")
        return 0

    fetched = 0
    t0 = time.time()
    # Yahoo uses '-' for class shares (BRK-B); Wikipedia uses '.' (BRK.B). Translate.
    sym_map = {s["ticker"].replace(".", "-"): s["ticker"] for s in todo}
    yf_syms = list(sym_map.keys())

    for i in range(0, len(yf_syms), batch_size):
        chunk = yf_syms[i:i + batch_size]
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
                payload = {
                    "ticker": ticker,
                    "updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "closes": closes,
                }
                # Only when we actually got some, so a file without the key is
                # an old file rather than a ticker Yahoo reports no volume for.
                if any(v is not None for v in volumes):
                    payload["volumes"] = volumes
                (PRICES_DIR / _news_filename(ticker)).write_text(
                    json.dumps(payload, separators=(",", ":")), encoding="utf-8"
                )
                fetched += 1
            except Exception:
                continue

    elapsed = time.time() - t0
    print(f"prices: wrote {fetched}/{len(todo)} ticker files in {elapsed:.1f}s ({skipped} cached < {max_age_hours}h).")
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
    "eps_diluted": ["EarningsPerShareDiluted",
                    "IncomeLossFromContinuingOperationsPerDilutedShare"],
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
)


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
                return None
        except Exception:
            if attempt == tries - 1:
                return None
        time.sleep(1.0 * (3 ** attempt))
    return None


def _filing_to_text(raw):
    """Strip a filing document to readable text.

    The ix:header removal is not optional. An inline-XBRL filing carries a
    couple of KB of taxonomy context at the top, and naive tag-stripping turns
    it into a wall of 'http://fasb.org/us-gaap/2025#LongTermDebtNoncurrent'."""
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
    return text[:_FILING_MAX_TEXT]


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


def _fetch_segment_note(cik, ticker):
    """Latest 10-Q or 10-K segment note as text.

    Returns (accession, form, filing_date, name, text). The date is the periodic
    filing's own, not the date of the 8-K that triggered the fetch: the note
    describes the quarter the 10-Q covers, and stamping it with today's date
    would make a note from August read as current on the page."""
    raw = _edgar_get(EDGAR_SUBMISSIONS_URL.format(cik=cik))
    if not raw:
        return None
    try:
        rec = json.loads(raw).get("filings", {}).get("recent", {})
    except Exception:
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
# there is nothing in the 10-K to extract. Item 7 is therefore not collected.
_ITEM_MAX = {"business": 26000, "risk_factors": 42000}
_ITEM_MIN = 1500
# A real section runs at least this far before the next item heading. Table of
# contents entries are a few hundred characters apart at most.
_ITEM_SECTION_MIN = 3000
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
        span = after[0] - st
        if span >= _ITEM_SECTION_MIN and _opens_a_section(text, st, en):
            best, best_len = st, span
            break
    if best is None:
        return None
    body = text[best:best + min(best_len, _ITEM_MAX[kind])].strip()
    return body if len(body) >= _ITEM_MIN else None


def fetch_10k_items(cik, ticker):
    """Item 1 and Item 1A from the latest 10-K. Returns [(kind, accession, filed, text)]."""
    raw = _edgar_get(EDGAR_SUBMISSIONS_URL.format(cik=cik))
    if not raw:
        return []
    try:
        rec = json.loads(raw).get("filings", {}).get("recent", {})
    except Exception:
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
    "net_income":       ["NetIncomeLoss"],
    "eps_diluted":      ["EarningsPerShareDiluted"],
    "ocf":              ["NetCashProvidedByUsedInOperatingActivities",
                         "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"],
    "capex":            ["PaymentsToAcquirePropertyPlantAndEquipment"],
    "assets":           ["Assets"],
    "equity":           ["StockholdersEquity"],
    "shares_diluted":   ["WeightedAverageNumberOfDilutedSharesOutstanding"],
}
_FIN_FLOW = {"revenue", "gross_profit", "operating_income", "net_income",
             "eps_diluted", "ocf", "capex", "shares_diluted"}
_FIN_ANNUAL = (350, 380, 12)
_FIN_QUARTERLY = (80, 100, 20)


def _fin_series(facts, metric, lo, hi):
    """One metric as {period_end: (value, tag)}.

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


def record_financials(rows, observed_at):
    """Append reported periods not already held. Dedupe on ticker+period+end."""
    if not rows:
        return 0
    path = FINANCIALS_CSV_DIR / "reported.csv"
    seen = set()
    if path.exists():
        try:
            with path.open(encoding="utf-8", newline="") as fh:
                for r in csv.DictReader(fh):
                    seen.add((r.get("ticker"), r.get("period"), r.get("period_end")))
        except Exception as exc:
            print(f"csv: could not read reported.csv for dedupe ({exc}); appending all.")
    fresh = []
    for r in rows:
        key = (r["ticker"], r["period"], r["period_end"])
        if key in seen:
            continue
        seen.add(key)
        fresh.append({**r, "collected_at": observed_at})
    n = _append_csv(path, FINANCIAL_COLUMNS, fresh)
    if n:
        print(f"csv: appended {n} reported periods to data/financials/reported.csv.")
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


def _quarterly_from_records(records):
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
    guessed at."""
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
                    if key not in quarters:
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


def _select_concept_series(facts, concept_keys, builder, unit_keys=None):
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
    decides, because that order is a real preference between live tags."""
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
            candidates.append((rank, series))
    if not candidates:
        return []
    newest = max(s[0]["end"] for _, s in candidates)
    try:
        cutoff = (_date.fromisoformat(newest) - timedelta(days=_CONCEPT_STALE_DAYS)).isoformat()
    except Exception:
        cutoff = ""
    live = [c for c in candidates if c[1][0]["end"] >= cutoff]
    return min(live or candidates, key=lambda c: c[0])[1]


def _extract_quarterly_series(facts, concept_keys, max_periods=12):
    """Quarterly values for the best matching concept, most recent first."""
    return _select_concept_series(facts, concept_keys, _quarterly_from_records)[:max_periods]


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


def compute_edgar_factors(facts):
    """Compute the 5 quarterly-trend factors from a CIK's XBRL facts dict.
    Each factor goes through a plausibility clamp; out-of-range values are dropped."""
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
    eps_dil = _extract_quarterly_series(facts, EDGAR_CONCEPT_FALLBACKS["eps_diluted"])
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

    out = {}
    # Trailing-twelve-month aggregates and latest balance-sheet values. These
    # are the inputs the price-dependent ratios need; the ratios themselves are
    # computed in derive_ratios_from_fundamentals once a price is known.
    for key, val in (
        ("ttm_revenue", _ttm(revenues)),
        ("ttm_gross_profit", _ttm(gp)),
        ("ttm_operating_income", _ttm(op_inc)),
        ("ttm_net_income", _ttm(net_income)),
        ("ttm_eps_diluted", _ttm(eps_dil)),
        ("ttm_dep_amort", _ttm(dep_amort) if dep_amort else split_da),
        ("prior_ttm_revenue", _ttm(revenues, _TTM_QUARTERS)),
        ("prior_ttm_eps_diluted", _ttm(eps_dil, _TTM_QUARTERS)),
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
    newest = max([s[0]["end"] for s in (revenues, net_income, eps_dil) if s] or [""])
    if newest:
        out["fiscal_period_end"] = newest

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
    cap_sorted = sorted(stocks, key=lambda s: -(s.get("market_cap") or 0))
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


def enrich_with_edgar(stocks, ticker_cik_map, max_workers=8):
    """For each stock with a CIK match, fetch EDGAR companyfacts and compute the
    5 quarterly-trend factors. Updates dicts in place. Honors SEC's 10 req/sec
    rate limit via 8 worker threads (each thread sleeps minimally between calls).
    Returns count of tickers enriched."""
    if not stocks or not ticker_cik_map:
        print("EDGAR enrichment: no stocks or empty CIK map, skipping.")
        return 0
    from concurrent.futures import ThreadPoolExecutor, as_completed

    by_ticker = {s["ticker"]: s for s in stocks}
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
        return sym, out

    enriched = 0
    budget_hit = False
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
            sym, factors = f.result()
            if not factors:
                continue
            s = by_ticker.get(sym)
            if s:
                s.update(factors)
                s["edgar_updated"] = today_str
                enriched += 1
    elapsed = time.time() - t0
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


def compute_peer_scores(stocks):
    """Stamp g/v/m/q dimension scores and per-field sector percentiles onto each stock.

    Sets, per stock: g, v, m, q (sector z-scores, None when under-covered),
    dims_present (0-4), and pct (a dict of field -> 0-100 percentile rank).
    Returns a summary dict for logging."""
    # Cohort values per sector per field, non-null only.
    cohorts = {}
    for s in stocks:
        sector = s.get("sector")
        if not sector:
            continue          # no sector means no peers; scored as unknown below
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
    for s in stocks:
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
    }
    print(f"scoring: {summary['sectors']} sector cohorts; "
          f"{summary['scorable']}/{len(stocks)} stocks scorable "
          f"(>= {MIN_DIMENSIONS_FOR_COMPOSITE} of 4 dimensions); "
          f"dimension counts {summary['dims_distribution']}; "
          f"{pct_emitted:,} percentile ranks emitted.")
    return summary


def get_or_generate_stocks_universe():
    """Cached US stocks universe scraped from Wikipedia (S&P 500/400/600), enriched
    with live quote data from Yahoo Finance via yfinance.

    Cache strategy: skip the full refresh ONLY if the cache is very fresh (< 4 hours)
    AND in the same ISO week. Otherwise: re-pull Wikipedia (fast, free), merge any
    previous static enrichment (market_cap, pe) as a fallback layer, then attempt a
    fresh yfinance pass. Yahoo rate-limits aggressively so a single run rarely covers
    100% of 1500 names; subsequent runs accumulate coverage."""
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

    # Short-circuit: if cache is very fresh (< 4h) and same week, skip the heavy
    # refresh. News still gets a chance to fetch on its own 12h cadence so a
    # fresh-deploy after a recent run doesn't have to wait until tomorrow morning.
    # Schema bump: any cached stock lacking op_margin_history forces a full
    # rebuild even if the 4h cache window says we could short-circuit.
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
        enrich_with_prices(cached_list)
        derive_from_price_history(cached_list)
        enrich_with_market_series()
        derive_risk_metrics(cached_list)
        derive_ratios_from_fundamentals(cached_list)
        return last_known
    if last_known and not schema_ok:
        print("stocks_universe: schema bump, bypassing the daily cache.")

    # Build fresh universe from Wikipedia (S&P 500/400/600) + iShares (Russell 1000/2000)
    stocks = fetch_all_universes()
    if not stocks:
        print("stocks_universe: Wikipedia + iShares returned nothing, falling back to last cache.")
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
        # Tag the fallback so the caller can tell a real scrape from a repeat of
        # yesterday. Its prices are the prior session's and must not be written
        # into the panel under today's date.
        return {**(last_known or {"iso_week": week_key, "generated_at": now.isoformat(), "stocks": []}),
                "stale": True}

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
        "prior_ttm_revenue", "prior_ttm_eps_diluted",
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

    top_n = sorted(stocks, key=lambda s: -(s.get("market_cap") or 0))[:_INSIDER_TOP_N_BY_MARKET_CAP]
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
    enrich_with_prices(stocks)
    derive_from_price_history(stocks)
    enrich_with_market_series()
    derive_risk_metrics(stocks)
    # After the price derivation, because every ratio here needs a price.
    derive_ratios_from_fundamentals(stocks)

    # Peer scoring last: it reads every factor the steps above populate.
    compute_peer_scores(stocks)

    total_with_cap = sum(1 for s in stocks if s.get("market_cap"))
    total_with_price = sum(1 for s in stocks if s.get("price"))
    total_with_edgar = sum(1 for s in stocks if s.get("edgar_updated"))

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
        "stocks": stocks,
    }
    cache_path.write_text(json.dumps(result, separators=(",", ":")), encoding="utf-8")
    pct_cap = (total_with_cap / len(stocks) * 100) if stocks else 0
    pct_price = (total_with_price / len(stocks) * 100) if stocks else 0
    pct_edgar = (total_with_edgar / len(stocks) * 100) if stocks else 0
    print(f"stocks_universe: regenerated for {date_key} ({len(stocks)} stocks; {fresh_count} fresh yfinance, {edgar_count} fresh EDGAR, {insider_count} insider signals, {news_count} news pulls; coverage: {pct_cap:.0f}% market_cap, {pct_price:.0f}% price, {pct_edgar:.0f}% EDGAR).")
    return result



# ── Per-page generators ─────────────────────────────────────────────────────

def _hero_eyebrow_text(briefs):
    now_et = datetime.now(EASTERN)
    today_str = now_et.strftime("%Y-%m-%d")
    latest = briefs[0] if briefs else None
    latest_date = (latest or {}).get("date", today_str)
    latest_type = (latest or {}).get("type", "morning")
    edition_label = {"morning": "Morning", "midday": "Midday", "evening": "Evening"}.get(latest_type, latest_type.title())
    try:
        _d = datetime.strptime(latest_date, "%Y-%m-%d")
        pretty = _d.strftime("%b %d, %Y")
    except Exception:
        pretty = latest_date
    return f"Live, {edition_label} edition, {pretty}"


def generate_home(briefs, recent_trends):
    """Write docs/index.html: v6 hero, Recent Trends panel, three destination cards."""
    eyebrow_text = _hero_eyebrow_text(briefs)

    snapshot = recent_trends.get("snapshot") or []
    if not snapshot:
        # Backwards-compat: read legacy 'synthesis' as a single bullet
        legacy = (recent_trends.get("synthesis") or "").strip()
        snapshot = [legacy] if legacy else ["Recent trends will appear here once briefs accumulate."]
    snapshot_html = '<ul class="snapshot-list">' + "".join(f'<li>{b}</li>' for b in snapshot) + '</ul>'

    themes = recent_trends.get("themes") or []
    themes_html = ""
    if themes:
        pills = "".join(f'<span class="theme-pill">{t}</span>' for t in themes)
        themes_html = f'<div class="themes-list">{pills}</div>'

    total_briefs = len(briefs)
    sources_set = set()
    total_stories = 0
    for b in briefs:
        for sec in b.get("sections", []):
            for st in sec.get("stories", []):
                total_stories += 1
                src = st.get("source", "")
                if src:
                    sources_set.add(src.split("·")[0].strip())
    total_sources = len(sources_set)

    body = f"""
<section class="hero">
  <div class="hero-l">
    <div class="eyebrow"><span class="live-dot"></span>{eyebrow_text}</div>
    <h1 class="hero-title">Regular Briefs and Curated&nbsp;Stories</h1>
  </div>
  <div class="hero-r">
    <p class="hero-sub">Finance, Politics, Tech, and more. Apterreon's three-times-daily intelligence brief, plus a running story library and a filterable universe of US-listed stocks.</p>
    <div class="hero-actions">
      <a class="btn-primary" href="./today.html">Read today's briefs &rarr;</a>
      <a class="btn-secondary" href="./stories.html">Browse stories &rarr;</a>
    </div>
  </div>
</section>

<section class="featured" id="recent-trends">
  <article class="featured-card">
    <div class="feat-meta">
      <span class="tag">Recent Trends</span>
      <span>Apterreon</span><span class="dot"></span>
      <span>Past {min(10, total_briefs)} brief days</span>
    </div>
    <div class="feat-kicker">Snapshot</div>
    {snapshot_html}
    {themes_html}
    <div class="feat-grid">
      <div class="feat-stat">
        <div class="fs-label">Stories synthesized</div>
        <div class="fs-val">{total_stories}</div>
        <div class="fs-delta">across {total_briefs} briefs</div>
      </div>
      <div class="feat-stat">
        <div class="fs-label">Sources</div>
        <div class="fs-val">{total_sources}</div>
        <div class="fs-delta" style="color:var(--text-4)">unique publications</div>
      </div>
      <div class="feat-stat">
        <div class="fs-label">Cadence</div>
        <div class="fs-val">3x</div>
        <div class="fs-delta">briefs per weekday</div>
      </div>
    </div>
    <div class="feat-actions">
      <a class="btn-primary" href="./today.html">See today's briefs <span style="font-size:16px">&rarr;</span></a>
      <a class="quiet" href="./stories.html">Explore the story library</a>
    </div>
  </article>
</section>

<section class="destinations">
  <div class="destinations-h">
    <h2>Three places to land.</h2>
    <p>Pick what you came for. Today's read, the running archive, or this week's watchlist.</p>
  </div>
  <div class="destinations-grid">
    <a class="dest-card" href="./today.html">
      <div class="dest-eyebrow">Daily</div>
      <div class="dest-title">Today's Briefs</div>
      <p class="dest-body">Morning, midday, and evening editions for today, with the section grid and the cross-domain edge from each.</p>
      <span class="dest-cta">Open today &rarr;</span>
    </a>
    <a class="dest-card" href="./stories.html">
      <div class="dest-eyebrow">Archive</div>
      <div class="dest-title">Story Library</div>
      <p class="dest-body">Search and filter every story across recent briefs. Headline, source, section, and a deep link to the original.</p>
      <span class="dest-cta">Browse the library &rarr;</span>
    </a>
    <a class="dest-card" href="./stocks.html">
      <div class="dest-eyebrow">Research</div>
      <div class="dest-title">Stocks</div>
      <p class="dest-body">Filterable universe of US-listed names from S&amp;P 500, 400, and 600. Search by ticker or sector, sort by market cap. Refreshed weekly.</p>
      <span class="dest-cta">Explore &rarr;</span>
    </a>
  </div>
</section>
"""
    html = render_page("Apterreon, Daily Intelligence Brief", body, active_nav="home")
    (DOCS_DIR / "index.html").write_text(html, encoding="utf-8")


def generate_today(briefs):
    """Write docs/today.html: today's editions (morning/midday/evening) with section grid each."""
    now_et = datetime.now(EASTERN)
    today_iso = now_et.strftime("%Y-%m-%d")
    pretty_today = now_et.strftime("%A, %B %d, %Y")

    todays = [b for b in briefs if b.get("date") == today_iso]
    todays.sort(key=lambda b: {"morning": 0, "midday": 1, "evening": 2}.get(b.get("type", ""), 9))

    edition_blocks = ""
    if not todays:
        edition_blocks = '<div class="edition-empty">Today’s brief has not generated yet. The next scheduled run will populate this view.</div>'
    else:
        edition_times = {"morning": "7:00 AM ET", "midday": "12:15 PM ET", "evening": "4:45 PM ET"}
        edition_names = {"morning": "Morning Brief", "midday": "Midday Update", "evening": "Evening Wrap"}
        for b in todays:
            ed_type = b.get("type", "")
            ed_key = b.get("key", "")
            brief_url = f"./{ed_key}" if ed_key else "#"
            edge = (b.get("the_edge") or "").strip()
            edge_html = f'<p class="edition-edge">{edge}</p>' if edge else ""

            section_cards_html = ""
            for idx, sec in enumerate(b.get("sections", []), start=1):
                sec_name = sec.get("name", "")
                stories = sec.get("stories", [])
                top = stories[:2]
                if not top:
                    continue
                stories_html = ""
                for st in top:
                    headline = (st.get("headline") or "").replace('"', '&quot;')
                    source = (st.get("source") or "").replace('"', '&quot;')
                    link = (st.get("link") or brief_url).replace('"', '&quot;')
                    stories_html += (
                        f'<a class="sc-item" href="{link}" target="_blank" rel="noopener">'
                        f'<div><div class="sc-item-headline">{headline}</div>'
                        f'<div class="sc-item-source">{source}</div></div>'
                        f'<span class="sc-arrow">&rarr;</span>'
                        f'</a>'
                    )
                num_str = f"{idx:02d}"
                count_str = f"{len(stories):02d}"
                section_cards_html += f"""
    <article class="sec-card">
      <div class="sc-head">
        <div class="sc-num">{num_str}</div>
        <div class="sc-titles">
          <div class="sc-eyebrow">Section</div>
          <div class="sc-title">{sec_name}</div>
        </div>
        <div class="sc-count">{count_str}</div>
      </div>
      <div class="sc-list">{stories_html}</div>
    </article>"""

            edition_blocks += f"""
<div class="edition-block">
  <div class="edition-head">
    <div class="edition-name">{edition_names.get(ed_type, ed_type.title())}</div>
    <div class="edition-time">{edition_times.get(ed_type, '')}</div>
    <a class="edition-link" href="./{ed_key}">Open full brief &rarr;</a>
  </div>
  {edge_html}
  <div class="section-grid">{section_cards_html}</div>
</div>
"""

    body = f"""
<section class="editions">
  <div class="editions-h">
    <h2>Today, {pretty_today}.</h2>
    <p>Each edition's top sections at a glance. Open the full brief for everything else.</p>
  </div>
  {edition_blocks}
</section>
"""
    html = render_page("Today's Briefs, Apterreon", body, active_nav="today")
    (DOCS_DIR / "today.html").write_text(html, encoding="utf-8")


def generate_stories(briefs):
    """Write docs/stories.html: search + filter + library across all archived briefs."""
    site_url = os.environ.get("APTERREON_SITE_URL", "https://ctlsmith5689.github.io/daily-intelligence-brief")

    all_stories = []
    sections_present = []
    seen = set()
    for b in briefs:
        b_key = b.get("key", "")
        b_type = b.get("type", "")
        b_date = b.get("date", "")
        for sec in b.get("sections", []):
            sec_name = sec.get("name", "")
            if sec_name and sec_name not in seen:
                seen.add(sec_name)
                sections_present.append(sec_name)
            for st in sec.get("stories", []):
                if not st.get("headline"):
                    continue
                all_stories.append({
                    "headline": st.get("headline", ""),
                    "summary": st.get("summary", ""),
                    "source": st.get("source", ""),
                    "link": st.get("link") or f"{site_url}/{b_key}",
                    "edition": b_type,
                    "date": b_date,
                    "section": sec_name,
                    "brief_url": f"{site_url}/{b_key}",
                })
    edition_rank = {"morning": 0, "midday": 1, "evening": 2}
    all_stories.sort(key=lambda s: (s["date"], edition_rank.get(s["edition"], 99)), reverse=True)
    all_stories_json = json.dumps(all_stories, separators=(",", ":"))
    sections_present_json = json.dumps(sections_present)

    body = """
<section class="lib">
  <div class="lib-h">
    <h2>Story library.</h2>
    <span class="lib-count" id="lib-count">All stories</span>
  </div>
  <div class="lib-controls">
    <label class="lib-search">
      <span class="icon">&#8981;</span>
      <input type="search" id="lib-search" placeholder="Search headlines, summaries, sources..." autocomplete="off" spellcheck="false">
      <button type="button" class="clear-btn" id="lib-clear" hidden>Clear</button>
    </label>
    <div class="lib-chips" id="lib-chips">
      <span class="lib-chip-label">Topic</span>
      <span class="lib-chip active" data-section="">All</span>
    </div>
  </div>
  <div class="lib-list" id="lib-list"></div>
</section>
"""
    stories_js = STORIES_JS_TEMPLATE.replace("__ALL_STORIES_JSON__", all_stories_json).replace("__SECTIONS_JSON__", sections_present_json)
    html = render_page("Story Library, Apterreon", body, active_nav="stories", extra_scripts=stories_js)
    (DOCS_DIR / "stories.html").write_text(html, encoding="utf-8")


# Filter panel config. Each row becomes a min/max input pair on the stocks page.
# type drives input parsing + display:
#   "cap"   -> 300M / 5B / 1T suffixes; data unit is raw dollars
#   "pct"   -> bare numbers treated as percent (10 -> 0.10); data unit is decimal
#   "score" -> bare numbers stored as-is; sentiment range -1 to +1 etc.
#   "ratio" -> bare numbers stored as-is; absolute multiples
#   "int"   -> bare integers; counts
FILTER_PANEL = [
    {"title": "Universe",        "open": True,  "rows": [
        {"label": "Market Cap ($M)",       "key": "market_cap",         "type": "cap",   "placeholder_min": "300", "placeholder_max": "10,000", "tier_chips": True},
    ]},
    {"title": "Growth",          "open": False, "rows": [
        {"label": "Revenue Growth YoY",    "key": "revenue_growth_yoy", "type": "pct",   "placeholder_min": "min % (e.g. 10)",  "placeholder_max": "max %"},
        {"label": "EPS Growth YoY",        "key": "eps_growth_yoy",     "type": "pct",   "placeholder_min": "min % (e.g. 5)",   "placeholder_max": "max %"},
        {"label": "Revenue Acceleration",  "key": "revenue_acceleration", "type": "pct", "placeholder_min": "min % (e.g. 0)",   "placeholder_max": "max %"},
        {"label": "Gross Margin Trend",    "key": "gross_margin_trend", "type": "pct",   "placeholder_min": "min % (e.g. 0)",   "placeholder_max": "max %"},
        {"label": "FCF Growth YoY",        "key": "fcf_growth_yoy",     "type": "pct",   "placeholder_min": "min % (e.g. 0)",   "placeholder_max": "max %"},
    ]},
    {"title": "Value",           "open": False, "rows": [
        {"label": "P/E (Trailing)",        "key": "pe",                 "type": "ratio", "placeholder_min": "min",              "placeholder_max": "max (e.g. 30)"},
        {"label": "EV/EBITDA",             "key": "ev_ebitda",          "type": "ratio", "placeholder_min": "min",              "placeholder_max": "max (e.g. 20)"},
        {"label": "EV/Revenue",            "key": "ev_revenue",         "type": "ratio", "placeholder_min": "min",              "placeholder_max": "max (e.g. 8)"},
        {"label": "Price/Book",            "key": "price_book",         "type": "ratio", "placeholder_min": "min",              "placeholder_max": "max (e.g. 5)"},
        {"label": "FCF Yield",             "key": "fcf_yield",          "type": "pct",   "placeholder_min": "min % (e.g. 5)",   "placeholder_max": "max %"},
    ]},
    {"title": "Momentum",        "open": False, "rows": [
        {"label": "12-2 Month Return",     "key": "return_12_2",        "type": "pct",   "placeholder_min": "min % (e.g. 10)",  "placeholder_max": "max %"},
        {"label": "1-Month Return",        "key": "return_1m",          "type": "pct",   "placeholder_min": "min %",            "placeholder_max": "max %"},
        {"label": "52W High Proximity",    "key": "high52w_proximity",  "type": "pct",   "placeholder_min": "min % (e.g. -30)", "placeholder_max": "max % (e.g. -5)"},
        {"label": "Rel Strength vs S&P",   "key": "rel_strength_sp500", "type": "pct",   "placeholder_min": "min %",            "placeholder_max": "max %"},
        {"label": "Volume Trend",          "key": "volume_trend",       "type": "pct",   "placeholder_min": "min %",            "placeholder_max": "max %"},
    ]},
    {"title": "Quality",         "open": False, "rows": [
        {"label": "ROE (TTM)",             "key": "roe_ttm",            "type": "pct",   "placeholder_min": "min % (e.g. 15)",  "placeholder_max": "max %"},
        {"label": "Earnings Consistency",  "key": "earnings_consistency", "type": "ratio", "placeholder_min": "min (0 to 1)",   "placeholder_max": "max"},
        {"label": "Net Debt/EBITDA",       "key": "net_debt_ebitda",    "type": "ratio", "placeholder_min": "min (e.g. -1)",    "placeholder_max": "max (e.g. 3)"},
        {"label": "Op Margin Stability",   "key": "op_margin_stability", "type": "ratio", "placeholder_min": "min",             "placeholder_max": "max (e.g. 0.05)"},
        {"label": "Accruals Ratio",        "key": "accruals_ratio",     "type": "pct",   "placeholder_min": "min %",            "placeholder_max": "max %"},
        {"label": "Gross Margin",          "key": "gross_margin",       "type": "pct",   "placeholder_min": "min %",            "placeholder_max": "max %"},
        {"label": "Operating Margin",      "key": "operating_margin",   "type": "pct",   "placeholder_min": "min %",            "placeholder_max": "max %"},
        {"label": "Volatility (1y)",       "key": "volatility_1y",      "type": "pct",   "placeholder_min": "min %",            "placeholder_max": "max % (e.g. 30)"},
        {"label": "Beta vs S&P 500",       "key": "beta_1y",            "type": "ratio",   "placeholder_min": "min (e.g. 0.5)",   "placeholder_max": "max (e.g. 1.2)"},
        {"label": "Sharpe (1y)",           "key": "sharpe_1y",          "type": "ratio",   "placeholder_min": "min (e.g. 1)",     "placeholder_max": "max"},
        {"label": "Max Drawdown (1y)",     "key": "max_drawdown_1y",    "type": "pct",   "placeholder_min": "min % (e.g. -30)", "placeholder_max": "max %"},
    ]},
]


def render_screener_page(title, body_html, extra_scripts=""):
    """Full-bleed shell for the screener, transcribed from the design.

    Deliberately not render_page: that wrapper centres content in an article
    column with its own topnav and footer, and the design's screener is edge to
    edge with its own header. Keeps the same <head> so the theme boot, fonts and
    stylesheet stay identical across pages."""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="color-scheme" content="dark light">
<meta name="theme-color" content="#EDE8DC">
<link rel="manifest" href="manifest.json">
<title>{title}</title>
<script>
(function(){{
  try {{
    var t = localStorage.getItem('apt-theme-v2');
    document.documentElement.setAttribute('data-theme', t === 'dark' ? 'dark' : 'light');
  }} catch (e) {{}}
}})();
</script>
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Instrument+Serif:ital@0;1&family=Space+Grotesk:wght@400;500;700&family=Space+Mono:wght@400;700&display=swap" rel="stylesheet">
<style>
{SITE_CSS}
</style>
</head>
<body class="page-stocks scr-page">
{body_html}
<script>
{THEME_JS}
{extra_scripts}
</script>
</body>
</html>"""


def generate_stocks_page(universe):
    """Write docs/stocks.html: filterable table of US stocks scraped from Wikipedia
    (S&P 500/400/600), optionally enriched with live FMP quote data."""
    stocks = universe.get("stocks", []) or []
    iso_week = universe.get("date") or universe.get("iso_week", "")
    source = universe.get("source", "wikipedia")

    sectors = sorted({(s.get("sector") or "").strip() for s in stocks if (s.get("sector") or "").strip()})
    indexes = []
    seen_idx = set()
    for s in stocks:
        idx = (s.get("index") or "").strip()
        if idx and idx not in seen_idx:
            seen_idx.add(idx)
            indexes.append(idx)

    # Trim before writing: drop nulls and empty strings (absent keys read the
    # same to the client) and round floats, which otherwise carry ~12 digits of
    # binary noise apiece. Together these take the payload from 4.64 MB to 3.3 MB
    # before gzip, and GitHub Pages serves it gzipped at ~0.6 MB.
    def _trim(stock):
        out = {}
        for k, v in stock.items():
            if v is None or v == "":
                continue
            out[k] = round(v, 4) if isinstance(v, float) else v
        return out

    data_path = DOCS_DIR / "stocks-data.json"
    data_path.write_text(
        json.dumps([_trim(s) for s in stocks], separators=(",", ":")),
        encoding="utf-8")
    print(f"stocks: wrote {data_path.name} "
          f"({data_path.stat().st_size / 1024 / 1024:.2f} MB, {len(stocks)} tickers).")
    stocks_json = json.dumps("stocks-data.json")
    sectors_json = json.dumps(sectors)
    indexes_json = json.dumps(indexes)

    meta_line = f"Updated {iso_week} · Source: {source}" if iso_week else f"Source: {source}"

    # ---- The screener rail, built from FILTER_PANEL ----------------------
    # Structure and every style value are transcribed from the design file
    # (Screener Creative.dc.html, the 953-line cream variant), with its sc-for
    # loops expanded here and its {{ }} bindings filled from real data. The JS
    # hook ids are preserved so the existing filter, sort and expand behaviour
    # keeps working against the new markup.
    rail = []
    for sec in FILTER_PANEL:
        fields = []
        for row in sec["rows"]:
            fields.append(
                f'''<div style="margin-bottom:11px">
  <div style="font-family:\'Space Mono\',monospace;font-size:9px;letter-spacing:1px;text-transform:uppercase;color:var(--text-3);margin-bottom:4px">{row["label"]}</div>
  <div style="display:flex;align-items:center;gap:7px">
    <input class="stk-filter-input" data-filter="{row["key"]}" data-bound="min" placeholder="{row.get("placeholder_min", "min")}" style="width:100%;min-width:0;background:transparent;border:none;border-bottom:1px solid var(--border-bright);outline:none;font-family:\'Space Mono\',monospace;font-size:11px;color:var(--text-1);padding:2px 0">
    <span style="color:var(--text-4);font-size:10px">to</span>
    <input class="stk-filter-input" data-filter="{row["key"]}" data-bound="max" placeholder="{row.get("placeholder_max", "max")}" style="width:100%;min-width:0;background:transparent;border:none;border-bottom:1px solid var(--border-bright);outline:none;font-family:\'Space Mono\',monospace;font-size:11px;color:var(--text-1);padding:2px 0">
  </div>
</div>''')
        opened = " open" if sec.get("open") else ""
        rail.append(
            f'''<details class="stk-rg"{opened}>
  <summary style="display:flex;align-items:center;justify-content:space-between;padding:11px 20px;cursor:pointer;list-style:none">
    <span style="font-family:\'Space Mono\',monospace;font-size:10px;letter-spacing:2.5px;text-transform:uppercase">{sec["title"]}</span>
    <span style="font-family:\'Space Mono\',monospace;font-size:9px;color:var(--apt-red)" data-setcount="{sec["title"]}"></span>
  </summary>
  <div style="padding:2px 20px 15px">{"".join(fields)}</div>
</details>''')
    rail_html = "".join(rail)

    index_chips = '<span class="lib-chip active" data-index="">All</span>'
    sector_chips = '<span class="lib-chip active" data-sector="">All</span>'

    body = f"""
<div class="scr">
  {render_topnav("stocks")}

  <div class="scr-body">
    <aside class="scr-rail">
      <div class="scr-rail-in">
        <div class="scr-rail-h">
          <span>Refine</span>
          <span id="stk-reset" class="scr-reset">Reset</span>
        </div>
        <details class="stk-rg" open>
          <summary style="display:flex;align-items:center;justify-content:space-between;padding:11px 20px;cursor:pointer;list-style:none">
            <span style="font-family:'Space Mono',monospace;font-size:10px;letter-spacing:2.5px;text-transform:uppercase">Index</span>
          </summary>
          <div style="padding:2px 20px 15px" class="lib-chips" id="stk-index-chips">{index_chips}</div>
        </details>
        <details class="stk-rg">
          <summary style="display:flex;align-items:center;justify-content:space-between;padding:11px 20px;cursor:pointer;list-style:none">
            <span style="font-family:'Space Mono',monospace;font-size:10px;letter-spacing:2.5px;text-transform:uppercase">Sector</span>
          </summary>
          <div style="padding:2px 20px 15px" class="lib-chips" id="stk-sector-chips">{sector_chips}</div>
        </details>
        <div id="stk-filter-panel">{rail_html}</div>
        <div style="padding:14px 20px;border-bottom:1px solid var(--border)">
          <div class="scr-rail-lab">Hygiene</div>
          <label class="scr-check"><input type="checkbox" id="stk-only-enriched"><span>Require live market cap data</span></label>
          <div class="scr-cov">
            <div class="scr-cov-h">Minimum datapoints per factor</div>
            <div class="scr-cov-grid">
              <label><span>Growth</span><input type="number" class="stk-cov-input" data-cov="Growth" min="0" max="5" value="0"></label>
              <label><span>Value</span><input type="number" class="stk-cov-input" data-cov="Value" min="0" max="5" value="0"></label>
              <label><span>Momentum</span><input type="number" class="stk-cov-input" data-cov="Momentum" min="0" max="5" value="0"></label>
              <label><span>Quality</span><input type="number" class="stk-cov-input" data-cov="Quality" min="0" max="5" value="0"></label>
            </div>
            <p class="scr-cov-note">Each factor is built from 5 inputs. Raise these to drop
            companies whose score rests on one or two numbers.</p>
          </div>
        </div>
        <div style="padding:14px 20px 24px">
          <div class="scr-rail-lab">Saved</div>
          <div id="stk-views-list" class="scr-saved"></div>
          <div class="scr-saveline">
            <input type="text" id="stk-views-name" class="stk-views-input" placeholder="Name this view...">
            <button type="button" id="stk-views-save" class="stk-views-save">Save</button>
          </div>
        </div>
      </div>
    </aside>

    <main class="scr-main">
      <div class="scr-tool">
        <div class="stk-views-switch" id="stk-view-switch">
          <button type="button" class="stk-view-btn" data-view="chart">Chart</button>
          <button type="button" class="stk-view-btn" data-view="radar">Radar</button>
          <button type="button" class="stk-view-btn active" data-view="list">List</button>
        </div>
        <div class="scr-find">
          <span class="ic">&#9906;</span>
          <input type="search" id="stk-search" placeholder="Search ticker or company" autocomplete="off"
                 spellcheck="false" role="combobox" aria-autocomplete="list" aria-expanded="false"
                 aria-controls="stk-ac">
          <button type="button" class="clear-btn" id="stk-clear" hidden>&times;</button>
          <div class="scr-ac" id="stk-ac" role="listbox" hidden></div>
        </div>
        <span class="scr-note" id="stk-score-note"></span>
        <span class="scr-count" id="stk-count"></span>
      </div>

      <div class="scr-pane">
        <div id="stk-chart" class="stk-chart" hidden>
          <div class="stk-lenses" id="stk-lenses"></div>
          <p class="stk-chart-blurb" id="stk-chart-blurb"></p>
          <div class="stk-focus" id="stk-focus"></div>
          <div class="stk-chart-plot">
            <span class="ax tl"></span><span class="ax tr"></span>
            <span class="ax bl"></span><span class="ax br"></span>
            <canvas id="stk-chart-canvas"></canvas>
            <div class="stk-chart-tip" id="stk-chart-tip" hidden></div>
          </div>
          <p class="stk-chart-foot" id="stk-chart-foot"></p>
        </div>

        <div id="stk-radar" class="stk-radar" hidden>
          <div class="stk-map">
            <div class="stk-col-h">
              <div class="stk-col-h-1"><span>Factor map</span>
                <button type="button" class="stk-map-spin" id="stk-spin">Spin</button></div>
              <div class="stk-col-h-2">Click to focus &middot; ctrl-click to add &middot; drag to turn</div>
            </div>
            <div class="stk-map-plot"><canvas id="stk-map-canvas"></canvas></div>
            <p class="stk-map-foot" id="stk-map-foot"></p>
          </div>
          <div class="stk-col-h stk-rings-h">
            <div class="stk-col-h-1"><span>Sector percentiles</span></div>
            <div class="stk-col-h-2">Each spoke ranks the company against its own sector peers</div>
          </div>
          <div class="stk-radar-plot">
            <div class="stk-radar-quads">
              <span class="q tl">Momentum</span><span class="q tr">Growth</span>
              <span class="q bl">Value</span><span class="q br">Quality</span>
            </div>
            <svg id="stk-radar-svg" viewBox="0 0 620 620" role="img" aria-label="Factor radar"></svg>
          </div>
          <p class="stk-radar-note" id="stk-radar-note"></p>
          <aside class="stk-radar-side">
            <div class="stk-col-h">
              <div class="stk-col-h-1"><h3 id="stk-radar-title">Pick a company</h3></div>
              <div class="stk-col-h-2" id="stk-radar-hint">Click one on the map, or take one from here</div>
            </div>
            <div class="stk-cmps" id="stk-radar-chips"></div>
            <div id="stk-radar-breakdown"></div>
            <div id="stk-radar-picklist" class="stk-picklist" hidden></div>
          </aside>
        </div>

        <div class="stk-table">
          <div class="stk-sortrow">
            <span class="stk-sortlab">Sort</span>
            <button type="button" class="stk-sort active" data-sortby="__score__">Score</button>
            <button type="button" class="stk-sort" data-sortby="change_pct">Move</button>
            <button type="button" class="stk-sort" data-sortby="market_cap">Size</button>
            <button type="button" class="stk-sort" data-sortby="ticker">A&ndash;Z</button>
          </div>
          <div class="stk-head">
            <div class="stk-th"></div>
            <div class="stk-th" data-sort="ticker">Company</div>
            <div class="stk-th" data-sort="sector">Sector</div>
            <div class="stk-th desc" data-sort="market_cap">Cap</div>
            <div class="stk-th" data-sort="change_pct">1D</div>
            <div class="stk-th stk-th-fac">
              <span>Growth</span><span>Value</span><span>Momentum</span><span>Quality</span>
            </div>
            <div class="stk-th" data-sort="__score__">Score</div>
            <div class="stk-th" data-sort="earnings_date">Earnings</div>
          </div>
          <div id="stk-list"></div>
        </div>
      </div>

      {render_footer(meta_line)}
    </main>
  </div>
</div>
"""
    stocks_js = (STOCKS_JS_TEMPLATE
                 .replace("__DATA_URL__", stocks_json)
                 .replace("__PCT_FIELDS_JSON__", json.dumps(PCT_ARRAY_FIELDS))
                 .replace("__FIELD_METHODS_JSON__", json.dumps(FIELD_METHODS))
                 .replace("__MIN_COHORT_JSON__", json.dumps(MIN_COHORT_FOR_PERCENTILE))
                 .replace("__FIELD_STATUS_JSON__", json.dumps(FIELD_STATUS))
                 .replace("__REFRESH_CLASSES_JSON__", json.dumps(REFRESH_CLASSES))
                 .replace("__SECTORS_JSON__", sectors_json)
                 .replace("__INDEXES_JSON__", indexes_json))
    html = render_screener_page("Stocks, Apterreon", body, extra_scripts=stocks_js)
    (DOCS_DIR / "stocks.html").write_text(html, encoding="utf-8")


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


def write_thesis_views():
    """One JSON per ticker holding the current view and its history."""
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
        fm = _parse_front_matter(latest.read_text(encoding="utf-8"))
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
    out = {}
    if not FILINGS_CSV_DIR.is_dir():
        return out
    for path in sorted(FILINGS_CSV_DIR.glob("*.csv")):
        try:
            with path.open(encoding="utf-8", newline="") as fh:
                for row in csv.DictReader(fh):
                    tk = (row.get("ticker") or "").upper()
                    if tk:
                        out.setdefault(tk, []).append(row)
        except Exception as exc:
            print(f"company: could not read {path.name} ({exc}).")
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
            keep.append({"filed": r.get("filed"), "form": r.get("form"),
                         "doc_kind": r.get("doc_kind"), "items": r.get("items"),
                         "accession": r.get("accession"),
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
    try:
        blob = json.loads((PRICES_DIR / _news_filename(ticker)).read_text(encoding="utf-8"))
        closes = [c for c in (blob.get("closes") or []) if c and c[1] is not None]
    except Exception:
        return {}
    if not closes:
        return {}
    last_date, last = closes[-1][0], float(closes[-1][1])
    out = {"last": round(last, 2), "as_of": last_date}
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

    out = []
    for tdir in sorted(notes_dir.iterdir()):
        if not tdir.is_dir():
            continue
        ticker = tdir.name
        notes = sorted(tdir.glob("*.md"))
        if not notes:
            continue
        latest = notes[-1]
        fm = _parse_front_matter(latest.read_text(encoding="utf-8"))
        if not fm:
            continue
        direction = (fm.get("direction") or "").strip()
        mine = pred_by_ticker.get(ticker) or []
        open_preds = [p for p in mine if p.get("prediction_id") not in scored]
        if direction not in GRADEABLE_DIRECTIONS:
            status = "watching"
        elif mine and not open_preds:
            status = "graded"
        else:
            status = "open"
        review_by = (fm.get("review_by") or "").strip()
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
            rec[k] = (fm.get(k) or "").strip()
        hist = [e for e in events if e.get("ticker") == ticker]
        rec["history"] = [{"date": e.get("date"), "kind": e.get("kind"),
                           "direction": e.get("direction"),
                           "conviction": e.get("conviction"),
                           "prior_direction": e.get("prior_direction"),
                           "prior_conviction": e.get("prior_conviction"),
                           "trigger": e.get("trigger")} for e in hist]
        rec["claim_changed_ever"] = any(e.get("claim_changed") == "yes" for e in hist)
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


def _research_sleeve_bar(label, z, span=2.0):
    """One diverging bar for a sector z-score, zero at the centre.

    The sign is carried by the number as well as by the direction and colour, so
    the bar never has to be read on colour alone."""
    frac = max(-1.0, min(1.0, (z or 0.0) / span))
    half = abs(frac) * 50.0
    left = 50.0 - half if frac < 0 else 50.0
    cls = " rs-sl-neg" if frac < 0 else ""
    return (f'<span class="rs-sl"><i>{_html.escape(label)}</i>'
            f'<span class="rs-sl-track">'
            f'<span class="rs-sl-zero"></span>'
            f'<span class="rs-sl-fill{cls}" style="left:{left:.1f}%;width:{half:.1f}%">'
            f'</span></span>'
            f'<b>{z:+.2f}</b></span>')


def _research_cards(views):
    """Server-rendered cards. The page must be readable before its JS runs."""
    e = _html.escape
    if not views:
        return ('<div class="rs-empty">No notes have been written yet. The analyst '
                'writes to <code>theses/notes/</code> and this page is built from '
                'that directory.</div>')
    parts = []
    for v in views:
        conv = v.get("conviction")
        pips = "".join(
            f'<span class="rs-pip{" rs-pip-on" if conv and i <= conv else ""}"></span>'
            for i in range(1, 6))
        comps = []
        for key, short, hi in (("evidence_base", "evidence", 2),
                               ("falsifier_specific", "falsifier", 1),
                               ("variant_perception", "variant", 1),
                               ("disconfirmation", "counter-case", 1)):
            got = v.get(key)
            on = " rs-comp-on" if got else ""
            comps.append(f'<span class="rs-comp{on}" title="{e(key)} scores '
                         f'{got if got is not None else "?"} of {hi}">'
                         f'{e(short)} <b>{got if got is not None else "?"}</b>'
                         f'<i>/{hi}</i></span>')
        nums = []
        if v.get("entry_price") is not None:
            nums.append(f'<span>entry <b>{v["entry_price"]:.2f}</b></span>')
        price = v.get("price") or {}
        if price.get("last") is not None:
            mv = price.get("move")
            cls = ""
            if mv is not None:
                cls = " rs-up" if mv >= 0 else " rs-down"
            moved = f' <i class="rs-move{cls}">{mv * 100:+.1f}%</i>' if mv is not None else ""
            nums.append(f'<span>now <b>{price["last"]:.2f}</b>{moved}</span>')
        if v.get("target_price") is not None:
            nums.append(f'<span>target <b>{v["target_price"]:.2f}</b></span>')
        if v.get("horizon_days"):
            nums.append(f'<span>horizon <b>{v["horizon_days"]}d</b></span>')
        d2r = v.get("days_to_review")
        if d2r is not None:
            word = f"in {d2r}d" if d2r >= 0 else f"{abs(d2r)}d ago"
            nums.append(f'<span>review <b>{e(word)}</b></span>')

        sleeves = v.get("sleeves") or {}
        screen_line = ""
        if len(sleeves) == 4:
            # These are z-scores against the sector, not percentiles: measured
            # range across the universe is -2.05 to +1.87 with 55 percent of
            # names below zero. Rendering them as a left-anchored 0-100 bar
            # clamped every negative sleeve to empty and printed HIMS as
            # "-39 of 100", which is not a quantity that exists.
            bars = "".join(_research_sleeve_bar(lbl, sleeves[k])
                           for k, lbl in (("g", "G"), ("v", "V"),
                                          ("m", "M"), ("q", "Q")))
            ew = v.get("equal_weight")
            screen_line = (
                '<div class="rs-screen"><span class="rs-k">Where the screen puts it</span>'
                f'<div class="rs-sleeves">{bars}</div>'
                f'<div class="rs-note">Standard deviations from the '
                f'{e(v["sector"] or "sector")} mean, one bar per sleeve, zero at the '
                f'tick. Equal-weighted across the four that is <b>{ew:+.2f}</b>. '
                f'The screener\'s own composite moves with the weights you set there, '
                f'so there is no single number to quote.</div></div>')

        drift = ('<div class="rs-drift">A revision kept the direction and the conviction '
                 'and changed the claim. That is what thesis drift looks like.</div>'
                 if v.get("claim_changed_ever") else "")
        claim = (f'<p class="rs-claim">{e(v["key_claim"])}</p>' if v.get("key_claim")
                 else '<p class="rs-claim rs-claim-none">No claim recorded. The note '
                      'declined to take a view.</p>')
        fals = (f'<div class="rs-fals"><span class="rs-k">What would prove it wrong</span>'
                f'<div>{e(v["falsifier"])}</div></div>' if v.get("falsifier") else "")
        parts.append(
            f'<article class="rs-card" data-dir="{e(v["direction"])}" '
            f'data-status="{e(v["status"])}" data-conv="{conv if conv is not None else 0}" '
            f'data-move="{(price.get("move") if price.get("move") is not None else 0):.4f}" '
            f'data-review="{e(v.get("review_by") or "")}" '
            f'data-written="{e(v.get("written_on") or "")}" '
            f'data-ticker="{e(v["ticker"])}">'
            f'<div class="rs-top">'
            f'<a class="rs-tk" href="./stocks.html#{e(v["ticker"])}">{e(v["ticker"])}</a>'
            f'<span class="rs-nm">{e(v["name"])}</span>'
            f'<span class="rs-status rs-status-{e(v["status"])}">{e(v["status"])}</span>'
            f'</div>'
            f'<div class="rs-dirline">'
            f'<span class="rs-dir rs-dir-{e(v["direction"].replace(" ", "-"))}">'
            f'{e(v["direction"])}</span>'
            f'<span class="rs-pips" title="conviction {conv if conv is not None else "?"} of 5">'
            f'{pips}</span>'
            f'<span class="rs-comps">{"".join(comps)}</span>'
            f'</div>'
            f'{drift}{claim}'
            f'<div class="rs-nums">{"".join(nums)}</div>'
            f'{fals}{screen_line}'
            f'<div class="rs-links">'
            f'<a href="https://github.com/CTLSmith5689/daily-intelligence-brief/blob/main/'
            f'{e(v["note_path"])}" target="_blank" rel="noopener">Read the note</a>'
            f'<span class="rs-meta">{e(v.get("kind") or "note")} '
            f'{e(v.get("written_on") or "")}'
            + (f' &middot; {v["note_count"]} notes on this name' if v["note_count"] > 1 else "")
            + (f' &middot; surfaced by the {e(v["slot"])} slot' if v.get("slot") else "")
            + '</span></div></article>')
    return "".join(parts)


def _research_record_html(rec):
    e = _html.escape
    tiles = [("Names covered", str(rec["tickers"])),
             ("Open calls", str(rec["open"])),
             ("Watching, not graded", str(rec["watching"])),
             ("Graded", str(rec["graded"]))]
    tile_html = "".join(
        f'<div class="rs-tile"><span class="rs-tile-v">{e(v)}</span>'
        f'<span class="rs-tile-k">{e(k)}</span></div>' for k, v in tiles)
    rows = "".join(
        f'<tr><td>{t["conviction"]}</td><td>{t["n"]}</td><td>{t.get("open", 0)}</td>'
        f'<td>{t.get("watching", 0)}</td><td>{t.get("graded", 0)}</td>'
        f'<td class="rs-td-na">&mdash;</td><td class="rs-td-na">&mdash;</td></tr>'
        for t in rec["tiers"]) or '<tr><td colspan="7" class="rs-td-na">No notes yet.</td></tr>'
    if rec["scored"]:
        verdict = ""
    else:
        when = (f' The first call matures on {e(rec["earliest_maturity"])}.'
                if rec.get("earliest_maturity") else "")
        verdict = (
            '<p class="rs-note rs-note-loud">No call has been graded yet, so the two '
            'right-hand columns are empty and the hit rate is unknown.' + when +
            ' Until then this page records what was claimed and when, which is the '
            'part that cannot be reconstructed later.</p>')
    return (
        '<section class="rs-record">'
        '<h2 class="rs-h2">The record</h2>'
        f'<div class="rs-tiles">{tile_html}</div>'
        '<table class="rs-tiers"><thead><tr>'
        '<th>Conviction</th><th>Names</th><th>Open</th><th>Watching</th><th>Graded</th>'
        '<th>Hit rate</th><th>vs peers</th></tr></thead>'
        f'<tbody>{rows}</tbody></table>'
        f'{verdict}'
        '<p class="rs-note">Conviction is not a feeling. It is the sum of four '
        'checkable properties of the note, shown on every card below: whether the '
        'claim rests on filings or only on factors, whether the falsifier carries a '
        'number and a date, whether the view differs from what the price already '
        'says, and whether the note engaged with the best case against itself. '
        'The only reason to track it is this table: if the high-conviction calls do '
        'not beat the low-conviction ones, the judgement is adding nothing and the '
        'table is the only place that shows it.</p>'
        '</section>')


def generate_research(universe):
    """Write docs/research.html: the analyst's current views and their record."""
    stocks = {s.get("ticker"): s for s in (universe.get("stocks") or [])
              if s.get("ticker")}
    today = datetime.now(tz=timezone.utc).date()
    views = _research_views(stocks, today)
    record = _research_record(views)

    dirs = ["all"] + sorted({v["direction"] for v in views})
    statuses = ["all"] + [s for s in ("due", "open", "watching", "graded")
                          if any(v["status"] == s for v in views)]
    e = _html.escape

    # The filter group name is the JS state key; the view key it counts is not
    # always the same word.
    COUNT_KEY = {"dir": "direction", "status": "status"}

    def chips(group, values):
        out = []
        key = COUNT_KEY[group]
        for i, val in enumerate(values):
            on = " on" if i == 0 else ""
            n = "" if val == "all" else (
                f' <i>{sum(1 for v in views if v[key] == val)}</i>')
            label = "all" if val == "all" else val
            out.append(f'<button type="button" class="rs-chip{on}" data-filter="{e(group)}" '
                       f'data-value="{e(val)}" aria-pressed="{"true" if i == 0 else "false"}">'
                       f'{e(label)}{n}</button>')
        return '<div class="rs-chips">' + "".join(out) + '</div>'

    body = (
        '<section class="rs-head">'
        '<h1 class="rs-h1">Research</h1>'
        '<p class="rs-lede">Written views on individual names, and the record of '
        'whether they were right. The screen next door ranks 5,354 companies by '
        'arithmetic and surfaces about four a week for a written opinion, so almost '
        'every name on it will never appear here. That is the intended behaviour of '
        'a screen, not a gap in coverage.</p>'
        '</section>'
        + _research_record_html(record) +
        '<section class="rs-views">'
        '<div class="rs-h2row"><h2 class="rs-h2">The views</h2>'
        '<span class="rs-count" id="rs-count"></span></div>'
        '<div class="rs-controls">'
        f'<div class="rs-group"><span class="rs-k">Direction</span>{chips("dir", dirs)}</div>'
        f'<div class="rs-group"><span class="rs-k">Status</span>{chips("status", statuses)}</div>'
        '<div class="rs-group"><label class="rs-k" for="rs-sort">Sort</label>'
        '<select id="rs-sort" class="rs-select">'
        '<option value="status">Status, then newest</option>'
        '<option value="written">Newest written</option>'
        '<option value="review">Review date</option>'
        '<option value="convict">Conviction</option>'
        '<option value="move">Move since written</option>'
        '</select></div></div>'
        f'<div class="rs-list" id="rs-list">{_research_cards(views)}</div>'
        '<div class="rs-empty" id="rs-noresult" hidden>Nothing matches those filters.</div>'
        '<p class="rs-note">A name shows as <b>watching</b> when the note declined to '
        'take a position. Those produce no row in the prediction ledger and are never '
        'graded, which is deliberate: scoring a "no view" against the market would '
        'manufacture a track record out of abstentions.</p>'
        '</section>')

    html = render_page("Research, Apterreon", body, active_nav="research",
                       extra_scripts=RESEARCH_JS_TEMPLATE)
    (DOCS_DIR / "research.html").write_text(html, encoding="utf-8")
    print(f"research: wrote research.html ({len(views)} names, "
          f"{record['open']} open, {record['graded']} graded).")
    return len(views)


def generate_site(briefs, universe=None):
    """Orchestrator. Generates the full multi-page static site under docs/.
    Triggers Recent Trends (daily-cached) and Stocks Universe (weekly, Wikipedia
    scrape + optional FMP enrichment).

    `universe` lets a caller supply one it already has. A publish run passes the
    committed cache so that rebuilding pages cannot trigger a 1,500-name refresh."""
    recent_trends = get_or_generate_recent_trends(briefs)
    if universe is None:
        universe = get_or_generate_stocks_universe()

    generate_home(briefs, recent_trends)
    generate_today(briefs)
    generate_stories(briefs)
    generate_stocks_page(universe)
    write_manifest()
    # Derived from theses/, which the scheduled analyst writes and which is the
    # source of truth. Regenerated every run so the published view cannot drift
    # from the notes.
    n_thesis = write_thesis_views()
    # Same shape, different author: these come from the filings and companyfacts
    # passes rather than from the analyst. The scored stock dicts carry
    # sub_industry and ttm_revenue, which is what peer share is computed from.
    n_company = write_company_views(universe.get("stocks") or [])
    # After the view writers, because it reads the same theses/ tree and the log
    # line below reports all three together.
    n_views = generate_research(universe)
    print("Wrote docs/index.html, today.html, stories.html, stocks.html, "
          f"research.html ({n_views} names), manifest.json"
          + (f", {n_thesis} thesis views" if n_thesis else "")
          + (f", {n_company} company views" if n_company else "") + ".")


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
        # get_or_generate_stocks_universe. That function short-circuits only when
        # the cache is under four hours old and in the same ISO week, so on any
        # realistic push it would re-scrape Wikipedia and run a yfinance pass
        # over 1,500 names, which is minutes of rate-limited work to rebuild
        # pages from numbers that are already on disk. state/stocks_universe.json
        # is committed, so the cache is always present in CI.
        cache_path = STATE_DIR / "stocks_universe.json"
        universe = None
        if cache_path.exists():
            try:
                universe = json.loads(cache_path.read_text(encoding="utf-8"))
            except Exception as exc:
                print(f"publish: universe cache unreadable ({exc}).")
        if not (universe or {}).get("stocks"):
            print("publish: no usable universe cache; regenerating it once.")
            universe = get_or_generate_stocks_universe()
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
    # the work now, and the rest see the row and stay cheap.
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
    universe = get_or_generate_stocks_universe()
    stocks = (universe or {}).get("stocks") or []

    # Only record a panel row on days the market actually traded. The daily run
    # fires after the close, so a Saturday run would stamp Friday's closing prices
    # with Saturday's date, and Sunday would do it again: three identical rows for
    # one trading day, which silently corrupts any return, volatility or drawdown
    # computed over the panel.
    #
    # Weekends only. Exchange holidays still slip through, since detecting them
    # needs a market calendar this project does not carry; those rows repeat the
    # prior close but are identifiable via the last_updated column.
    panel_rows = 0
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
        panel_rows = record_fundamentals(stocks, date_iso)

    # 5b. Earnings press releases (8-K item 2.02, exhibit EX-99.1).
    #
    # Deliberately after the panel and wrapped, because this is the one part of
    # the run that depends on a third party's index being up. The panel row is
    # the thing that cannot be backfilled; a missed filing is picked up by the
    # next run's four-day window. So nothing in here is allowed to cost the day
    # its row, or the site its rebuild.
    try:
        cik_to_ticker = {}
        for _tkr, _cik in (fetch_edgar_ticker_cik_map() or {}).items():
            try:
                cik_to_ticker.setdefault(int(_cik), _tkr)
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
        if _fin:
            record_financials(_fin, observed_at)
    except Exception as exc:
        print(f"filings: collection failed ({type(exc).__name__}: {exc}); "
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
    healthy = bool(stocks) and (panel_satisfied or not panel_expected)
    return {"status": "published", "mode": mode, "stories": len(headlines),
            "quotes": len(quotes), "stocks": len(stocks),
            "panel_rows": panel_rows, "priced": priced,
            "panel_expected": panel_expected, "panel_satisfied": panel_satisfied,
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
