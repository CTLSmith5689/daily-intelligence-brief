"""
Daily Intelligence Brief. AWS Lambda Handler.
Full-spectrum newsfeed with real market data via Alpha Vantage.
Fetches news via RSS, market data via Alpha Vantage, analysis via Claude API.
Sends via iCloud SMTP. Triggered by EventBridge rules at 7 AM, 12:15 PM, and 4:45 PM ET.
"""

import os
import re
import ssl
import collections
import csv
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

QUOTE_COLUMNS = ["observed_at", "ticker", "label", "price", "change_pct", "is_yield"]
HEADLINE_COLUMNS = ["first_seen", "published", "section", "category", "source", "title", "link"]

# Nested values (benford is a dict, op_margin_history a list) have no sensible
# CSV representation, so they are dropped rather than stringified.
FUNDAMENTAL_SKIP_FIELDS = {"benford", "op_margin_history"}
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
    """Append rows, writing a header only when creating the file."""
    if not rows:
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    is_new = not path.exists()
    with path.open("a", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns, extrasaction="ignore")
        if is_new:
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
    """Append one row per quote. Called on every run, including hourly ones."""
    rows = [{
        "observed_at": observed_at,
        "ticker": q.get("ticker", ""),
        "label": q.get("label", ""),
        # Stored bare so the column is numeric: the display strings carry $ and %.
        "price": str(q.get("price", "")).replace("$", "").replace("%", "").strip(),
        "change_pct": str(q.get("change_pct", "")).replace("%", "").strip(),
        "is_yield": "1" if q.get("is_yield") else "0",
    } for q in quotes]
    n = _append_csv(QUOTES_CSV, QUOTE_COLUMNS, rows)
    print(f"csv: appended {n} quote rows to data/{QUOTES_CSV.name}.")
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
    "Reuters Biz": "https://news.google.com/rss/search?q=when:24h+allinurl:reuters.com+business+OR+markets&hl=en-US&gl=US&ceid=US:en",
    "Bloomberg": "https://news.google.com/rss/search?q=when:24h+allinurl:bloomberg.com+markets+OR+economy&hl=en-US&gl=US&ceid=US:en",
    "WSJ Markets": "https://news.google.com/rss/search?q=when:24h+allinurl:wsj.com+markets+OR+economy&hl=en-US&gl=US&ceid=US:en",
    "FT Markets": "https://news.google.com/rss/search?q=when:24h+allinurl:ft.com+markets+OR+economy&hl=en-US&gl=US&ceid=US:en",
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
            per_feed[category] = {"raw": raw_items, "kept": kept_here, "error": None}
        except Exception as e:
            per_feed[category] = {"raw": 0, "kept": 0, "error": str(e)}
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
  /* Theme-tunable surface tints (used by topnav, cards, table). Dark default. */
  --surface-1:var(--surface-1); --surface-2:var(--surface-2); --surface-3:var(--surface-3);
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

#plexus { position:fixed; inset:0; z-index:0; opacity:var(--plexus-opacity); }
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

.hero { max-width:1200px; margin:0 auto; padding:96px 24px 48px; }
.eyebrow {
  display:inline-flex; align-items:center; gap:8px; padding:6px 14px; border-radius:999px;
  background:rgba(255,31,61,0.08); border:1px solid rgba(255,31,61,0.20);
  font-family:'Space Mono',monospace; font-size:11px; letter-spacing:2px; color:var(--apt-rose);
  text-transform:uppercase; margin-bottom:24px;
  opacity:0; transform:translateY(8px); animation:fadeUp .8s .1s ease-out forwards;
}
.eyebrow .live-dot { width:6px; height:6px; border-radius:50%; background:#34D27A; }

h1.hero-title {
  font-family:'Space Grotesk',sans-serif; font-weight:800; font-size:84px; line-height:0.98;
  letter-spacing:-0.03em; margin-bottom:24px; max-width:1000px;
  background:linear-gradient(135deg, #FFFFFF 0%, #FFFFFF 40%, #FF7A85 70%, #FF1F3D 100%);
  -webkit-background-clip:text; background-clip:text; -webkit-text-fill-color:transparent;
  opacity:0; transform:translateY(16px); animation:fadeUp .9s .25s ease-out forwards;
}
@keyframes fadeUp { to { opacity:1; transform:translateY(0); } }

.hero-sub {
  font-size:19px; line-height:1.6; color:var(--text-2); max-width:640px; margin-bottom:40px; font-weight:400;
  opacity:0; transform:translateY(12px); animation:fadeUp .9s .4s ease-out forwards;
}
.hero-actions { display:flex; gap:12px; flex-wrap:wrap; opacity:0; transform:translateY(12px); animation:fadeUp .9s .55s ease-out forwards; }
.btn-primary {
  padding:14px 24px; font-size:14px; font-weight:600;
  background:linear-gradient(135deg, #FF1F3D 0%, #CC0028 100%); color:#FFF;
  border-radius:12px; cursor:pointer; transition:transform .15s, box-shadow .25s;
  box-shadow:0 8px 32px rgba(255,31,61,0.3); display:inline-flex; align-items:center; gap:8px;
  text-decoration:none;
}
.btn-primary:hover { transform:translateY(-2px); box-shadow:0 12px 40px rgba(255,31,61,0.45); }
.btn-secondary {
  padding:14px 24px; font-size:14px; font-weight:500;
  background:rgba(255,255,255,0.04); color:var(--text-1);
  border:1px solid var(--border-bright); border-radius:12px;
  cursor:pointer; transition:all .15s; display:inline-flex; align-items:center; gap:8px;
  text-decoration:none;
}
.btn-secondary:hover { background:rgba(255,255,255,0.07); border-color:rgba(255,255,255,0.20); }

.featured { max-width:1200px; margin:0 auto; padding:32px 24px 64px; }
.featured-card {
  position:relative;
  background:linear-gradient(180deg, rgba(22,23,31,0.85) 0%, rgba(17,18,26,0.92) 100%);
  backdrop-filter:blur(24px); -webkit-backdrop-filter:blur(24px);
  border:1px solid var(--border-bright); border-radius:24px;
  padding:48px; overflow:hidden;
  opacity:0; transform:translateY(20px); animation:fadeUp 1s .7s ease-out forwards;
}
.featured-card::before {
  content:''; position:absolute; inset:-1px; border-radius:24px; padding:1px;
  background:linear-gradient(135deg, rgba(255,31,61,0.5), transparent 40%, transparent 60%, rgba(255,122,133,0.3));
  -webkit-mask:linear-gradient(#000,#000) content-box, linear-gradient(#000,#000);
  mask:linear-gradient(#000,#000) content-box, linear-gradient(#000,#000);
  -webkit-mask-composite:xor; mask-composite:exclude; pointer-events:none;
}
.feat-meta { display:flex; align-items:center; gap:10px; margin-bottom:18px; font-family:'Space Mono',monospace; font-size:11px; letter-spacing:2px; color:var(--text-3); text-transform:uppercase; flex-wrap:wrap; }
.feat-meta .tag { padding:4px 10px; border-radius:6px; background:rgba(255,31,61,0.10); color:var(--apt-rose); border:1px solid rgba(255,31,61,0.20); }
.feat-meta .dot { width:3px; height:3px; border-radius:50%; background:var(--text-4); }
.feat-kicker { font-family:'Space Grotesk',sans-serif; font-weight:700; font-size:13px; letter-spacing:4px; text-transform:uppercase; color:var(--apt-rose); margin-bottom:14px; }
.feat-body { font-size:18px; line-height:1.7; color:var(--text-1); max-width:920px; margin-bottom:8px; font-weight:400; letter-spacing:-0.005em; }
.feat-body::first-letter { font-family:'Space Grotesk',sans-serif; font-size:1.4em; font-weight:700; line-height:1; color:var(--apt-rose); padding-right:2px; }
.feat-grid { display:grid; grid-template-columns:repeat(3, 1fr); gap:18px; margin-top:32px; }
.feat-stat { padding:18px 20px; background:rgba(255,255,255,0.03); border:1px solid var(--border); border-radius:14px; transition:all .25s; }
.feat-stat:hover { background:rgba(255,255,255,0.05); border-color:rgba(255,255,255,0.12); transform:translateY(-2px); }
.fs-label { font-family:'Space Mono',monospace; font-size:10px; letter-spacing:2px; color:var(--text-4); text-transform:uppercase; margin-bottom:8px; }
.fs-val { font-family:'Space Grotesk',sans-serif; font-size:30px; font-weight:700; color:var(--text-1); letter-spacing:-0.02em; line-height:1; }
.fs-delta { font-size:12px; color:#34D27A; margin-top:6px; }
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
  display:block; padding:32px;
  background:rgba(17,18,26,0.65);
  backdrop-filter:blur(20px); -webkit-backdrop-filter:blur(20px);
  border:1px solid var(--border); border-radius:20px;
  transition:all .35s cubic-bezier(0.2,0.8,0.2,1);
}
.dest-card:hover { transform:translateY(-4px); border-color:rgba(255,31,61,0.4); box-shadow:0 20px 60px rgba(255,31,61,0.15), 0 8px 24px rgba(0,0,0,0.3); }
.dest-eyebrow { font-family:'Space Mono',monospace; font-size:10px; letter-spacing:2px; color:var(--apt-rose); text-transform:uppercase; margin-bottom:12px; }
.dest-title { font-family:'Space Grotesk',sans-serif; font-size:24px; font-weight:700; letter-spacing:-0.01em; color:var(--text-1); margin-bottom:10px; }
.dest-body { font-size:14px; line-height:1.55; color:var(--text-3); margin-bottom:18px; }
.dest-cta { font-family:'Space Mono',monospace; font-size:11px; letter-spacing:2px; color:var(--apt-rose); text-transform:uppercase; }

.features { max-width:1200px; margin:0 auto; padding:64px 24px; }
.features-h { display:flex; justify-content:space-between; align-items:end; margin-bottom:40px; flex-wrap:wrap; gap:18px; }
.features-h h2 { font-family:'Space Grotesk',sans-serif; font-weight:700; font-size:42px; letter-spacing:-0.02em; line-height:1.1; max-width:600px; }
.features-h p { font-size:16px; color:var(--text-3); line-height:1.6; max-width:380px; }

.section-grid { display:grid; grid-template-columns:repeat(2, 1fr); gap:18px; }
@media (max-width:780px) { .section-grid { grid-template-columns:1fr; } }

.sec-card {
  position:relative;
  background:rgba(17,18,26,0.65);
  backdrop-filter:blur(20px); -webkit-backdrop-filter:blur(20px);
  border:1px solid var(--border); border-radius:20px;
  padding:28px; transition:all .35s cubic-bezier(0.2,0.8,0.2,1);
  overflow:hidden;
}
.sec-card::after {
  content:''; position:absolute; inset:0; border-radius:20px;
  background:linear-gradient(135deg, rgba(255,31,61,0.18), transparent 50%);
  opacity:0; transition:opacity .35s; pointer-events:none;
}
.sec-card:hover { transform:translateY(-4px); border-color:rgba(255,31,61,0.4); box-shadow:0 20px 60px rgba(255,31,61,0.15), 0 8px 24px rgba(0,0,0,0.3); }
.sec-card:hover::after { opacity:1; }
.sc-head { display:flex; align-items:flex-start; gap:14px; margin-bottom:20px; }
.sc-num {
  width:44px; height:44px; flex-shrink:0;
  display:flex; align-items:center; justify-content:center;
  background:linear-gradient(135deg, rgba(255,31,61,0.16), rgba(255,31,61,0.04));
  border:1px solid rgba(255,31,61,0.22); border-radius:12px;
  font-family:'Space Mono',monospace; font-size:14px; font-weight:500; color:var(--apt-rose);
}
.sc-titles { flex:1; min-width:0; }
.sc-eyebrow { font-family:'Space Mono',monospace; font-size:10px; letter-spacing:2px; color:var(--text-4); text-transform:uppercase; margin-bottom:4px; }
.sc-title { font-family:'Space Grotesk',sans-serif; font-size:22px; font-weight:700; letter-spacing:-0.01em; color:var(--text-1); }
.sc-count { font-family:'Space Mono',monospace; font-size:11px; color:var(--text-4); padding:4px 10px; background:rgba(255,255,255,0.04); border-radius:8px; }
.sc-list { display:flex; flex-direction:column; gap:0; }
.sc-item { padding:14px 0; border-top:1px solid var(--border); display:grid; grid-template-columns:1fr auto; gap:12px; align-items:start; transition:padding-left .15s; }
.sc-item:first-child { border-top:none; padding-top:4px; }
.sc-item:hover { padding-left:6px; }
.sc-item-headline { font-size:15px; font-weight:500; color:var(--text-1); line-height:1.45; }
.sc-item-source { font-family:'Space Mono',monospace; font-size:10px; letter-spacing:1.5px; color:var(--text-4); text-transform:uppercase; margin-top:6px; }
.sc-arrow { color:var(--text-4); font-size:18px; transition:color .15s, transform .15s; align-self:start; padding-top:2px; }
.sc-item:hover .sc-arrow { color:var(--apt-red); transform:translateX(4px); }

.editions { max-width:1200px; margin:0 auto; padding:96px 24px 64px; }
.editions-h { display:flex; justify-content:space-between; align-items:end; margin-bottom:40px; flex-wrap:wrap; gap:18px; }
.editions-h h2 { font-family:'Space Grotesk',sans-serif; font-weight:700; font-size:42px; letter-spacing:-0.02em; line-height:1.1; }
.editions-h p { font-size:16px; color:var(--text-3); line-height:1.6; max-width:380px; }
.edition-block { margin-bottom:48px; }
.edition-head { display:flex; align-items:baseline; gap:12px; margin-bottom:18px; padding-bottom:12px; border-bottom:1px solid var(--border); flex-wrap:wrap; }
.edition-name { font-family:'Space Grotesk',sans-serif; font-size:24px; font-weight:700; color:var(--text-1); }
.edition-time { font-family:'Space Mono',monospace; font-size:11px; letter-spacing:2px; color:var(--text-4); text-transform:uppercase; }
.edition-link { margin-left:auto; font-family:'Space Mono',monospace; font-size:11px; letter-spacing:2px; color:var(--apt-rose); text-transform:uppercase; }
.edition-edge { font-size:16px; line-height:1.65; color:var(--text-2); margin-bottom:24px; padding:18px 22px; background:var(--surface-1); border-left:3px solid var(--apt-red); border-radius:8px; }
.edition-empty { padding:32px; text-align:center; font-family:'Space Mono',monospace; font-size:12px; letter-spacing:2px; color:var(--text-4); text-transform:uppercase; background:rgba(17,18,26,0.5); border:1px dashed var(--border); border-radius:14px; }

.lib { max-width:1200px; margin:0 auto; padding:96px 24px 64px; }
.lib.lib-wide { padding-top:36px; padding-bottom:24px; max-width:1480px; }
.lib.lib-wide .lib-h { margin-bottom:10px; }
.lib.lib-wide .lib-h h2 { font-size:32px; }
.lib.lib-wide { max-width:min(1640px, 96vw); }
.lib-h { display:flex; justify-content:space-between; align-items:end; margin-bottom:18px; flex-wrap:wrap; gap:14px; }
.lib-h h2 { font-family:'Space Grotesk',sans-serif; font-weight:700; font-size:42px; letter-spacing:-0.02em; line-height:1.1; }
.lib-h .lib-count { font-family:'Space Mono',monospace; font-size:11px; letter-spacing:2px; color:var(--text-4); text-transform:uppercase; padding:6px 12px; border:1px solid var(--border); border-radius:999px; }

.lib-controls { display:flex; flex-direction:column; gap:14px; margin-bottom:24px; padding:20px; background:var(--surface-1); backdrop-filter:blur(20px); -webkit-backdrop-filter:blur(20px); border:1px solid var(--border); border-radius:16px; }
.lib-search { display:flex; align-items:center; gap:12px; padding:12px 16px; background:var(--bg-1); border:1px solid var(--border); border-radius:12px; transition:all .15s; }
.lib-search:focus-within { border-color:var(--apt-red); box-shadow:0 0 0 3px rgba(255,31,61,0.10); }
.lib-search .icon { color:var(--text-3); font-size:16px; }
.lib-search input { flex:1; background:transparent; border:none; outline:none; font-family:'Space Mono',monospace; font-size:14px; color:var(--text-1); }
.lib-search input::placeholder { color:var(--text-4); }
.lib-search .clear-btn { background:transparent; border:none; cursor:pointer; padding:4px 8px; color:var(--text-3); font-family:'Space Mono',monospace; font-size:10px; letter-spacing:2px; text-transform:uppercase; transition:color .15s; }
.lib-search .clear-btn:hover { color:var(--text-1); }
.lib-search .clear-btn[hidden] { display:none; }

.lib-chips { display:flex; flex-wrap:wrap; gap:6px; align-items:center; }
.lib-chip-label { font-family:'Space Mono',monospace; font-size:9px; letter-spacing:2px; color:var(--text-4); text-transform:uppercase; margin-right:4px; }
.lib-chip {
  padding:6px 12px; font-family:'Space Mono',monospace; font-size:10px; letter-spacing:1.5px;
  color:var(--text-3); cursor:pointer; background:transparent;
  border:1px solid var(--border); border-radius:999px; text-transform:uppercase;
  user-select:none; transition:all .15s;
}
.lib-chip:hover { color:var(--text-1); border-color:var(--border-bright); }
.lib-chip.active { color:#FFF; background:rgba(255,31,61,0.18); border-color:var(--apt-red); }

.lib-list { display:flex; flex-direction:column; gap:1px; background:var(--border); border:1px solid var(--border); border-radius:14px; overflow:hidden; }
.lib-item {
  background:var(--surface-2); padding:18px 22px;
  display:grid; grid-template-columns:120px 1fr auto; gap:18px; align-items:center;
  transition:background .15s;
}
.lib-item:hover { background:rgba(22,23,31,0.95); }
.lib-item .li-section {
  font-family:'Space Mono',monospace; font-size:9px; letter-spacing:1.5px;
  color:var(--apt-rose); text-transform:uppercase; padding:4px 8px;
  border:1px solid rgba(255,31,61,0.20); border-radius:6px; text-align:center;
  background:rgba(255,31,61,0.06); justify-self:start;
}
.lib-item .li-headline { font-size:15px; color:var(--text-1); line-height:1.45; font-weight:500; }
.lib-item .li-meta { font-family:'Space Mono',monospace; font-size:9px; letter-spacing:1.5px; color:var(--text-4); text-transform:uppercase; margin-top:5px; }
.lib-item .li-src { font-family:'Space Mono',monospace; font-size:10px; letter-spacing:1.5px; color:var(--text-4); text-transform:uppercase; text-align:right; }
@media (max-width:680px) {
  .lib-item { grid-template-columns:1fr; gap:6px; padding:16px 18px; }
  .lib-item .li-src { text-align:left; }
}

.empty-state { padding:48px; text-align:center; font-family:'Space Mono',monospace; font-size:12px; letter-spacing:2px; color:var(--text-4); text-transform:uppercase; background:rgba(17,18,26,0.5); border:1px solid var(--border); border-radius:14px; }

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
.stk-radar { display:grid; grid-template-columns:minmax(0,720px) 320px; gap:0 24px;
  align-items:start; justify-content:start; }
.stk-radar > .stk-radar-plot { grid-column:1; grid-row:1; }
.stk-radar > .stk-radar-note { grid-column:1; grid-row:2; max-width:720px; }
.stk-radar > .stk-radar-side { grid-column:2; grid-row:1 / span 2; }
.stk-radar-plot { position:relative; border:1px solid var(--border); background:var(--surface-1);
  padding:14px; max-width:720px; }
.stk-radar-plot svg { width:100%; height:auto; display:block; }
.stk-radar-quads .q { position:absolute; font-family:'Space Mono',monospace; font-size:10px;
  letter-spacing:2px; text-transform:uppercase; color:var(--text-3); }
.stk-radar-quads .tl { top:14px; left:16px; } .stk-radar-quads .tr { top:14px; right:16px; }
.stk-radar-quads .bl { bottom:14px; left:16px; } .stk-radar-quads .br { bottom:14px; right:16px; }
.stk-radar-note { font-size:11px; color:var(--text-4); line-height:1.6; margin:10px 2px 0; }
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
.stk-radar-fam { margin-top:16px; }
.stk-radar-fam-h { display:flex; justify-content:space-between; align-items:baseline;
  border-bottom:1px solid var(--border-bright); padding-bottom:5px; margin-bottom:7px; }
.stk-radar-fam-h b { font-family:'Space Mono',monospace; font-size:10px; letter-spacing:2px;
  text-transform:uppercase; font-weight:400; color:var(--text-2); }
.stk-radar-row { display:grid; grid-template-columns:1fr 46px 26px; gap:8px; align-items:center;
  font-size:11px; color:var(--text-3); padding:2px 0; }
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
.scr-count { margin-left:auto; flex-shrink:0; white-space:nowrap;
  font-family:'Space Mono',monospace; font-size:10px; letter-spacing:1.5px; color:var(--text-4); }
.scr-pane { padding:0 32px 30px; }
/* Ultrawide: the rail stays pinned left, the working area stops growing. Left
   aligned rather than centred so the table keeps its edge against the rail. */
@media (min-width:1700px) {
  .scr-tool > *:last-child { margin-right:auto; }
  .scr-tool, .scr-pane, .scr-foot { max-width:1660px; }
}
.scr-foot { border-top:1px solid var(--text-1); padding:20px 32px; display:flex;
  justify-content:space-between; gap:16px; font-family:'Space Mono',monospace; font-size:10px;
  letter-spacing:1.5px; color:var(--text-4); }
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
.stk-row:hover { background:rgba(22,23,31,0.6); }
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
.fp-grid { display:grid; grid-template-columns:repeat(4, 1fr); gap:14px; }
@media (max-width:1000px) { .fp-grid { grid-template-columns:repeat(2, 1fr); } }
@media (max-width:560px) { .fp-grid { grid-template-columns:1fr; } }
.fp-card { padding:14px 16px; background:transparent; border:1px solid var(--border);  }
.fp-card-h { font-family:'Space Grotesk',sans-serif; font-size:13px; font-weight:700; letter-spacing:0.02em; color:var(--text-1); margin-bottom:10px; padding-bottom:8px; border-bottom:1px solid var(--border); }
.fp-card-toggle { display:flex; align-items:center; justify-content:space-between; width:100%; background:transparent; border:0; cursor:pointer; color:var(--text-1); font-family:'Space Grotesk',sans-serif; font-size:13px; font-weight:700; letter-spacing:0.02em; text-align:left; padding:0 0 8px 0; margin-bottom:10px; border-bottom:1px solid var(--border); transition:color .15s; }
.fp-card-toggle:hover { color:var(--apt-rose); }
.fp-card-caret { font-size:10px; color:var(--text-4); transition:transform .18s ease, color .15s; }
.fp-card.fp-card-active .fp-card-caret { transform:rotate(90deg); color:var(--apt-rose); }
.fp-card.fp-card-active { border-color:var(--apt-rose); box-shadow:0 0 0 1px rgba(255,57,77,0.18); }

/* Standalone methodology card. Slides in below the 4 factor cards when one is clicked. */
.fp-meta-panel { margin-top:14px; }
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
"""


PLEXUS_JS = """
(function() {
  const c = document.getElementById('plexus');
  if (!c) return;
  const ctx = c.getContext('2d');
  let W, H, dpr, stars = [], nodes = [], flow = [];
  const CONNECT = 220; let mx = -999, my = -999;
  function resize() {
    dpr = window.devicePixelRatio || 1;
    W = innerWidth; H = innerHeight;
    c.width = W*dpr; c.height = H*dpr; c.style.width = W+'px'; c.style.height = H+'px';
    ctx.setTransform(dpr,0,0,dpr,0,0); build();
  }
  function build() {
    stars = []; for (let i = 0; i < 240; i++) stars.push({x:Math.random()*W,y:Math.random()*H,r:Math.random()*0.7+0.2,b:Math.random()*0.18+0.03,p:Math.random()*6.28});
    nodes = []; const n = Math.max(40, Math.floor((W*H)/26000));
    for (let i = 0; i < n; i++) nodes.push({x:Math.random()*W,y:Math.random()*H,size:0.5+Math.random()*1.5,b:0.10+Math.random()*0.30,ph:Math.random()*6.28,vx:(Math.random()-0.5)*0.16,vy:(Math.random()-0.5)*0.12});
    flow = []; for (let i = 0; i < 40; i++) flow.push({a:-1,b:-1,t:Math.random(),s:0.002+Math.random()*0.003,sz:0.3+Math.random()*0.6,br:0.15+Math.random()*0.3});
  }
  function pickEdge(fp) {
    if (!nodes.length) return;
    const a = Math.floor(Math.random()*nodes.length); let bj = -1, bd = CONNECT;
    for (let j = 0; j < nodes.length; j++) { if (j===a) continue; const dx = nodes[a].x-nodes[j].x, dy = nodes[a].y-nodes[j].y; const d = Math.sqrt(dx*dx+dy*dy); if (d < bd) { bd = d; bj = j; } }
    fp.a = a; fp.b = bj; fp.t = 0;
  }
  document.addEventListener('mousemove', e => { mx = e.clientX; my = e.clientY; });
  document.addEventListener('mouseleave', () => { mx = -999; my = -999; });
  let t = 0;
  function draw() {
    t += 0.004;
    ctx.fillStyle = '#0A0A0F'; ctx.fillRect(0,0,W,H);
    for (const s of stars) { const tw = 0.5+0.5*Math.sin(t*5+s.p); ctx.fillStyle = `rgba(220,210,210,${s.b*tw})`; ctx.beginPath(); ctx.arc(s.x,s.y,s.r,0,Math.PI*2); ctx.fill(); }
    for (const n of nodes) {
      n.x += n.vx + Math.sin(t*1.5+n.ph)*0.06; n.y += n.vy + Math.cos(t*1.2+n.ph*1.3)*0.04;
      if (n.x < -40) n.x = W+40; if (n.x > W+40) n.x = -40; if (n.y < -40) n.y = H+40; if (n.y > H+40) n.y = -40;
      const dx = n.x-mx, dy = n.y-my, md = Math.sqrt(dx*dx+dy*dy);
      if (md < 180 && md > 0) { const f = (1-md/180)*0.5; n.x += (dx/md)*f; n.y += (dy/md)*f; }
    }
    for (let i = 0; i < nodes.length; i++) for (let j = i+1; j < nodes.length; j++) {
      const dx = nodes[i].x-nodes[j].x, dy = nodes[i].y-nodes[j].y, d = Math.sqrt(dx*dx+dy*dy);
      if (d < CONNECT) {
        const a = (1-d/CONNECT);
        ctx.strokeStyle = `rgba(122,16,16,${a*0.06})`; ctx.lineWidth = 2.5; ctx.lineCap='round';
        ctx.beginPath(); ctx.moveTo(nodes[i].x,nodes[i].y); ctx.lineTo(nodes[j].x,nodes[j].y); ctx.stroke();
        ctx.strokeStyle = `rgba(255,31,61,${a*0.18})`; ctx.lineWidth = 0.6;
        ctx.beginPath(); ctx.moveTo(nodes[i].x,nodes[i].y); ctx.lineTo(nodes[j].x,nodes[j].y); ctx.stroke();
      }
    }
    if (mx > -100) for (const n of nodes) {
      const dx = n.x-mx, dy = n.y-my, d = Math.sqrt(dx*dx+dy*dy);
      if (d < 180) { const a = (1-d/180)*0.4; ctx.strokeStyle = `rgba(255,80,100,${a})`; ctx.lineWidth = 0.7; ctx.beginPath(); ctx.moveTo(mx,my); ctx.lineTo(n.x,n.y); ctx.stroke(); }
    }
    for (const fp of flow) {
      if (fp.a < 0 || fp.b < 0 || fp.a >= nodes.length || fp.b >= nodes.length) { pickEdge(fp); continue; }
      const na = nodes[fp.a], nb = nodes[fp.b]; if (!na || !nb) { pickEdge(fp); continue; }
      const edx = na.x-nb.x, edy = na.y-nb.y; if (Math.sqrt(edx*edx+edy*edy) > CONNECT*1.2) { pickEdge(fp); continue; }
      fp.t += fp.s;
      if (fp.t > 1) {
        fp.a = fp.b; let bj = -1, bd = CONNECT;
        for (let j = 0; j < nodes.length; j++) { if (j===fp.a) continue; const dx = nodes[fp.a].x-nodes[j].x, dy = nodes[fp.a].y-nodes[j].y; const d = Math.sqrt(dx*dx+dy*dy); if (d < bd && Math.random() < 0.5) { bd = d; bj = j; } }
        fp.b = bj >= 0 ? bj : Math.floor(Math.random()*nodes.length); fp.t = 0;
      }
      const x = na.x + (nb.x-na.x)*fp.t, y = na.y + (nb.y-na.y)*fp.t;
      ctx.fillStyle = `rgba(255,31,61,${fp.br*0.10})`; ctx.beginPath(); ctx.arc(x,y,fp.sz*3,0,Math.PI*2); ctx.fill();
      ctx.fillStyle = `rgba(255,160,170,${fp.br*0.55})`; ctx.beginPath(); ctx.arc(x,y,fp.sz,0,Math.PI*2); ctx.fill();
    }
    for (const n of nodes) {
      ctx.fillStyle = `rgba(255,31,61,${n.b*0.10})`; ctx.beginPath(); ctx.arc(n.x,n.y,n.size*2.5,0,Math.PI*2); ctx.fill();
      ctx.fillStyle = `rgba(255,150,160,${n.b})`; ctx.beginPath(); ctx.arc(n.x,n.y,n.size,0,Math.PI*2); ctx.fill();
    }
    requestAnimationFrame(draw);
  }
  addEventListener('resize', resize); resize(); requestAnimationFrame(draw);
})();

// Theme toggle. Persists to localStorage; the head pre-script applied any
// saved value before stylesheets rendered to avoid a flash.
(function(){
  const root = document.documentElement;
  const btn = document.getElementById('apt-theme-toggle');
  function syncIcon() {
    const t = root.getAttribute('data-theme') || 'dark';
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
      // Re-render any open charts (they cache colors at draw time).
      if (window.__aptRedrawCharts) window.__aptRedrawCharts();
    });
  }
})();
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
  const PCT_FIELDS = new Set([
    'revenue_growth_yoy', 'high52w_proximity', 'roe_ttm', 'fcf_yield',
    'eps_growth_yoy', 'change_pct', 'return_1m', 'return_12_2',
    'rel_strength_sp500', 'volume_trend',
    'gross_margin', 'operating_margin', 'gross_margin_trend',
    'revenue_acceleration', 'fcf_growth_yoy', 'accruals_ratio',
    'inst_ownership', 'insider_ownership',
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
      const d = new Date(iso + 'T12:00:00Z');
      if (isNaN(d.getTime())) return '—';
      return d.toLocaleDateString('en-US', { month:'short', day:'numeric', timeZone:'UTC' }).toUpperCase();
    } catch (e) { return '—'; }
  }
  function fmtDateMDY(iso) {
    if (!iso) return '—';
    try {
      const d = new Date(iso + 'T12:00:00Z');
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
        { label: '12-2 Month Return',   key: 'return_12_2',        type: 'pct', source: 'yfinance', method: 'Trailing 12-month return excluding the most recent month (classic Jegadeesh-Titman momentum).' },
        { label: '1-Month Return',      key: 'return_1m',          type: 'pct', source: 'yfinance', method: 'Last 30 calendar days price change.' },
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
  ];

  const SOURCE_LABEL = {
    yfinance: 'Yahoo Finance (yfinance)',
    edgar:    'SEC EDGAR XBRL',
    derived:  'Computed from Yahoo + reference price series',
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

  // Pre-compute per-sector mean and stddev for every scoring field at init.
  // ~12 sectors x ~20 fields = 240 stats objects, computed once. Fast.
  let PEER_STATS = {};
  function buildPeerStats() {
    const stats = {};
    const allFields = new Set();
    for (const g of Object.values(SCORE_GROUPS)) {
      for (const f of g.fields) allFields.add(f);
    }
    for (const s of ALL) {
      const sector = s.sector || 'Unknown';
      if (!stats[sector]) stats[sector] = {};
      for (const f of allFields) {
        const v = s[f];
        if (v == null || !isFinite(v)) continue;
        if (!stats[sector][f]) stats[sector][f] = { sum: 0, sumSq: 0, count: 0 };
        stats[sector][f].sum += v;
        stats[sector][f].sumSq += v * v;
        stats[sector][f].count++;
      }
    }
    for (const sector of Object.keys(stats)) {
      for (const f of Object.keys(stats[sector])) {
        const x = stats[sector][f];
        x.mean = x.sum / x.count;
        const variance = (x.sumSq / x.count) - (x.mean * x.mean);
        x.stddev = Math.sqrt(Math.max(0, variance));
      }
    }
    return stats;
  }

  function scoreDimension(s, groupKey) {
    const sector = s.sector || 'Unknown';
    const sectorStats = PEER_STATS[sector];
    if (!sectorStats) return null;
    const group = SCORE_GROUPS[groupKey];
    let sum = 0, count = 0;
    for (const f of group.fields) {
      const v = s[f];
      if (v == null || !isFinite(v)) continue;
      const stat = sectorStats[f];
      if (!stat || !stat.stddev || stat.stddev === 0 || stat.count < 5) continue;
      let z = (v - stat.mean) / stat.stddev;
      if (group.invert.includes(f)) z = -z;
      // Clamp to ±3 for display sanity (real outliers usually mean bad data)
      z = Math.max(-3, Math.min(3, z));
      sum += z;
      count++;
    }
    return count > 0 ? sum / count : null;
  }

  // Per-dimension weights for the composite. Range 0 to 2, default 1.0 (equal).
  // Mutated by the slider event handlers; render() reads on every paint.
  const weights = { Growth: 1, Value: 1, Momentum: 1, Quality: 1 };

  function computeComposite(s) {
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

    function buildSubcard(title, fitLabel, fitClass, chi, n, observed, expected, startDigit, scale) {
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
        +   ' <span class="bf-sub-meta">&chi;<sup>2</sup> ' + chi + '  &middot;  n=' + n.toLocaleString() + '</span>'
        + '</div>'
        + rows
      + '</div>';
    }

    const d1Card = buildSubcard(
      'First Digit',
      String(b.fit).toUpperCase() + ' FIT',
      'bf-fit-' + b.fit,
      b.chi_sq,
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
    return '<div class="stk-detail">' + scoreCard + '<div class="fp-grid">'+groups+'</div>' + metaPanel + chartCard + signalsRow + newsCard + benfordCard + '</div>';
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
    const rows = group.rows.map(r => {
      const asOf = fieldAsOf(s, r.source);
      const asOfTxt = asOf ? fmtDateMDY(asOf) : 'n/a';
      return '<tr>'
        + '<td class="fp-meta-label">'+escapeHtml(r.label)+'</td>'
        + '<td class="fp-meta-src" data-src="'+r.source+'">'+escapeHtml(SOURCE_LABEL[r.source] || r.source)+'</td>'
        + '<td class="fp-meta-asof">'+asOfTxt+'</td>'
        + '<td class="fp-meta-method">'+escapeHtml(r.method)+'</td>'
        + '</tr>';
    }).join('');
    return '<div class="fp-meta-card fp-meta-' + dimTitle.toLowerCase() + '">'
      + '<div class="fp-meta-card-h">'
      +   '<span class="fp-meta-card-eyebrow">Methodology</span>'
      +   '<span class="fp-meta-card-title">' + escapeHtml(dimTitle) + ' factors, sources and as-of dates</span>'
      +   '<button type="button" class="fp-meta-card-close" aria-label="Close">&times;</button>'
      + '</div>'
      + '<table class="fp-meta-table"><thead><tr>'
      +   '<th>Metric</th><th>Source</th><th>As of</th><th>Method</th>'
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
        + '<div class="nws-sent-cell"><span class="nws-sent-label">LM</span> <span class="nws-sent-val ' + sentClass(lm) + '">' + fmtSent(lm) + '</span></div>'
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
    let guard = 0, grew = false;
    while (shownCount < windowRows.length && guard++ < 60) {
      if (listEl.getBoundingClientRect().bottom > window.innerHeight + EXTEND_MARGIN) break;
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
      if (av == null && bv == null) return 0;
      if (av == null) return 1;
      if (bv == null) return -1;
      if (typeof av === 'string') return av.localeCompare(bv) * sortDir;
      return (av - bv) * sortDir;
    });
    renderHero(filtered);
    countEl.textContent = filtered.length === ALL.length
      ? String(ALL.length) + ' stocks'
      : String(filtered.length) + ' of ' + String(ALL.length) + ' stocks';
    if (filtered.length === 0) {
      listEl.innerHTML = '<div class="empty-state">No matches. Adjust filters or clear search.</div>';
      return;
    }
    windowRows = filtered;
    shownCount = 0;
    listEl.innerHTML = '';
    fillWindow();
    redrawExpanded();
    // The chart and the radar read the same filtered set, so they have to be
    // redrawn when it changes. Without this they kept whatever they were
    // showing when the view was last opened, which reads as the filters simply
    // not applying to them.
    tetraInvalidate();
    if (currentView === 'chart') drawChart();
    else if (currentView === 'radar') renderRadar();
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
  document.querySelectorAll('.stk-th[data-sort]').forEach(th => {
    th.addEventListener('click', () => {
      const k = th.dataset.sort;
      if (sortKey === k) {
        sortDir = -sortDir;
      } else {
        sortKey = k;
        // String columns: ascending. Numeric / score / date: descending (best/most recent first).
        sortDir = (k === 'ticker' || k === 'name' || k === 'sector') ? 1 : -1;
      }
      document.querySelectorAll('.stk-th').forEach(h => h.classList.remove('asc','desc'));
      th.classList.add(sortDir > 0 ? 'asc' : 'desc');
      render();
    });
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
        if (String(inp.value || '').trim() !== '') n++;
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
      if (PCT_FIELDS.has(f)) inp.value = (dataVal * 100).toString();
      else if (CAP_FIELDS.has(f)) {
        if (dataVal >= 1e9) inp.value = (dataVal / 1e9) + 'B';
        else if (dataVal >= 1e6) inp.value = (dataVal / 1e6) + 'M';
        else inp.value = String(dataVal);
      } else inp.value = String(dataVal);
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
  const RADAR_MAX = 3;
  const RADAR_COLORS = ['var(--apt-red)', '#2E6F8E', '#B4832B'];
  let radarTickers = [];

  function radarAdd(t) {
    if (!t || radarTickers.indexOf(t) !== -1) return false;
    if (radarTickers.length >= RADAR_MAX) radarTickers.shift();
    radarTickers.push(t);
    return true;
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
    const host = document.getElementById('stk-radar-chips');
    if (!host) return;
    host.addEventListener('click', function(e) {
      const btn = e.target.closest('.stk-cmp-x');
      if (!btn) return;
      const t = btn.dataset.drop;
      radarTickers = radarTickers.filter(function(x) { return x !== t; });
      renderRadar();
    });
  })();

  function renderRadar() {
    const side = document.getElementById('stk-radar-breakdown');
    const title = document.getElementById('stk-radar-title');
    const hint = document.getElementById('stk-radar-hint');
    const svg = document.getElementById('stk-radar-svg');
    if (!side) return;
    const picks = radarTickers
      .map(function(t) { return ALL.find(function(x) { return x.ticker === t; }); })
      .filter(Boolean);
    const chipsEl = document.getElementById('stk-radar-chips');

    if (!picks.length) {
      if (svg) svg.innerHTML = '';
      if (chipsEl) chipsEl.innerHTML = '';
      if (title) title.textContent = 'Pick a company';
      if (hint) hint.textContent = 'Search above and pick a company to plot it. Add up to ' + RADAR_MAX +
        ' to compare them on the same rings.';
      side.innerHTML = '';
      return;
    }

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
    }).join('');
  }

  // -- Chart ---------------------------------------------------------------
  // Canvas, not SVG: an unfiltered view is 5,336 points, and that many DOM
  // nodes costs far more than it buys when each one is a 3px dot.
  const LENSES = [
    { key:'gvmq3d', label:'GVMQ 3D', tetra:true,
      blurb:'All four factors at once. Each corner of the tetrahedron is one factor, and a company sits in the direction of the ones it scores on, so two profiles that look identical on a flat chart separate here. Drag to turn it.' },
    { key:'gvmq', label:'GVMQ', compass:true,
      tl:'Momentum', tr:'Growth', bl:'Value', br:'Quality',
      blurb:'Each corner is one factor. A company sits in the direction of the factors it scores best on, and the further from the centre, the more lopsided the profile.' },
    { key:'compounders', label:'Compounders', x:'q', y:'g',
      tl:'', tr:'High growth + high quality', bl:'Weak on both', br:'',
      xl:'Quality', yl:'Growth',
      blurb:'Quality against growth. Top right is the compounder quadrant: earning well and still growing.' },
    { key:'valmo', label:'Value / momentum', x:'v', y:'m',
      tl:'', tr:'Cheap and moving', bl:'Expensive and falling', br:'',
      xl:'Value', yl:'Momentum',
      blurb:'Cheapness against price momentum. Top right is cheap and already re-rating; bottom left is the value trap corner.' },
    { key:'neglect', label:'Neglect', x:'neglect_score', y:'q', raw:true,
      xl:'Neglect', yl:'Quality',
      blurb:'Under-followed names that still score on quality. Further right means less analyst, institutional and news coverage.' },
  ];
  let lens = LENSES[0];

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
  let yaw = 0.62, pitch = -0.32, spinRAF = null;
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
    if (spinRAF || !spin || !lens.tetra || currentView !== 'chart') return;
    spinRAF = requestAnimationFrame(function step() {
      spinRAF = null;
      if (!spin || !lens.tetra || currentView !== 'chart') return;
      yaw += 0.0030;
      drawChart();
      spinRAF = requestAnimationFrame(step);
    });
  }
  function stopSpin() {
    if (spinRAF) { cancelAnimationFrame(spinRAF); spinRAF = null; }
  }

  function chartPoints() {
    const out = [];
    for (const s of currentFiltered()) {
      let x, y;
      if (lens.tetra) {
        if (!s.scorable) continue;
        out.push({ s: s, p: tetraPos(s) });
        continue;
      }
      if (lens.compass) {
        // Vector sum of the four dimension scores, one per corner. A balanced
        // company lands near the middle; a lopsided one is flung toward whatever
        // it is strongest on.
        if (!s.scorable) continue;
        const g = s.g || 0, v = s.v || 0, m = s.m || 0, q = s.q || 0;
        x = (g + q) - (v + m);
        y = (g + m) - (v + q);
      } else if (lens.raw) {
        x = s[lens.x]; y = s[lens.y];
      } else {
        x = s[lens.x]; y = s[lens.y];
      }
      if (x == null || y == null || !isFinite(x) || !isFinite(y)) continue;
      out.push({ s: s, x: x, y: y });
    }
    return out;
  }

  let chartHit = [];

  function drawTetra(cv, pts, ref) {
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
    const foot = document.getElementById('stk-chart-foot');
    if (!pts.length) {
      ctx.fillStyle = dim;
      ctx.font = "12px 'Space Mono', monospace";
      ctx.fillText('No companies are scored on enough dimensions to place.', 46, h / 2);
      chartHit = [];
      if (foot) foot.textContent = '0 plotted';
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
    chartHit = [];
    for (const q of proj) {
      const cap = q.s.market_cap || 0;
      const r = (2 + Math.sqrt(cap / capMax) * 9) * q.k;
      // Near dots are more opaque. Depth is the only thing separating an
      // overlapping pair, so it has to be visible without being loud.
      const t = Math.max(0, Math.min(1, (q.z + vertMax) / (vertMax * 2)));
      ctx.beginPath(); ctx.arc(q.x, q.y, Math.max(r, 1), 0, Math.PI * 2);
      ctx.fillStyle = ink; ctx.globalAlpha = 0.14 + t * 0.30; ctx.fill();
      chartHit.push({ x: q.x, y: q.y, r: Math.max(r, 4), s: q.s });
    }
    ctx.globalAlpha = 1;

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

    if (foot) {
      foot.textContent = pts.length.toLocaleString() + ' plotted of ' +
        tetraMatch.toLocaleString() + ' matching \u00b7 ' +
        'only companies scored on 3 of 4 dimensions can be placed \u00b7 ' +
        'dot size: market cap \u00b7 drag to turn';
    }
    const plot = cv.parentElement;
    if (plot) {
      ['tl','tr','bl','br'].forEach(function(c) {
        const el = plot.querySelector('.ax.' + c);
        if (el) el.textContent = '';
      });
    }
  }

  function drawChart() {
    const cv = document.getElementById('stk-chart-canvas');
    if (!cv) return;
    if (lens.tetra) {
      if (tetraPts === null) {
        tetraPts = chartPoints();
        tetraMatch = currentFiltered().length;
        const ds = tetraPts.map(function(p) {
          return Math.hypot(p.p[0], p.p[1], p.p[2]);
        }).sort(function(a, b) { return a - b; });
        // The 97th percentile rather than the maximum. A handful of extreme
        // profiles reach nearly twice the next-widest, and scaling to them
        // shrinks the entire cloud to a smudge in the middle of the frame. The
        // few that fall outside the cage are the point of the cage.
        const p97 = ds.length ? ds[Math.min(ds.length - 1, Math.floor(ds.length * 0.97))] : 1;
        tetraRef = Math.max(p97, Math.sqrt(3));
      }
      drawTetra(cv, tetraPts, tetraRef);
      return;
    }
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
    const pad = 46;
    if (!pts.length) {
      ctx.fillStyle = css.getPropertyValue('--text-4').trim() || '#888';
      ctx.font = "12px 'Space Mono', monospace";
      ctx.fillText('No companies have both inputs for this lens.', pad, h / 2);
      chartHit = [];
      document.getElementById('stk-chart-foot').textContent = '0 plotted';
      return;
    }
    let xs = pts.map(p => p.x), ys = pts.map(p => p.y);
    let x0 = Math.min.apply(null, xs), x1 = Math.max.apply(null, xs);
    let y0 = Math.min.apply(null, ys), y1 = Math.max.apply(null, ys);
    if (lens.compass) { const m = Math.max(Math.abs(x0),Math.abs(x1),Math.abs(y0),Math.abs(y1)) || 1;
                        x0 = -m; x1 = m; y0 = -m; y1 = m; }
    if (x1 === x0) { x1 = x0 + 1; } if (y1 === y0) { y1 = y0 + 1; }
    const sx = v => pad + (v - x0) / (x1 - x0) * (w - pad * 2);
    const sy = v => h - pad - (v - y0) / (y1 - y0) * (h - pad * 2);
    // Quadrant crosshair through the origin, or the midpoint for raw lenses.
    const cx = lens.compass ? sx(0) : sx((x0 + x1) / 2);
    const cy = lens.compass ? sy(0) : sy((y0 + y1) / 2);
    ctx.strokeStyle = line; ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(cx, pad*0.4); ctx.lineTo(cx, h-pad*0.4);
    ctx.moveTo(pad*0.4, cy); ctx.lineTo(w-pad*0.4, cy); ctx.stroke();
    // Dot area carries market cap, so the eye weights big companies more.
    const caps = pts.map(p => p.s.market_cap || 0);
    const capMax = Math.max.apply(null, caps) || 1;
    chartHit = [];
    for (const p of pts) {
      const px = sx(p.x), py = sy(p.y);
      const cap = p.s.market_cap || 0;
      const r = 2 + Math.sqrt(cap / capMax) * 9;
      ctx.beginPath(); ctx.arc(px, py, r, 0, Math.PI * 2);
      ctx.fillStyle = ink; ctx.globalAlpha = 0.30; ctx.fill();
      ctx.globalAlpha = 1;
      chartHit.push({ x: px, y: py, r: Math.max(r, 4), s: p.s });
    }
    document.getElementById('stk-chart-foot').textContent =
      pts.length.toLocaleString() + ' plotted of ' + currentFiltered().length.toLocaleString() +
      ' matching' + (lens.compass ? ' \u00b7 only companies scored on 3 of 4 dimensions can be placed' : '') +
      ' \u00b7 dot size: market cap';
    const q = lens.compass
      ? { tl: lens.tl, tr: lens.tr, bl: lens.bl, br: lens.br }
      : { tl: '', tr: (lens.yl || '') + ' high', bl: (lens.xl || '') + ' low', br: (lens.xl || '') + ' high' };
    const plot = cv.parentElement;
    plot.querySelector('.tl').textContent = q.tl || '';
    plot.querySelector('.tr').textContent = q.tr || '';
    plot.querySelector('.bl').textContent = q.bl || '';
    plot.querySelector('.br').textContent = q.br || '';
  }

  function renderLenses() {
    const el = document.getElementById('stk-lenses');
    if (!el) return;
    el.innerHTML = '<span class="lib-chip-label">View</span>' + LENSES.map(function(l) {
      return '<button type="button" class="stk-lens' + (l.key === lens.key ? ' active' : '') +
             '" data-lens="' + l.key + '">' + escapeHtml(l.label) + '</button>';
    }).join('') + (lens.tetra && !REDUCED_MOTION
      ? '<button type="button" class="stk-lens stk-spin' + (spin ? ' active' : '') +
        '" id="stk-spin">Spin</button>'
      : '');
    const spinBtn = el.querySelector('#stk-spin');
    if (spinBtn) {
      spinBtn.addEventListener('click', function() {
        spin = !spin;
        spinBtn.classList.toggle('active', spin);
        if (spin) startSpin(); else stopSpin();
      });
    }
    el.querySelectorAll('.stk-lens').forEach(function(b) {
      b.addEventListener('click', function() {
        lens = LENSES.find(function(l) { return l.key === b.dataset.lens; }) || LENSES[0];
        renderLenses();
        document.getElementById('stk-chart-blurb').textContent = lens.blurb || '';
        const cv = document.getElementById('stk-chart-canvas');
        if (cv) cv.style.cursor = lens.tetra ? 'grab' : '';
        tetraInvalidate();
        stopSpin();
        drawChart();
        startSpin();
      });
    });
    document.getElementById('stk-chart-blurb').textContent = lens.blurb || '';
  }

  (function wireTetraDrag() {
    const cv = document.getElementById('stk-chart-canvas');
    if (!cv) return;
    let dragging = false, lastX = 0, lastY = 0, moved = 0;
    cv.addEventListener('pointerdown', function(e) {
      if (!lens.tetra) return;
      dragging = true; moved = 0;
      lastX = e.clientX; lastY = e.clientY;
      stopSpin();
      cv.setPointerCapture(e.pointerId);
      cv.style.cursor = 'grabbing';
    });
    cv.addEventListener('pointermove', function(e) {
      if (!dragging) return;
      const dx = e.clientX - lastX, dy = e.clientY - lastY;
      lastX = e.clientX; lastY = e.clientY;
      moved += Math.abs(dx) + Math.abs(dy);
      yaw += dx * 0.008;
      // Clamped so the cloud can never be turned past vertical, where the
      // vertex labels would invert and the shape stops being readable.
      pitch = Math.max(-1.35, Math.min(1.35, pitch + dy * 0.008));
      drawChart();
    });
    function release(e) {
      if (!dragging) return;
      dragging = false;
      cv.style.cursor = lens.tetra ? 'grab' : '';
      try { cv.releasePointerCapture(e.pointerId); } catch (err) {}
      // A deliberate turn parks the shape where the reader put it. Spin, if it
      // was on, stays off until they ask for it again.
      if (moved >= 4) {
        spin = false;
        const b = document.getElementById('stk-spin');
        if (b) b.classList.remove('active');
      } else if (spin) { startSpin(); }
    }
    cv.addEventListener('pointerup', release);
    cv.addEventListener('pointercancel', release);
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
    if (v === 'chart') { renderLenses(); drawChart(); startSpin(); }
    else stopSpin();
    document.querySelectorAll('.stk-view-btn').forEach(function(b) {
      b.classList.toggle('active', b.dataset.view === v);
    });
    if (v === 'radar') renderRadar();
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
    b.addEventListener('click', function() {
      const key = b.dataset.sortby;
      // Clicking the active control flips direction, matching the column headers.
      if (sortKey === key) { sortDir = -sortDir; }
      else { sortKey = key; sortDir = (key === 'ticker') ? 1 : -1; }
      document.querySelectorAll('.stk-sort').forEach(function(x) {
        x.classList.toggle('active', x.dataset.sortby === key);
      });
      render();
    });
  });

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
    if (radarAdd(ticker)) setView('radar');
    else renderRadar();
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
    PEER_STATS = buildPeerStats();
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


def render_topnav(active=""):
    """Topnav with real-URL nav links. active is one of: 'home', 'today', 'stories', 'stocks'."""
    logo = apt_logo_svg(22, 29, 0.45)
    def cls(name):
        return ' class="active"' if active == name else ''
    return f'''<nav class="topnav">
  <a class="lockup" href="./index.html" title="Apterreon home">
    {logo}
    <div class="lockup-text">
      <span class="brand">Apterreon</span>
      <span class="lockup-tagline">Explore what's out there.</span>
    </div>
    <div class="pulse-row"><span class="pulse-dot"></span><span>Live</span></div>
  </a>
  <div class="nav">
    <a href="./index.html"{cls('home')}>Home</a>
    <a href="./today.html"{cls('today')}>Today</a>
    <a href="./stories.html"{cls('stories')}>Stories</a>
    <a href="./stocks.html"{cls('stocks')}>Stocks</a>
    <button type="button" id="apt-theme-toggle" class="theme-toggle" aria-label="Toggle light/dark theme" title="Toggle light/dark"><span class="theme-toggle-icon">&#9788;</span></button>
  </div>
</nav>'''


def render_footer():
    return '''<footer class="footer">
  <div style="display:flex;flex-direction:column;gap:6px">
    <span class="brand-foot">Apterreon</span>
    <span style="font-family:'Space Mono',monospace;font-size:11px;letter-spacing:1.5px;color:var(--apt-rose)">Explore what's out there.</span>
  </div>
  <span class="meta">Daily Intelligence Brief, generated by Apterreon, hosted on GitHub Pages</span>
</footer>'''


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
<body class="page-{active_nav or 'home'}">

<canvas id="plexus" aria-hidden="true"></canvas>

{topnav}

{body_html}

{footer}

<script>
{PLEXUS_JS}
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
    if nt_rows:
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
    "returnOnEquity", "totalDebt", "totalCash", "ebitda", "netIncomeToCommon",
    "operatingCashflow", "totalAssets", "operatingMargins", "grossMargins",
    "numberOfAnalystOpinions", "heldPercentInstitutions", "heldPercentInsiders",
    "earningsTimestamp", "earningsTimestampStart", "earningsCallTimestampStart",
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
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = [ex.submit(fetch_one, s) for s in tickers]
        for f in as_completed(futures):
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
                if chg is not None:
                    pct = chg if abs(chg) > 1 else chg * 100
                    if abs(pct) <= 20:
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
                if ev_eb is not None and abs(ev_eb) < 200:
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
                ma50 = info.get("fiftyDayAverage")
                if price and ma50 and ma50 > 0:
                    ret_1m = (price - ma50) / ma50
                    if abs(ret_1m) < 2:
                        s["return_1m"] = ret_1m
                chg52 = info.get("52WeekChange")
                if chg52 is not None and abs(chg52) < 10:
                    s["return_52w"] = chg52
                    if "return_1m" in s:
                        s["return_12_2"] = chg52 - s["return_1m"]
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
                if ebitda is not None and ebitda != 0:
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

                # Earnings date (next expected). yfinance exposes this under several keys
                # depending on data availability: earningsTimestamp (single), or a list at
                # earningsDate, or a range start/end. Take the first valid one.
                ed_iso = None
                for ts_key in ("earningsTimestamp", "earningsTimestampStart", "earningsCallTimestampStart"):
                    ts = info.get(ts_key)
                    if ts and isinstance(ts, (int, float)) and ts > 0:
                        try:
                            ed = datetime.fromtimestamp(ts, tz=timezone.utc).date()
                            delta = (ed - datetime.now(tz=timezone.utc).date()).days
                            # Sanity: within ~2 years past or future
                            if -730 < delta < 730:
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


def compute_lm_score(text):
    """Loughran-McDonald financial sentiment polarity. Returns float in [-1, +1]:
    (positive_count - negative_count) / (positive_count + negative_count). 0 if
    no LM words found."""
    if not text:
        return 0.0
    words = re.findall(r"[a-z]+", text.lower())
    pos = sum(1 for w in words if w in LM_POSITIVE)
    neg = sum(1 for w in words if w in LM_NEGATIVE)
    total = pos + neg
    if total == 0:
        return 0.0
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


def fetch_company_news(ticker, name="", max_items=15):
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
    if clean_name and clean_name.upper() != ticker and len(clean_name) > 2:
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
    for item in root.findall(".//item")[:max_items * 2]:
        title = (item.findtext("title") or "").strip()
        pub_date = (item.findtext("pubDate") or "").strip()
        link = (item.findtext("link") or "").strip()
        source = (item.findtext("source") or "").strip()
        if not title or not link:
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
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = [ex.submit(process, s) for s in todo]
        for f in as_completed(futures):
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
    return fetched


def compute_neglect_score(stocks):
    """Peter Lynch neglect signal: under-followed names tend to have asymmetric
    upside when something good happens because Wall Street isn't watching. Composite
    of three normalized 0-to-1 components, each scoring "more neglected" higher:
      - analyst coverage:  1 - min(analyst_count, 30) / 30
      - institutional %:   1 - min(inst_ownership, 0.50) / 0.50
      - news mentions 7d:  1 - min(news_count_7d, 20) / 20
    Score in [0, 1]; >0.7 is genuinely off-the-radar; <0.3 is heavily covered.
    Skips a component when its input is missing rather than penalizing it."""
    scored = 0
    for s in stocks:
        parts = []
        if isinstance(s.get("analyst_count"), (int, float)):
            parts.append(1 - min(s["analyst_count"], 30) / 30)
        if isinstance(s.get("inst_ownership"), (int, float)):
            parts.append(1 - min(s["inst_ownership"], 0.50) / 0.50)
        if isinstance(s.get("news_count_7d"), (int, float)):
            parts.append(1 - min(s["news_count_7d"], 20) / 20)
        if parts:
            s["neglect_score"] = sum(parts) / len(parts)
            scored += 1
    print(f"neglect_score: computed for {scored} tickers.")
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
        lm_vals = [i["lm"] for i in recent if isinstance(i.get("lm"), (int, float))]
        vd_vals = [i["vader"] for i in recent if isinstance(i.get("vader"), (int, float))]
        if lm_vals:
            s["news_lm_avg"] = sum(lm_vals) / len(lm_vals)
        if vd_vals:
            s["news_vader_avg"] = sum(vd_vals) / len(vd_vals)
        s["news_count_7d"] = len(recent)
        if lm_vals or vd_vals:
            stamped += 1
    print(f"news_sentiment: aggregated for {stamped} tickers.")
    return stamped


# ── Per-ticker price history (yfinance bulk download, 1y daily) ─────────────

PRICES_DIR = DOCS_DIR / "prices"


def enrich_with_prices(stocks, max_age_hours=24, batch_size=200):
    """Fetch ~1y daily closes per ticker via yf.download bulk endpoint and write
    docs/prices/{TICKER}.json. The bulk endpoint is dramatically faster than
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

    def needs_fetch(ticker):
        f = PRICES_DIR / _news_filename(ticker)
        if not f.exists():
            return True
        # Freshness comes from the "updated" field the writer stores in the file,
        # not from the mtime, which CI resets on every checkout.
        try:
            age = _age_hours_from_iso(json.loads(f.read_text(encoding="utf-8")).get("updated"))
        except Exception:
            return True
        return age is None or age > max_age_hours

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
                closes = []
                for idx, val in series.dropna().items():
                    try:
                        date_str = idx.strftime("%Y-%m-%d")
                        v = float(val)
                        if v > 0 and v < 1e6:
                            closes.append([date_str, round(v, 4)])
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
}


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


def _extract_quarterly_series(facts, concept_keys, max_periods=12):
    """Extract quarterly values for the first matching concept name.
    Filters to ~90-day periods from 10-Qs (avoids YTD/comparable overlaps).
    Returns list of {end, val} sorted most-recent-first, dedup'd by end date
    (most recent filing wins)."""
    from datetime import date as _date
    us_gaap = facts.get("us-gaap", {})
    for concept in concept_keys:
        if concept not in us_gaap:
            continue
        units_dict = us_gaap[concept].get("units", {})
        records = units_dict.get("USD") or units_dict.get("USD/shares") or []
        if not records:
            continue
        clean = []
        for r in records:
            if r.get("form") not in ("10-Q", "10-Q/A"):
                continue
            start, end = r.get("start", ""), r.get("end", "")
            if not start or not end:
                continue
            try:
                period_days = (_date.fromisoformat(end) - _date.fromisoformat(start)).days
                if 60 <= period_days <= 100:
                    clean.append({"end": end, "val": r.get("val"), "filed": r.get("filed", "")})
            except Exception:
                continue
        # Dedup by end date, keep most recent filing
        by_end = {}
        for r in clean:
            existing = by_end.get(r["end"])
            if not existing or r["filed"] > existing["filed"]:
                by_end[r["end"]] = r
        sorted_periods = sorted(by_end.values(), key=lambda x: x["end"], reverse=True)
        if sorted_periods:
            return sorted_periods[:max_periods]
    return []


def _extract_instant_series(facts, concept_keys, max_periods=12):
    """Extract point-in-time (balance-sheet) values for the first matching concept.

    Balance-sheet facts are instants, not durations: they carry an `end` and no
    `start`, so _extract_quarterly_series drops every one of them at its
    `if not start or not end` guard. That is exactly why accruals_ratio sat at 0%
    coverage across the whole universe. Returns [{end, val}] most-recent-first."""
    us_gaap = facts.get("us-gaap", {})
    for concept in concept_keys:
        if concept not in us_gaap:
            continue
        records = us_gaap[concept].get("units", {}).get("USD") or []
        if not records:
            continue
        by_end = {}
        for r in records:
            if r.get("form") not in ("10-Q", "10-Q/A", "10-K", "10-K/A"):
                continue
            end, val = r.get("end", ""), r.get("val")
            if not end or val is None:
                continue
            existing = by_end.get(end)
            if not existing or r.get("filed", "") > existing.get("filed", ""):
                by_end[end] = {"end": end, "val": val, "filed": r.get("filed", "")}
        periods = sorted(by_end.values(), key=lambda x: x["end"], reverse=True)
        if periods:
            return periods[:max_periods]
    return []


def compute_benford(facts):
    """Compute Benford's law fit across all USD-denominated XBRL values for a company.

    Returns dict with both first-digit and second-digit distributions:
      observed (1st-digit, 9 values), chi_sq, n, fit
      observed_d2 (2nd-digit, 10 values), chi_sq_d2, n_d2, fit_d2 (when n_d2 >= 30)

    Critical chi-squared values for the fit verdict:
      df=8 (1st digit): 13.36 (p=.10), 15.51 (p=.05), 20.09 (p=.01)
      df=9 (2nd digit): 14.68 (p=.10), 16.92 (p=.05), 21.67 (p=.01)

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
    chi_sq_d1 = sum((observed_d1[i] - expected_d1[i]) ** 2 / expected_d1[i] for i in range(9))
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
        chi_sq_d2 = sum(
            (observed_d2[i] - expected_d2[i]) ** 2 / expected_d2[i] for i in range(10)
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

    out = {}

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
_INSIDER_MAX_DOCS_PER_TICKER = 8   # cap per-ticker fetches to keep workflow under 30 min
_INSIDER_TOP_N_BY_MARKET_CAP = 600  # only fetch insider data for the largest N tickers
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
    for i, form in enumerate(forms):
        if form != "4":
            continue
        if i >= len(accessions) or i >= len(dates) or i >= len(primary_docs):
            continue
        if dates[i] < cutoff:
            continue
        out.append({
            "accession": accessions[i],
            "filing_date": dates[i],
            "primary_doc": primary_docs[i],
        })
        if len(out) >= _INSIDER_MAX_DOCS_PER_TICKER:
            break
    return out


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
    """Top-level per-ticker Form 4 fetch. Returns list of normalized transactions
    over the last _INSIDER_LOOKBACK_DAYS, capped to _INSIDER_MAX_DOCS_PER_TICKER
    most recent filings. Empty list when the company has no Form 4 activity.

    Raises if the SEC fetch failed, so the caller counts it as an error rather
    than as an absence of insider trading."""
    filings = _fetch_recent_form4_filings(cik)
    if filings is None:
        raise IOError(f"SEC submissions fetch failed for CIK {cik}")
    if not filings:
        return []
    txs = []
    for f in filings:
        txs.extend(_parse_form4_xml(cik, f["accession"], f["primary_doc"]))
    return txs


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
            txs = fetch_insider_form4(cik)
            return sym, compute_insider_signal(txs), len(txs), None
        except Exception as e:
            return sym, {}, 0, str(e)

    enriched = 0
    no_activity = 0
    errors = 0
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = [ex.submit(process, item) for item in matched]
        for f in as_completed(futures):
            sym, signal, n_tx, err = f.result()
            s = by_ticker.get(sym)
            if not s:
                continue
            # Always stamp insider_updated even when signal is empty, so we know
            # we tried this ticker (avoids re-fetching on every workflow run).
            s["insider_updated"] = today_str
            if err:
                errors += 1
            elif signal:
                s.update(signal)
                enriched += 1
            else:
                no_activity += 1
    elapsed = time.time() - t0
    print(f"Insider Form 4 enrichment: signals for {enriched}/{len(matched)} tickers "
          f"({no_activity} no activity, {errors} errors) in {elapsed:.1f}s.")
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

    total_matched = len(matched)
    matched = [(t, cik) for t, cik in matched if not is_fresh(t)]
    if total_matched != len(matched):
        print(f"EDGAR: {total_matched - len(matched)} tickers already stamped this week, "
              f"{len(matched)} to fetch.")

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
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = [ex.submit(process, item) for item in matched]
        for f in as_completed(futures):
            sym, factors = f.result()
            if not factors:
                continue
            s = by_ticker.get(sym)
            if s:
                s.update(factors)
                s["edgar_updated"] = today_str
                enriched += 1
    elapsed = time.time() - t0
    print(f"EDGAR enrichment: enriched {enriched}/{len(matched)} matched tickers ({len(by_ticker) - len(matched)} no CIK match) in {elapsed:.1f}s.")
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
INVERTED_FIELDS = {f for g in SCORE_GROUPS_PY.values() for f in g["invert"]}

# A percentile needs a cohort large enough to mean something. With 20 peers the
# finest distinction expressible is 5 points; below that a "73rd percentile"
# claims precision the sample cannot support.
MIN_COHORT_FOR_PERCENTILE = 20
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
        for f in SCORE_FIELDS:
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
                if st["sd"] and st["n"] >= 5:
                    z = (v - st["mean"]) / st["sd"]
                    if f in group["invert"]:
                        z = -z
                    zs.append(max(-3.0, min(3.0, z)))
            dim_scores[dim] = round(sum(zs) / len(zs), 4) if len(zs) >= MIN_FIELDS_PER_DIMENSION else None

        s["g"] = dim_scores["Growth"]
        s["v"] = dim_scores["Value"]
        s["m"] = dim_scores["Momentum"]
        s["q"] = dim_scores["Quality"]
        present = sum(1 for d in dim_scores.values() if d is not None)
        s["dims_present"] = present
        # Positional, in SCORE_FIELDS order, not a dict. Twenty full field names
        # repeated across 5,336 stocks cost 0.72 MB in key strings alone, which
        # was 80% of what this map weighed in the payload.
        s["pct"] = [pct.get(f) for f in SCORE_FIELDS]
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
        "last_updated", "earnings_date",
    )
    carried_forward = 0
    if last_known and last_known.get("stocks"):
        prev_by_ticker = {s["ticker"]: s for s in last_known["stocks"]}
        for s in stocks:
            prev = prev_by_ticker.get(s["ticker"])
            if not prev:
                continue
            for field in CARRY_FIELDS:
                if prev.get(field) is not None and s.get(field) is None:
                    s[field] = prev[field]
            if prev.get("market_cap"):
                carried_forward += 1
    if carried_forward:
        print(f"stocks_universe: carried forward enrichment for {carried_forward} tickers from previous cache.")

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

    # Peer scoring last: it reads every factor the steps above populate.
    compute_peer_scores(stocks)

    # Per-ticker daily price history (1y) for the chart card. 24h cache, bulk
    # download via yf.download in batches so we hit Yahoo once per ~200 tickers.
    enrich_with_prices(stocks)

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
  <div class="eyebrow"><span class="live-dot"></span>{eyebrow_text}</div>
  <h1 class="hero-title">Regular Briefs and Curated&nbsp;Stories</h1>
  <p class="hero-sub">Finance, Politics, Tech, and more. Apterreon's three-times-daily intelligence brief, plus a running story library and a filterable universe of US-listed stocks.</p>
  <div class="hero-actions">
    <a class="btn-primary" href="./today.html">Read today's briefs <span style="font-size:16px">&rarr;</span></a>
    <a class="btn-secondary" href="./stories.html">Browse stories <span style="font-size:16px">&rarr;</span></a>
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
        {"label": "Market Cap",            "key": "market_cap",         "type": "cap",   "placeholder_min": "min (e.g. 300M)", "placeholder_max": "max (e.g. 10B)", "tier_chips": True},
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
    <input class="stk-filter-input" data-filter="{row["key"]}" data-bound="min" placeholder="min" style="width:100%;min-width:0;background:transparent;border:none;border-bottom:1px solid var(--border-bright);outline:none;font-family:\'Space Mono\',monospace;font-size:11px;color:var(--text-1);padding:2px 0">
    <span style="color:var(--text-4);font-size:10px">to</span>
    <input class="stk-filter-input" data-filter="{row["key"]}" data-bound="max" placeholder="max" style="width:100%;min-width:0;background:transparent;border:none;border-bottom:1px solid var(--border-bright);outline:none;font-family:\'Space Mono\',monospace;font-size:11px;color:var(--text-1);padding:2px 0">
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
  <div class="scr-top">
    <div class="scr-brand">
      <span class="scr-mark">Apterreon</span>
      <span class="scr-sub">The Screen</span>
    </div>
    <div class="scr-nav">
      <a href="./index.html">Home</a>
      <a href="./today.html">Today</a>
      <a href="./stories.html">Stories</a>
      <a href="./stocks.html" class="on">Stocks</a>
      <button type="button" id="apt-theme-toggle" class="theme-toggle" aria-label="Toggle light/dark theme" title="Toggle light/dark"><span class="theme-toggle-icon">&#9788;</span></button>
    </div>
  </div>

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
        <span class="scr-count" id="stk-count"></span>
      </div>

      <div class="scr-pane">
        <div id="stk-chart" class="stk-chart" hidden>
          <div class="stk-lenses" id="stk-lenses"></div>
          <p class="stk-chart-blurb" id="stk-chart-blurb"></p>
          <div class="stk-chart-plot">
            <span class="ax tl"></span><span class="ax tr"></span>
            <span class="ax bl"></span><span class="ax br"></span>
            <canvas id="stk-chart-canvas"></canvas>
            <div class="stk-chart-tip" id="stk-chart-tip" hidden></div>
          </div>
          <p class="stk-chart-foot" id="stk-chart-foot"></p>
        </div>

        <div id="stk-radar" class="stk-radar" hidden>
          <div class="stk-radar-plot">
            <div class="stk-radar-quads">
              <span class="q tl">Momentum</span><span class="q tr">Growth</span>
              <span class="q bl">Value</span><span class="q br">Quality</span>
            </div>
            <svg id="stk-radar-svg" viewBox="0 0 620 620" role="img" aria-label="Factor radar"></svg>
          </div>
          <p class="stk-radar-note" id="stk-radar-note"></p>
          <aside class="stk-radar-side">
            <h3 id="stk-radar-title">Pick a company</h3>
            <p class="stk-radar-hint" id="stk-radar-hint">Name a ticker above to plot it. Add up to three to compare them on the same rings.</p>
            <div class="stk-cmps" id="stk-radar-chips"></div>
            <div id="stk-radar-breakdown"></div>
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

      <div class="scr-foot">
        <span>Apterreon &middot; Explore what&rsquo;s out there.</span>
        <span>{meta_line}</span>
      </div>
    </main>
  </div>
</div>
"""
    stocks_js = (STOCKS_JS_TEMPLATE
                 .replace("__DATA_URL__", stocks_json)
                 .replace("__PCT_FIELDS_JSON__", json.dumps(SCORE_FIELDS))
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


def generate_site(briefs):
    """Orchestrator. Generates the full multi-page static site under docs/.
    Triggers Recent Trends (daily-cached Claude call) and Stocks Universe (weekly,
    Wikipedia scrape + optional FMP enrichment)."""
    recent_trends = get_or_generate_recent_trends(briefs)
    universe = get_or_generate_stocks_universe()

    generate_home(briefs, recent_trends)
    generate_today(briefs)
    generate_stories(briefs)
    generate_stocks_page(universe)
    write_manifest()
    print("Wrote docs/index.html, today.html, stories.html, stocks.html, manifest.json.")


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
    #   record  cheap, runs hourly. Quotes + headlines appended to the CSVs.
    #   daily   record, plus the fundamentals panel, the snapshot page and the
    #           site rebuild. Runs once a day.
    # The split exists because docs/index.html is ~325KB and is rewritten in full
    # on every site rebuild; doing that hourly would bloat the repo for nothing,
    # while appending a few CSV rows hourly costs almost nothing.
    mode = event.get("mode") or event.get("brief_type") or "daily"
    if mode in ("morning", "midday", "evening"):
        mode = "daily"          # legacy edition names still dispatch a daily run
    if mode not in ("record", "daily"):
        return {"status": "error", "error": f"unknown mode {mode!r}"}

    now_et = datetime.now(EASTERN)
    observed_at = now_et.isoformat(timespec="seconds")
    date_iso = now_et.strftime("%Y-%m-%d")
    date_str = now_et.strftime("%A, %B %d")
    timestamp = now_et.strftime("%I:%M %p ET")

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
        return {"status": "recorded", "mode": mode, "quotes": n_quotes,
                "headlines_new": n_heads, "headlines_seen": len(headlines)}

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
    if now_et.weekday() >= 5:
        print(f"fundamentals: {date_iso} is a weekend, no trading day to record.")
    elif (universe or {}).get("stale"):
        # The universe fell back to cache, either because every source returned
        # nothing or because the partial-scrape ABORT fired. Its prices are the
        # prior session's. Writing them under today's date would append a
        # permanent flat day that record_fundamentals can never correct. Today
        # having no panel row is recoverable; today having a wrong one is not.
        print(f"fundamentals: universe is a cached fallback, not a live scrape for "
              f"{date_iso}; skipping the panel rather than recording stale prices "
              f"under today's date.")
    else:
        record_fundamentals(stocks, date_iso)

    # 6. Publish the snapshot page and rebuild the site.
    title = f"Daily Brief · {date_str}"
    interactive_html = build_interactive_html(title, data, quotes, timestamp)
    s3_publish_brief("daily", now_et, interactive_html, data=data, quotes=quotes, timestamp=timestamp)

    return {"status": "published", "mode": mode, "stories": len(headlines),
            "quotes": len(quotes), "stocks": len(stocks)}


if __name__ == "__main__":
    import sys
    # "record" for the hourly run, "daily" for the full one. The old edition
    # names (morning/midday/evening) still work and map to a daily run.
    mode = sys.argv[1] if len(sys.argv) > 1 else "daily"
    result = lambda_handler({"mode": mode}, None)
    print(json.dumps(result, indent=2))
