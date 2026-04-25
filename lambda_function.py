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


def s3_generate_index(briefs):
    """Generate docs/index.html (and manifest.json) for the Apterreon home page.
    v6 design (modern landing): floating pill topnav, gradient hero, Daily Pick panel
    sourced from the latest brief's the_edge synthesis, section grid, live feed.
    """
    now_et = datetime.now(timezone(ET_OFFSET))
    today_str = now_et.strftime("%Y-%m-%d")
    site_url = os.environ.get("APTERREON_SITE_URL", "https://ctlsmith5689.github.io/daily-intelligence-brief")

    logo_nav = apt_logo_svg(22, 29, 0.45)

    # ── Latest brief (drives Daily Pick + Section grid + Live feed) ──
    latest = briefs[0] if briefs else None
    edge_text = (latest or {}).get("the_edge", "") or ""
    latest_date = (latest or {}).get("date", today_str)
    latest_type = (latest or {}).get("type", "morning")
    latest_key = (latest or {}).get("key", "")
    latest_url = f"{site_url}/{latest_key}" if latest_key else "#"

    # Pretty edition + date for the meta line
    edition_label = {"morning": "Morning", "midday": "Midday", "evening": "Evening"}.get(latest_type, latest_type.title())
    try:
        _d = datetime.strptime(latest_date, "%Y-%m-%d")
        pretty_date = _d.strftime("%b %d, %Y")
    except Exception:
        pretty_date = latest_date

    # Eyebrow line
    eyebrow_text = f"Live, {edition_label} edition, {pretty_date}"

    # ── Daily Pick: synthesize a headline + summary from the_edge ──
    if edge_text:
        # First sentence is headline-y, rest is summary
        parts = re.split(r"(?<=[.!?])\s+", edge_text.strip(), maxsplit=1)
        pick_headline = parts[0] if parts else "Apterreon Daily Pick"
        pick_summary = parts[1] if len(parts) > 1 else ""
    else:
        pick_headline = "Today's brief is generating."
        pick_summary = "Check back in a moment, or open the latest brief from the link above."

    # Stats for the Daily Pick panel
    total_stories = sum(len(s.get("stories", [])) for b in briefs for s in b.get("sections", []))
    sources_set = set()
    for b in briefs:
        for sec in b.get("sections", []):
            for st in sec.get("stories", []):
                src = st.get("source", "")
                if src:
                    sources_set.add(src.split("·")[0].strip())
    total_sources = len(sources_set) or 0

    # ── Section cards (from latest brief sections) ──
    section_cards_html = ""
    if latest:
        for idx, sec in enumerate(latest.get("sections", []), start=1):
            sec_name = sec.get("name", "")
            stories = sec.get("stories", [])
            top = stories[:2]
            if not top:
                continue
            stories_html = ""
            for st in top:
                headline = st.get("headline", "").replace('"', '&quot;')
                source = st.get("source", "").replace('"', '&quot;')
                link = (st.get("link") or latest_url).replace('"', '&quot;')
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
    </article>
"""
    if not section_cards_html:
        section_cards_html = '<div class="empty-state">No sections yet. The next scheduled brief will populate this view.</div>'

    # ── Live feed (top 8 stories across the last few briefs) ──
    feed_items = []
    for b in briefs[:3]:
        b_key = b.get("key", "")
        b_type = b.get("type", "")
        for sec in b.get("sections", []):
            for st in sec.get("stories", []):
                if not st.get("headline"):
                    continue
                feed_items.append({
                    "headline": st.get("headline", ""),
                    "source": st.get("source", ""),
                    "link": st.get("link") or f"{site_url}/{b_key}",
                    "edition": b_type,
                    "date": b.get("date", ""),
                })
        if len(feed_items) >= 12:
            break
    feed_items = feed_items[:8]

    feed_html = ""
    for item in feed_items:
        h = (item["headline"] or "").replace('"', '&quot;')
        s = (item["source"] or "").replace('"', '&quot;')
        link = (item["link"] or "#").replace('"', '&quot;')
        ed = (item["edition"] or "").upper()
        feed_html += (
            f'<a class="feed-item" href="{link}" target="_blank" rel="noopener">'
            f'<span class="feed-time">{ed}</span>'
            f'<span class="feed-headline">{h}</span>'
            f'<span class="feed-src">{s}</span>'
            f'</a>'
        )
    if not feed_html:
        feed_html = '<div class="empty-state">Feed populates after the next brief runs.</div>'

    # Brief count + cost (totals)
    total_briefs = len(briefs)
    cost_estimate = 0.04  # static placeholder; live cost lives inside each brief

    index_html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="color-scheme" content="dark">
<meta name="theme-color" content="#0A0A0F">
<link rel="manifest" href="manifest.json">
<title>Apterreon, Daily Intelligence Brief</title>
<link href="https://fonts.googleapis.com/css2?family=Syne:wght@600;700;800&family=Inter:wght@300;400;500;600;700&family=DM+Mono:wght@300;400;500&display=swap" rel="stylesheet">
<style>
  *,*::before,*::after {{ box-sizing:border-box; margin:0; padding:0; }}
  :root {{
    --bg-base:#0A0A0F; --bg-1:#11121A; --bg-2:#16171F;
    --border:rgba(255,255,255,0.06); --border-bright:rgba(255,255,255,0.12);
    --apt-red:#FF1F3D; --apt-red-deep:#CC0028; --apt-rose:#FF7A85; --apt-amber:#FFB347;
    --text-1:#FFFFFF; --text-2:#E2E5EC; --text-3:#9CA3AF; --text-4:#6B7280; --text-5:#3F4654;
  }}
  html {{ background:var(--bg-base); color:var(--text-1); font-family:'Inter',-apple-system,BlinkMacSystemFont,sans-serif; font-size:15px; -webkit-font-smoothing:antialiased; scroll-behavior:smooth; }}
  body {{ min-height:100vh; overflow-x:hidden; }}
  ::-webkit-scrollbar {{ width:6px; }}
  ::-webkit-scrollbar-thumb {{ background:var(--border-bright); border-radius:3px; }}
  a {{ color:inherit; text-decoration:none; }}

  /* Plexus + soft mesh background */
  #plexus {{ position:fixed; inset:0; z-index:0; opacity:0.55; }}
  body::before {{
    content:''; position:fixed; inset:0; z-index:1; pointer-events:none;
    background:
      radial-gradient(800px 600px at 15% 20%, rgba(255,31,61,0.10), transparent 60%),
      radial-gradient(900px 700px at 85% 80%, rgba(204,0,40,0.07), transparent 60%),
      radial-gradient(1200px 800px at 50% 40%, rgba(255,122,133,0.04), transparent 70%);
  }}
  body::after {{
    content:''; position:fixed; inset:0; z-index:2; pointer-events:none;
    background-image:radial-gradient(rgba(255,255,255,0.025) 1px, transparent 1px);
    background-size:3px 3px; opacity:0.5; mix-blend-mode:overlay;
  }}
  .topnav, .hero, .featured, .features, .feed, .footer {{ position:relative; z-index:3; }}

  /* Floating pill topnav */
  .topnav {{
    position:sticky; top:16px; max-width:1200px; margin:16px auto 0; padding:10px 14px 10px 18px;
    display:flex; align-items:center; gap:14px;
    background:rgba(17,18,26,0.55);
    backdrop-filter:blur(24px) saturate(160%); -webkit-backdrop-filter:blur(24px) saturate(160%);
    border:1px solid var(--border); border-radius:18px;
  }}
  .lockup {{ display:flex; align-items:center; gap:12px; }}
  .lockup-text {{ display:flex; flex-direction:column; line-height:1; }}
  .brand {{ font-family:'Syne',sans-serif; font-weight:800; font-size:14px; letter-spacing:4px; color:var(--text-1); text-transform:uppercase; }}
  .lockup-tagline {{ font-family:'DM Mono',monospace; font-size:9px; letter-spacing:2px; color:var(--apt-rose); text-transform:uppercase; margin-top:5px; }}
  .pulse-row {{ display:flex; align-items:center; gap:8px; margin-left:14px; padding-left:14px; border-left:1px solid var(--border); font-family:'DM Mono',monospace; font-size:10px; letter-spacing:2px; color:var(--text-3); text-transform:uppercase; }}
  .pulse-dot {{ width:6px; height:6px; border-radius:50%; background:#34D27A; box-shadow:0 0 12px rgba(52,210,122,0.7); animation:pulse 1.8s ease-out infinite; }}
  @keyframes pulse {{ 0%{{box-shadow:0 0 0 0 rgba(52,210,122,0.55);}} 70%{{box-shadow:0 0 0 10px rgba(52,210,122,0);}} 100%{{box-shadow:0 0 0 0 rgba(52,210,122,0);}} }}

  .nav {{ margin-left:auto; display:flex; gap:4px; }}
  .nav a {{ padding:8px 14px; font-size:13px; font-weight:500; color:var(--text-3); border-radius:10px; transition:all .2s; }}
  .nav a:hover {{ color:var(--text-1); background:rgba(255,255,255,0.04); }}
  .nav a.active {{ color:var(--text-1); background:rgba(255,31,61,0.10); }}

  .cta {{
    padding:8px 16px; font-size:13px; font-weight:600;
    background:linear-gradient(135deg, #FF1F3D 0%, #CC0028 100%);
    color:#FFF; border:none; cursor:pointer; border-radius:10px;
    transition:transform .15s, box-shadow .25s;
    box-shadow:0 4px 24px rgba(255,31,61,0.25);
  }}
  .cta:hover {{ transform:translateY(-1px); box-shadow:0 8px 32px rgba(255,31,61,0.4); }}

  /* Hero */
  .hero {{ max-width:1200px; margin:0 auto; padding:96px 24px 48px; }}
  .eyebrow {{
    display:inline-flex; align-items:center; gap:8px; padding:6px 14px; border-radius:999px;
    background:rgba(255,31,61,0.08); border:1px solid rgba(255,31,61,0.20);
    font-family:'DM Mono',monospace; font-size:11px; letter-spacing:2px; color:var(--apt-rose);
    text-transform:uppercase; margin-bottom:24px;
    opacity:0; transform:translateY(8px); animation:fadeUp .8s .1s ease-out forwards;
  }}
  .eyebrow .live-dot {{ width:6px; height:6px; border-radius:50%; background:#34D27A; }}

  h1.hero-title {{
    font-family:'Syne',sans-serif; font-weight:800; font-size:84px; line-height:0.98;
    letter-spacing:-0.03em; margin-bottom:24px; max-width:1000px;
    background:linear-gradient(135deg, #FFFFFF 0%, #FFFFFF 40%, #FF7A85 70%, #FF1F3D 100%);
    -webkit-background-clip:text; background-clip:text; -webkit-text-fill-color:transparent;
    opacity:0; transform:translateY(16px); animation:fadeUp .9s .25s ease-out forwards;
  }}
  @keyframes fadeUp {{ to {{ opacity:1; transform:translateY(0); }} }}

  .hero-sub {{
    font-size:19px; line-height:1.6; color:var(--text-2); max-width:640px; margin-bottom:40px; font-weight:400;
    opacity:0; transform:translateY(12px); animation:fadeUp .9s .4s ease-out forwards;
  }}
  .hero-actions {{ display:flex; gap:12px; flex-wrap:wrap; opacity:0; transform:translateY(12px); animation:fadeUp .9s .55s ease-out forwards; }}
  .btn-primary {{
    padding:14px 24px; font-size:14px; font-weight:600;
    background:linear-gradient(135deg, #FF1F3D 0%, #CC0028 100%); color:#FFF;
    border-radius:12px; cursor:pointer; transition:transform .15s, box-shadow .25s;
    box-shadow:0 8px 32px rgba(255,31,61,0.3); display:inline-flex; align-items:center; gap:8px;
    text-decoration:none;
  }}
  .btn-primary:hover {{ transform:translateY(-2px); box-shadow:0 12px 40px rgba(255,31,61,0.45); }}
  .btn-secondary {{
    padding:14px 24px; font-size:14px; font-weight:500;
    background:rgba(255,255,255,0.04); color:var(--text-1);
    border:1px solid var(--border-bright); border-radius:12px;
    cursor:pointer; transition:all .15s; display:inline-flex; align-items:center; gap:8px;
    text-decoration:none;
  }}
  .btn-secondary:hover {{ background:rgba(255,255,255,0.07); border-color:rgba(255,255,255,0.20); }}

  /* Featured Daily Pick */
  .featured {{ max-width:1200px; margin:0 auto; padding:32px 24px 64px; }}
  .featured-card {{
    position:relative;
    background:linear-gradient(180deg, rgba(22,23,31,0.85) 0%, rgba(17,18,26,0.92) 100%);
    backdrop-filter:blur(24px); -webkit-backdrop-filter:blur(24px);
    border:1px solid var(--border-bright); border-radius:24px;
    padding:48px; overflow:hidden;
    opacity:0; transform:translateY(20px); animation:fadeUp 1s .7s ease-out forwards;
  }}
  .featured-card::before {{
    content:''; position:absolute; inset:-1px; border-radius:24px; padding:1px;
    background:linear-gradient(135deg, rgba(255,31,61,0.5), transparent 40%, transparent 60%, rgba(255,122,133,0.3));
    -webkit-mask:linear-gradient(#000,#000) content-box, linear-gradient(#000,#000);
    mask:linear-gradient(#000,#000) content-box, linear-gradient(#000,#000);
    -webkit-mask-composite:xor; mask-composite:exclude; pointer-events:none;
  }}
  .feat-meta {{ display:flex; align-items:center; gap:10px; margin-bottom:18px; font-family:'DM Mono',monospace; font-size:11px; letter-spacing:2px; color:var(--text-3); text-transform:uppercase; flex-wrap:wrap; }}
  .feat-meta .tag {{ padding:4px 10px; border-radius:6px; background:rgba(255,31,61,0.10); color:var(--apt-rose); border:1px solid rgba(255,31,61,0.20); }}
  .feat-meta .dot {{ width:3px; height:3px; border-radius:50%; background:var(--text-4); }}
  .feat-h2 {{ font-family:'Syne',sans-serif; font-weight:700; font-size:38px; line-height:1.15; letter-spacing:-0.02em; color:var(--text-1); margin-bottom:18px; max-width:920px; }}
  .feat-summary {{ font-size:17px; line-height:1.65; color:var(--text-2); max-width:920px; margin-bottom:28px; }}
  .feat-grid {{ display:grid; grid-template-columns:repeat(3, 1fr); gap:18px; margin-top:32px; }}
  .feat-stat {{ padding:18px 20px; background:rgba(255,255,255,0.03); border:1px solid var(--border); border-radius:14px; transition:all .25s; }}
  .feat-stat:hover {{ background:rgba(255,255,255,0.05); border-color:rgba(255,255,255,0.12); transform:translateY(-2px); }}
  .fs-label {{ font-family:'DM Mono',monospace; font-size:10px; letter-spacing:2px; color:var(--text-4); text-transform:uppercase; margin-bottom:8px; }}
  .fs-val {{ font-family:'Syne',sans-serif; font-size:30px; font-weight:700; color:var(--text-1); letter-spacing:-0.02em; line-height:1; }}
  .fs-delta {{ font-size:12px; color:#34D27A; margin-top:6px; }}
  .feat-actions {{ margin-top:28px; display:flex; gap:14px; flex-wrap:wrap; align-items:center; }}
  .feat-actions .quiet {{ font-family:'DM Mono',monospace; font-size:11px; letter-spacing:2px; color:var(--text-3); text-transform:uppercase; border-bottom:1px solid var(--border); padding-bottom:2px; }}

  /* Section grid */
  .features {{ max-width:1200px; margin:0 auto; padding:64px 24px; }}
  .features-h {{ display:flex; justify-content:space-between; align-items:end; margin-bottom:40px; flex-wrap:wrap; gap:18px; }}
  .features-h h2 {{ font-family:'Syne',sans-serif; font-weight:700; font-size:42px; letter-spacing:-0.02em; line-height:1.1; max-width:600px; }}
  .features-h p {{ font-size:16px; color:var(--text-3); line-height:1.6; max-width:380px; }}

  .section-grid {{ display:grid; grid-template-columns:repeat(2, 1fr); gap:18px; }}
  @media (max-width:780px) {{ .section-grid {{ grid-template-columns:1fr; }} }}

  .sec-card {{
    position:relative;
    background:rgba(17,18,26,0.65);
    backdrop-filter:blur(20px); -webkit-backdrop-filter:blur(20px);
    border:1px solid var(--border); border-radius:20px;
    padding:28px; transition:all .35s cubic-bezier(0.2,0.8,0.2,1);
    overflow:hidden;
  }}
  .sec-card::after {{
    content:''; position:absolute; inset:0; border-radius:20px;
    background:linear-gradient(135deg, rgba(255,31,61,0.18), transparent 50%);
    opacity:0; transition:opacity .35s; pointer-events:none;
  }}
  .sec-card:hover {{ transform:translateY(-4px); border-color:rgba(255,31,61,0.4); box-shadow:0 20px 60px rgba(255,31,61,0.15), 0 8px 24px rgba(0,0,0,0.3); }}
  .sec-card:hover::after {{ opacity:1; }}
  .sc-head {{ display:flex; align-items:flex-start; gap:14px; margin-bottom:20px; }}
  .sc-num {{
    width:44px; height:44px; flex-shrink:0;
    display:flex; align-items:center; justify-content:center;
    background:linear-gradient(135deg, rgba(255,31,61,0.16), rgba(255,31,61,0.04));
    border:1px solid rgba(255,31,61,0.22); border-radius:12px;
    font-family:'DM Mono',monospace; font-size:14px; font-weight:500; color:var(--apt-rose);
  }}
  .sc-titles {{ flex:1; min-width:0; }}
  .sc-eyebrow {{ font-family:'DM Mono',monospace; font-size:10px; letter-spacing:2px; color:var(--text-4); text-transform:uppercase; margin-bottom:4px; }}
  .sc-title {{ font-family:'Syne',sans-serif; font-size:22px; font-weight:700; letter-spacing:-0.01em; color:var(--text-1); }}
  .sc-count {{ font-family:'DM Mono',monospace; font-size:11px; color:var(--text-4); padding:4px 10px; background:rgba(255,255,255,0.04); border-radius:8px; }}
  .sc-list {{ display:flex; flex-direction:column; gap:0; }}
  .sc-item {{ padding:14px 0; border-top:1px solid var(--border); display:grid; grid-template-columns:1fr auto; gap:12px; align-items:start; transition:padding-left .15s; }}
  .sc-item:first-child {{ border-top:none; padding-top:4px; }}
  .sc-item:hover {{ padding-left:6px; }}
  .sc-item-headline {{ font-size:15px; font-weight:500; color:var(--text-1); line-height:1.45; }}
  .sc-item-source {{ font-family:'DM Mono',monospace; font-size:10px; letter-spacing:1.5px; color:var(--text-4); text-transform:uppercase; margin-top:6px; }}
  .sc-arrow {{ color:var(--text-4); font-size:18px; transition:color .15s, transform .15s; align-self:start; padding-top:2px; }}
  .sc-item:hover .sc-arrow {{ color:var(--apt-red); transform:translateX(4px); }}

  /* Live feed */
  .feed {{ max-width:1200px; margin:0 auto; padding:64px 24px; }}
  .feed-h {{ display:flex; justify-content:space-between; align-items:center; margin-bottom:24px; }}
  .feed-h h2 {{ font-family:'Syne',sans-serif; font-weight:700; font-size:32px; letter-spacing:-0.01em; }}
  .feed-h .live-tag {{ display:inline-flex; align-items:center; gap:8px; padding:6px 12px; border-radius:999px; background:rgba(52,210,122,0.10); border:1px solid rgba(52,210,122,0.30); font-family:'DM Mono',monospace; font-size:10px; letter-spacing:2px; color:#34D27A; text-transform:uppercase; }}
  .feed-list {{ display:flex; flex-direction:column; gap:1px; background:var(--border); border:1px solid var(--border); border-radius:14px; overflow:hidden; }}
  .feed-item {{ background:rgba(17,18,26,0.85); padding:18px 22px; display:grid; grid-template-columns:auto 1fr auto; gap:18px; align-items:center; cursor:pointer; transition:background .15s; }}
  .feed-item:hover {{ background:rgba(22,23,31,0.95); }}
  .feed-time {{ font-family:'DM Mono',monospace; font-size:11px; color:var(--apt-rose); letter-spacing:1.5px; min-width:64px; }}
  .feed-headline {{ font-size:15px; color:var(--text-1); line-height:1.45; font-weight:500; }}
  .feed-src {{ font-family:'DM Mono',monospace; font-size:10px; letter-spacing:1.5px; color:var(--text-4); text-transform:uppercase; }}

  .empty-state {{ padding:48px; text-align:center; font-family:'DM Mono',monospace; font-size:12px; letter-spacing:2px; color:var(--text-4); text-transform:uppercase; background:rgba(17,18,26,0.5); border:1px solid var(--border); border-radius:14px; }}

  .footer {{ max-width:1200px; margin:64px auto 0; padding:32px 24px 48px; border-top:1px solid var(--border); display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:14px; }}
  .footer .brand-foot {{ font-family:'Syne',sans-serif; font-size:12px; font-weight:800; letter-spacing:5px; color:var(--text-3); text-transform:uppercase; }}
  .footer .meta {{ font-family:'DM Mono',monospace; font-size:11px; color:var(--text-4); letter-spacing:1px; }}

  @media (max-width:760px) {{
    h1.hero-title {{ font-size:48px; }}
    .feat-h2 {{ font-size:28px; }}
    .featured-card {{ padding:32px 24px; }}
    .feat-grid {{ grid-template-columns:1fr; }}
  }}
</style>
</head>
<body>

<canvas id="plexus" aria-hidden="true"></canvas>

<nav class="topnav">
  <a class="lockup" href="./index.html" title="Apterreon home">
    {logo_nav}
    <div class="lockup-text">
      <span class="brand">Apterreon</span>
      <span class="lockup-tagline">Explore what's out there.</span>
    </div>
    <div class="pulse-row"><span class="pulse-dot"></span><span>Live</span></div>
  </a>
  <div class="nav">
    <a href="#briefs">Briefs</a>
    <a href="#stories">Stories</a>
    <a href="#pinned">Pinned</a>
    <a href="#archive">Archive</a>
  </div>
  <a class="cta" href="{latest_url}">Open latest &rarr;</a>
</nav>

<section class="hero">
  <div class="eyebrow"><span class="live-dot"></span>{eyebrow_text}</div>
  <h1 class="hero-title">Regular Briefs and Curated&nbsp;Stories</h1>
  <p class="hero-sub">Finance, Politics, Tech, and more. Explore what's out there with Apterreon's Daily Intelligence Briefs and story catalogue.</p>
  <div class="hero-actions">
    <a class="btn-primary" href="{latest_url}">Read today's briefs <span style="font-size:16px">&rarr;</span></a>
    <a class="btn-secondary" href="#stories">Browse stories <span style="font-size:16px">&rarr;</span></a>
  </div>
</section>

<section class="featured" id="daily-pick">
  <article class="featured-card">
    <div class="feat-meta">
      <span class="tag">Daily Pick</span>
      <span>Apterreon</span><span class="dot"></span>
      <span>{pretty_date}</span><span class="dot"></span>
      <span>{edition_label}</span>
    </div>
    <h2 class="feat-h2">{pick_headline}</h2>
    <p class="feat-summary">{pick_summary}</p>
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
        <div class="fs-label">Cost to produce</div>
        <div class="fs-val">${cost_estimate:.2f}</div>
        <div class="fs-delta">per brief, average</div>
      </div>
    </div>
    <div class="feat-actions">
      <a class="btn-primary" href="{latest_url}">Read the full take <span style="font-size:16px">&rarr;</span></a>
      <a class="quiet" href="#archive">Browse past picks</a>
    </div>
  </article>
</section>

<section class="features" id="briefs">
  <div class="features-h">
    <h2>Today's sections at a glance.</h2>
    <p>Each card opens to source links. Click through to read the full brief or jump to a story.</p>
  </div>
  <div class="section-grid">
    {section_cards_html}
  </div>
</section>

<section class="feed" id="stories">
  <div class="feed-h">
    <h2>Live feed.</h2>
    <span class="live-tag"><span class="pulse-dot"></span>Updating</span>
  </div>
  <div class="feed-list">
    {feed_html}
  </div>
</section>

<footer class="footer">
  <div style="display:flex;flex-direction:column;gap:6px">
    <span class="brand-foot">Apterreon</span>
    <span style="font-family:'DM Mono',monospace;font-size:11px;letter-spacing:1.5px;color:var(--apt-rose)">Explore what's out there.</span>
  </div>
  <span class="meta">Daily Intelligence Brief, generated by Apterreon, hosted on GitHub Pages</span>
</footer>

<script>
// Plexus background canvas, Apterreon red palette
(function() {{
  const c = document.getElementById('plexus');
  if (!c) return;
  const ctx = c.getContext('2d');
  let W, H, dpr, stars = [], nodes = [], flow = [];
  const CONNECT = 220; let mx = -999, my = -999;
  function resize() {{
    dpr = window.devicePixelRatio || 1;
    W = innerWidth; H = innerHeight;
    c.width = W*dpr; c.height = H*dpr; c.style.width = W+'px'; c.style.height = H+'px';
    ctx.setTransform(dpr,0,0,dpr,0,0); build();
  }}
  function build() {{
    stars = []; for (let i = 0; i < 240; i++) stars.push({{x:Math.random()*W,y:Math.random()*H,r:Math.random()*0.7+0.2,b:Math.random()*0.18+0.03,p:Math.random()*6.28}});
    nodes = []; const n = Math.max(40, Math.floor((W*H)/26000));
    for (let i = 0; i < n; i++) nodes.push({{x:Math.random()*W,y:Math.random()*H,size:0.5+Math.random()*1.5,b:0.10+Math.random()*0.30,ph:Math.random()*6.28,vx:(Math.random()-0.5)*0.16,vy:(Math.random()-0.5)*0.12}});
    flow = []; for (let i = 0; i < 40; i++) flow.push({{a:-1,b:-1,t:Math.random(),s:0.002+Math.random()*0.003,sz:0.3+Math.random()*0.6,br:0.15+Math.random()*0.3}});
  }}
  function pickEdge(fp) {{
    if (!nodes.length) return;
    const a = Math.floor(Math.random()*nodes.length); let bj = -1, bd = CONNECT;
    for (let j = 0; j < nodes.length; j++) {{ if (j===a) continue; const dx = nodes[a].x-nodes[j].x, dy = nodes[a].y-nodes[j].y; const d = Math.sqrt(dx*dx+dy*dy); if (d < bd) {{ bd = d; bj = j; }} }}
    fp.a = a; fp.b = bj; fp.t = 0;
  }}
  document.addEventListener('mousemove', e => {{ mx = e.clientX; my = e.clientY; }});
  document.addEventListener('mouseleave', () => {{ mx = -999; my = -999; }});
  let t = 0;
  function draw() {{
    t += 0.004;
    ctx.fillStyle = '#0A0A0F'; ctx.fillRect(0,0,W,H);
    for (const s of stars) {{ const tw = 0.5+0.5*Math.sin(t*5+s.p); ctx.fillStyle = `rgba(220,210,210,${{s.b*tw}})`; ctx.beginPath(); ctx.arc(s.x,s.y,s.r,0,Math.PI*2); ctx.fill(); }}
    for (const n of nodes) {{
      n.x += n.vx + Math.sin(t*1.5+n.ph)*0.06; n.y += n.vy + Math.cos(t*1.2+n.ph*1.3)*0.04;
      if (n.x < -40) n.x = W+40; if (n.x > W+40) n.x = -40; if (n.y < -40) n.y = H+40; if (n.y > H+40) n.y = -40;
      const dx = n.x-mx, dy = n.y-my, md = Math.sqrt(dx*dx+dy*dy);
      if (md < 180 && md > 0) {{ const f = (1-md/180)*0.5; n.x += (dx/md)*f; n.y += (dy/md)*f; }}
    }}
    for (let i = 0; i < nodes.length; i++) for (let j = i+1; j < nodes.length; j++) {{
      const dx = nodes[i].x-nodes[j].x, dy = nodes[i].y-nodes[j].y, d = Math.sqrt(dx*dx+dy*dy);
      if (d < CONNECT) {{
        const a = (1-d/CONNECT);
        ctx.strokeStyle = `rgba(122,16,16,${{a*0.06}})`; ctx.lineWidth = 2.5; ctx.lineCap='round';
        ctx.beginPath(); ctx.moveTo(nodes[i].x,nodes[i].y); ctx.lineTo(nodes[j].x,nodes[j].y); ctx.stroke();
        ctx.strokeStyle = `rgba(255,31,61,${{a*0.18}})`; ctx.lineWidth = 0.6;
        ctx.beginPath(); ctx.moveTo(nodes[i].x,nodes[i].y); ctx.lineTo(nodes[j].x,nodes[j].y); ctx.stroke();
      }}
    }}
    if (mx > -100) for (const n of nodes) {{
      const dx = n.x-mx, dy = n.y-my, d = Math.sqrt(dx*dx+dy*dy);
      if (d < 180) {{ const a = (1-d/180)*0.4; ctx.strokeStyle = `rgba(255,80,100,${{a}})`; ctx.lineWidth = 0.7; ctx.beginPath(); ctx.moveTo(mx,my); ctx.lineTo(n.x,n.y); ctx.stroke(); }}
    }}
    for (const fp of flow) {{
      if (fp.a < 0 || fp.b < 0 || fp.a >= nodes.length || fp.b >= nodes.length) {{ pickEdge(fp); continue; }}
      const na = nodes[fp.a], nb = nodes[fp.b]; if (!na || !nb) {{ pickEdge(fp); continue; }}
      const edx = na.x-nb.x, edy = na.y-nb.y; if (Math.sqrt(edx*edx+edy*edy) > CONNECT*1.2) {{ pickEdge(fp); continue; }}
      fp.t += fp.s;
      if (fp.t > 1) {{
        fp.a = fp.b; let bj = -1, bd = CONNECT;
        for (let j = 0; j < nodes.length; j++) {{ if (j===fp.a) continue; const dx = nodes[fp.a].x-nodes[j].x, dy = nodes[fp.a].y-nodes[j].y; const d = Math.sqrt(dx*dx+dy*dy); if (d < bd && Math.random() < 0.5) {{ bd = d; bj = j; }} }}
        fp.b = bj >= 0 ? bj : Math.floor(Math.random()*nodes.length); fp.t = 0;
      }}
      const x = na.x + (nb.x-na.x)*fp.t, y = na.y + (nb.y-na.y)*fp.t;
      ctx.fillStyle = `rgba(255,31,61,${{fp.br*0.10}})`; ctx.beginPath(); ctx.arc(x,y,fp.sz*3,0,Math.PI*2); ctx.fill();
      ctx.fillStyle = `rgba(255,160,170,${{fp.br*0.55}})`; ctx.beginPath(); ctx.arc(x,y,fp.sz,0,Math.PI*2); ctx.fill();
    }}
    for (const n of nodes) {{
      ctx.fillStyle = `rgba(255,31,61,${{n.b*0.10}})`; ctx.beginPath(); ctx.arc(n.x,n.y,n.size*2.5,0,Math.PI*2); ctx.fill();
      ctx.fillStyle = `rgba(255,150,160,${{n.b}})`; ctx.beginPath(); ctx.arc(n.x,n.y,n.size,0,Math.PI*2); ctx.fill();
    }}
    requestAnimationFrame(draw);
  }}
  addEventListener('resize', resize); resize(); requestAnimationFrame(draw);
}})();
</script>
</body>
</html>"""

    (DOCS_DIR / "index.html").write_text(index_html, encoding="utf-8")

    # PWA manifest
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
    print("Wrote docs/index.html and docs/manifest.json (v6 modern landing).")
def s3_publish_brief(brief_type, now_et, interactive_html, data=None, quotes=None, timestamp=None):
    """Write brief HTML + JSON sidecar, clean old ones, regenerate index."""
    date_iso = now_et.strftime("%Y-%m-%d")
    s3_write_brief(brief_type, date_iso, interactive_html, data=data, quotes=quotes, timestamp=timestamp)
    s3_cleanup_old_briefs()
    briefs = s3_list_briefs()
    s3_generate_index(briefs)


# ── Lambda Handler ──────────────────────────────────────────────────────────

def lambda_handler(event, context):
    # Handle pin toggle requests (from API Gateway or Function URL)
    if event.get("action") == "pin":
        key = event.get("key", "")
        if key:
            new_state = s3_toggle_pin(key)
            briefs = s3_list_briefs()
            s3_generate_index(briefs)
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
