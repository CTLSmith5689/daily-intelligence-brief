"""
Daily Intelligence Brief. AWS Lambda Handler.
Full-spectrum newsfeed with real market data via Alpha Vantage.
Fetches news via RSS, market data via Alpha Vantage, analysis via Claude API.
Sends via iCloud SMTP. Triggered by EventBridge rules at 7 AM, 12:15 PM, and 4:45 PM ET.
"""

import os
import re
import ssl
import json
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
from pathlib import Path

# ── Config ──────────────────────────────────────────────────────────────────

SMTP_USER = "ctlsmith@me.com"  # Apple ID for SMTP auth (must match the APTERREON_ICLOUD_APP_PASSWORD owner)
SENDER_EMAIL = "Daily_Intel_Briefs@icloud.com"  # iCloud alias used as From: header
SENDER_NAME = "Daily Intelligence Brief"
RECIPIENT_EMAIL = os.environ.get("RECIPIENTS", SMTP_USER)
SMTP_SERVER = "smtp.mail.me.com"
SMTP_PORT = 587

ANTHROPIC_MODEL = os.environ.get("APTERREON_MODEL", "claude-sonnet-4-6")
ET_OFFSET = timedelta(hours=-4)  # EDT

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
RETENTION_DAYS = 30

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
    "P&I": "https://www.pionline.com/pf/feed/rss/pionline/news",
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

    # Money market yields: scrape Fidelity fund pages for SPAXX and FZFXX
    # Falls back to federal funds rate if scrape fails
    mm_funds = [
        ("SPAXX", "SPAXX 7d Yield", "https://fundresearch.fidelity.com/mutual-funds/summary/31617H102"),
        ("FZFXX", "FZFXX 7d Yield", "https://fundresearch.fidelity.com/mutual-funds/summary/316341304"),
    ]
    mm_success = False
    for mm_ticker, mm_label, mm_url in mm_funds:
        try:
            req = urllib.request.Request(mm_url, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml",
            })
            with urllib.request.urlopen(req, timeout=10) as resp:
                html = resp.read().decode("utf-8")

            # Fidelity pages typically show 7-day yield in a pattern like:
            # "7-Day Yield" followed by a percentage value
            # Try multiple patterns to find the 7-day yield
            patterns = [
                r'7[- ]?[Dd]ay\s+[Yy]ield[^0-9]*?(\d+\.\d+)\s*%',
                r'seven[- ]?day\s+yield[^0-9]*?(\d+\.\d+)\s*%',
                r'7-Day Yield.*?(\d+\.\d+)%',
                r'"sevenDayYield"\s*:\s*"?(\d+\.\d+)',
                r'7-Day Yield<.*?(\d+\.\d+)\s*%',
            ]
            yield_val = None
            for pattern in patterns:
                match = re.search(pattern, html, re.IGNORECASE | re.DOTALL)
                if match:
                    yield_val = match.group(1)
                    break

            if yield_val:
                quotes.append({
                    "ticker": mm_ticker,
                    "label": mm_label,
                    "price": f"{float(yield_val):.2f}%",
                    "change_pct": "0",
                    "is_yield": True,
                })
                mm_success = True
                print(f"Fidelity scrape success for {mm_ticker}: {yield_val}%")
            else:
                print(f"Fidelity scrape: could not find 7-day yield in HTML for {mm_ticker}")
        except Exception as e:
            print(f"Fidelity scrape error for {mm_ticker}: {e}")

    # Fallback: federal funds rate if Fidelity scrape failed for both
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
                        "label": "MM Yield (avg)",
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
    for category, url in RSS_FEEDS.items():
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "IntelBrief/1.0"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                xml_data = resp.read().decode("utf-8")
            root = ET.fromstring(xml_data)
            feed_items = _extract_feed_items(root)
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
        except Exception as e:
            print(f"RSS fetch error for {category}: {e}")

    print(f"Recency filter: kept {len(all_items)} articles, dropped {stale_count} stale (>{max_age_hours}h old)")
    return all_items


# ── Claude API ──────────────────────────────────────────────────────────────

# Pricing per million tokens. Updates automatically based on model env var.
# Opus: $15/$75, Sonnet: $3/$15, Haiku: $0.80/$4
MODEL_PRICING = {
    "claude-opus-4-6": (15.00, 75.00),
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-haiku-4-5-20251001": (0.80, 4.00),
}
_default_pricing = (15.00, 75.00)  # Opus default
INPUT_COST_PER_MTOK, OUTPUT_COST_PER_MTOK = MODEL_PRICING.get(ANTHROPIC_MODEL, _default_pricing)

def call_claude(system_prompt, user_content):
    """Call Anthropic Messages API. Returns (text, usage_dict)."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise ValueError("ANTHROPIC_API_KEY not set")

    payload = json.dumps({
        "model": ANTHROPIC_MODEL,
        "max_tokens": 32000,
        "system": system_prompt,
        "messages": [{"role": "user", "content": user_content}],
    }).encode("utf-8")

    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        },
        method="POST",
    )

    with urllib.request.urlopen(req, timeout=290) as resp:
        result = json.loads(resp.read().decode("utf-8"))

    text = result["content"][0]["text"]
    usage = result.get("usage", {})
    input_tokens = usage.get("input_tokens", 0)
    output_tokens = usage.get("output_tokens", 0)

    # Calculate cost for this call
    input_cost = (input_tokens / 1_000_000) * INPUT_COST_PER_MTOK
    output_cost = (output_tokens / 1_000_000) * OUTPUT_COST_PER_MTOK
    total_cost = input_cost + output_cost

    # Project monthly: 3 briefs/day * 30 days
    monthly_projected = total_cost * 90

    usage_info = {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
        "cost_this_call": round(total_cost, 4),
        "cost_daily_projected": round(total_cost * 3, 4),
        "cost_monthly_projected": round(monthly_projected, 2),
    }

    return text, usage_info


# ── Brief Config ────────────────────────────────────────────────────────────

READER_CONTEXT = os.environ.get("APTERREON_READER_CONTEXT", "A curious, analytically rigorous reader.")

SECTION_NAMES = [s[0] for s in SECTIONS]

ANALYSIS_PROMPT = """You are drafting an intelligence brief. The reader: {reader_context}

You will receive headlines grouped by section. For EACH section, select the 2-3 most important stories:
{section_list}

Return ONLY valid JSON (no markdown fences, no preamble):
{{
  "sections": [
    {{
      "name": "EXACT section name from the list above",
      "stories": [
        {{
          "headline": "Concise headline",
          "summary": "What happened. 1 sentence.",
          "insight": "Why it matters. 1 sentence. Be specific, not verbose.",
          "source": "Publication name(s)",
          "link": "URL from the input data, or empty string if unavailable"
        }}
      ]
    }}
  ],
  "the_edge": "One cross-domain insight connecting dots most people miss. 1-2 sentences."
}}

RULES:
- Include ALL sections in this exact order: {section_list}
- For "Breaking News": include headline, source, and link ONLY. Set summary and insight to empty strings. These are raw headlines, no analysis needed.
- For all other sections: 2-3 stories per section with full summary and insight.
- CRITICAL: Every summary must be ONE sentence. Every insight must be ONE sentence. No exceptions.
- NEVER use em dashes (the long dash character). Use periods, commas, or colons instead. Em dashes are an AI-writing tell and the brand voice forbids them.
- Deduplicate similar headlines.
- Output ONLY the JSON object. No commentary, no summary, no text before or after the JSON. Start with {{ and end with }}."""

def get_brief_config(brief_type):
    """Build brief config with current env vars (not import-time)."""
    reader = os.environ.get("APTERREON_READER_CONTEXT", "A curious, analytically rigorous reader.")
    section_list = ", ".join(SECTION_NAMES)
    base_prompt = ANALYSIS_PROMPT.format(reader_context=reader, section_list=section_list)

    configs = {
        "morning": {
            "subject_prefix": "Morning Brief",
            "max_per_feed": 4,
            "system_prompt": base_prompt,
        },
        "midday": {
            "subject_prefix": "Midday Update",
            "max_per_feed": 3,
            "system_prompt": base_prompt + "\n\nThis is a MIDDAY DELTA UPDATE. Only genuine new developments since morning. 6-10 stories max. Shorter insights.",
        },
        "evening": {
            "subject_prefix": "Evening Wrap",
            "max_per_feed": 3,
            "system_prompt": base_prompt + '\n\nThis is an EVENING WRAP. Pick the single most important story per section. Add a "tomorrow_watch" field (string) to the root JSON with 2-3 things to watch tomorrow.',
        },
    }
    return configs.get(brief_type, configs["morning"])


# ── Market Data Bar HTML ───────────────────────────────────────────────────

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

def usage_banner_email(usage_info):
    """API usage banner for the email (Apterreon)."""
    if not usage_info:
        return ""
    cost = usage_info.get("cost_this_call", 0)
    monthly = usage_info.get("cost_monthly_projected", 0)
    tokens = usage_info.get("total_tokens", 0)

    if monthly < 2:
        bar_color = "#5599CC"  # singularity blue, calm
        status = "LOW"
    elif monthly < 5:
        bar_color = "#888888"  # grey, neutral
        status = "MODERATE"
    else:
        bar_color = "#CC0000"  # red, over budget
        status = "HIGH"

    budget = 10.0
    pct = min(100, (monthly / budget) * 100)

    return f"""<table width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:18px;border-collapse:collapse">
<tr><td style="padding:12px 16px;background:#070A0F;border:1px solid #1A2030">
<table width="100%" cellpadding="0" cellspacing="0">
<tr>
<td style="font-size:9px;letter-spacing:3px;color:#9AA8B8;text-transform:uppercase;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif">API Usage</td>
<td style="text-align:right;font-size:9px;letter-spacing:3px;color:{bar_color};font-weight:700;text-transform:uppercase;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif">{status}</td>
</tr>
<tr><td colspan="2" style="padding-top:8px">
<div style="background:#111420;height:2px;overflow:hidden"><div style="background:{bar_color};width:{pct:.0f}%;height:2px"></div></div>
</td></tr>
<tr><td colspan="2" style="padding-top:8px;font-size:10px;color:#9AA8B8;font-family:'SF Mono',Menlo,Consolas,monospace">
${cost:.4f} this brief &middot; {tokens:,} tokens &middot; ${monthly:.2f}/mo projected &middot; $10.00 budget
</td></tr>
</table>
</td></tr></table>"""


def build_email_preview(title, data, quotes, timestamp, usage_info=None, brief_url=None, site_url=None):
    """Email preview, Apterreon. Email-safe (inline styles, tables,
    system fonts only, no web fonts since most clients strip @import).
    brief_url: deep link to this brief on the public site.
    site_url: home page link."""
    sans = "-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif"
    mono = "'SF Mono',Menlo,Consolas,'Courier New',monospace"

    usage_html = usage_banner_email(usage_info)
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
<link href="https://fonts.googleapis.com/css2?family=Syne:wght@600;700;800&family=DM+Mono:wght@300;400;500&display=swap" rel="stylesheet">
<style>
  *,*::before,*::after {{ box-sizing:border-box; margin:0; padding:0; }}
  :root {{
    --bg-base:#050810; --bg-surface:#0D0F18; --bg-elevated:#111420; --bg-deep:#070A0F;
    --border-dim:#1A2030; --border-red:#3A0A0A;
    --apt-red:#CC0000; --apt-dark-red:#7A1010; --apt-grey:#888888;
    --text-primary:#E0E8F0; --text-body:#CCD4DC; --text-dim:#9AA8B8; --text-muted:#6A7888; --text-faint:#4A5A6A;
  }}
  html {{ background:var(--bg-base); color:var(--text-primary); font-family:'DM Mono',ui-monospace,Menlo,Consolas,monospace; font-size:13px; -webkit-font-smoothing:antialiased; }}
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
    font-family:'DM Mono',monospace; font-size:10px; letter-spacing:2px;
    color:var(--text-dim); text-transform:uppercase; transition:color .15s;
    display:flex; align-items:center; gap:6px;
  }}
  .topnav .back:hover {{ color:var(--text-primary); }}
  .topnav .lockup {{ display:flex; align-items:center; gap:10px; margin-left:auto; }}
  .topnav .lockup .dm {{ font-family:'Syne',sans-serif; font-weight:800; font-size:11px; letter-spacing:4px; color:var(--text-primary); text-transform:uppercase; }}
  .topnav .lockup .prod {{ font-family:'Syne',sans-serif; font-weight:700; font-size:8px; letter-spacing:4px; color:var(--apt-red); text-transform:uppercase; }}
  .topnav .suite {{ display:none; font-size:9px; letter-spacing:2px; color:var(--text-faint); text-transform:uppercase; }}
  @media (min-width:720px) {{ .topnav .suite {{ display:inline; }} }}

  .container {{ max-width:760px; margin:0 auto; padding:32px 24px 96px; }}

  .header {{ margin-bottom:36px; padding-bottom:24px; border-bottom:1px solid var(--border-dim); }}
  .header .tag {{ font-family:'DM Mono',monospace; font-size:10px; letter-spacing:4px; color:var(--apt-red); text-transform:uppercase; margin-bottom:10px; }}
  .header h1 {{ font-family:'Syne',sans-serif; font-size:30px; font-weight:800; letter-spacing:0.5px; color:#FFFFFF; line-height:1.2; }}
  .header .meta {{ font-family:'DM Mono',monospace; font-size:10px; letter-spacing:2px; color:var(--text-dim); text-transform:uppercase; margin-top:10px; }}

  .market-bar {{
    display:grid; grid-template-columns:repeat(auto-fit,minmax(120px,1fr)); gap:8px;
    margin-bottom:32px;
  }}
  .market-card {{
    background:var(--bg-surface); border:1px solid var(--border-dim);
    padding:14px 12px; text-align:center;
  }}
  .market-card .label {{ font-family:'DM Mono',monospace; font-size:9px; letter-spacing:2px; color:var(--text-dim); text-transform:uppercase; }}
  .market-card .price {{ font-family:'DM Mono',monospace; font-size:18px; font-weight:500; color:var(--text-primary); margin:6px 0 4px; }}
  .market-card .change {{ font-family:'DM Mono',monospace; font-size:11px; }}
  .market-card .change.up {{ color:#5599CC; }}
  .market-card .change.down {{ color:var(--apt-red); }}

  .usage-banner {{
    background:var(--bg-deep); border:1px solid var(--border-dim);
    padding:12px 16px; margin-bottom:24px;
  }}
  .usage-row {{ display:flex; justify-content:space-between; align-items:center; margin-bottom:8px; }}
  .usage-label {{ font-family:'DM Mono',monospace; font-size:9px; letter-spacing:3px; color:var(--text-dim); text-transform:uppercase; }}
  .usage-status {{ font-family:'DM Mono',monospace; font-size:9px; letter-spacing:3px; font-weight:500; text-transform:uppercase; }}
  .usage-bar {{ background:var(--bg-elevated); height:2px; overflow:hidden; margin-bottom:8px; }}
  .usage-bar-fill {{ height:2px; transition:width 0.3s; }}
  .usage-details {{ font-family:'DM Mono',monospace; font-size:10px; color:var(--text-dim); }}

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
  .widget-num {{ font-family:'DM Mono',monospace; font-size:10px; letter-spacing:3px; color:var(--text-muted); flex-shrink:0; padding-top:2px; }}
  .widget-info {{ flex:1; min-width:0; }}
  .widget-title {{ font-family:'Syne',sans-serif; font-size:14px; font-weight:700; letter-spacing:3px; text-transform:uppercase; }}
  .widget-headlines {{ margin:8px 0 0; padding:0; list-style:none; }}
  .widget-headlines li {{ font-family:'DM Mono',monospace; font-size:11px; color:var(--text-dim); line-height:1.55; padding:3px 0; padding-left:14px; position:relative; word-wrap:break-word; }}
  .widget-headlines li::before {{ content:'·'; position:absolute; left:2px; color:var(--text-muted); }}
  .widget-count {{ font-family:'DM Mono',monospace; font-size:10px; letter-spacing:2px; color:var(--text-dim); flex-shrink:0; padding-top:2px; }}
  .widget-chevron {{ color:var(--text-muted); font-size:14px; transition:transform .2s,color .2s; flex-shrink:0; padding-top:4px; }}
  .widget.active .widget-chevron {{ transform:rotate(90deg); color:var(--apt-red); }}

  .widget-body {{ max-height:0; overflow:hidden; transition:max-height .35s ease; }}
  .widget.active .widget-body {{ max-height:4000px; }}

  .widget-stories {{ padding:0 22px 20px; border-top:1px solid var(--border-dim); }}

  .story {{ padding:18px 0; border-top:1px solid var(--border-dim); cursor:pointer; }}
  .story:first-child {{ border-top:none; }}
  .story-headline {{ font-family:'Syne',sans-serif; font-size:15px; font-weight:600; color:var(--text-primary); line-height:1.4; display:flex; justify-content:space-between; align-items:flex-start; gap:12px; }}
  .story-headline .arrow {{ font-size:11px; color:var(--text-muted); transition:transform .2s,color .2s; flex-shrink:0; padding-top:4px; }}
  .story.open .story-headline .arrow {{ transform:rotate(90deg); color:var(--apt-red); }}
  .story-source {{ font-family:'DM Mono',monospace; font-size:9px; letter-spacing:2px; color:var(--text-muted); text-transform:uppercase; margin-top:6px; }}

  .story-details {{ max-height:0; overflow:hidden; transition:max-height .3s ease; }}
  .story.open .story-details {{ max-height:600px; }}

  .story-summary {{ font-family:'DM Mono',monospace; font-size:13px; color:var(--text-body); margin:14px 0 12px; line-height:1.65; }}
  .story-insight {{ font-family:'DM Mono',monospace; font-size:12px; color:var(--text-body); line-height:1.65; padding:14px 16px; background:var(--bg-deep); border-left:2px solid var(--apt-red); }}
  .insight-label {{ font-family:'DM Mono',monospace; font-size:9px; font-weight:500; text-transform:uppercase; letter-spacing:3px; color:var(--apt-red); margin-bottom:8px; }}
  .story-link {{ display:inline-block; margin-top:12px; font-family:'DM Mono',monospace; font-size:10px; letter-spacing:2px; color:var(--apt-red); text-transform:uppercase; }}
  .story-link:hover {{ color:#FFFFFF; }}

  .panel {{ margin-top:32px; padding:22px 24px; background:var(--bg-deep); border:1px solid var(--border-dim); }}
  .panel.edge {{ border-left:3px solid var(--apt-red); }}
  .panel-title {{ font-family:'DM Mono',monospace; font-size:10px; font-weight:500; text-transform:uppercase; letter-spacing:4px; margin-bottom:12px; }}
  .panel.edge .panel-title {{ color:var(--apt-red); }}
  .panel:not(.edge) .panel-title {{ color:var(--apt-grey); }}
  .panel p {{ font-family:'DM Mono',monospace; font-size:13px; color:var(--text-body); line-height:1.7; }}

  .footer {{ margin-top:64px; padding-top:24px; border-top:1px solid var(--border-dim); display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:12px; }}
  .footer .brand {{ font-family:'Syne',sans-serif; font-size:9px; font-weight:800; letter-spacing:4px; color:var(--text-muted); text-transform:uppercase; }}
  .footer .ts {{ font-family:'DM Mono',monospace; font-size:9px; letter-spacing:2px; color:var(--text-muted); }}

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
        mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        if mtime < cutoff:
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
}
html { background:var(--bg-base); color:var(--text-1); font-family:'Inter',-apple-system,BlinkMacSystemFont,sans-serif; font-size:15px; -webkit-font-smoothing:antialiased; scroll-behavior:smooth; }
body { min-height:100vh; overflow-x:hidden; }
::-webkit-scrollbar { width:6px; }
::-webkit-scrollbar-thumb { background:var(--border-bright); border-radius:3px; }
a { color:inherit; text-decoration:none; }

#plexus { position:fixed; inset:0; z-index:0; opacity:0.55; }
body::before {
  content:''; position:fixed; inset:0; z-index:1; pointer-events:none;
  background:
    radial-gradient(800px 600px at 15% 20%, rgba(255,31,61,0.10), transparent 60%),
    radial-gradient(900px 700px at 85% 80%, rgba(204,0,40,0.07), transparent 60%),
    radial-gradient(1200px 800px at 50% 40%, rgba(255,122,133,0.04), transparent 70%);
}
body::after {
  content:''; position:fixed; inset:0; z-index:2; pointer-events:none;
  background-image:radial-gradient(rgba(255,255,255,0.025) 1px, transparent 1px);
  background-size:3px 3px; opacity:0.5; mix-blend-mode:overlay;
}
.topnav, .hero, .featured, .features, .feed, .lib, .footer, .destinations, .picks, .editions { position:relative; z-index:3; }

.topnav {
  position:sticky; top:16px; max-width:1200px; margin:16px auto 0; padding:10px 14px 10px 18px;
  display:flex; align-items:center; gap:14px;
  background:rgba(17,18,26,0.55);
  backdrop-filter:blur(24px) saturate(160%); -webkit-backdrop-filter:blur(24px) saturate(160%);
  border:1px solid var(--border); border-radius:18px;
}
.lockup { display:flex; align-items:center; gap:12px; }
.lockup-text { display:flex; flex-direction:column; line-height:1; }
.brand { font-family:'Syne',sans-serif; font-weight:800; font-size:14px; letter-spacing:4px; color:var(--text-1); text-transform:uppercase; }
.lockup-tagline { font-family:'DM Mono',monospace; font-size:9px; letter-spacing:2px; color:var(--apt-rose); text-transform:uppercase; margin-top:5px; }
.pulse-row { display:flex; align-items:center; gap:8px; margin-left:14px; padding-left:14px; border-left:1px solid var(--border); font-family:'DM Mono',monospace; font-size:10px; letter-spacing:2px; color:var(--text-3); text-transform:uppercase; }
.pulse-dot { width:6px; height:6px; border-radius:50%; background:#34D27A; box-shadow:0 0 12px rgba(52,210,122,0.7); animation:pulse 1.8s ease-out infinite; }
@keyframes pulse { 0%{box-shadow:0 0 0 0 rgba(52,210,122,0.55);} 70%{box-shadow:0 0 0 10px rgba(52,210,122,0);} 100%{box-shadow:0 0 0 0 rgba(52,210,122,0);} }

.nav { margin-left:auto; display:flex; gap:4px; }
.nav a { padding:8px 14px; font-size:13px; font-weight:500; color:var(--text-3); border-radius:10px; transition:all .2s; }
.nav a:hover { color:var(--text-1); background:rgba(255,255,255,0.04); }
.nav a.active { color:var(--text-1); background:rgba(255,31,61,0.10); }

.hero { max-width:1200px; margin:0 auto; padding:96px 24px 48px; }
.eyebrow {
  display:inline-flex; align-items:center; gap:8px; padding:6px 14px; border-radius:999px;
  background:rgba(255,31,61,0.08); border:1px solid rgba(255,31,61,0.20);
  font-family:'DM Mono',monospace; font-size:11px; letter-spacing:2px; color:var(--apt-rose);
  text-transform:uppercase; margin-bottom:24px;
  opacity:0; transform:translateY(8px); animation:fadeUp .8s .1s ease-out forwards;
}
.eyebrow .live-dot { width:6px; height:6px; border-radius:50%; background:#34D27A; }

h1.hero-title {
  font-family:'Syne',sans-serif; font-weight:800; font-size:84px; line-height:0.98;
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
.feat-meta { display:flex; align-items:center; gap:10px; margin-bottom:18px; font-family:'DM Mono',monospace; font-size:11px; letter-spacing:2px; color:var(--text-3); text-transform:uppercase; flex-wrap:wrap; }
.feat-meta .tag { padding:4px 10px; border-radius:6px; background:rgba(255,31,61,0.10); color:var(--apt-rose); border:1px solid rgba(255,31,61,0.20); }
.feat-meta .dot { width:3px; height:3px; border-radius:50%; background:var(--text-4); }
.feat-kicker { font-family:'Syne',sans-serif; font-weight:700; font-size:13px; letter-spacing:4px; text-transform:uppercase; color:var(--apt-rose); margin-bottom:14px; }
.feat-body { font-size:18px; line-height:1.7; color:var(--text-1); max-width:920px; margin-bottom:8px; font-weight:400; letter-spacing:-0.005em; }
.feat-body::first-letter { font-family:'Syne',sans-serif; font-size:1.4em; font-weight:700; line-height:1; color:var(--apt-rose); padding-right:2px; }
.feat-grid { display:grid; grid-template-columns:repeat(3, 1fr); gap:18px; margin-top:32px; }
.feat-stat { padding:18px 20px; background:rgba(255,255,255,0.03); border:1px solid var(--border); border-radius:14px; transition:all .25s; }
.feat-stat:hover { background:rgba(255,255,255,0.05); border-color:rgba(255,255,255,0.12); transform:translateY(-2px); }
.fs-label { font-family:'DM Mono',monospace; font-size:10px; letter-spacing:2px; color:var(--text-4); text-transform:uppercase; margin-bottom:8px; }
.fs-val { font-family:'Syne',sans-serif; font-size:30px; font-weight:700; color:var(--text-1); letter-spacing:-0.02em; line-height:1; }
.fs-delta { font-size:12px; color:#34D27A; margin-top:6px; }
.feat-actions { margin-top:28px; display:flex; gap:14px; flex-wrap:wrap; align-items:center; }
.feat-actions .quiet { font-family:'DM Mono',monospace; font-size:11px; letter-spacing:2px; color:var(--text-3); text-transform:uppercase; border-bottom:1px solid var(--border); padding-bottom:2px; }

.themes-list { display:flex; flex-wrap:wrap; gap:8px; margin-top:20px; }
.theme-pill { padding:6px 12px; font-family:'DM Mono',monospace; font-size:10px; letter-spacing:1.5px; color:var(--apt-rose); text-transform:uppercase; background:rgba(255,31,61,0.06); border:1px solid rgba(255,31,61,0.20); border-radius:999px; }

.snapshot-list { list-style:none; padding:0; margin:0 0 4px 0; max-width:920px; }
.snapshot-list li { position:relative; padding:14px 0 14px 28px; border-top:1px solid var(--border); font-size:17px; line-height:1.55; color:var(--text-1); font-weight:400; letter-spacing:-0.005em; }
.snapshot-list li:first-child { border-top:none; padding-top:6px; }
.snapshot-list li::before { content:''; position:absolute; left:6px; top:24px; width:8px; height:8px; border-radius:50%; background:var(--apt-rose); box-shadow:0 0 12px rgba(255,31,61,0.35); }
.snapshot-list li:first-child::before { top:16px; }

.destinations { max-width:1200px; margin:0 auto; padding:24px 24px 64px; }
.destinations-h { display:flex; justify-content:space-between; align-items:end; margin-bottom:32px; flex-wrap:wrap; gap:18px; }
.destinations-h h2 { font-family:'Syne',sans-serif; font-weight:700; font-size:36px; letter-spacing:-0.02em; line-height:1.1; }
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
.dest-eyebrow { font-family:'DM Mono',monospace; font-size:10px; letter-spacing:2px; color:var(--apt-rose); text-transform:uppercase; margin-bottom:12px; }
.dest-title { font-family:'Syne',sans-serif; font-size:24px; font-weight:700; letter-spacing:-0.01em; color:var(--text-1); margin-bottom:10px; }
.dest-body { font-size:14px; line-height:1.55; color:var(--text-3); margin-bottom:18px; }
.dest-cta { font-family:'DM Mono',monospace; font-size:11px; letter-spacing:2px; color:var(--apt-rose); text-transform:uppercase; }

.features { max-width:1200px; margin:0 auto; padding:64px 24px; }
.features-h { display:flex; justify-content:space-between; align-items:end; margin-bottom:40px; flex-wrap:wrap; gap:18px; }
.features-h h2 { font-family:'Syne',sans-serif; font-weight:700; font-size:42px; letter-spacing:-0.02em; line-height:1.1; max-width:600px; }
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
  font-family:'DM Mono',monospace; font-size:14px; font-weight:500; color:var(--apt-rose);
}
.sc-titles { flex:1; min-width:0; }
.sc-eyebrow { font-family:'DM Mono',monospace; font-size:10px; letter-spacing:2px; color:var(--text-4); text-transform:uppercase; margin-bottom:4px; }
.sc-title { font-family:'Syne',sans-serif; font-size:22px; font-weight:700; letter-spacing:-0.01em; color:var(--text-1); }
.sc-count { font-family:'DM Mono',monospace; font-size:11px; color:var(--text-4); padding:4px 10px; background:rgba(255,255,255,0.04); border-radius:8px; }
.sc-list { display:flex; flex-direction:column; gap:0; }
.sc-item { padding:14px 0; border-top:1px solid var(--border); display:grid; grid-template-columns:1fr auto; gap:12px; align-items:start; transition:padding-left .15s; }
.sc-item:first-child { border-top:none; padding-top:4px; }
.sc-item:hover { padding-left:6px; }
.sc-item-headline { font-size:15px; font-weight:500; color:var(--text-1); line-height:1.45; }
.sc-item-source { font-family:'DM Mono',monospace; font-size:10px; letter-spacing:1.5px; color:var(--text-4); text-transform:uppercase; margin-top:6px; }
.sc-arrow { color:var(--text-4); font-size:18px; transition:color .15s, transform .15s; align-self:start; padding-top:2px; }
.sc-item:hover .sc-arrow { color:var(--apt-red); transform:translateX(4px); }

.editions { max-width:1200px; margin:0 auto; padding:96px 24px 64px; }
.editions-h { display:flex; justify-content:space-between; align-items:end; margin-bottom:40px; flex-wrap:wrap; gap:18px; }
.editions-h h2 { font-family:'Syne',sans-serif; font-weight:700; font-size:42px; letter-spacing:-0.02em; line-height:1.1; }
.editions-h p { font-size:16px; color:var(--text-3); line-height:1.6; max-width:380px; }
.edition-block { margin-bottom:48px; }
.edition-head { display:flex; align-items:baseline; gap:12px; margin-bottom:18px; padding-bottom:12px; border-bottom:1px solid var(--border); flex-wrap:wrap; }
.edition-name { font-family:'Syne',sans-serif; font-size:24px; font-weight:700; color:var(--text-1); }
.edition-time { font-family:'DM Mono',monospace; font-size:11px; letter-spacing:2px; color:var(--text-4); text-transform:uppercase; }
.edition-link { margin-left:auto; font-family:'DM Mono',monospace; font-size:11px; letter-spacing:2px; color:var(--apt-rose); text-transform:uppercase; }
.edition-edge { font-size:16px; line-height:1.65; color:var(--text-2); margin-bottom:24px; padding:18px 22px; background:rgba(17,18,26,0.55); border-left:3px solid var(--apt-red); border-radius:8px; }
.edition-empty { padding:32px; text-align:center; font-family:'DM Mono',monospace; font-size:12px; letter-spacing:2px; color:var(--text-4); text-transform:uppercase; background:rgba(17,18,26,0.5); border:1px dashed var(--border); border-radius:14px; }

.lib { max-width:1200px; margin:0 auto; padding:96px 24px 64px; }
.lib.lib-wide { padding-top:36px; padding-bottom:24px; max-width:1480px; }
.lib.lib-wide .lib-h { margin-bottom:10px; }
.lib.lib-wide .lib-h h2 { font-size:32px; }
.lib.lib-wide { max-width:min(1640px, 96vw); }
.lib-h { display:flex; justify-content:space-between; align-items:end; margin-bottom:18px; flex-wrap:wrap; gap:14px; }
.lib-h h2 { font-family:'Syne',sans-serif; font-weight:700; font-size:42px; letter-spacing:-0.02em; line-height:1.1; }
.lib-h .lib-count { font-family:'DM Mono',monospace; font-size:11px; letter-spacing:2px; color:var(--text-4); text-transform:uppercase; padding:6px 12px; border:1px solid var(--border); border-radius:999px; }

.lib-controls { display:flex; flex-direction:column; gap:14px; margin-bottom:24px; padding:20px; background:rgba(17,18,26,0.55); backdrop-filter:blur(20px); -webkit-backdrop-filter:blur(20px); border:1px solid var(--border); border-radius:16px; }
.lib-search { display:flex; align-items:center; gap:12px; padding:12px 16px; background:rgba(10,10,15,0.6); border:1px solid var(--border); border-radius:12px; transition:all .15s; }
.lib-search:focus-within { border-color:var(--apt-red); box-shadow:0 0 0 3px rgba(255,31,61,0.10); }
.lib-search .icon { color:var(--text-3); font-size:16px; }
.lib-search input { flex:1; background:transparent; border:none; outline:none; font-family:'DM Mono',monospace; font-size:14px; color:var(--text-1); }
.lib-search input::placeholder { color:var(--text-4); }
.lib-search .clear-btn { background:transparent; border:none; cursor:pointer; padding:4px 8px; color:var(--text-3); font-family:'DM Mono',monospace; font-size:10px; letter-spacing:2px; text-transform:uppercase; transition:color .15s; }
.lib-search .clear-btn:hover { color:var(--text-1); }
.lib-search .clear-btn[hidden] { display:none; }

.lib-chips { display:flex; flex-wrap:wrap; gap:6px; align-items:center; }
.lib-chip-label { font-family:'DM Mono',monospace; font-size:9px; letter-spacing:2px; color:var(--text-4); text-transform:uppercase; margin-right:4px; }
.lib-chip {
  padding:6px 12px; font-family:'DM Mono',monospace; font-size:10px; letter-spacing:1.5px;
  color:var(--text-3); cursor:pointer; background:transparent;
  border:1px solid var(--border); border-radius:999px; text-transform:uppercase;
  user-select:none; transition:all .15s;
}
.lib-chip:hover { color:var(--text-1); border-color:var(--border-bright); }
.lib-chip.active { color:#FFF; background:rgba(255,31,61,0.18); border-color:var(--apt-red); }

.lib-list { display:flex; flex-direction:column; gap:1px; background:var(--border); border:1px solid var(--border); border-radius:14px; overflow:hidden; }
.lib-item {
  background:rgba(17,18,26,0.85); padding:18px 22px;
  display:grid; grid-template-columns:120px 1fr auto; gap:18px; align-items:center;
  transition:background .15s;
}
.lib-item:hover { background:rgba(22,23,31,0.95); }
.lib-item .li-section {
  font-family:'DM Mono',monospace; font-size:9px; letter-spacing:1.5px;
  color:var(--apt-rose); text-transform:uppercase; padding:4px 8px;
  border:1px solid rgba(255,31,61,0.20); border-radius:6px; text-align:center;
  background:rgba(255,31,61,0.06); justify-self:start;
}
.lib-item .li-headline { font-size:15px; color:var(--text-1); line-height:1.45; font-weight:500; }
.lib-item .li-meta { font-family:'DM Mono',monospace; font-size:9px; letter-spacing:1.5px; color:var(--text-4); text-transform:uppercase; margin-top:5px; }
.lib-item .li-src { font-family:'DM Mono',monospace; font-size:10px; letter-spacing:1.5px; color:var(--text-4); text-transform:uppercase; text-align:right; }
@media (max-width:680px) {
  .lib-item { grid-template-columns:1fr; gap:6px; padding:16px 18px; }
  .lib-item .li-src { text-align:left; }
}

.empty-state { padding:48px; text-align:center; font-family:'DM Mono',monospace; font-size:12px; letter-spacing:2px; color:var(--text-4); text-transform:uppercase; background:rgba(17,18,26,0.5); border:1px solid var(--border); border-radius:14px; }

/* Stocks page: filterable table */
.lib-sub { font-size:14px; color:var(--text-3); line-height:1.6; max-width:780px; margin-bottom:24px; }
.picks-meta { font-family:'DM Mono',monospace; font-size:11px; letter-spacing:2px; color:var(--text-4); text-transform:uppercase; }
/* Advanced filter panel */
.stk-filter-bar { display:flex; align-items:center; gap:10px; padding-top:6px; border-top:1px solid var(--border); margin-top:4px; flex-wrap:wrap; }
.stk-filter-toggle {
  display:inline-flex; align-items:center; gap:8px;
  padding:8px 14px; font-family:'DM Mono',monospace; font-size:11px; letter-spacing:1.5px;
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
  font-family:'DM Mono',monospace; font-size:10px; letter-spacing:1.5px;
  color:var(--text-3); background:transparent; border:none; cursor:pointer;
  text-transform:uppercase; padding:8px 4px;
}
.stk-filter-reset:hover { color:var(--apt-rose); }

.stk-filter-panel { display:flex; flex-direction:column; gap:10px; margin-top:6px; padding:14px 16px; background:rgba(10,10,15,0.5); border:1px solid var(--border); border-radius:12px; }
.stk-filter-cols { display:grid; grid-template-columns:1fr 1fr; gap:24px; }
@media (max-width:980px) { .stk-filter-cols { grid-template-columns:1fr; gap:18px; } }
.stk-filter-col { display:flex; flex-direction:column; gap:8px; }
.stk-filter-col-h { font-family:'Syne',sans-serif; font-size:13px; font-weight:700; letter-spacing:0.02em; color:var(--text-1); padding-bottom:8px; margin-bottom:4px; border-bottom:1px solid var(--border); display:flex; align-items:baseline; gap:10px; flex-wrap:wrap; }
.stk-filter-col-sub { font-family:'DM Mono',monospace; font-size:9px; letter-spacing:1.5px; color:var(--text-4); text-transform:uppercase; font-weight:400; }
.stk-filter-row { display:grid; grid-template-columns:130px 90px auto 90px 1fr; align-items:center; gap:8px; }

/* Dimension weight sliders (right column of filter panel) */
.stk-weight-row { display:grid; grid-template-columns:90px 1fr 50px; align-items:center; gap:12px; padding:6px 0; }
.stk-weight-label { font-family:'DM Mono',monospace; font-size:11px; letter-spacing:1px; color:var(--text-2); text-transform:uppercase; }
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
.stk-weight-val { font-family:'DM Mono',monospace; font-size:12px; color:var(--apt-rose); font-weight:600; text-align:right; }
.stk-weight-presets { display:flex; flex-wrap:wrap; gap:5px; align-items:center; padding-top:10px; margin-top:6px; border-top:1px solid var(--border); }
.stk-weight-presets-label { font-family:'DM Mono',monospace; font-size:9px; letter-spacing:2px; color:var(--text-4); text-transform:uppercase; margin-right:4px; }
.stk-filter-row-toggle { grid-template-columns:1fr; }
.stk-filter-label { font-family:'DM Mono',monospace; font-size:10px; letter-spacing:1.5px; color:var(--text-3); text-transform:uppercase; }
.stk-filter-input {
  padding:7px 10px; font-family:'DM Mono',monospace; font-size:12px;
  color:var(--text-1); background:rgba(10,10,15,0.6);
  border:1px solid var(--border); border-radius:8px; outline:none;
  transition:border-color .15s, box-shadow .15s; width:100%;
}
.stk-filter-input:focus { border-color:var(--apt-red); box-shadow:0 0 0 2px rgba(255,31,61,0.10); }
.stk-filter-input::placeholder { color:var(--text-5); }
.stk-filter-sep { font-family:'DM Mono',monospace; font-size:10px; color:var(--text-4); text-align:center; }
.stk-filter-hint { font-family:'DM Mono',monospace; font-size:9px; letter-spacing:1px; color:var(--text-4); text-transform:uppercase; }
.stk-filter-quicks { display:flex; gap:5px; flex-wrap:wrap; }
.stk-quick {
  padding:5px 10px; font-family:'DM Mono',monospace; font-size:9px; letter-spacing:1.2px;
  color:var(--text-3); cursor:pointer; background:transparent;
  border:1px solid var(--border); border-radius:6px; text-transform:uppercase; transition:all .15s;
}
.stk-quick:hover { color:var(--text-1); border-color:var(--border-bright); }
.stk-quick.active { color:#FFF; background:rgba(255,31,61,0.18); border-color:var(--apt-red); }
.stk-filter-checkbox { display:flex; align-items:center; gap:8px; font-family:'DM Mono',monospace; font-size:11px; color:var(--text-2); cursor:pointer; }
.stk-filter-checkbox input { accent-color:var(--apt-red); width:14px; height:14px; cursor:pointer; }

@media (max-width:780px) {
  .stk-filter-row { grid-template-columns:1fr 1fr; gap:6px 10px; }
  .stk-filter-row .stk-filter-label { grid-column:1 / -1; }
  .stk-filter-sep { display:none; }
  .stk-filter-hint { grid-column:1 / -1; }
  .stk-filter-quicks { grid-column:1 / -1; margin-top:4px; }
}

.stk-table { background:rgba(17,18,26,0.55); border:1px solid var(--border); border-radius:14px; overflow-y:auto; max-height:calc(100vh - 220px); }
.stk-table::-webkit-scrollbar { width:8px; }
.stk-table::-webkit-scrollbar-thumb { background:var(--border-bright); border-radius:4px; }
.stk-head { display:grid; grid-template-columns:80px 1fr 180px 95px 75px 60px 70px 70px; gap:12px; padding:14px 22px; border-bottom:1px solid var(--border); background:rgba(10,10,15,0.92); backdrop-filter:blur(16px); -webkit-backdrop-filter:blur(16px); font-family:'DM Mono',monospace; font-size:10px; letter-spacing:1.5px; color:var(--text-3); text-transform:uppercase; position:sticky; top:0; z-index:3; }
.stk-th { cursor:pointer; user-select:none; transition:color .15s; }
.stk-th:nth-child(n+4) { text-align:right; }
.stk-th:hover { color:var(--text-1); }
.stk-th.asc::after { content:' \\2191'; color:var(--apt-rose); margin-left:4px; }
.stk-th.desc::after { content:' \\2193'; color:var(--apt-rose); margin-left:4px; }
.stk-row { display:grid; grid-template-columns:80px 1fr 180px 95px 75px 60px 70px 70px; gap:12px; padding:13px 22px; border-top:1px solid var(--border); align-items:start; transition:background .12s; }
.stk-row:hover { background:rgba(22,23,31,0.6); }
.stk-ticker { font-family:'Syne',sans-serif; font-size:14px; font-weight:700; color:var(--apt-rose); letter-spacing:0.02em; padding-top:1px; }
.stk-name { font-size:13px; color:var(--text-1); line-height:1.35; }
.stk-name .stk-sub { font-family:'DM Mono',monospace; font-size:9px; letter-spacing:1.5px; color:var(--text-4); text-transform:uppercase; margin-top:4px; font-weight:400; }
.stk-sector { font-family:'DM Mono',monospace; font-size:10px; letter-spacing:1.5px; color:var(--text-3); text-transform:uppercase; padding-top:1px; }
.stk-cap { font-family:'DM Mono',monospace; font-size:12px; color:var(--text-1); text-align:right; padding-top:1px; }
.stk-pct { font-family:'DM Mono',monospace; font-size:12px; text-align:right; padding-top:1px; }
.stk-pct.stk-pos { color:#34D27A; }
.stk-pct.stk-neg { color:var(--apt-red); }
.stk-pe { font-family:'DM Mono',monospace; font-size:12px; color:var(--text-3); text-align:right; padding-top:1px; }
.stk-score { font-family:'DM Mono',monospace; font-size:13px; font-weight:600; text-align:right; padding-top:1px; letter-spacing:0.02em; }
.stk-score-pos { color:#34D27A; }
.stk-score-neg { color:var(--apt-red); }
.stk-score-neutral { color:var(--text-2); }
.stk-score-na { color:var(--text-5); }
.stk-date { font-family:'DM Mono',monospace; font-size:10px; letter-spacing:1px; color:var(--text-3); text-align:right; padding-top:3px; }
.stk-date-dim { color:var(--text-4); }
.stk-row { cursor:pointer; }
.stk-row .stk-ticker { transition:color .15s; }
.stk-row:hover .stk-ticker { color:#FFB347; }

/* Expand-on-click factor panel */
.stk-detail { padding:14px 22px 22px; background:rgba(10,10,15,0.6); border-top:1px solid var(--border); animation:fpFadeIn .25s ease-out; }
@keyframes fpFadeIn { from { opacity:0; transform:translateY(-4px); } to { opacity:1; transform:translateY(0); } }

/* Score breakdown card (sits above the 4 factor cards) */
.sb-card { padding:16px 18px; background:rgba(17,18,26,0.85); border:1px solid var(--border); border-radius:10px; margin-bottom:14px; }
.sb-h { font-family:'DM Mono',monospace; font-size:10px; letter-spacing:2px; color:var(--text-3); text-transform:uppercase; margin-bottom:14px; padding-bottom:10px; border-bottom:1px solid var(--border); display:flex; align-items:baseline; gap:10px; }
.sb-h-sub { font-size:9px; letter-spacing:1.5px; color:var(--text-4); text-transform:none; font-style:italic; opacity:0.8; }
.sb-row { display:grid; grid-template-columns:90px 1fr 60px; align-items:center; gap:14px; padding:5px 0; }
.sb-label { font-family:'DM Mono',monospace; font-size:11px; letter-spacing:1px; color:var(--text-2); text-transform:capitalize; }
.sb-bar { position:relative; height:6px; background:rgba(255,255,255,0.04); border-radius:3px; overflow:hidden; }
.sb-bar-axis { position:absolute; left:50%; top:0; bottom:0; width:1px; background:rgba(255,255,255,0.18); z-index:2; }
.sb-bar-fill { position:absolute; top:0; bottom:0; border-radius:2px; z-index:1; transition:width .25s ease-out; }
.sb-bar-fill.sb-pos { background:linear-gradient(90deg, rgba(52,210,122,0.4), rgba(52,210,122,0.85)); }
.sb-bar-fill.sb-neg { background:linear-gradient(270deg, rgba(255,31,61,0.4), rgba(255,31,61,0.85)); }
.sb-val { font-family:'DM Mono',monospace; font-size:12px; text-align:right; font-weight:500; }
.sb-val-pos { color:#34D27A; }
.sb-val-neg { color:var(--apt-red); }
.sb-val-na { color:var(--text-5); }
.sb-comp-row { display:grid; grid-template-columns:90px 1fr; align-items:baseline; gap:14px; margin-top:14px; padding-top:14px; border-top:1px solid var(--border); }
.sb-comp-label { font-family:'DM Mono',monospace; font-size:11px; letter-spacing:2px; color:var(--text-3); text-transform:uppercase; }
.sb-comp { font-family:'Syne',sans-serif; font-size:32px; font-weight:800; letter-spacing:-0.02em; text-align:right; line-height:1; }
.sb-comp-pos { color:#34D27A; }
.sb-comp-neg { color:var(--apt-red); }
.sb-comp-na { color:var(--text-5); }

/* Benford's Law card (sits below the 4 factor cards in the expand panel) */
.bf-card { margin-top:14px; padding:16px 18px; background:rgba(17,18,26,0.85); border:1px solid var(--border); border-radius:10px; }
.bf-grid { display:grid; grid-template-columns:1fr 1fr; gap:18px; }
@media (max-width:780px) { .bf-grid { grid-template-columns:1fr; } }
.bf-sub { display:flex; flex-direction:column; }
.bf-sub-h { font-family:'DM Mono',monospace; font-size:10px; letter-spacing:1.5px; color:var(--text-3); text-transform:uppercase; margin-bottom:10px; padding-bottom:8px; border-bottom:1px solid var(--border); display:flex; align-items:baseline; gap:8px; flex-wrap:wrap; }
.bf-sub-meta { font-family:'DM Mono',monospace; font-size:9px; color:var(--text-4); margin-left:auto; letter-spacing:1px; text-transform:none; }
.bf-sub-meta sup { font-size:7px; vertical-align:super; }
.bf-h { font-family:'DM Mono',monospace; font-size:10px; letter-spacing:2px; color:var(--text-3); text-transform:uppercase; margin-bottom:14px; padding-bottom:10px; border-bottom:1px solid var(--border); display:flex; align-items:baseline; gap:10px; flex-wrap:wrap; }
.bf-fit { font-size:9px; font-weight:600; padding:2px 8px; border-radius:4px; letter-spacing:1.5px; }
.bf-fit-good { color:#34D27A; background:rgba(52,210,122,0.10); border:1px solid rgba(52,210,122,0.25); }
.bf-fit-fair { color:#FFB347; background:rgba(255,179,71,0.10); border:1px solid rgba(255,179,71,0.25); }
.bf-fit-poor { color:var(--apt-red); background:rgba(255,31,61,0.10); border:1px solid rgba(255,31,61,0.25); }
.bf-meta { font-family:'DM Mono',monospace; font-size:10px; color:var(--text-4); margin-left:auto; letter-spacing:1px; text-transform:none; }
.bf-meta sup { font-size:8px; vertical-align:super; }
.bf-row { display:grid; grid-template-columns:20px 1fr 52px 48px; align-items:center; gap:10px; padding:4px 0; }
.bf-d { font-family:'Syne',sans-serif; font-size:14px; font-weight:700; color:var(--text-2); text-align:center; }
.bf-bar { position:relative; height:8px; background:rgba(255,255,255,0.04); border-radius:3px; }
.bf-bar-fill { position:absolute; left:0; top:0; bottom:0; border-radius:3px; transition:background .2s; }
.bf-marker { position:absolute; top:-3px; bottom:-3px; width:2px; background:var(--text-2); opacity:0.65; }
.bf-obs { font-family:'DM Mono',monospace; font-size:12px; text-align:right; font-weight:500; transition:color .2s; }
.bf-exp { font-family:'DM Mono',monospace; font-size:10px; color:var(--text-4); }
/* Per-row deviation severity: green if within 10% of expected, amber 10-25%, red >25% */
.bf-row-close .bf-bar-fill    { background:linear-gradient(90deg, rgba(52,210,122,0.45), rgba(52,210,122,0.85)); }
.bf-row-close .bf-obs         { color:#34D27A; }
.bf-row-moderate .bf-bar-fill { background:linear-gradient(90deg, rgba(255,179,71,0.45), rgba(255,179,71,0.90)); }
.bf-row-moderate .bf-obs      { color:#FFB347; }
.bf-row-far .bf-bar-fill      { background:linear-gradient(90deg, rgba(255,122,133,0.55), rgba(255,31,61,0.95)); }
.bf-row-far .bf-obs           { color:var(--apt-red); }
.bf-foot { margin-top:14px; padding-top:12px; border-top:1px solid var(--border); font-family:'Inter',sans-serif; font-size:11px; color:var(--text-4); line-height:1.5; }
.bf-empty { padding:18px; text-align:center; font-family:'DM Mono',monospace; font-size:11px; color:var(--text-4); text-transform:uppercase; }

/* News card per ticker (lazy-loaded on row expand) */
.nws-card { margin-top:14px; padding:16px 18px; background:rgba(17,18,26,0.85); border:1px solid var(--border); border-radius:10px; }
.nws-h { font-family:'DM Mono',monospace; font-size:10px; letter-spacing:2px; color:var(--text-3); text-transform:uppercase; margin-bottom:14px; padding-bottom:10px; border-bottom:1px solid var(--border); display:flex; align-items:baseline; gap:10px; }
.nws-loading { font-size:9px; color:var(--text-5); text-transform:none; letter-spacing:1px; font-style:italic; }
.nws-grid { display:grid; grid-template-columns:1fr 1fr 1fr; gap:18px; }
@media (max-width:780px) { .nws-grid { grid-template-columns:1fr; } }
.nws-col { display:flex; flex-direction:column; }
.nws-col-h { font-family:'Syne',sans-serif; font-size:13px; font-weight:700; letter-spacing:0.02em; color:var(--text-2); margin-bottom:10px; padding-bottom:8px; border-bottom:1px solid var(--border); display:flex; align-items:baseline; gap:8px; }
.nws-count { font-family:'DM Mono',monospace; font-size:9px; color:var(--apt-rose); letter-spacing:1px; }
.nws-item { display:block; padding:10px 0; border-top:1px solid var(--border); transition:padding-left .12s; text-decoration:none; }
.nws-item:first-of-type { border-top:none; padding-top:4px; }
.nws-item:hover { padding-left:6px; }
.nws-title { font-size:13px; line-height:1.4; color:var(--text-1); }
.nws-meta { font-family:'DM Mono',monospace; font-size:9px; letter-spacing:1px; color:var(--text-4); text-transform:uppercase; margin-top:5px; }
.nws-item:hover .nws-title { color:var(--apt-rose); }
.nws-empty { padding:14px 0; font-family:'DM Mono',monospace; font-size:10px; color:var(--text-5); text-transform:uppercase; text-align:center; }

/* Per-bucket sentiment header (Loughran-McDonald + VADER) */
.nws-sent-row { display:flex; gap:14px; padding:8px 10px; margin-bottom:8px; background:rgba(10,10,15,0.5); border:1px solid var(--border); border-radius:6px; align-items:center; }
.nws-sent-cell { display:flex; align-items:baseline; gap:6px; flex:1; }
.nws-sent-label { font-family:'DM Mono',monospace; font-size:9px; letter-spacing:1.5px; color:var(--text-4); text-transform:uppercase; }
.nws-sent-val { font-family:'DM Mono',monospace; font-size:12px; font-weight:600; }
.nws-sent-pos { color:#34D27A; }
.nws-sent-neg { color:var(--apt-red); }
.nws-sent-neutral { color:var(--text-2); }
.nws-sent-na { color:var(--text-5); }

/* Stocks page top row: search + Index/Sector chips full-width above the wrap */
.stk-toprow { display:flex; flex-wrap:wrap; gap:10px 18px; align-items:center; padding:12px 16px; margin-bottom:12px; background:rgba(17,18,26,0.85); backdrop-filter:blur(20px); -webkit-backdrop-filter:blur(20px); border:1px solid var(--border); border-radius:14px; position:sticky; top:90px; z-index:4; }
.stk-toprow > .lib-search { flex:1 1 280px; min-width:220px; }

/* Stocks page sidebar layout: filters left, table right */
.stk-wrap { display:grid; grid-template-columns:300px 1fr; gap:20px; align-items:start; margin-top:6px; }
.stk-sidebar { position:sticky; top:210px; max-height:calc(100vh - 230px); overflow-y:auto; padding:18px 18px; background:rgba(17,18,26,0.55); backdrop-filter:blur(20px); -webkit-backdrop-filter:blur(20px); border:1px solid var(--border); border-radius:16px; display:flex; flex-direction:column; gap:14px; }
.stk-sidebar::-webkit-scrollbar { width:6px; }
.stk-sidebar::-webkit-scrollbar-thumb { background:var(--border-bright); border-radius:3px; }
.stk-sidebar-h { display:flex; align-items:baseline; justify-content:space-between; padding-bottom:10px; border-bottom:1px solid var(--border); font-family:'Syne',sans-serif; font-size:14px; font-weight:700; letter-spacing:0.04em; color:var(--text-1); text-transform:uppercase; }
.stk-sidebar-h .stk-filter-reset { padding:0; font-family:'DM Mono',monospace; font-size:9px; letter-spacing:1.5px; color:var(--text-3); background:transparent; border:none; cursor:pointer; text-transform:uppercase; }
.stk-sidebar-h .stk-filter-reset:hover { color:var(--apt-rose); }
.stk-main { min-width:0; }

/* Sidebar overrides for the existing filter HTML (drop toggle, always-visible panel, single-column inner stacking) */
.stk-sidebar .stk-filter-panel { display:flex; flex-direction:column; gap:8px; margin-top:0; padding:0; background:transparent; border:0; border-radius:0; }
.stk-sidebar .stk-filter-cols { display:flex; flex-direction:column; gap:14px; }
.stk-sidebar .stk-filter-col-h { font-family:'DM Mono',monospace; font-size:10px; letter-spacing:2px; color:var(--text-3); text-transform:uppercase; padding-bottom:8px; margin-bottom:4px; border-bottom:1px solid var(--border); display:block; }
.stk-sidebar .stk-filter-col-sub { display:none; }
.stk-sidebar .stk-filter-row { display:grid; grid-template-columns:1fr 1fr; column-gap:6px; row-gap:4px; padding:5px 0; align-items:center; }
.stk-sidebar .stk-filter-row > .stk-filter-label { grid-column:1 / -1; font-size:9px; }
.stk-sidebar .stk-filter-row > input.stk-filter-input[data-bound="min"] { grid-column:1; }
.stk-sidebar .stk-filter-row > input.stk-filter-input[data-bound="max"] { grid-column:2; }
.stk-sidebar .stk-filter-row > .stk-filter-sep { display:none; }
.stk-sidebar .stk-filter-row > .stk-filter-hint { grid-column:1 / -1; font-size:8px; }
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
.stk-filter-toggle-count { font-family:'DM Mono',monospace; font-size:9px; letter-spacing:1.5px; color:var(--apt-rose); text-transform:uppercase; padding:2px 0; }

@media (max-width:1080px) {
  .stk-wrap { grid-template-columns:1fr; }
  .stk-sidebar { position:static; max-height:none; overflow:visible; }
}
.fp-grid { display:grid; grid-template-columns:repeat(4, 1fr); gap:14px; }
@media (max-width:1000px) { .fp-grid { grid-template-columns:repeat(2, 1fr); } }
@media (max-width:560px) { .fp-grid { grid-template-columns:1fr; } }
.fp-card { padding:14px 16px; background:rgba(17,18,26,0.85); border:1px solid var(--border); border-radius:10px; }
.fp-card-h { font-family:'Syne',sans-serif; font-size:13px; font-weight:700; letter-spacing:0.02em; color:var(--text-1); margin-bottom:10px; padding-bottom:8px; border-bottom:1px solid var(--border); }
.fp-row { display:flex; justify-content:space-between; align-items:baseline; padding:5px 0; font-size:12px; }
.fp-label { color:var(--text-3); font-family:'DM Mono',monospace; font-size:10px; letter-spacing:1px; text-transform:uppercase; }
.fp-val { font-family:'DM Mono',monospace; font-size:12px; color:var(--text-1); font-weight:500; }
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
.footer .brand-foot { font-family:'Syne',sans-serif; font-size:12px; font-weight:800; letter-spacing:5px; color:var(--text-3); text-transform:uppercase; }
.footer .meta { font-family:'DM Mono',monospace; font-size:11px; color:var(--text-4); letter-spacing:1px; }

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
  const ALL = __STOCKS_JSON__;
  const SECTORS = __SECTORS_JSON__;
  const INDEXES = __INDEXES_JSON__;
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
    'revenue_acceleration', 'fcf_growth_yoy',
  ]);
  // Market-cap-coded fields use 1B / 300M / 5T suffixes
  const CAP_FIELDS = new Set(['market_cap', 'volume']);

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
    return c;
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
  // Op Margin Stability).
  const FACTOR_GROUPS = [
    {
      title: 'Growth',
      rows: [
        ['Revenue Growth YoY', 'revenue_growth_yoy', 'pct'],
        ['EPS Growth YoY (GAAP)', 'eps_growth_yoy', 'pct'],
        ['Revenue Acceleration', 'revenue_acceleration', 'pct'],
        ['Gross Margin Trend', 'gross_margin_trend', 'pct'],
        ['FCF Growth YoY', 'fcf_growth_yoy', 'pct'],
      ],
    },
    {
      title: 'Value',
      rows: [
        ['P/E (Trailing)', 'pe', 'ratio'],
        ['EV/EBITDA', 'ev_ebitda', 'ratio'],
        ['EV/Revenue', 'ev_revenue', 'ratio'],
        ['Price/Book', 'price_book', 'ratio'],
        ['FCF Yield', 'fcf_yield', 'pct'],
      ],
    },
    {
      title: 'Momentum',
      rows: [
        ['12-2 Month Return', 'return_12_2', 'pct'],
        ['1-Month Return', 'return_1m', 'pct'],
        ['52W High Proximity', 'high52w_proximity', 'pct'],
        ['Rel Strength vs S&P', 'rel_strength_sp500', 'pct'],
        ['Volume Trend', 'volume_trend', 'pct'],
      ],
    },
    {
      title: 'Quality',
      rows: [
        ['ROE (TTM)', 'roe_ttm', 'pct'],
        ['Earnings Consistency', 'earnings_consistency', 'ratio'],
        ['Net Debt/EBITDA', 'net_debt_ebitda', 'ratio'],
        ['Op Margin Stability', 'op_margin_stability', 'ratio'],
        ['Accruals Ratio', 'accruals_ratio', 'pct'],
      ],
    },
  ];

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
  const PEER_STATS = (function() {
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
  })();

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
      const items = g.rows.map(([label, key, type]) => {
        const val = s[key];
        const cls = (val == null || isNaN(val)) ? 'fp-row fp-row-na' : 'fp-row';
        return '<div class="'+cls+'"><span class="fp-label">'+escapeHtml(label)+'</span><span class="fp-val">'+fmtFactor(val, type)+'</span></div>';
      }).join('');
      return '<div class="fp-card"><div class="fp-card-h">'+g.title+'</div>'+items+'</div>';
    }).join('');
    const scoreCard = buildScoreBreakdown(s);
    const benfordCard = buildBenfordCard(s);
    // News card is a placeholder; populated lazily on expand via fetchNewsFor.
    const newsCard = '<div class="nws-card" id="nws-' + escapeHtml(s.ticker) + '">'
      + '<div class="nws-h">News <span class="nws-loading">loading…</span></div>'
      + '</div>';
    return '<div class="stk-detail">' + scoreCard + '<div class="fp-grid">'+groups+'</div>' + newsCard + benfordCard + '</div>';
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

  function render() {
    const q = query.toLowerCase();
    let filtered = ALL.filter(s => {
      if (activeSector && s.sector !== activeSector) return false;
      if (activeIndex && s.index !== activeIndex) return false;
      if (!passesFilters(s)) return false;
      if (!q) return true;
      return ((s.ticker||'')+' '+(s.name||'')+' '+(s.sector||'')+' '+(s.sub_industry||'')).toLowerCase().includes(q);
    });
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
    countEl.textContent = filtered.length === ALL.length
      ? String(ALL.length) + ' stocks'
      : String(filtered.length) + ' of ' + String(ALL.length) + ' stocks';
    if (filtered.length === 0) {
      listEl.innerHTML = '<div class="empty-state">No matches. Adjust filters or clear search.</div>';
      return;
    }
    const rows = filtered.slice(0, 5000).map(s => {
      const chgClass = (s.change_pct != null && Number(s.change_pct) < 0) ? 'stk-neg' : 'stk-pos';
      const isOpen = expanded.has(s.ticker);
      const arrow = isOpen ? '▾' : '▸';
      const detailHtml = isOpen ? buildDetail(s) : '';
      const composite = computeComposite(s);
      return '<div class="stk-row" data-ticker="'+escapeHtml(s.ticker)+'">'
        + '<div class="stk-ticker">'+arrow+' '+escapeHtml(s.ticker||'')+'</div>'
        + '<div class="stk-name">'+escapeHtml(s.name||'')+'<div class="stk-sub">'+escapeHtml(s.sub_industry||'')+'</div></div>'
        + '<div class="stk-sector">'+escapeHtml(s.sector||'')+'</div>'
        + '<div class="stk-cap">'+fmtCap(s.market_cap)+'</div>'
        + '<div class="stk-pct '+chgClass+'">'+fmtPct(s.change_pct)+'</div>'
        + '<div class="stk-score '+scoreClass(composite)+'">'+fmtScore(composite)+'</div>'
        + '<div class="stk-date">'+fmtDateMDY(s.earnings_date)+'</div>'
        + '<div class="stk-date stk-date-dim">'+fmtDate(s.last_updated)+'</div>'
      + '</div>'
      + detailHtml;
    }).join('');
    listEl.innerHTML = rows;
  }

  // Click on a row toggles the expand state, and on first expand kicks off
  // the lazy news fetch for that ticker.
  listEl.addEventListener('click', e => {
    const row = e.target.closest('.stk-row');
    if (!row) return;
    const t = row.dataset.ticker;
    if (!t) return;
    const wasOpen = expanded.has(t);
    if (wasOpen) expanded.delete(t); else expanded.add(t);
    render();
    if (!wasOpen) fetchNewsFor(t);
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
  const filterReset = document.getElementById('stk-filter-reset');
  const onlyEnrichedEl = document.getElementById('stk-only-enriched');

  function syncFilterCount() {
    const c = activeFilterCount();
    if (c > 0) {
      filterCountEl.hidden = false;
      filterCountEl.textContent = '(' + c + ' active)';
      filterReset.hidden = false;
    } else {
      filterCountEl.hidden = true;
      filterReset.hidden = true;
    }
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
      document.querySelectorAll('.stk-filter-input').forEach(i => i.value = '');
      document.querySelectorAll('.stk-quick').forEach(b => b.classList.remove('active'));
      // Also reset weights to balanced
      ['Growth', 'Value', 'Momentum', 'Quality'].forEach(d => { weights[d] = 1; });
      document.querySelectorAll('.stk-weight-slider').forEach(sl => {
        sl.value = '1';
        const lbl = document.getElementById('stk-w-' + sl.dataset.weight);
        if (lbl) lbl.innerHTML = '1.0&times;';
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

  render();
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
  </div>
</nav>'''


def render_footer():
    return '''<footer class="footer">
  <div style="display:flex;flex-direction:column;gap:6px">
    <span class="brand-foot">Apterreon</span>
    <span style="font-family:'DM Mono',monospace;font-size:11px;letter-spacing:1.5px;color:var(--apt-rose)">Explore what's out there.</span>
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
<meta name="color-scheme" content="dark">
<meta name="theme-color" content="#0A0A0F">
<link rel="manifest" href="manifest.json">
<title>{title}</title>
<link href="https://fonts.googleapis.com/css2?family=Syne:wght@600;700;800&family=Inter:wght@300;400;500;600;700&family=DM+Mono:wght@300;400;500&display=swap" rel="stylesheet">
<style>
{SITE_CSS}
</style>
</head>
<body>

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

RECENT_TRENDS_PROMPT = """You are an analyst summarizing the past several days of an intelligence brief.

You will receive a chronological list of brief synthesis lines and top headlines. Identify the dominant shifts, the recurring threads, and the structural narratives that emerged across the period.

Return ONLY valid JSON (no markdown fences, no preamble):
{
  "snapshot": [
    "Bullet 1: a single-sentence shift or narrative across the period. Specific. References actual events.",
    "Bullet 2: a different shift or narrative.",
    "Bullet 3: a third shift or narrative."
  ],
  "themes": ["short phrase", "short phrase", "..."]
}

RULES:
- snapshot: EXACTLY 3 bullets. Each is one sentence, 20 to 35 words. No filler, no hedging. Each must stand alone (the reader scans these in 5 seconds).
- themes: 3 to 5 short phrases, 4 to 8 words each, capturing recurring threads orthogonally to the snapshot bullets.
- NEVER use em dashes (the long dash character). Use periods, commas, or colons instead.
- Output ONLY the JSON object. No commentary."""


def _parse_json_strict(text):
    """Tolerant JSON parse: strip code fences, trim to outermost braces, drop em dashes, then loads."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[-1]
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3]
    cleaned = cleaned.strip()
    start = cleaned.find("{")
    if start != -1:
        depth = 0
        end = len(cleaned)
        for i in range(start, len(cleaned)):
            if cleaned[i] == "{":
                depth += 1
            elif cleaned[i] == "}":
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        cleaned = cleaned[start:end]
    cleaned = cleaned.replace(" — ", ", ").replace("—", ",")
    return json.loads(cleaned)


def get_or_generate_recent_trends(briefs):
    """Daily-cached snapshot of the past ~10 calendar days of briefs as 3 scannable
    bullets plus 3-5 recurring themes. Returns dict with 'date', 'snapshot' (list of
    bullet strings), 'themes' (list of phrase strings). Falls back gracefully on
    any error."""
    today = datetime.now(timezone(ET_OFFSET)).strftime("%Y-%m-%d")
    cache_path = STATE_DIR / "recent_trends.json"

    if cache_path.exists():
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            if cached.get("date") == today and cached.get("snapshot"):
                return cached
        except Exception as e:
            print(f"recent_trends: cache read error: {e}")

    # Fallback: derive bullets from the latest few the_edge synthesis lines
    fallback_bullets = []
    for b in briefs[:3]:
        edge = (b.get("the_edge") or "").strip()
        if edge:
            fallback_bullets.append(edge)
    if not fallback_bullets:
        fallback_bullets = ["Recent trends will appear here once briefs accumulate."]
    fallback = {"date": today, "snapshot": fallback_bullets[:3], "themes": []}

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("recent_trends: no API key, using fallback (latest the_edge bullets).")
        return fallback

    by_date = {}
    for b in briefs:
        d = b.get("date", "")
        if d:
            by_date.setdefault(d, []).append(b)
    recent_dates = sorted(by_date.keys(), reverse=True)[:10]
    if not recent_dates:
        return fallback

    lines = []
    for d in recent_dates:
        for b in by_date[d]:
            ed = b.get("type", "")
            edge = (b.get("the_edge") or "").strip()
            lines.append(f"=== {d} {ed} ===")
            if edge:
                lines.append(f"Edge: {edge}")
            for sec in b.get("sections", [])[:7]:
                sec_name = sec.get("name", "")
                for st in sec.get("stories", [])[:2]:
                    h = (st.get("headline") or "").strip()
                    if h:
                        lines.append(f"- {sec_name}: {h}")
            lines.append("")
    user_input = "\n".join(lines)[:18000]

    try:
        text, usage = call_claude(RECENT_TRENDS_PROMPT, user_input)
        parsed = _parse_json_strict(text)
        snapshot_raw = parsed.get("snapshot") or []
        if not isinstance(snapshot_raw, list):
            snapshot_raw = []
        snapshot = [str(b).strip().replace(" — ", ", ").replace("—", ",") for b in snapshot_raw if str(b).strip()][:3]
        themes = parsed.get("themes") or []
        if not isinstance(themes, list):
            themes = []
        themes = [str(t).strip() for t in themes if str(t).strip()][:5]
        if not snapshot:
            print("recent_trends: empty snapshot from Claude, using fallback.")
            return fallback
        result = {
            "date": today,
            "snapshot": snapshot,
            "themes": themes,
            "usage": usage,
        }
        cache_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
        print(f"recent_trends: regenerated for {today} ({len(snapshot)} bullets, {len(themes)} themes, ${usage.get('cost_this_call', 0):.4f}).")
        return result
    except Exception as e:
        print(f"recent_trends: generation failed: {e}")
        return fallback


# ── Wikipedia constituent scraper ───────────────────────────────────────────

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

ISHARES_SOURCES = [
    {
        "url": "https://www.ishares.com/us/products/239707/ishares-russell-1000-etf/1467271812596.ajax?fileType=csv&fileName=IWB_holdings&dataType=fund",
        "label": "Russell 1000",
    },
    {
        "url": "https://www.ishares.com/us/products/239710/ishares-russell-2000-etf/1467271812596.ajax?fileType=csv&fileName=IWM_holdings&dataType=fund",
        "label": "Russell 2000",
    },
]


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
        print(f"iShares parse failed for {label}: no Ticker header row.")
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


def fetch_all_universes():
    """Build the full deduplicated stock universe from Wikipedia (S&P 500/400/600)
    plus iShares (Russell 1000/2000). S&P sources go first because their sector
    classification is cleaner, then Russell fills in everything else.
    First-occurrence-by-ticker wins."""
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

def enrich_with_yfinance(stocks, max_workers=10):
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

    def fetch_one(sym):
        # Yahoo uses '-' for class shares (BRK-B); Wikipedia uses '.' (BRK.B). Translate.
        yf_sym = sym.replace(".", "-")
        try:
            return sym, yf.Ticker(yf_sym).info
        except Exception:
            return sym, None

    enriched = 0
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
            ni = info.get("netIncomeToCommon")
            cfo = info.get("operatingCashflow")
            ta = info.get("totalAssets")
            if ni is not None and cfo is not None and ta and ta > 0:
                ar = (ni - cfo) / ta
                if -1 < ar < 1:
                    s["accruals_ratio"] = ar
            op_m = info.get("operatingMargins")
            if op_m is not None and -2 < op_m < 2:
                s["operating_margin"] = op_m
            gm = info.get("grossMargins")
            if gm is not None and -2 < gm < 2:
                s["gross_margin"] = gm

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
                s["last_updated"] = datetime.now(timezone(ET_OFFSET)).strftime("%Y-%m-%d")

    elapsed = time.time() - t0
    print(f"yfinance: enriched {enriched}/{len(tickers)} tickers in {elapsed:.1f}s ({max_workers} threads).")
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


def fetch_company_news(ticker, name="", max_items=15):
    """Pull recent news for a ticker from Google News RSS. Returns list of
    {title, source, link, ts (unix int)}. Empty list on any failure."""
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
        return []
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
    whose existing file is younger than max_age_hours (so midday + evening workflow
    runs reuse morning's news without re-hitting Google). Returns count fetched."""
    if not stocks:
        return 0
    NEWS_DIR.mkdir(parents=True, exist_ok=True)
    now_ts = time.time()

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
        return (now_ts - f.stat().st_mtime) / 3600 > max_age_hours

    todo = [s for s in stocks if needs_fetch(s["ticker"])]
    skipped = len(stocks) - len(todo)
    if not todo:
        print(f"news: all {len(stocks)} ticker files within {max_age_hours}h, skipping fetch.")
        return 0

    from concurrent.futures import ThreadPoolExecutor, as_completed

    def process(s):
        try:
            items = fetch_company_news(s["ticker"], s.get("name", ""))
            (NEWS_DIR / _news_filename(s["ticker"])).write_text(
                json.dumps(items, separators=(",", ":")), encoding="utf-8"
            )
            return s["ticker"], len(items)
        except Exception:
            return s["ticker"], 0

    fetched = 0
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = [ex.submit(process, s) for s in todo]
        for f in as_completed(futures):
            sym, n = f.result()
            if n > 0:
                fetched += 1
    elapsed = time.time() - t0
    print(f"news: wrote {fetched}/{len(todo)} ticker files in {elapsed:.1f}s ({skipped} cached < {max_age_hours}h).")
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
    """Fetch full XBRL facts for one CIK. Returns the 'facts' dict or None."""
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
    if chi_sq_d1 < 13.36:
        fit = "good"
    elif chi_sq_d1 < 20.09:
        fit = "fair"
    else:
        fit = "poor"
    result = {
        "observed": observed_d1,
        "chi_sq": round(chi_sq_d1, 1),
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
        if chi_sq_d2 < 14.68:
            fit_d2 = "good"
        elif chi_sq_d2 < 21.67:
            fit_d2 = "fair"
        else:
            fit_d2 = "poor"
        result["observed_d2"] = observed_d2
        result["chi_sq_d2"] = round(chi_sq_d2, 1)
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

    out = {}

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
    # We report raw stddev so users see the dispersion directly; smaller is better.
    if revenues and op_inc:
        rev_by_end = {r["end"]: r["val"] for r in revenues if r.get("val")}
        op_by_end = {r["end"]: r["val"] for r in op_inc if r.get("val") is not None}
        common = sorted(set(rev_by_end) & set(op_by_end), reverse=True)
        margins = []
        for d in common[:8]:
            if rev_by_end[d] > 0:
                margins.append(op_by_end[d] / rev_by_end[d])
        if len(margins) >= 4:
            mean_m = sum(margins) / len(margins)
            variance = sum((m - mean_m) ** 2 for m in margins) / len(margins)
            stddev = variance ** 0.5
            if 0 <= stddev < 1:
                out["op_margin_stability"] = stddev

    return out


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

    today_str = datetime.now(timezone(ET_OFFSET)).strftime("%Y-%m-%d")

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

def get_or_generate_stocks_universe():
    """Cached US stocks universe scraped from Wikipedia (S&P 500/400/600), enriched
    with live quote data from Yahoo Finance via yfinance.

    Cache strategy: skip the full refresh ONLY if the cache is very fresh (< 4 hours)
    AND in the same ISO week. Otherwise: re-pull Wikipedia (fast, free), merge any
    previous static enrichment (market_cap, pe) as a fallback layer, then attempt a
    fresh yfinance pass. Yahoo rate-limits aggressively so a single run rarely covers
    100% of 1500 names; subsequent runs accumulate coverage."""
    now = datetime.now(timezone(ET_OFFSET))
    iso_year, iso_week, _ = now.isocalendar()
    week_key = f"{iso_year}-W{iso_week:02d}"
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
    if last_known and last_known.get("iso_week") == week_key:
        try:
            last_dt = datetime.fromisoformat(last_known.get("generated_at", ""))
            age_hours = (now - last_dt).total_seconds() / 3600
            if age_hours < 4:
                print(f"stocks_universe: using cache from {age_hours:.1f}h ago.")
                # News has its own 12h per-file cache; this call is a no-op for
                # tickers already cached and just fills any holes.
                enrich_with_news(last_known.get("stocks") or [])
                return last_known
        except Exception:
            pass

    # Build fresh universe from Wikipedia (S&P 500/400/600) + iShares (Russell 1000/2000)
    stocks = fetch_all_universes()
    if not stocks:
        print("stocks_universe: Wikipedia + iShares returned nothing, falling back to last cache.")
        return last_known or {"iso_week": week_key, "generated_at": now.isoformat(), "stocks": []}

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
        "earnings_consistency", "op_margin_stability", "edgar_updated",
        "benford",
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
    should_run_edgar = True
    if last_known and last_known.get("stocks"):
        recent_edgar = next((s for s in last_known["stocks"] if s.get("edgar_updated")), None)
        if recent_edgar:
            try:
                edgar_dt = datetime.fromisoformat(recent_edgar["edgar_updated"]).date()
                edgar_iso_year, edgar_iso_week, _ = edgar_dt.isocalendar()
                if edgar_iso_year == iso_year and edgar_iso_week == iso_week:
                    should_run_edgar = False
                    print(f"EDGAR: cache stamped {recent_edgar['edgar_updated']} (this week), skipping refresh.")
            except Exception:
                pass
    edgar_count = 0
    if should_run_edgar:
        cik_map = fetch_edgar_ticker_cik_map()
        if cik_map:
            edgar_count = enrich_with_edgar(stocks, cik_map)

    # Per-ticker news fetched once a day (12h cache) so midday/evening workflow
    # runs reuse morning's pull. Writes one small JSON per ticker, lazy-loaded
    # by the page on row expand.
    news_count = enrich_with_news(stocks)

    total_with_cap = sum(1 for s in stocks if s.get("market_cap"))
    total_with_price = sum(1 for s in stocks if s.get("price"))
    total_with_edgar = sum(1 for s in stocks if s.get("edgar_updated"))

    result = {
        "iso_week": week_key,
        "generated_at": now.isoformat(),
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
    print(f"stocks_universe: regenerated for {week_key} ({len(stocks)} stocks; {fresh_count} fresh yfinance, {edgar_count} fresh EDGAR, {news_count} news pulls; coverage: {pct_cap:.0f}% market_cap, {pct_price:.0f}% price, {pct_edgar:.0f}% EDGAR).")
    return result



# ── Per-page generators ─────────────────────────────────────────────────────

def _hero_eyebrow_text(briefs):
    now_et = datetime.now(timezone(ET_OFFSET))
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
    now_et = datetime.now(timezone(ET_OFFSET))
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


def generate_stocks_page(universe):
    """Write docs/stocks.html: filterable table of US stocks scraped from Wikipedia
    (S&P 500/400/600), optionally enriched with live FMP quote data."""
    stocks = universe.get("stocks", []) or []
    iso_week = universe.get("iso_week", "")
    enriched = universe.get("enriched", False)
    source = universe.get("source", "wikipedia")

    sectors = sorted({(s.get("sector") or "").strip() for s in stocks if (s.get("sector") or "").strip()})
    indexes = []
    seen_idx = set()
    for s in stocks:
        idx = (s.get("index") or "").strip()
        if idx and idx not in seen_idx:
            seen_idx.add(idx)
            indexes.append(idx)

    stocks_json = json.dumps(stocks, separators=(",", ":"))
    sectors_json = json.dumps(sectors)
    indexes_json = json.dumps(indexes)

    enrich_note = "Live price, market cap, 1d %, and P/E from Yahoo Finance. " if enriched else ""
    meta_line = f"Updated for {iso_week} · Source: {source}" if iso_week else f"Source: {source}"

    body = f"""
<section class="lib lib-wide">
  <div class="lib-h">
    <h2>Stocks.</h2>
    <span class="lib-count" id="stk-count">{len(stocks)} stocks</span>
  </div>
  <div class="stk-toprow">
    <label class="lib-search">
      <span class="icon">&#8981;</span>
      <input type="search" id="stk-search" placeholder="Ticker or company name..." autocomplete="off" spellcheck="false">
      <button type="button" class="clear-btn" id="stk-clear" hidden>Clear</button>
    </label>
    <div class="lib-chips" id="stk-index-chips">
      <span class="lib-chip-label">Index</span>
      <span class="lib-chip active" data-index="">All</span>
    </div>
    <div class="lib-chips" id="stk-sector-chips">
      <span class="lib-chip-label">Sector</span>
      <span class="lib-chip active" data-sector="">All</span>
    </div>
  </div>
  <div class="stk-wrap">
    <aside class="stk-sidebar">
      <div class="stk-sidebar-h">
        <span>Advanced</span>
        <button type="button" class="stk-filter-reset" id="stk-filter-reset" hidden>Reset</button>
      </div>
      <span class="stk-filter-toggle-count" id="stk-filter-count" hidden></span>
    <div class="stk-filter-panel" id="stk-filter-panel">
      <div class="stk-filter-cols">
        <div class="stk-filter-col">
          <div class="stk-filter-col-h">Characteristics</div>
          <div class="stk-filter-row">
            <span class="stk-filter-label">Market Cap</span>
            <input type="text" class="stk-filter-input" data-filter="market_cap" data-bound="min" placeholder="min (e.g. 300M)">
            <span class="stk-filter-sep">to</span>
            <input type="text" class="stk-filter-input" data-filter="market_cap" data-bound="max" placeholder="max (e.g. 10B)">
            <span class="stk-filter-quicks">
              <button type="button" class="stk-quick" data-tier="micro">Micro</button>
              <button type="button" class="stk-quick" data-tier="small">Small</button>
              <button type="button" class="stk-quick" data-tier="mid">Mid</button>
              <button type="button" class="stk-quick" data-tier="large">Large</button>
              <button type="button" class="stk-quick" data-tier="mega">Mega</button>
            </span>
          </div>
          <div class="stk-filter-row">
            <span class="stk-filter-label">P/E (Trailing)</span>
            <input type="text" class="stk-filter-input" data-filter="pe" data-bound="min" placeholder="min">
            <span class="stk-filter-sep">to</span>
            <input type="text" class="stk-filter-input" data-filter="pe" data-bound="max" placeholder="max (e.g. 30)">
            <span class="stk-filter-hint">absolute multiple</span>
          </div>
          <div class="stk-filter-row">
            <span class="stk-filter-label">Revenue Growth YoY</span>
            <input type="text" class="stk-filter-input" data-filter="revenue_growth_yoy" data-bound="min" placeholder="min % (e.g. 10)">
            <span class="stk-filter-sep">to</span>
            <input type="text" class="stk-filter-input" data-filter="revenue_growth_yoy" data-bound="max" placeholder="max %">
            <span class="stk-filter-hint">as percent</span>
          </div>
          <div class="stk-filter-row">
            <span class="stk-filter-label">52W High Proximity</span>
            <input type="text" class="stk-filter-input" data-filter="high52w_proximity" data-bound="min" placeholder="min % (e.g. -30)">
            <span class="stk-filter-sep">to</span>
            <input type="text" class="stk-filter-input" data-filter="high52w_proximity" data-bound="max" placeholder="max % (e.g. -5)">
            <span class="stk-filter-hint">always &le; 0</span>
          </div>
          <div class="stk-filter-row">
            <span class="stk-filter-label">ROE (TTM)</span>
            <input type="text" class="stk-filter-input" data-filter="roe_ttm" data-bound="min" placeholder="min % (e.g. 15)">
            <span class="stk-filter-sep">to</span>
            <input type="text" class="stk-filter-input" data-filter="roe_ttm" data-bound="max" placeholder="max %">
            <span class="stk-filter-hint">as percent</span>
          </div>
          <div class="stk-filter-row">
            <span class="stk-filter-label">FCF Yield</span>
            <input type="text" class="stk-filter-input" data-filter="fcf_yield" data-bound="min" placeholder="min % (e.g. 5)">
            <span class="stk-filter-sep">to</span>
            <input type="text" class="stk-filter-input" data-filter="fcf_yield" data-bound="max" placeholder="max %">
            <span class="stk-filter-hint">as percent</span>
          </div>
          <div class="stk-filter-row">
            <span class="stk-filter-label">Net Debt/EBITDA</span>
            <input type="text" class="stk-filter-input" data-filter="net_debt_ebitda" data-bound="min" placeholder="min (e.g. -1)">
            <span class="stk-filter-sep">to</span>
            <input type="text" class="stk-filter-input" data-filter="net_debt_ebitda" data-bound="max" placeholder="max (e.g. 3)">
            <span class="stk-filter-hint">ratio</span>
          </div>
          <div class="stk-filter-row stk-filter-row-toggle">
            <label class="stk-filter-checkbox">
              <input type="checkbox" id="stk-only-enriched">
              <span>Hide stocks without live market cap data</span>
            </label>
          </div>
        </div>

        <div class="stk-filter-col">
          <div class="stk-filter-col-h">Dimension Weights <span class="stk-filter-col-sub">how much each factor group counts in the Score</span></div>
          <div class="stk-weight-row">
            <span class="stk-weight-label">Growth</span>
            <input type="range" class="stk-weight-slider" data-weight="Growth" min="0" max="2" step="0.1" value="1">
            <span class="stk-weight-val" id="stk-w-Growth">1.0&times;</span>
          </div>
          <div class="stk-weight-row">
            <span class="stk-weight-label">Value</span>
            <input type="range" class="stk-weight-slider" data-weight="Value" min="0" max="2" step="0.1" value="1">
            <span class="stk-weight-val" id="stk-w-Value">1.0&times;</span>
          </div>
          <div class="stk-weight-row">
            <span class="stk-weight-label">Momentum</span>
            <input type="range" class="stk-weight-slider" data-weight="Momentum" min="0" max="2" step="0.1" value="1">
            <span class="stk-weight-val" id="stk-w-Momentum">1.0&times;</span>
          </div>
          <div class="stk-weight-row">
            <span class="stk-weight-label">Quality</span>
            <input type="range" class="stk-weight-slider" data-weight="Quality" min="0" max="2" step="0.1" value="1">
            <span class="stk-weight-val" id="stk-w-Quality">1.0&times;</span>
          </div>
          <div class="stk-weight-presets">
            <span class="stk-weight-presets-label">Presets</span>
            <button type="button" class="stk-quick" data-preset="balanced">Balanced</button>
            <button type="button" class="stk-quick" data-preset="value">Value tilt</button>
            <button type="button" class="stk-quick" data-preset="growth">Growth tilt</button>
            <button type="button" class="stk-quick" data-preset="quality">Quality tilt</button>
            <button type="button" class="stk-quick" data-preset="momentum">Momentum tilt</button>
          </div>
        </div>
      </div>
    </div>
    </aside>
    <main class="stk-main">
    <div class="stk-table">
    <div class="stk-head">
      <div class="stk-th" data-sort="ticker">Ticker</div>
      <div class="stk-th" data-sort="name">Name</div>
      <div class="stk-th" data-sort="sector">Sector</div>
      <div class="stk-th desc" data-sort="market_cap">Mkt Cap</div>
      <div class="stk-th" data-sort="change_pct">1d %</div>
      <div class="stk-th" data-sort="__score__">Score</div>
      <div class="stk-th" data-sort="earnings_date">Earnings</div>
      <div class="stk-th" data-sort="last_updated">Updated</div>
    </div>
    <div id="stk-list"></div>
  </div>
  <div class="picks-meta" style="margin-top:18px">{meta_line}</div>
    </main>
  </div>
</section>
"""
    stocks_js = (STOCKS_JS_TEMPLATE
                 .replace("__STOCKS_JSON__", stocks_json)
                 .replace("__SECTORS_JSON__", sectors_json)
                 .replace("__INDEXES_JSON__", indexes_json))
    html = render_page("Stocks, Apterreon", body, active_nav="stocks", extra_scripts=stocks_js)
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

    brief_type = event.get("brief_type", "morning")

    now_et = datetime.now(timezone(ET_OFFSET))
    day_of_week = now_et.weekday()  # 0=Mon, 5=Sat, 6=Sun
    is_weekend = day_of_week >= 5

    # Weekends: only send the morning brief
    if is_weekend and brief_type != "morning":
        print(f"Weekend, skipping {brief_type} brief.")
        return {"status": "skipped_weekend", "brief_type": brief_type}

    # On weekends, relabel as "Weekend Brief"
    config = get_brief_config(brief_type)
    if is_weekend:
        config["subject_prefix"] = "Weekend Brief"

    date_str = now_et.strftime("%A, %B %d")
    timestamp = now_et.strftime("%I:%M %p ET")

    # 1. Fetch market data
    print("Fetching market data...")
    quotes = fetch_market_data()

    # 2. Fetch headlines
    print(f"Fetching RSS headlines for {brief_type} brief...")
    headlines = fetch_rss_headlines(max_per_feed=config["max_per_feed"], brief_type=brief_type)

    if not headlines:
        print("No headlines fetched. Sending fallback.")
        subject = f"{config['subject_prefix']} \u00b7 {date_str}"
        send_email(subject, "<p>No headlines could be retrieved. RSS feeds may be temporarily unavailable.</p>")
        return {"status": "sent_fallback"}

    # 3. Group headlines by section
    headlines_text = f"Today is {date_str}. Brief type: {brief_type}.\n\n"
    for section_name, categories in SECTIONS:
        section_items = [h for h in headlines if h["category"] in categories]
        if section_items:
            headlines_text += f"=== {section_name} ===\n"
            for h in section_items:
                headlines_text += f"- {h['title']} (Source: {h['source']}, Category: {h['category']}, Link: {h['link']})\n"
            headlines_text += "\n"

    if quotes:
        headlines_text += "=== Market Data ===\n"
        for q in quotes:
            headlines_text += f"- {q['label']} ({q['ticker']}): ${q['price']}, change: {q['change_pct']}%\n"

    # 4. Generate analysis
    print(f"Calling Claude ({ANTHROPIC_MODEL}) for analysis...")
    raw_response, usage_info = call_claude(config["system_prompt"], headlines_text)

    # Parse JSON:extract the object even if the model adds commentary or truncates.
    cleaned = raw_response.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[-1]
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3]
    cleaned = cleaned.strip()

    # Find the JSON object by matching braces. If depth never closes (truncation),
    # take everything from the first { to the end so json.loads gives a useful error
    #:not an empty string.
    start = cleaned.find("{")
    if start != -1:
        depth = 0
        end = len(cleaned)  # default: full remainder, not start (avoid empty slice on truncation)
        for i in range(start, len(cleaned)):
            if cleaned[i] == "{":
                depth += 1
            elif cleaned[i] == "}":
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        cleaned = cleaned[start:end]

    # Brand voice forbids em dashes (AI-writing tell). Strip them defensively
    # in case the model ignored the instruction in the prompt.
    cleaned = cleaned.replace(" \u2014 ", ", ").replace("\u2014", ",")

    data = None
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as e:
        # Try one repair pass: close any unterminated string and balance braces/brackets.
        repaired = cleaned
        # If the last quote is unmatched, append a closing quote.
        if repaired.count('"') % 2 == 1:
            repaired += '"'
        # Trim a trailing comma that often appears mid-truncation.
        repaired = re.sub(r',\s*$', '', repaired)
        # Balance brackets and braces (close in correct order using a stack walk).
        stack = []
        for ch in repaired:
            if ch in '{[':
                stack.append(ch)
            elif ch == '}' and stack and stack[-1] == '{':
                stack.pop()
            elif ch == ']' and stack and stack[-1] == '[':
                stack.pop()
        for opener in reversed(stack):
            repaired += '}' if opener == '{' else ']'
        try:
            data = json.loads(repaired)
            print(f"JSON repaired after truncation (added {len(repaired) - len(cleaned)} chars).")
        except json.JSONDecodeError as e2:
            print(f"JSON parse error (unrecoverable): {e2}")
            print(f"Raw[:1000]: {raw_response[:1000]}")
            subject = f"{config['subject_prefix']}, {date_str}, Generation Error"
            fallback = (
                f"<div style='font-family:-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;"
                f"max-width:600px;margin:0 auto;padding:32px 24px;background:#0D0F18;color:#E0E8F0'>"
                f"<h1 style='font-size:18px;font-weight:700;margin:0 0 12px'>Brief generation failed</h1>"
                f"<p style='font-size:13px;color:#7A8A9A;line-height:1.6;margin:0 0 16px'>"
                f"The model response could not be parsed. The next scheduled brief will retry automatically."
                f"</p>"
                f"<p style='font-size:11px;color:#6A7888;margin:0'>Error: {e2}</p>"
                f"</div>"
            )
            send_email(subject, fallback)
            return {"status": "sent_fallback_parse_error", "error": str(e2)}

    # 5. Build all views
    title = f"{config['subject_prefix']} \u00b7 {date_str}"
    site_url = os.environ.get("APTERREON_SITE_URL", "https://ctlsmith5689.github.io/daily-intelligence-brief")
    date_iso_for_url = now_et.strftime("%Y-%m-%d")
    brief_url = f"{site_url}/briefs/{date_iso_for_url}-{brief_type}.html"
    email_html = build_email_preview(title, data, quotes, timestamp, usage_info, brief_url=brief_url, site_url=site_url)
    interactive_html = build_interactive_html(title, data, quotes, timestamp, usage_info)

    # 6. Send email (preview only, no attachment for minimal traceability)
    send_email(title, email_html)

    # 7. Publish brief HTML + JSON sidecar, regenerate site index
    s3_publish_brief(brief_type, now_et, interactive_html, data=data, quotes=quotes, timestamp=timestamp)

    return {"status": "sent", "brief_type": brief_type, "stories": len(headlines), "quotes": len(quotes), "usage": usage_info}


if __name__ == "__main__":
    import sys
    brief_type = sys.argv[1] if len(sys.argv) > 1 else "morning"
    result = lambda_handler({"brief_type": brief_type}, None)
    print(json.dumps(result, indent=2))
