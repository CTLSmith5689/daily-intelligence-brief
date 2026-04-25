"""
Daily Intelligence Brief — AWS Lambda Handler
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

SMTP_USER = "ctlsmith@me.com"  # Apple ID for SMTP auth — must match the APTERREON_ICLOUD_APP_PASSWORD owner
SENDER_EMAIL = "Daily_Intel_Briefs@icloud.com"  # iCloud alias used as From: header
SENDER_NAME = "Daily Intelligence Brief"
RECIPIENT_EMAIL = os.environ.get("RECIPIENTS", SMTP_USER)
SMTP_SERVER = "smtp.mail.me.com"
SMTP_PORT = 587

ANTHROPIC_MODEL = os.environ.get("APTERREON_MODEL", "claude-sonnet-4-6")
ET_OFFSET = timedelta(hours=-4)  # EDT

# ── Brand: Apterreon ─────────────────────────────────────────────────────────
APT_RED        = "#CC0000"  # bright red — leads, accent
APT_DARK_RED   = "#7A1010"  # dark red — grounds
APT_GREY       = "#888888"  # grey — recedes
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

# Sections that skip Claude insights — just headlines + source
NO_INSIGHT_SECTIONS = {"Breaking News"}

# Fixed sections — always present, always this order
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
#   tier 1 — bright red (#CC0000): primary attention
#   tier 2 — dark red  (#7A1010): important context
#   tier 3 — grey      (#888888): supporting context
SECTION_COLORS = {
    "Breaking News":     APT_RED,
    "Finance & Markets": APT_RED,
    "Politics & Policy": APT_DARK_RED,
    "AI & Technology":   APT_DARK_RED,
    "International":     APT_GREY,
    "Culture & Sports":  APT_GREY,
    "Boston":            APT_GREY,
}

# Emoji icons retired — brand is minimalist typography. Section labels use
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

                # Filter by recency — drop articles older than the cutoff
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

# Pricing per million tokens — updates automatically based on model env var
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
        bar_color = "#5599CC"  # singularity blue — calm
        status = "LOW"
    elif monthly < 5:
        bar_color = "#888888"  # grey — neutral
        status = "MODERATE"
    else:
        bar_color = "#CC0000"  # red — over budget
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


def build_email_preview(title, data, quotes, timestamp, usage_info=None):
    """Email preview — Apterreon. Email-safe (inline styles, tables,
    system fonts only — no web fonts since most clients strip @import)."""
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
<div style="font-family:{mono};font-size:10px;letter-spacing:2px;color:#9AA8B8;text-transform:uppercase">{timestamp}</div>

{usage_html}
{market_html}
{sections_html}
{edge_html}
{tomorrow_html}

<div style="margin-top:48px;padding-top:18px;border-top:1px solid #1A2030">
<table width="100%" cellpadding="0" cellspacing="0"><tr>
<td style="vertical-align:middle">{apt_logo_svg(14, 19, 0.3)} <span style="font-family:{sans};font-size:10px;font-weight:700;color:#6A7888;letter-spacing:1px;vertical-align:middle">Apterreon</span> <span style="font-family:{sans};font-size:10px;color:#4A5A6A;vertical-align:middle">&nbsp;&middot;&nbsp;Explore what&#8217;s out there.</span></td>
<td style="text-align:right;vertical-align:middle"><span style="font-family:{mono};font-size:9px;letter-spacing:2px;color:#6A7888">{timestamp}</span></td>
</tr></table>
</div>

</td></tr></table>
</td></tr></table>
</body>
</html>"""


# ── Interactive HTML Attachment ─────────────────────────────────────────────

def build_interactive_html(title, data, quotes, timestamp, usage_info=None):
    """Self-contained interactive HTML brief — Apterreon."""

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
  <a class="back" href="./index.html"><span>&#9664;</span> Briefs</a>
  <div class="lockup">
    {apt_logo_svg(20, 27, 0.45)}
    <div>
      <div class="dm">Apterreon</div>
      <div class="prod">Daily Intelligence Brief</div>
    </div>
  </div>
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
            '<div class="insight-label">Claude Insight</div>' +
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
    """Static HTML attachment — no JavaScript. Renders in any mail client, including iOS."""

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
    <div style="font-size:9px;font-weight:700;text-transform:uppercase;letter-spacing:1.5px;color:{APT_RED};opacity:0.6;margin-bottom:4px">Claude Insight</div>
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
    (Cron context only — there's no live API endpoint; UI pin button uses localStorage.)"""
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
                # Compact representation for the search index — keep only what's
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
    """Generate docs/index.html (and manifest.json) with today/archive/pinned views."""
    now_et = datetime.now(timezone(ET_OFFSET))
    today_str = now_et.strftime("%Y-%m-%d")

    briefs_json = json.dumps(briefs)

    logo_hero = apt_logo_svg(56, 75, 0.55)
    logo_nav = apt_logo_svg(20, 27, 0.45)

    section_names_json = json.dumps([s[0] for s in SECTIONS])
    section_colors_json = json.dumps(SECTION_COLORS)

    index_html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="color-scheme" content="dark">
<meta name="theme-color" content="#050810">
<link rel="manifest" href="manifest.json">
<title>Daily Intelligence Brief &middot; Apterreon</title>
<link href="https://fonts.googleapis.com/css2?family=Syne:wght@600;700;800&family=DM+Mono:wght@300;400;500&display=swap" rel="stylesheet">
<style>
  *,*::before,*::after {{ box-sizing:border-box; margin:0; padding:0; }}
  :root {{
    --bg-base:#050810; --bg-surface:#0D0F18; --bg-elevated:#141826; --bg-deep:#070A0F;
    --border-dim:#1F2738; --border-bright:#3A4A5C;
    --apt-red:#CC0000; --apt-dark-red:#7A1010; --apt-grey:#888888;
    --text-primary:#F0F4F8; --text-body:#CCD4DC; --text-dim:#9AA8B8;
    --text-muted:#6A7888; --text-faint:#4A5A6A;
  }}
  html {{ background:var(--bg-base); color:var(--text-primary); font-family:'DM Mono',ui-monospace,Menlo,Consolas,monospace; font-size:13px; -webkit-font-smoothing:antialiased; scroll-behavior:smooth; }}
  body {{ background:var(--bg-base); min-height:100vh; padding:env(safe-area-inset-top) 0 env(safe-area-inset-bottom); }}
  ::-webkit-scrollbar {{ width:6px; height:6px; }}
  ::-webkit-scrollbar-track {{ background:transparent; }}
  ::-webkit-scrollbar-thumb {{ background:var(--border-dim); border-radius:3px; }}
  ::-webkit-scrollbar-thumb:hover {{ background:var(--border-bright); }}
  a {{ color:inherit; text-decoration:none; }}
  ::selection {{ background:rgba(204,0,0,0.35); color:#fff; }}

  /* Topnav */
  .topnav {{
    position:sticky; top:0; z-index:120; height:56px; background:rgba(13,15,24,0.85);
    backdrop-filter:blur(16px); -webkit-backdrop-filter:blur(16px);
    border-bottom:1px solid var(--border-dim); display:flex; align-items:center;
    padding:0 32px; gap:18px;
  }}
  .topnav .lockup {{ display:flex; align-items:center; gap:14px; }}
  .topnav .dm {{ font-family:'Syne',sans-serif; font-weight:800; font-size:12px; letter-spacing:5px; color:var(--text-primary); text-transform:uppercase; }}
  .topnav .prod {{ font-family:'Syne',sans-serif; font-weight:700; font-size:9px; letter-spacing:4px; color:var(--apt-red); text-transform:uppercase; }}
  .topnav .stats {{ margin-left:auto; display:none; gap:24px; align-items:center; }}
  @media (min-width:880px) {{ .topnav .stats {{ display:flex; }} }}
  .topnav .stat {{ display:flex; flex-direction:column; align-items:flex-end; }}
  .topnav .stat .v {{ font-family:'DM Mono',monospace; font-size:14px; font-weight:500; color:var(--text-primary); }}
  .topnav .stat .l {{ font-family:'DM Mono',monospace; font-size:8px; letter-spacing:2px; color:var(--text-dim); text-transform:uppercase; margin-top:2px; }}

  /* Hero — compact, doesn't dominate */
  .hero {{
    text-align:center; padding:64px 24px 48px; max-width:1100px; margin:0 auto;
    border-bottom:1px solid var(--border-dim);
  }}
  .hero .mark {{ display:flex; justify-content:center; margin-bottom:24px; }}
  .hero h1 {{ font-family:'Syne',sans-serif; font-weight:800; font-size:44px; letter-spacing:11px; color:#FFFFFF; text-transform:uppercase; margin-bottom:12px; line-height:1.05; }}
  .hero .sub {{ font-family:'Syne',sans-serif; font-weight:700; font-size:11px; letter-spacing:6px; color:var(--apt-red); text-transform:uppercase; margin-bottom:14px; }}
  .hero .desc {{ font-family:'DM Mono',monospace; font-size:11px; letter-spacing:2px; color:var(--text-dim); text-transform:uppercase; }}
  .hero .rule {{ width:80px; height:1px; margin:32px auto 0; background:linear-gradient(to right, transparent, var(--apt-dark-red), transparent); }}
  @media (max-width:640px) {{
    .hero {{ padding:48px 20px 36px; }}
    .hero h1 {{ font-size:30px; letter-spacing:7px; }}
  }}

  /* Sticky filter rail */
  .rail {{
    position:sticky; top:56px; z-index:90; background:rgba(5,8,16,0.92);
    backdrop-filter:blur(14px); -webkit-backdrop-filter:blur(14px);
    border-bottom:1px solid var(--border-dim);
    padding:14px 24px;
  }}
  .rail-inner {{ max-width:1200px; margin:0 auto; display:flex; flex-direction:column; gap:12px; }}
  .rail-row {{ display:flex; align-items:center; gap:10px; flex-wrap:wrap; }}

  /* Search input */
  .search {{
    flex:1; min-width:240px; display:flex; align-items:center; gap:10px;
    background:var(--bg-surface); border:1px solid var(--border-dim);
    padding:10px 14px; transition:border-color .2s;
  }}
  .search:focus-within {{ border-color:var(--apt-red); }}
  .search .icon {{ color:var(--text-dim); font-size:13px; flex-shrink:0; }}
  .search input {{
    flex:1; background:transparent; border:none; outline:none;
    font-family:'DM Mono',monospace; font-size:13px; color:var(--text-primary);
  }}
  .search input::placeholder {{ color:var(--text-muted); letter-spacing:1px; }}
  .search .clear {{
    background:transparent; border:none; cursor:pointer; padding:0 4px;
    font-family:'DM Mono',monospace; font-size:11px; letter-spacing:2px;
    color:var(--text-muted); text-transform:uppercase; transition:color .15s;
  }}
  .search .clear:hover {{ color:var(--text-primary); }}
  .search .clear[hidden] {{ display:none; }}

  /* Tabs (BRIEFS / STORIES / PINNED) */
  .tabs {{ display:flex; gap:4px; }}
  .tab {{
    padding:10px 16px; font-family:'DM Mono',monospace; font-size:10px;
    letter-spacing:3px; font-weight:500; color:var(--text-dim);
    cursor:pointer; transition:all .15s; text-transform:uppercase;
    user-select:none; background:var(--bg-surface);
    border:1px solid var(--border-dim);
  }}
  .tab:hover {{ color:var(--text-primary); border-color:var(--text-muted); }}
  .tab.active {{ background:var(--bg-elevated); color:var(--text-primary); border-color:var(--text-muted); }}
  .tab .count {{ display:inline-block; margin-left:8px; color:var(--text-muted); }}
  .tab.active .count {{ color:var(--apt-red); }}

  /* Chips (section + edition filters) */
  .chip-group {{ display:flex; flex-wrap:wrap; gap:6px; align-items:center; }}
  .chip-group .chip-label {{
    font-family:'DM Mono',monospace; font-size:9px; letter-spacing:2px;
    color:var(--text-muted); text-transform:uppercase; margin-right:4px;
  }}
  .chip {{
    padding:5px 10px; font-family:'DM Mono',monospace; font-size:10px;
    letter-spacing:2px; color:var(--text-dim); cursor:pointer;
    background:transparent; border:1px solid var(--border-dim);
    text-transform:uppercase; user-select:none; transition:all .15s;
  }}
  .chip:hover {{ color:var(--text-primary); border-color:var(--text-muted); }}
  .chip.active {{ color:#fff; background:rgba(204,0,0,0.18); border-color:var(--apt-red); }}
  .chip.dim.active {{ background:rgba(122,16,16,0.25); border-color:var(--apt-dark-red); }}
  .chip.grey.active {{ background:rgba(136,136,136,0.18); border-color:var(--apt-grey); }}

  /* Sort */
  .sort-group {{ display:flex; gap:6px; margin-left:auto; }}

  /* Main content area */
  .main {{ max-width:1200px; margin:0 auto; padding:32px 24px 96px; }}

  .view {{ display:none; }}
  .view.active {{ display:block; }}

  /* Date group header */
  .date-group {{ margin-bottom:36px; }}
  .date-label {{
    font-family:'DM Mono',monospace; font-size:11px; letter-spacing:4px;
    color:var(--text-dim); text-transform:uppercase; margin-bottom:14px;
    padding-bottom:10px; border-bottom:1px solid var(--border-dim);
    display:flex; justify-content:space-between; align-items:baseline;
  }}
  .date-label .count {{ color:var(--text-muted); font-size:9px; letter-spacing:2px; }}

  /* Brief cards (BRIEFS view) — grid */
  .brief-grid {{
    display:grid; grid-template-columns:repeat(auto-fill, minmax(380px, 1fr));
    gap:14px;
  }}
  .brief-card {{
    background:var(--bg-surface); border:1px solid var(--border-dim);
    padding:0; display:flex; flex-direction:column; transition:border-color .2s, background .2s;
    overflow:hidden;
  }}
  .brief-card:hover {{ border-color:var(--text-muted); background:var(--bg-elevated); }}
  .brief-card[data-type="morning"]  {{ border-top:2px solid var(--apt-red); }}
  .brief-card[data-type="midday"]   {{ border-top:2px solid var(--apt-dark-red); }}
  .brief-card[data-type="evening"]  {{ border-top:2px solid var(--apt-grey); }}

  .bc-head {{
    display:flex; align-items:flex-start; justify-content:space-between;
    padding:18px 20px 12px; gap:14px;
  }}
  .bc-num {{ font-family:'DM Mono',monospace; font-size:11px; letter-spacing:3px; color:var(--text-muted); flex-shrink:0; }}
  .bc-title {{
    font-family:'Syne',sans-serif; font-size:16px; font-weight:700;
    letter-spacing:3px; color:var(--text-primary); text-transform:uppercase;
  }}
  .bc-meta {{
    font-family:'DM Mono',monospace; font-size:10px; letter-spacing:2px;
    color:var(--text-dim); margin-top:6px; text-transform:uppercase;
  }}
  .bc-pin {{
    background:transparent; border:1px solid var(--border-dim); cursor:pointer;
    font-family:'DM Mono',monospace; font-size:9px; letter-spacing:2px;
    color:var(--text-dim); padding:4px 8px; transition:all .15s;
    text-transform:uppercase; flex-shrink:0;
  }}
  .bc-pin:hover {{ color:var(--apt-red); border-color:var(--apt-red); }}
  .bc-pin.pinned {{ color:var(--apt-red); border-color:var(--apt-red); background:rgba(204,0,0,0.08); }}

  .bc-preview {{ padding:0 20px 14px; }}
  .bc-preview-headline {{
    font-family:'DM Mono',monospace; font-size:12px; color:var(--text-body);
    line-height:1.55; padding:8px 0 8px 14px; position:relative;
    border-top:1px solid var(--border-dim); cursor:pointer; transition:color .15s;
  }}
  .bc-preview-headline:hover {{ color:#fff; }}
  .bc-preview-headline:hover::before {{ background:var(--apt-red); }}
  .bc-preview-headline:first-child {{ border-top:none; }}
  .bc-preview-headline::before {{
    content:''; position:absolute; left:0; top:14px; width:6px; height:1px;
    background:var(--text-muted); transition:background .15s;
  }}
  .bc-preview-headline.no-data {{ color:var(--text-muted); font-style:italic; cursor:default; }}
  .bc-preview-headline.no-data:hover {{ color:var(--text-muted); }}

  .bc-foot {{
    display:flex; align-items:center; justify-content:space-between;
    padding:12px 20px; border-top:1px solid var(--border-dim);
    background:var(--bg-deep);
  }}
  .bc-open {{
    font-family:'DM Mono',monospace; font-size:10px; letter-spacing:2px;
    color:var(--apt-red); text-transform:uppercase;
  }}
  .bc-open:hover {{ color:#fff; }}
  .bc-section-tags {{ display:flex; gap:6px; flex-wrap:wrap; }}
  .bc-section-tag {{
    font-family:'DM Mono',monospace; font-size:8px; letter-spacing:1.5px;
    color:var(--text-dim); text-transform:uppercase;
  }}

  /* Stories view — flat list */
  .story-row {{
    display:grid; grid-template-columns:140px 1fr 100px 80px;
    gap:18px; align-items:center;
    padding:14px 18px; background:var(--bg-surface);
    border:1px solid var(--border-dim); border-bottom:none;
    transition:border-color .15s, background .15s;
  }}
  .story-row:hover {{ background:var(--bg-elevated); border-color:var(--text-muted); }}
  .story-row:last-child {{ border-bottom:1px solid var(--border-dim); }}
  .story-section {{
    font-family:'DM Mono',monospace; font-size:10px; letter-spacing:2px;
    text-transform:uppercase; padding:4px 8px;
    border:1px solid var(--border-dim); display:inline-block; text-align:center;
  }}
  .story-headline {{ font-family:'DM Mono',monospace; font-size:13px; color:var(--text-primary); line-height:1.5; }}
  .story-headline:hover {{ color:var(--apt-red); }}
  .story-source {{ font-family:'DM Mono',monospace; font-size:10px; letter-spacing:1px; color:var(--text-dim); text-transform:uppercase; text-align:right; }}
  .story-edition {{ font-family:'DM Mono',monospace; font-size:9px; letter-spacing:2px; color:var(--text-muted); text-transform:uppercase; text-align:right; }}

  @media (max-width:680px) {{
    .story-row {{ grid-template-columns:1fr; gap:6px; }}
    .story-source, .story-edition {{ text-align:left; }}
  }}

  /* Empty / no-results */
  .empty {{
    text-align:center; padding:80px 20px; font-family:'DM Mono',monospace;
    font-size:11px; letter-spacing:2px; color:var(--text-muted); text-transform:uppercase;
  }}
  .empty .big {{ font-family:'Syne',sans-serif; font-size:18px; font-weight:700; letter-spacing:4px; color:var(--text-dim); margin-bottom:10px; }}

  /* Story modal */
  .modal-backdrop {{
    position:fixed; inset:0; z-index:200; background:rgba(5,8,16,0.78);
    backdrop-filter:blur(6px); -webkit-backdrop-filter:blur(6px);
    display:none; align-items:flex-start; justify-content:center;
    padding:64px 16px 24px; overflow-y:auto;
  }}
  .modal-backdrop.open {{ display:flex; }}
  .modal {{
    background:var(--bg-surface); border:1px solid var(--border-bright);
    border-top:2px solid var(--apt-red);
    max-width:720px; width:100%; padding:0;
    box-shadow:0 24px 64px rgba(0,0,0,0.6);
  }}
  .modal-head {{
    display:flex; align-items:flex-start; justify-content:space-between;
    padding:22px 28px 18px; gap:16px; border-bottom:1px solid var(--border-dim);
  }}
  .modal-section {{
    font-family:'DM Mono',monospace; font-size:10px; letter-spacing:3px;
    text-transform:uppercase; padding:5px 10px;
    border:1px solid currentColor; flex-shrink:0;
  }}
  .modal-meta {{
    font-family:'DM Mono',monospace; font-size:10px; letter-spacing:2px;
    color:var(--text-dim); text-transform:uppercase; flex:1; text-align:right;
  }}
  .modal-close {{
    background:transparent; border:1px solid var(--border-dim);
    cursor:pointer; padding:6px 12px;
    font-family:'DM Mono',monospace; font-size:10px; letter-spacing:2px;
    color:var(--text-dim); text-transform:uppercase; transition:all .15s;
    flex-shrink:0;
  }}
  .modal-close:hover {{ color:var(--text-primary); border-color:var(--text-muted); }}
  .modal-body {{ padding:24px 28px; }}
  .modal-headline {{
    font-family:'Syne',sans-serif; font-size:22px; font-weight:700;
    letter-spacing:0.5px; color:#FFFFFF; line-height:1.3; margin-bottom:14px;
  }}
  .modal-source {{
    font-family:'DM Mono',monospace; font-size:11px; letter-spacing:2px;
    color:var(--text-dim); text-transform:uppercase; margin-bottom:24px;
  }}
  .modal-block {{ margin-top:20px; padding-top:20px; border-top:1px solid var(--border-dim); }}
  .modal-block:first-of-type {{ border-top:none; padding-top:0; margin-top:0; }}
  .modal-label {{
    font-family:'DM Mono',monospace; font-size:9px; letter-spacing:4px;
    color:var(--apt-red); text-transform:uppercase; margin-bottom:10px;
  }}
  .modal-text {{
    font-family:'DM Mono',monospace; font-size:14px; color:var(--text-body);
    line-height:1.7; white-space:pre-wrap;
  }}
  .modal-insight {{
    background:var(--bg-deep); border-left:2px solid var(--apt-red);
    padding:16px 18px; margin-top:8px;
  }}
  .modal-foot {{
    display:flex; gap:10px; padding:18px 28px; border-top:1px solid var(--border-dim);
    background:var(--bg-deep); flex-wrap:wrap;
  }}
  .modal-btn {{
    padding:10px 16px; font-family:'DM Mono',monospace; font-size:10px;
    letter-spacing:2px; color:var(--text-primary); text-transform:uppercase;
    background:transparent; border:1px solid var(--border-bright); cursor:pointer;
    transition:all .15s; flex:1; text-align:center; min-width:140px;
  }}
  .modal-btn:hover {{ border-color:var(--apt-red); color:var(--apt-red); }}
  .modal-btn.primary {{ border-color:var(--apt-red); color:var(--apt-red); background:rgba(204,0,0,0.06); }}
  .modal-btn.primary:hover {{ background:rgba(204,0,0,0.18); color:#FFFFFF; }}
  @media (max-width:560px) {{
    .modal-head {{ padding:18px 20px 14px; }}
    .modal-body {{ padding:20px; }}
    .modal-foot {{ padding:14px 20px; }}
    .modal-headline {{ font-size:18px; }}
  }}

  /* Footer */
  .footer {{
    max-width:1200px; margin:80px auto 0; padding:24px;
    border-top:1px solid var(--border-dim);
    display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:12px;
  }}
  .footer .brand {{ font-family:'Syne',sans-serif; font-size:9px; font-weight:800; letter-spacing:5px; color:var(--text-dim); text-transform:uppercase; }}
  .footer .tagline {{ font-family:'DM Mono',monospace; font-size:10px; color:var(--text-muted); letter-spacing:1px; }}
  .footer .ts {{ font-family:'DM Mono',monospace; font-size:9px; letter-spacing:2px; color:var(--text-muted); }}

  @media (max-width:560px) {{
    .topnav {{ padding:0 18px; }}
    .rail {{ padding:12px 18px; top:56px; }}
    .main {{ padding:24px 18px 64px; }}
    .footer {{ padding:24px 18px; }}
    .brief-grid {{ grid-template-columns:1fr; }}
  }}
</style>
</head>
<body>

<nav class="topnav">
  <div class="lockup">
    {logo_nav}
    <div>
      <div class="dm">Apterreon</div>
      <div class="prod">Daily Intelligence Brief</div>
    </div>
  </div>
  <div class="stats">
    <div class="stat"><span class="v" id="stat-briefs">··</span><span class="l">Briefs</span></div>
    <div class="stat"><span class="v" id="stat-stories">··</span><span class="l">Stories</span></div>
    <div class="stat"><span class="v" id="stat-since">··</span><span class="l">Since</span></div>
  </div>
</nav>

<header class="hero">
  <div class="mark">{logo_hero}</div>
  <h1>Daily Intelligence Brief</h1>
  <div class="sub">Apterreon</div>
  <div class="desc">Three editions per day &middot; Markets, Politics, AI, World</div>
  <div class="rule"></div>
</header>

<div class="rail">
  <div class="rail-inner">
    <div class="rail-row">
      <label class="search">
        <span class="icon">&#8981;</span>
        <input type="search" id="search" placeholder="Search across briefs, stories, sources..." autocomplete="off" spellcheck="false">
        <button type="button" class="clear" id="search-clear" hidden>Clear</button>
      </label>
      <div class="tabs">
        <div class="tab active" data-tab="briefs">Briefs <span class="count">··</span></div>
        <div class="tab" data-tab="stories">Stories <span class="count">··</span></div>
        <div class="tab" data-tab="pinned">Pinned <span class="count">··</span></div>
      </div>
    </div>
    <div class="rail-row">
      <div class="chip-group">
        <span class="chip-label">Edition</span>
        <span class="chip" data-edition="morning">Morning</span>
        <span class="chip dim" data-edition="midday">Midday</span>
        <span class="chip grey" data-edition="evening">Evening</span>
      </div>
      <div class="chip-group" id="section-chips">
        <span class="chip-label">Sections</span>
      </div>
      <div class="sort-group">
        <span class="chip" data-sort="newest" id="sort-newest">Newest</span>
        <span class="chip" data-sort="oldest" id="sort-oldest">Oldest</span>
      </div>
    </div>
  </div>
</div>

<main class="main">
  <div id="briefs" class="view active"></div>
  <div id="stories" class="view"></div>
  <div id="pinned" class="view"></div>
</main>

<footer class="footer">
  <span class="brand">Apterreon</span>
  <span class="tagline">Explore what's out there.</span>
  <span class="ts" id="footer-ts"></span>
</footer>

<div class="modal-backdrop" id="story-modal" role="dialog" aria-modal="true" aria-labelledby="modal-headline">
  <div class="modal" role="document">
    <div class="modal-head">
      <span class="modal-section" id="modal-section">Section</span>
      <span class="modal-meta" id="modal-meta">&middot;</span>
      <button class="modal-close" id="modal-close" aria-label="Close">&#10005; Close</button>
    </div>
    <div class="modal-body">
      <h2 class="modal-headline" id="modal-headline">Headline</h2>
      <div class="modal-source" id="modal-source">Source</div>
      <div class="modal-block" id="modal-summary-block">
        <div class="modal-label">Summary</div>
        <div class="modal-text" id="modal-summary"></div>
      </div>
      <div class="modal-block" id="modal-insight-block">
        <div class="modal-label">Claude Insight</div>
        <div class="modal-text modal-insight" id="modal-insight"></div>
      </div>
    </div>
    <div class="modal-foot">
      <a class="modal-btn" id="modal-link-source" target="_blank" rel="noopener">Read source &#8594;</a>
      <a class="modal-btn primary" id="modal-link-brief">Open in brief &#8594;</a>
    </div>
  </div>
</div>

<script>
const briefs = {briefs_json};
const today = "{today_str}";
const SECTION_NAMES = {section_names_json};
const SECTION_COLORS = {section_colors_json};

document.getElementById('footer-ts').textContent =
  new Date().toLocaleString('en-US', {{ timeZone:'America/New_York', month:'short', day:'numeric', year:'numeric', hour:'numeric', minute:'2-digit' }}).toUpperCase();

// State
const state = {{
  query: '',
  tab: 'briefs',
  editionFilter: new Set(),  // 'morning', 'midday', 'evening'
  sectionFilter: new Set(),  // section names
  sort: 'newest',
}};

// Per-device pins (localStorage)
const PIN_STORE = 'dib.pins';
function loadLocalPins() {{
  try {{ return new Set(JSON.parse(localStorage.getItem(PIN_STORE) || '[]')); }}
  catch {{ return new Set(); }}
}}
function saveLocalPins(set) {{
  localStorage.setItem(PIN_STORE, JSON.stringify([...set]));
}}
const _localPins = loadLocalPins();
briefs.forEach(b => {{ if (_localPins.has(b.key)) b.pinned = true; }});

// Build a flat list of stories from all briefs (for STORIES view + search)
function buildStories() {{
  const out = [];
  briefs.forEach(b => {{
    (b.sections || []).forEach(sec => {{
      (sec.stories || []).forEach(s => {{
        out.push({{
          briefKey: b.key,
          briefDate: b.date,
          briefType: b.type,
          section: sec.name,
          headline: s.headline || '',
          summary: s.summary || '',
          insight: s.insight || '',
          source: s.source || '',
          link: s.link || '',
        }});
      }});
    }});
  }});
  return out;
}}
const allStories = buildStories();

// Helpers
function escapeHtml(s) {{
  return String(s == null ? '' : s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}}
function briefLabel(type) {{
  if (type === 'morning') return 'Morning Brief';
  if (type === 'midday')  return 'Midday Update';
  if (type === 'evening') return 'Evening Wrap';
  return type ? (type.charAt(0).toUpperCase() + type.slice(1)) : 'Brief';
}}
function briefNumber(type) {{
  if (type === 'morning') return '01';
  if (type === 'midday')  return '02';
  if (type === 'evening') return '03';
  return '··';
}}
function formatDate(dateStr) {{
  const d = new Date(dateStr + 'T12:00:00');
  return d.toLocaleDateString('en-US', {{ weekday:'long', month:'long', day:'numeric' }}).toUpperCase();
}}
function formatShortDate(dateStr) {{
  const d = new Date(dateStr + 'T12:00:00');
  return d.toLocaleDateString('en-US', {{ month:'short', day:'numeric' }}).toUpperCase();
}}

// Search matching — returns true if obj matches query (in any of the listed fields).
function matches(query, fields) {{
  if (!query) return true;
  const q = query.toLowerCase();
  for (const f of fields) {{ if (f && String(f).toLowerCase().includes(q)) return true; }}
  return false;
}}

function filterBriefs() {{
  return briefs.filter(b => {{
    if (state.editionFilter.size > 0 && !state.editionFilter.has(b.type)) return false;
    if (state.sectionFilter.size > 0) {{
      const briefSections = new Set((b.sections || []).map(s => s.name));
      let any = false;
      for (const s of state.sectionFilter) {{ if (briefSections.has(s)) {{ any = true; break; }} }}
      if (!any) return false;
    }}
    if (state.query) {{
      const fields = [b.date, b.type, briefLabel(b.type)];
      (b.sections || []).forEach(sec => {{
        fields.push(sec.name);
        (sec.stories || []).forEach(s => {{
          fields.push(s.headline, s.summary, s.source, s.insight);
        }});
      }});
      if (!matches(state.query, fields)) return false;
    }}
    return true;
  }});
}}

function filterStories() {{
  return allStories.filter(s => {{
    if (state.editionFilter.size > 0 && !state.editionFilter.has(s.briefType)) return false;
    if (state.sectionFilter.size > 0 && !state.sectionFilter.has(s.section)) return false;
    if (state.query) {{
      if (!matches(state.query, [s.headline, s.summary, s.source, s.insight, s.section, s.briefDate])) return false;
    }}
    return true;
  }});
}}

function sortByDate(arr, dateKey) {{
  return arr.slice().sort((a, b) => {{
    const cmp = (a[dateKey] || '').localeCompare(b[dateKey] || '');
    return state.sort === 'newest' ? -cmp : cmp;
  }});
}}

// Card renderers
function renderBriefCard(b) {{
  const pinClass = b.pinned ? 'pinned' : '';
  const pinLabel = b.pinned ? '\u2605 PINNED' : '\u2606 PIN';
  const safeKey = escapeHtml(b.key).replace(/'/g, "&#39;");
  const sections = b.sections || [];
  // Top 3 headlines preview (skip Breaking News if its stories have empty summaries — those are headline-only)
  let previews = [];
  for (const sec of sections) {{
    for (const story of (sec.stories || [])) {{
      if (story.headline) previews.push(story.headline);
      if (previews.length >= 3) break;
    }}
    if (previews.length >= 3) break;
  }}
  const previewHtml = previews.length
    ? previews.map(p => '<div class="bc-preview-headline">' + escapeHtml(p) + '</div>').join('')
    : '<div class="bc-preview-headline no-data">No story preview available</div>';
  const sectionTags = sections.length
    ? sections.slice(0, 4).map(s => '<span class="bc-section-tag">' + escapeHtml(s.name.split(' ')[0]) + '</span>').join('')
    : '';
  return '<article class="brief-card" data-type="' + escapeHtml(b.type) + '">' +
    '<div class="bc-head">' +
      '<div>' +
        '<div class="bc-num">' + briefNumber(b.type) + ' &middot; ' + formatShortDate(b.date) + '</div>' +
        '<div class="bc-title">' + briefLabel(b.type) + '</div>' +
        '<div class="bc-meta">' + formatDate(b.date) + '</div>' +
      '</div>' +
      '<button class="bc-pin ' + pinClass + '" data-key="' + safeKey + '">' + pinLabel + '</button>' +
    '</div>' +
    '<div class="bc-preview">' + previewHtml + '</div>' +
    '<a class="bc-foot" href="' + escapeHtml(b.key) + '">' +
      '<span class="bc-section-tags">' + sectionTags + '</span>' +
      '<span class="bc-open">Open &#8594;</span>' +
    '</a>' +
  '</article>';
}}

function renderBriefsView(list) {{
  if (list.length === 0) {{
    return '<div class="empty"><div class="big">No briefs match</div>Adjust filters or clear the search.</div>';
  }}
  // Group by date
  const byDate = {{}};
  list.forEach(b => {{ (byDate[b.date] = byDate[b.date] || []).push(b); }});
  const dates = Object.keys(byDate).sort();
  if (state.sort === 'newest') dates.reverse();
  let html = '';
  dates.forEach(date => {{
    const cards = byDate[date].map(renderBriefCard).join('');
    const dateHeader = date === today ? 'Today &middot; ' + formatDate(date) : formatDate(date);
    html += '<div class="date-group">' +
      '<div class="date-label"><span>' + dateHeader + '</span><span class="count">' + String(byDate[date].length).padStart(2,'0') + ' edition' + (byDate[date].length === 1 ? '' : 's') + '</span></div>' +
      '<div class="brief-grid">' + cards + '</div>' +
    '</div>';
  }});
  return html;
}}

function renderStoriesView(list) {{
  if (list.length === 0) {{
    return '<div class="empty"><div class="big">No stories match</div>Try a broader search or different filters.</div>';
  }}
  const sorted = list.slice().sort((a, b) => {{
    const cmp = (a.briefDate || '').localeCompare(b.briefDate || '');
    return state.sort === 'newest' ? -cmp : cmp;
  }});
  const byDate = {{}};
  sorted.forEach(s => {{ (byDate[s.briefDate] = byDate[s.briefDate] || []).push(s); }});
  const dates = Object.keys(byDate).sort();
  if (state.sort === 'newest') dates.reverse();
  let html = '';
  dates.forEach(date => {{
    const rows = byDate[date].map((s, i) => {{
      const link = escapeHtml(s.briefKey);
      const idx = allStories.indexOf(s);
      return '<a class="story-row" href="' + link + '" data-story-idx="' + idx + '">' +
        '<span class="story-section" style="color:' + (SECTION_COLORS[s.section] || '#888') + '">' + escapeHtml(s.section) + '</span>' +
        '<span class="story-headline">' + escapeHtml(s.headline) + '</span>' +
        '<span class="story-source">' + escapeHtml(s.source) + '</span>' +
        '<span class="story-edition">' + briefNumber(s.briefType) + ' &middot; ' + escapeHtml(s.briefType) + '</span>' +
      '</a>';
    }}).join('');
    const dateHeader = date === today ? 'Today &middot; ' + formatDate(date) : formatDate(date);
    html += '<div class="date-group">' +
      '<div class="date-label"><span>' + dateHeader + '</span><span class="count">' + String(byDate[date].length).padStart(2,'0') + ' stories</span></div>' +
      rows +
    '</div>';
  }});
  return html;
}}

// Story modal
const modalEl = document.getElementById('story-modal');
function openStoryModal(story) {{
  if (!story) return;
  const sectionEl = document.getElementById('modal-section');
  sectionEl.textContent = story.section || 'Story';
  sectionEl.style.color = SECTION_COLORS[story.section] || '#888';
  document.getElementById('modal-meta').textContent =
    formatShortDate(story.briefDate) + ' \u00B7 ' + briefLabel(story.briefType);
  document.getElementById('modal-headline').textContent = story.headline || '';
  document.getElementById('modal-source').textContent = story.source ? ('Source \u00B7 ' + story.source) : '';
  const summaryBlock = document.getElementById('modal-summary-block');
  const insightBlock = document.getElementById('modal-insight-block');
  if (story.summary) {{
    document.getElementById('modal-summary').textContent = story.summary;
    summaryBlock.style.display = '';
  }} else {{
    summaryBlock.style.display = 'none';
  }}
  if (story.insight) {{
    document.getElementById('modal-insight').textContent = story.insight;
    insightBlock.style.display = '';
  }} else {{
    insightBlock.style.display = 'none';
  }}
  const sourceLink = document.getElementById('modal-link-source');
  if (story.link) {{ sourceLink.href = story.link; sourceLink.style.display = ''; }}
  else {{ sourceLink.style.display = 'none'; }}
  const briefLink = document.getElementById('modal-link-brief');
  briefLink.href = story.briefKey || '#';
  modalEl.classList.add('open');
  document.body.style.overflow = 'hidden';
}}
function closeStoryModal() {{
  modalEl.classList.remove('open');
  document.body.style.overflow = '';
}}
document.getElementById('modal-close').addEventListener('click', closeStoryModal);
modalEl.addEventListener('click', e => {{ if (e.target === modalEl) closeStoryModal(); }});
document.addEventListener('keydown', e => {{ if (e.key === 'Escape' && modalEl.classList.contains('open')) closeStoryModal(); }});

// Delegate clicks on story rows: open modal unless modifier keys (let new-tab work).
document.getElementById('stories').addEventListener('click', e => {{
  const row = e.target.closest('.story-row[data-story-idx]');
  if (!row) return;
  if (e.metaKey || e.ctrlKey || e.shiftKey || e.button === 1) return;
  const idx = parseInt(row.dataset.storyIdx, 10);
  const story = allStories[idx];
  if (!story) return;
  e.preventDefault();
  openStoryModal(story);
}});

// Make brief-card preview headlines also open the matching story modal.
document.addEventListener('click', e => {{
  const ph = e.target.closest('.bc-preview-headline');
  if (!ph || ph.classList.contains('no-data')) return;
  if (e.metaKey || e.ctrlKey || e.shiftKey || e.button === 1) return;
  const card = ph.closest('.brief-card');
  if (!card) return;
  const headline = ph.textContent.trim();
  const briefType = card.dataset.type;
  const story = allStories.find(s => s.briefType === briefType && s.headline === headline);
  if (!story) return;
  e.preventDefault();
  e.stopPropagation();
  openStoryModal(story);
}});

function attachPinHandlers() {{
  document.querySelectorAll('.bc-pin').forEach(btn => {{
    btn.addEventListener('click', e => {{
      e.preventDefault(); e.stopPropagation();
      const key = btn.dataset.key;
      const b = briefs.find(x => x.key === key);
      if (!b) return;
      b.pinned = !b.pinned;
      const pins = loadLocalPins();
      if (b.pinned) pins.add(key); else pins.delete(key);
      saveLocalPins(pins);
      render();
    }});
  }});
}}

function render() {{
  const filteredBriefs = filterBriefs();
  const filteredStories = filterStories();
  const pinned = filteredBriefs.filter(b => b.pinned);

  document.getElementById('briefs').innerHTML = renderBriefsView(filteredBriefs);
  document.getElementById('stories').innerHTML = renderStoriesView(filteredStories);
  document.getElementById('pinned').innerHTML = renderBriefsView(pinned);

  // Update tab counts
  const counts = {{ briefs: filteredBriefs.length, stories: filteredStories.length, pinned: pinned.length }};
  document.querySelectorAll('.tab').forEach(t => {{
    const c = t.querySelector('.count');
    if (c) c.textContent = String(counts[t.dataset.tab] || 0).padStart(2, '0');
  }});

  attachPinHandlers();
}}

function setActiveTab(name) {{
  state.tab = name;
  document.querySelectorAll('.tab').forEach(t => t.classList.toggle('active', t.dataset.tab === name));
  document.querySelectorAll('.view').forEach(v => v.classList.toggle('active', v.id === name));
}}

// Wire UI
document.querySelectorAll('.tab').forEach(t => {{
  t.addEventListener('click', () => setActiveTab(t.dataset.tab));
}});

document.querySelectorAll('.chip[data-edition]').forEach(c => {{
  c.addEventListener('click', () => {{
    const ed = c.dataset.edition;
    if (state.editionFilter.has(ed)) state.editionFilter.delete(ed);
    else state.editionFilter.add(ed);
    c.classList.toggle('active');
    render();
  }});
}});

// Build section chips dynamically
const sectionChipsEl = document.getElementById('section-chips');
SECTION_NAMES.forEach(name => {{
  const chip = document.createElement('span');
  const color = SECTION_COLORS[name] || '#888';
  const tier = color === '#CC0000' ? '' : (color === '#7A1010' ? ' dim' : ' grey');
  chip.className = 'chip' + tier;
  chip.dataset.section = name;
  chip.textContent = name;
  chip.addEventListener('click', () => {{
    if (state.sectionFilter.has(name)) state.sectionFilter.delete(name);
    else state.sectionFilter.add(name);
    chip.classList.toggle('active');
    render();
  }});
  sectionChipsEl.appendChild(chip);
}});

// Sort
document.querySelectorAll('.chip[data-sort]').forEach(c => {{
  c.addEventListener('click', () => {{
    state.sort = c.dataset.sort;
    document.querySelectorAll('.chip[data-sort]').forEach(x => x.classList.toggle('active', x.dataset.sort === state.sort));
    render();
  }});
}});
document.getElementById('sort-newest').classList.add('active');

// Search
const searchInput = document.getElementById('search');
const searchClear = document.getElementById('search-clear');
searchInput.addEventListener('input', () => {{
  state.query = searchInput.value.trim();
  searchClear.hidden = !state.query;
  render();
}});
searchClear.addEventListener('click', () => {{
  searchInput.value = '';
  state.query = '';
  searchClear.hidden = true;
  searchInput.focus();
  render();
}});

// Stats
document.getElementById('stat-briefs').textContent = String(briefs.length).padStart(2, '0');
document.getElementById('stat-stories').textContent = String(allStories.length).padStart(3, '0');
const oldestDate = briefs.length ? briefs[briefs.length - 1].date : '';
document.getElementById('stat-since').textContent = oldestDate ? formatShortDate(oldestDate) : '\u2014';

// First render
render();
</script>
</body>
</html>"""

    (DOCS_DIR / "index.html").write_text(index_html, encoding="utf-8")

    # Also write PWA manifest
    manifest = {
        "name": "Daily Intelligence Brief — Apterreon",
        "short_name": "DIB",
        "description": "Apterreon Daily Intelligence Brief — explore what's out there.",
        "start_url": "./index.html",
        "display": "standalone",
        "background_color": BG_BASE,
        "theme_color": BG_BASE,
    }
    (DOCS_DIR / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print("Wrote docs/index.html and docs/manifest.json.")


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
        print(f"Weekend — skipping {brief_type} brief.")
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
        subject = f"{config['subject_prefix']} \u2014 {date_str}"
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

    # Parse JSON — extract the object even if the model adds commentary or truncates.
    cleaned = raw_response.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[-1]
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3]
    cleaned = cleaned.strip()

    # Find the JSON object by matching braces. If depth never closes (truncation),
    # take everything from the first { to the end so json.loads gives a useful error
    # — not an empty string.
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
            subject = f"{config['subject_prefix']} \u2014 {date_str} \u2014 Generation Error"
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
    title = f"{config['subject_prefix']} \u2014 {date_str}"
    email_html = build_email_preview(title, data, quotes, timestamp, usage_info)
    interactive_html = build_interactive_html(title, data, quotes, timestamp, usage_info)

    # 6. Send email (preview only — no attachment for minimal traceability)
    send_email(title, email_html)

    # 7. Publish brief HTML + JSON sidecar, regenerate site index
    s3_publish_brief(brief_type, now_et, interactive_html, data=data, quotes=quotes, timestamp=timestamp)

    return {"status": "sent", "brief_type": brief_type, "stories": len(headlines), "quotes": len(quotes), "usage": usage_info}


if __name__ == "__main__":
    import sys
    brief_type = sys.argv[1] if len(sys.argv) > 1 else "morning"
    result = lambda_handler({"brief_type": brief_type}, None)
    print(json.dumps(result, indent=2))
