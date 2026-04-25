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

SENDER_EMAIL = "ctlsmith@me.com"
RECIPIENT_EMAIL = os.environ.get("RECIPIENTS", "ctlsmith@me.com")
SMTP_SERVER = "smtp.mail.me.com"
SMTP_PORT = 587

ANTHROPIC_MODEL = os.environ.get("JEEVES_MODEL", "claude-sonnet-4-6")
ET_OFFSET = timedelta(hours=-4)  # EDT

CLAUDE_ORANGE = "#E07A2F"

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

SECTION_COLORS = {
    "Breaking News": "#c0392b",
    "Finance & Markets": "#1a5276",
    "Politics & Policy": "#7b241c",
    "AI & Technology": "#0e6655",
    "International": "#6c3483",
    "Culture & Sports": "#b9770e",
    "Boston": "#2e4053",
}

SECTION_ICONS = {
    "Breaking News": "&#128680;",
    "Finance & Markets": "&#128200;",
    "Politics & Policy": "&#127963;",
    "AI & Technology": "&#129302;",
    "International": "&#127758;",
    "Culture & Sports": "&#127917;",
    "Boston": "&#127961;",
}

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
        "max_tokens": 8192,
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

READER_CONTEXT = os.environ.get("JEEVES_READER_CONTEXT", "A curious, analytically rigorous reader.")

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
    reader = os.environ.get("JEEVES_READER_CONTEXT", "A curious, analytically rigorous reader.")
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
    """Market data row for the email preview."""
    if not quotes:
        return ""
    cells = ""
    for q in quotes:
        is_yield = q.get("is_yield", False)
        if is_yield:
            cells += f"""<td style="padding:8px 10px;text-align:center">
<div style="font-size:10px;color:#888;margin-bottom:2px">{q['label']}</div>
<div style="font-size:15px;font-weight:700;color:#1a1a1a">{q['price']}</div>
<div style="font-size:11px;color:#888">7d yield</div>
</td>"""
        else:
            try:
                change = float(q["change_pct"])
            except (ValueError, KeyError):
                change = 0
            color = "#27ae60" if change >= 0 else "#e74c3c"
            arrow = "&#9650;" if change >= 0 else "&#9660;"
            cells += f"""<td style="padding:8px 10px;text-align:center">
<div style="font-size:10px;color:#888;margin-bottom:2px">{q['label']}</div>
<div style="font-size:15px;font-weight:700;color:#1a1a1a">{q['price']}</div>
<div style="font-size:11px;color:{color}">{arrow} {abs(change):.2f}%</div>
</td>"""
    return f"""<table width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:24px;border-bottom:1px solid #eee;padding-bottom:16px">
<tr>{cells}</tr></table>"""


def market_bar_interactive(quotes):
    """Market data row for the interactive HTML."""
    if not quotes:
        return "[]"
    return json.dumps(quotes)


# ── Email Preview ──────────────────────────────────────────────────────────

def usage_banner_email(usage_info):
    """API usage banner for the email."""
    if not usage_info:
        return ""
    cost = usage_info.get("cost_this_call", 0)
    monthly = usage_info.get("cost_monthly_projected", 0)
    tokens = usage_info.get("total_tokens", 0)

    if monthly < 2:
        bar_color = "#27ae60"
        status = "LOW"
    elif monthly < 5:
        bar_color = "#f39c12"
        status = "MODERATE"
    else:
        bar_color = "#e74c3c"
        status = "HIGH"

    budget = 10.0
    pct = min(100, (monthly / budget) * 100)

    return f"""<table width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:16px">
<tr><td style="padding:10px 14px;background:#f8f8f8;border-radius:6px;border:1px solid #eee">
<table width="100%" cellpadding="0" cellspacing="0">
<tr>
<td style="font-size:10px;color:#888;text-transform:uppercase;letter-spacing:1px;font-weight:700">API Usage</td>
<td style="text-align:right;font-size:10px;color:{bar_color};font-weight:700">{status}</td>
</tr>
<tr><td colspan="2" style="padding-top:6px">
<div style="background:#eee;border-radius:3px;height:4px;overflow:hidden"><div style="background:{bar_color};width:{pct:.0f}%;height:4px;border-radius:3px"></div></div>
</td></tr>
<tr><td colspan="2" style="padding-top:4px;font-size:10px;color:#999">
This brief: ${cost:.4f} ({tokens:,} tokens) &middot; Projected: ${monthly:.2f}/mo &middot; Budget: $10.00/mo
</td></tr>
</table>
</td></tr></table>"""


def build_email_preview(title, data, quotes, timestamp, usage_info=None):
    """Clean headline-only email preview with market bar and usage banner."""
    usage_html = usage_banner_email(usage_info)
    market_html = market_bar_email(quotes)
    sections_html = ""

    for sec_name, _ in SECTIONS:
        color = SECTION_COLORS.get(sec_name, "#333")
        sec_data = next((s for s in data.get("sections", []) if s["name"] == sec_name), None)
        if not sec_data or not sec_data.get("stories"):
            continue

        stories_html = ""
        for story in sec_data["stories"]:
            stories_html += f"""<tr>
<td style="padding:5px 0;font-size:14px;color:#1a1a1a;border-bottom:1px solid #f0f0f0">{story['headline']} <span style="color:#aaa;font-size:11px">— {story.get('source','')}</span></td>
</tr>"""

        sections_html += f"""<table width="100%" cellpadding="0" cellspacing="0" style="margin:16px 0 4px">
<tr><td style="font-size:12px;font-weight:700;color:{color};text-transform:uppercase;letter-spacing:1px;padding-bottom:5px;border-bottom:2px solid {color}">{sec_name}</td></tr>
{stories_html}</table>"""

    edge_text = data.get("the_edge", "")
    edge_html = ""
    if edge_text:
        edge_html = f"""<table width="100%" cellpadding="0" cellspacing="0" style="margin:24px 0 0">
<tr><td style="padding:14px 16px;background:#fdf3eb;border-left:3px solid {CLAUDE_ORANGE};border-radius:4px">
<div style="font-size:11px;font-weight:700;color:{CLAUDE_ORANGE};text-transform:uppercase;letter-spacing:1px;margin-bottom:4px">The Edge</div>
<div style="font-size:13px;color:#555;line-height:1.5">{edge_text[:200]}...</div>
</td></tr></table>"""

    return f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#f5f5f5">
<div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;max-width:600px;margin:0 auto;padding:24px 20px;color:#1a1a1a;background:#ffffff">
{usage_html}
<h1 style="font-size:20px;font-weight:700;margin:0 0 4px">{title}</h1>
<p style="margin:0 0 16px;font-size:12px;color:#999">Headlines &amp; insights at a glance</p>
{market_html}
{sections_html}
{edge_html}
<div style="margin-top:28px;padding-top:10px;border-top:1px solid #eee">
<p style="color:#bbb;font-size:10px;margin:0">{timestamp}</p>
</div>
</div>
</body>
</html>"""


# ── Interactive HTML Attachment ─────────────────────────────────────────────

def build_interactive_html(title, data, quotes, timestamp, usage_info=None):
    """Self-contained interactive HTML with widget cards and tap-to-expand."""

    sections_json = json.dumps(data.get("sections", []))
    edge_text = json.dumps(data.get("the_edge", ""))
    tomorrow_text = json.dumps(data.get("tomorrow_watch", ""))
    colors_json = json.dumps(SECTION_COLORS)
    icons_json = json.dumps(SECTION_ICONS)
    quotes_json = market_bar_interactive(quotes)
    section_order_json = json.dumps(SECTION_NAMES)
    usage_json = json.dumps(usage_info or {})
    json_no_insight = json.dumps(sorted(NO_INSIGHT_SECTIONS))

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
<style>
  * {{ margin:0; padding:0; box-sizing:border-box; }}
  body {{ font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif; background:#f5f5f5; color:#1a1a1a; line-height:1.6; -webkit-font-smoothing:antialiased; }}
  .container {{ max-width:700px; margin:0 auto; padding:20px 16px 60px; }}

  /* Header */
  .header {{ margin-bottom:20px; }}
  .header h1 {{ font-size:22px; font-weight:700; color:#1a1a1a; }}
  .header .meta {{ font-size:11px; color:#999; margin-top:2px; }}

  /* Market Bar */
  .market-bar {{
    display:flex; gap:8px; margin-bottom:24px; overflow-x:auto;
    padding-bottom:4px; -webkit-overflow-scrolling:touch;
  }}
  .market-card {{
    flex:1; min-width:80px; background:#ffffff; border-radius:10px;
    padding:12px 10px; text-align:center; border:1px solid #e0e0e0;
  }}
  .market-card .label {{ font-size:10px; color:#888; text-transform:uppercase; letter-spacing:0.5px; }}
  .market-card .price {{ font-size:18px; font-weight:700; color:#1a1a1a; margin:4px 0 2px; }}
  .market-card .change {{ font-size:12px; font-weight:600; }}
  .market-card .change.up {{ color:#1e8449; }}
  .market-card .change.down {{ color:#c0392b; }}

  /* Section Widgets */
  .widgets {{ display:flex; flex-direction:column; gap:12px; }}

  .widget {{
    background:#ffffff; border-radius:12px; border:1px solid #e0e0e0;
    overflow:hidden; transition:border-color 0.2s;
  }}
  .widget.active {{ border-color:#bbb; }}

  .widget-header {{
    display:flex; align-items:flex-start; padding:16px 18px; cursor:pointer;
    gap:14px; transition:background 0.15s;
  }}
  .widget-header:hover {{ background:#fafafa; }}

  .widget-icon {{ font-size:24px; flex-shrink:0; }}
  .widget-info {{ flex:1; }}
  .widget-title {{
    font-size:14px; font-weight:700; text-transform:uppercase; letter-spacing:1px;
  }}
  .widget-headlines {{
    margin:6px 0 0 0; padding:0; list-style:none;
  }}
  .widget-headlines li {{
    font-size:12px; color:#666; line-height:1.4; padding:2px 0;
    padding-left:12px; position:relative; word-wrap:break-word;
  }}
  .widget-headlines li::before {{
    content:'\\2022'; position:absolute; left:0; color:#999;
  }}
  .widget-count {{
    font-size:11px; color:#888; background:#f0f0f0; padding:3px 8px;
    border-radius:10px; flex-shrink:0;
  }}
  .widget-chevron {{
    color:#bbb; font-size:16px; transition:transform 0.2s; flex-shrink:0;
  }}
  .widget.active .widget-chevron {{ transform:rotate(90deg); color:{CLAUDE_ORANGE}; }}

  /* Stories inside widget */
  .widget-body {{
    max-height:0; overflow:hidden; transition:max-height 0.35s ease;
  }}
  .widget.active .widget-body {{ max-height:2000px; }}

  .widget-stories {{ padding:0 18px 16px; }}

  .story {{
    padding:14px 0; border-top:1px solid #eee; cursor:pointer;
  }}
  .story:first-child {{ border-top:none; }}

  .story-headline {{
    font-size:15px; font-weight:600; color:#1a1a1a;
    display:flex; justify-content:space-between; align-items:flex-start; gap:10px;
  }}
  .story-headline .arrow {{
    font-size:11px; color:#ccc; transition:transform 0.2s, color 0.2s;
    flex-shrink:0; margin-top:4px;
  }}
  .story.open .story-headline .arrow {{ transform:rotate(90deg); color:{CLAUDE_ORANGE}; }}
  .story-source {{ font-size:11px; color:#999; margin-top:2px; }}

  .story-details {{
    max-height:0; overflow:hidden; transition:max-height 0.3s ease;
  }}
  .story.open .story-details {{ max-height:500px; }}

  .story-summary {{
    font-size:14px; color:#555; margin:12px 0 10px; line-height:1.55;
  }}
  .story-insight {{
    font-size:13px; color:#b35c1e; line-height:1.55;
    padding:12px 14px; background:rgba(224,122,47,0.08); border-radius:8px;
    border-left:3px solid {CLAUDE_ORANGE};
  }}
  .insight-label {{
    font-size:9px; font-weight:700; text-transform:uppercase; letter-spacing:1.5px;
    color:{CLAUDE_ORANGE}; opacity:0.7; margin-bottom:4px;
  }}

  /* The Edge */
  .edge {{
    margin-top:24px; padding:20px; background:#ffffff; border-radius:12px;
    border:1px solid rgba(224,122,47,0.2);
  }}
  .edge-title {{
    font-size:11px; font-weight:700; text-transform:uppercase; letter-spacing:2px;
    color:{CLAUDE_ORANGE}; margin-bottom:10px;
  }}
  .edge p {{ font-size:14px; color:#555; line-height:1.7; }}

  /* Tomorrow */
  .tomorrow {{
    margin-top:12px; padding:16px 20px; background:#ffffff; border-radius:12px;
    border:1px solid #e0e0e0;
  }}
  .tomorrow-title {{
    font-size:11px; font-weight:700; text-transform:uppercase; letter-spacing:2px;
    color:#888; margin-bottom:8px;
  }}
  .tomorrow p {{ font-size:13px; color:#666; line-height:1.55; }}

  /* Back Nav */
  .back-nav {{
    display:flex; align-items:center; gap:8px; margin-bottom:20px;
    padding:10px 0;
  }}
  .back-nav a {{
    display:flex; align-items:center; gap:6px; text-decoration:none;
    font-size:13px; font-weight:600; color:#888; transition:color 0.2s;
  }}
  .back-nav a:hover {{ color:{CLAUDE_ORANGE}; }}
  .back-nav .arrow {{ font-size:16px; }}

  .footer {{ margin-top:32px; text-align:center; }}
  .footer p {{ font-size:11px; color:#999; }}

  /* Usage Banner */
  .usage-banner {{
    background:#ffffff; border-radius:10px; padding:12px 16px; margin-bottom:20px;
    border:1px solid #e0e0e0;
  }}
  .usage-row {{
    display:flex; justify-content:space-between; align-items:center; margin-bottom:6px;
  }}
  .usage-label {{ font-size:10px; color:#888; text-transform:uppercase; letter-spacing:1.5px; font-weight:700; }}
  .usage-status {{ font-size:10px; font-weight:700; }}
  .usage-bar {{
    background:#e8e8e8; border-radius:3px; height:4px; overflow:hidden; margin-bottom:6px;
  }}
  .usage-bar-fill {{ height:4px; border-radius:3px; transition:width 0.3s; }}
  .usage-details {{ font-size:10px; color:#888; }}

  @media (max-width:480px) {{
    .container {{ padding:14px 10px 40px; }}
    .widget-header {{ padding:14px 14px; }}
    .widget-stories {{ padding:0 14px 14px; }}
    .market-card {{ min-width:70px; padding:10px 6px; }}
    .market-card .price {{ font-size:15px; }}
  }}
</style>
</head>
<body>
<div class="container">
  <div class="back-nav"><a href="/index.html"><span class="arrow">&#9664;</span> Briefs</a></div>
  <div id="usage"></div>
  <div class="header">
    <h1>{title}</h1>
    <div class="meta">{timestamp} &middot; Tap any section to expand</div>
  </div>
  <div id="market-bar" class="market-bar"></div>
  <div id="widgets" class="widgets"></div>
  <div id="edge"></div>
  <div id="tomorrow"></div>
  <div class="footer"><p>{timestamp}</p></div>
</div>
<script>
const usageInfo = {usage_json};
const rawSections = {sections_json};
const edgeText = {edge_text};
const tomorrowText = {tomorrow_text};
const colors = {colors_json};
const icons = {icons_json};
const quotes = {quotes_json};
const sectionOrder = {section_order_json};

// Usage banner
if (usageInfo && usageInfo.cost_monthly_projected !== undefined) {{
  const monthly = usageInfo.cost_monthly_projected;
  const cost = usageInfo.cost_this_call || 0;
  const tokens = usageInfo.total_tokens || 0;
  const budget = 10.0;
  const pct = Math.min(100, (monthly / budget) * 100);
  let barColor, status;
  if (monthly < 2) {{ barColor = '#27ae60'; status = 'LOW'; }}
  else if (monthly < 5) {{ barColor = '#f39c12'; status = 'MODERATE'; }}
  else {{ barColor = '#e74c3c'; status = 'HIGH'; }}

  document.getElementById('usage').innerHTML =
    '<div class="usage-banner">' +
      '<div class="usage-row">' +
        '<span class="usage-label">API Usage</span>' +
        '<span class="usage-status" style="color:' + barColor + '">' + status + '</span>' +
      '</div>' +
      '<div class="usage-bar"><div class="usage-bar-fill" style="background:' + barColor + ';width:' + pct.toFixed(0) + '%"></div></div>' +
      '<div class="usage-details">This brief: $' + cost.toFixed(4) + ' (' + tokens.toLocaleString() + ' tokens) &middot; Projected: $' + monthly.toFixed(2) + '/mo &middot; Budget: $10.00/mo</div>' +
    '</div>';
}}

// Market bar
const marketBar = document.getElementById('market-bar');
quotes.forEach(q => {{
  const card = document.createElement('div');
  card.className = 'market-card';
  if (q.is_yield) {{
    card.innerHTML =
      '<div class="label">' + q.label + '</div>' +
      '<div class="price">' + q.price + '</div>' +
      '<div class="change" style="color:#888">7d yield</div>';
  }} else {{
    const change = parseFloat(q.change_pct) || 0;
    const dir = change >= 0 ? 'up' : 'down';
    const arrow = change >= 0 ? '&#9650;' : '&#9660;';
    card.innerHTML =
      '<div class="label">' + q.label + '</div>' +
      '<div class="price">' + q.price + '</div>' +
      '<div class="change ' + dir + '">' + arrow + ' ' + Math.abs(change).toFixed(2) + '%</div>';
  }}
  marketBar.appendChild(card);
}});

// Build sections in fixed order
const sectionsMap = {{}};
rawSections.forEach(s => {{ sectionsMap[s.name] = s; }});

const widgetsContainer = document.getElementById('widgets');

sectionOrder.forEach(secName => {{
  const section = sectionsMap[secName] || {{ name: secName, stories: [] }};
  const color = colors[secName] || '#888';
  const icon = icons[secName] || '&#128196;';
  const stories = section.stories || [];

  // Build headline bullet list for widget header
  var headlineBullets = '';
  if (stories.length > 0) {{
    headlineBullets = '<ul class="widget-headlines">';
    stories.forEach(function(s) {{
      headlineBullets += '<li>' + s.headline + '</li>';
    }});
    headlineBullets += '</ul>';
  }} else {{
    headlineBullets = '<ul class="widget-headlines"><li>No major stories this cycle</li></ul>';
  }}

  const widget = document.createElement('div');
  widget.className = 'widget';

  // Header
  const header = document.createElement('div');
  header.className = 'widget-header';
  header.innerHTML =
    '<span class="widget-icon">' + icon + '</span>' +
    '<div class="widget-info">' +
      '<div class="widget-title" style="color:' + color + '">' + secName + '</div>' +
      headlineBullets +
    '</div>' +
    '<span class="widget-count">' + stories.length + '</span>' +
    '<span class="widget-chevron">&#9656;</span>';

  header.addEventListener('click', function() {{
    widget.classList.toggle('active');
  }});

  // Body
  const body = document.createElement('div');
  body.className = 'widget-body';

  const storiesDiv = document.createElement('div');
  storiesDiv.className = 'widget-stories';

  const noInsight = {json_no_insight};
  const skipInsight = noInsight.includes(secName);

  stories.forEach(story => {{
    const storyEl = document.createElement('div');
    storyEl.className = 'story';
    const linkHtml = story.link ? '<a href="' + story.link + '" target="_blank" rel="noopener" style="display:inline-block;margin-top:8px;font-size:12px;color:{CLAUDE_ORANGE};text-decoration:none">Read source &#8594;</a>' : '';
    if (skipInsight) {{
      // Breaking News — headline + source + link only, no expand
      storyEl.innerHTML =
        '<div class="story-headline"><span>' + story.headline + '</span></div>' +
        '<div class="story-source">' + (story.source || '') + '</div>' +
        (story.link ? '<a href="' + story.link + '" target="_blank" rel="noopener" style="display:inline-block;margin-top:4px;font-size:12px;color:{CLAUDE_ORANGE};text-decoration:none">Read source &#8594;</a>' : '');
    }} else {{
      storyEl.innerHTML =
        '<div class="story-headline"><span>' + story.headline + '</span><span class="arrow">&#9656;</span></div>' +
        '<div class="story-source">' + (story.source || '') + '</div>' +
        '<div class="story-details">' +
          '<div class="story-summary">' + story.summary + '</div>' +
          '<div class="story-insight">' +
            '<div class="insight-label">Claude Insight</div>' +
            story.insight +
          '</div>' +
          linkHtml +
        '</div>';
      storyEl.addEventListener('click', function(e) {{
        if (e.target.tagName === 'A') return;
        e.stopPropagation();
        this.classList.toggle('open');
      }});
    }}
    storiesDiv.appendChild(storyEl);
  }});

  body.appendChild(storiesDiv);
  widget.appendChild(header);
  widget.appendChild(body);
  widgetsContainer.appendChild(widget);
}});

// The Edge
if (edgeText) {{
  document.getElementById('edge').innerHTML =
    '<div class="edge"><div class="edge-title">&#9889; The Edge</div><p>' + edgeText + '</p></div>';
}}

// Tomorrow's Watch
if (tomorrowText) {{
  document.getElementById('tomorrow').innerHTML =
    '<div class="tomorrow"><div class="tomorrow-title">&#128337; Tomorrow\\'s Watch</div><p>' + tomorrowText + '</p></div>';
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
                link_html = f'<a href="{story["link"]}" style="display:inline-block;margin-top:8px;font-size:12px;color:{CLAUDE_ORANGE};text-decoration:none">Read source &#8594;</a>'
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
  <div style="font-size:13px;color:{CLAUDE_ORANGE};line-height:1.55;padding:12px 14px;background:rgba(224,122,47,0.06);border-radius:8px;border-left:3px solid {CLAUDE_ORANGE}">
    <div style="font-size:9px;font-weight:700;text-transform:uppercase;letter-spacing:1.5px;color:{CLAUDE_ORANGE};opacity:0.6;margin-bottom:4px">Claude Insight</div>
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
  <div style="font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:2px;color:{CLAUDE_ORANGE};margin-bottom:10px">&#9889; The Edge</div>
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
    app_password = os.environ.get("JEEVES_ICLOUD_APP_PASSWORD")
    if not app_password:
        raise ValueError("JEEVES_ICLOUD_APP_PASSWORD not set")

    recipients = [r.strip() for r in RECIPIENT_EMAIL.split(",") if r.strip()]
    if not recipients:
        raise ValueError("RECIPIENTS env var resolved to empty list")

    msg = MIMEMultipart("mixed")
    msg["Subject"] = subject
    msg["From"] = f"Intelligence Brief <{SENDER_EMAIL}>"
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
        server.login(SENDER_EMAIL, app_password)
        server.sendmail(SENDER_EMAIL, recipients, msg.as_string())

    print(f"Email sent to {len(recipients)} recipient(s): {subject}")


# ── Filesystem Storage (replaces S3) ───────────────────────────────────────

def s3_load_last_headlines():
    """Load the last brief's headline + URL sets from disk."""
    f = STATE_DIR / "last_headlines.json"
    if not f.exists():
        return set(), set()
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
        return set(data.get("headlines", [])), set(data.get("urls", []))
    except Exception:
        return set(), set()


def s3_save_headlines(headlines, urls):
    """Save current headline + URL sets to disk for next run comparison."""
    f = STATE_DIR / "last_headlines.json"
    f.write_text(
        json.dumps({"headlines": sorted(headlines), "urls": sorted(urls)}, indent=2),
        encoding="utf-8",
    )


def check_headline_overlap(current_headlines, current_urls, threshold=0.80):
    """Compare current headlines + URLs against last run. Returns (should_skip, overlap_pct).
    Uses the HIGHER overlap of headlines or URLs — catches reworded headlines for same articles."""
    last_headlines, last_urls = s3_load_last_headlines()
    if not last_headlines and not last_urls:
        return False, 0.0
    if not current_headlines:
        return True, 1.0

    headline_overlap = len(current_headlines & last_headlines) / len(current_headlines) if current_headlines else 0
    url_overlap = len(current_urls & last_urls) / len(current_urls) if current_urls else 0
    pct = max(headline_overlap, url_overlap)
    print(f"Dedup: headline overlap {headline_overlap:.0%}, URL overlap {url_overlap:.0%} → using {pct:.0%}")
    return pct >= threshold, round(pct, 2)


def s3_write_brief(brief_type, date_str_iso, interactive_html):
    """Write brief HTML to docs/briefs/YYYY-MM-DD-type.html."""
    key = f"briefs/{date_str_iso}-{brief_type}.html"
    out_path = DOCS_DIR / key
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(interactive_html, encoding="utf-8")
    print(f"Wrote {out_path}")
    return key


def s3_cleanup_old_briefs():
    """Delete briefs older than retention period, skip pinned."""
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
    """List all brief files with metadata, sorted newest first."""
    pinned = s3_load_pins()
    briefs = []
    for path in BRIEFS_DIR.glob("*.html"):
        filename = path.stem  # e.g., "2026-04-25-morning"
        parts = filename.rsplit("-", 1)
        if len(parts) == 2:
            date_part, brief_type = parts
        else:
            date_part, brief_type = filename, "unknown"
        key = f"briefs/{path.name}"
        briefs.append({
            "key": key,
            "date": date_part,
            "type": brief_type,
            "modified": datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat(),
            "pinned": key in pinned,
        })
    briefs.sort(key=lambda b: b["date"], reverse=True)
    return briefs


def s3_generate_index(briefs):
    """Generate docs/index.html (and manifest.json) with today/archive/pinned views."""
    now_et = datetime.now(timezone(ET_OFFSET))
    today_str = now_et.strftime("%Y-%m-%d")

    briefs_json = json.dumps(briefs)

    index_html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="theme-color" content="#0a0a0a">
<link rel="manifest" href="manifest.json">
<link rel="apple-touch-icon" href="icon-192.png">
<title>Intelligence Brief</title>
<style>
  * {{ margin:0; padding:0; box-sizing:border-box; }}
  body {{
    font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
    background:#0a0a0a; color:#e8e8e8; line-height:1.6;
    -webkit-font-smoothing:antialiased;
    padding:env(safe-area-inset-top) env(safe-area-inset-right) env(safe-area-inset-bottom) env(safe-area-inset-left);
  }}
  .container {{ max-width:700px; margin:0 auto; padding:20px 16px 80px; }}
  .header {{ text-align:center; margin-bottom:28px; padding-top:10px; }}
  .header h1 {{ font-size:28px; font-weight:700; color:#fff; letter-spacing:-0.5px; }}
  .header .sub {{ font-size:12px; color:#555; margin-top:4px; }}
  .header .orange {{ color:{CLAUDE_ORANGE}; }}

  /* Tab Navigation */
  .tabs {{
    display:flex; gap:0; margin-bottom:24px; background:#141414;
    border-radius:12px; padding:4px; border:1px solid #1e1e1e;
  }}
  .tab {{
    flex:1; text-align:center; padding:10px 12px; font-size:13px;
    font-weight:600; color:#666; cursor:pointer; border-radius:8px;
    transition:all 0.2s;
  }}
  .tab.active {{ background:#1e1e1e; color:#fff; }}
  .tab .count {{
    display:inline-block; font-size:10px; background:#1e1e1e; color:#888;
    padding:1px 6px; border-radius:8px; margin-left:4px;
  }}
  .tab.active .count {{ background:#333; color:#bbb; }}

  /* Brief Cards */
  .view {{ display:none; }}
  .view.active {{ display:block; }}

  .date-group {{ margin-bottom:20px; }}
  .date-label {{
    font-size:11px; font-weight:700; color:#555; text-transform:uppercase;
    letter-spacing:1.5px; margin-bottom:10px; padding-left:4px;
  }}

  .brief-card {{
    display:flex; align-items:center; background:#141414; border-radius:12px;
    border:1px solid #1e1e1e; padding:16px 18px; margin-bottom:8px;
    cursor:pointer; transition:border-color 0.2s, background 0.2s;
    text-decoration:none; color:inherit;
  }}
  .brief-card:hover {{ border-color:#333; background:#1a1a1a; }}

  .brief-icon {{
    width:40px; height:40px; border-radius:10px; display:flex;
    align-items:center; justify-content:center; font-size:18px;
    flex-shrink:0; margin-right:14px;
  }}
  .brief-icon.morning {{ background:rgba(241,196,15,0.15); }}
  .brief-icon.midday {{ background:rgba(52,152,219,0.15); }}
  .brief-icon.evening {{ background:rgba(155,89,182,0.15); }}

  .brief-info {{ flex:1; }}
  .brief-title {{ font-size:15px; font-weight:600; color:#e0e0e0; }}
  .brief-meta {{ font-size:11px; color:#555; margin-top:2px; }}

  .brief-pin {{
    width:32px; height:32px; border-radius:8px; border:none;
    background:transparent; cursor:pointer; font-size:16px;
    color:#444; transition:color 0.2s; display:flex;
    align-items:center; justify-content:center; flex-shrink:0;
  }}
  .brief-pin:hover {{ color:{CLAUDE_ORANGE}; }}
  .brief-pin.pinned {{ color:{CLAUDE_ORANGE}; }}

  .brief-arrow {{ color:#444; margin-left:8px; flex-shrink:0; }}

  .empty {{
    text-align:center; padding:40px 20px; color:#444; font-size:14px;
  }}

  .footer {{
    text-align:center; margin-top:40px; padding-top:20px;
    border-top:1px solid #1a1a1a;
  }}
  .footer p {{ font-size:11px; color:#333; }}

  @media (max-width:480px) {{
    .container {{ padding:14px 10px 60px; }}
    .brief-card {{ padding:14px 14px; }}
  }}
</style>
</head>
<body>
<div class="container">
  <div class="header">
    <h1>Intelligence Brief</h1>
    <div class="sub">Daily Intelligence Brief</div>
  </div>

  <div class="tabs">
    <div class="tab active" data-tab="today">Today</div>
    <div class="tab" data-tab="archive">Archive</div>
    <div class="tab" data-tab="pinned">Pinned</div>
  </div>

  <div id="today" class="view active"></div>
  <div id="archive" class="view"></div>
  <div id="pinned" class="view"></div>

  <div class="footer"><p>Intelligence on demand</p></div>
</div>

<script>
const briefs = {briefs_json};
const today = "{today_str}";
const ORANGE = "{CLAUDE_ORANGE}";

// Tab switching
document.querySelectorAll('.tab').forEach(tab => {{
  tab.addEventListener('click', () => {{
    document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.view').forEach(v => v.classList.remove('active'));
    tab.classList.add('active');
    document.getElementById(tab.dataset.tab).classList.add('active');
  }});
}});

function briefIcon(type) {{
  if (type === 'morning') return '\\u2600\\ufe0f';
  if (type === 'midday') return '\\u2601\\ufe0f';
  if (type === 'evening') return '\\ud83c\\udf19';
  return '\\ud83d\\udcf0';
}}

function briefLabel(type) {{
  if (type === 'morning') return 'Morning Brief';
  if (type === 'midday') return 'Midday Update';
  if (type === 'evening') return 'Evening Wrap';
  return type.charAt(0).toUpperCase() + type.slice(1);
}}

function formatDate(dateStr) {{
  const d = new Date(dateStr + 'T12:00:00');
  return d.toLocaleDateString('en-US', {{ weekday:'long', month:'long', day:'numeric' }});
}}

function renderCard(brief) {{
  const url = brief.key;
  const pinClass = brief.pinned ? 'pinned' : '';
  const pinIcon = brief.pinned ? '\\ud83d\\udccc' : '\\ud83d\\udccc';
  return '<div class="brief-card" style="position:relative">' +
    '<a href="' + url + '" style="display:flex;align-items:center;flex:1;text-decoration:none;color:inherit">' +
      '<div class="brief-icon ' + brief.type + '">' + briefIcon(brief.type) + '</div>' +
      '<div class="brief-info">' +
        '<div class="brief-title">' + briefLabel(brief.type) + '</div>' +
        '<div class="brief-meta">' + formatDate(brief.date) + '</div>' +
      '</div>' +
      '<span class="brief-arrow">&#9656;</span>' +
    '</a>' +
    '<button class="brief-pin ' + pinClass + '" onclick="togglePin(event, \\'' + brief.key + '\\')" title="Pin this brief">' + pinIcon + '</button>' +
  '</div>';
}}

// Pins are per-device (localStorage), since this is a static site.
const PIN_STORE = 'jeeves.pins';
function loadLocalPins() {{
  try {{ return new Set(JSON.parse(localStorage.getItem(PIN_STORE) || '[]')); }}
  catch {{ return new Set(); }}
}}
function saveLocalPins(set) {{
  localStorage.setItem(PIN_STORE, JSON.stringify([...set]));
}}
// Merge persisted local pins on top of server-side state.
const _localPins = loadLocalPins();
briefs.forEach(b => {{ if (_localPins.has(b.key)) b.pinned = true; }});

function togglePin(e, key) {{
  e.stopPropagation();
  e.preventDefault();
  const brief = briefs.find(b => b.key === key);
  if (!brief) return;
  brief.pinned = !brief.pinned;
  const pins = loadLocalPins();
  if (brief.pinned) pins.add(key); else pins.delete(key);
  saveLocalPins(pins);
  renderAll();
}}

function renderAll() {{
  // Today
  const todayBriefs = briefs.filter(b => b.date === today);
  const todayEl = document.getElementById('today');
  if (todayBriefs.length === 0) {{
    todayEl.innerHTML = '<div class="empty">No briefs yet today. Check back after 7 AM ET.</div>';
  }} else {{
    todayEl.innerHTML = '<div class="date-group"><div class="date-label">Today &middot; ' +
      formatDate(today) + '</div>' + todayBriefs.map(renderCard).join('') + '</div>';
  }}

  // Archive (grouped by date, skip today)
  const archiveBriefs = briefs.filter(b => b.date !== today);
  const archiveEl = document.getElementById('archive');
  if (archiveBriefs.length === 0) {{
    archiveEl.innerHTML = '<div class="empty">No archived briefs yet.</div>';
  }} else {{
    const grouped = {{}};
    archiveBriefs.forEach(b => {{
      if (!grouped[b.date]) grouped[b.date] = [];
      grouped[b.date].push(b);
    }});
    let html = '';
    Object.keys(grouped).sort().reverse().forEach(date => {{
      html += '<div class="date-group"><div class="date-label">' + formatDate(date) + '</div>' +
        grouped[date].map(renderCard).join('') + '</div>';
    }});
    archiveEl.innerHTML = html;
  }}

  // Pinned
  const pinnedBriefs = briefs.filter(b => b.pinned);
  const pinnedEl = document.getElementById('pinned');
  if (pinnedBriefs.length === 0) {{
    pinnedEl.innerHTML = '<div class="empty">No pinned briefs. Tap the pin icon on any brief to save it.</div>';
  }} else {{
    pinnedEl.innerHTML = pinnedBriefs.map(renderCard).join('');
  }}

  // Update tab counts
  document.querySelectorAll('.tab').forEach(tab => {{
    const name = tab.dataset.tab;
    let count = 0;
    if (name === 'today') count = todayBriefs.length;
    else if (name === 'archive') count = archiveBriefs.length;
    else if (name === 'pinned') count = pinnedBriefs.length;
    const existing = tab.querySelector('.count');
    if (existing) existing.textContent = count;
    else tab.innerHTML += ' <span class="count">' + count + '</span>';
  }});
}}

renderAll();
</script>
</body>
</html>"""

    (DOCS_DIR / "index.html").write_text(index_html, encoding="utf-8")

    # Also write PWA manifest (icons referenced but optional — 404 is harmless)
    manifest = {
        "name": "Intelligence Brief",
        "short_name": "Intel Brief",
        "description": "Daily Intelligence Brief",
        "start_url": "./index.html",
        "display": "standalone",
        "background_color": "#0a0a0a",
        "theme_color": "#0a0a0a",
    }
    (DOCS_DIR / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print("Wrote docs/index.html and docs/manifest.json.")


def s3_publish_brief(brief_type, now_et, interactive_html):
    """Write brief HTML, clean old ones, regenerate index."""
    date_iso = now_et.strftime("%Y-%m-%d")
    s3_write_brief(brief_type, date_iso, interactive_html)
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

    # 2b. Dedup check — skip Claude if headlines haven't changed
    current_headline_set = {h["title"] for h in headlines}
    current_url_set = {h["link"] for h in headlines if h.get("link")}
    should_skip, overlap_pct = check_headline_overlap(current_headline_set, current_url_set)
    if should_skip:
        print(f"Headline overlap {overlap_pct:.0%} — no material updates since last brief. Skipping.")
        subject = f"{config['subject_prefix']} — {date_str} — No Major Updates"
        skip_html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#f5f5f5">
<div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;max-width:600px;margin:0 auto;padding:24px 20px;color:#1a1a1a;background:#ffffff">
<h1 style="font-size:20px;font-weight:700;margin:0 0 12px">{config['subject_prefix']} — {date_str}</h1>
<p style="font-size:14px;color:#555;line-height:1.6;margin:0 0 16px">No material developments since the last brief. Headlines overlap at {int(overlap_pct * 100)}%. The next scheduled brief will run as normal.</p>
<div style="margin-top:20px;padding-top:10px;border-top:1px solid #eee">
<p style="color:#bbb;font-size:10px;margin:0">{timestamp} · No material updates.</p>
</div>
</div>
</body></html>"""
        send_email(subject, skip_html)
        return {"status": "skipped_no_new_content", "overlap": overlap_pct}

    # Save current headlines + URLs for next run's comparison
    s3_save_headlines(current_headline_set, current_url_set)

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

    # Parse JSON — extract the object even if the model adds commentary
    cleaned = raw_response.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[-1]
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3]
    cleaned = cleaned.strip()

    # Find the JSON object by matching braces
    start = cleaned.find("{")
    if start != -1:
        depth = 0
        end = start
        for i in range(start, len(cleaned)):
            if cleaned[i] == "{":
                depth += 1
            elif cleaned[i] == "}":
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        cleaned = cleaned[start:end]

    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as e:
        print(f"JSON parse error: {e}")
        print(f"Raw: {raw_response[:500]}")
        subject = f"{config['subject_prefix']} \u2014 {date_str}"
        fallback = f"<div style='font-family:sans-serif;padding:20px'><pre style='white-space:pre-wrap'>{raw_response}</pre></div>"
        send_email(subject, fallback)
        return {"status": "sent_fallback_parse_error"}

    # 5. Build all views
    title = f"{config['subject_prefix']} \u2014 {date_str}"
    email_html = build_email_preview(title, data, quotes, timestamp, usage_info)
    interactive_html = build_interactive_html(title, data, quotes, timestamp, usage_info)

    # 6. Send email (preview only — no attachment for minimal traceability)
    send_email(title, email_html)

    # 7. Publish to S3 app (if configured)
    s3_publish_brief(brief_type, now_et, interactive_html)

    return {"status": "sent", "brief_type": brief_type, "stories": len(headlines), "quotes": len(quotes), "usage": usage_info}


if __name__ == "__main__":
    import sys
    brief_type = sys.argv[1] if len(sys.argv) > 1 else "morning"
    result = lambda_handler({"brief_type": brief_type}, None)
    print(json.dumps(result, indent=2))
