#!/usr/bin/env python3
"""The News Desk's pack: company headlines, graded by source.

Deterministic and model-free. Zero model tokens. The News Desk routine
(theses/NEWS_DESK.md) runs it on weekdays at 06:00 and on Sundays at 15:00 US
Eastern; it writes news.json and news.md to theses/news/latest/ (NEWS_LATEST in
common.py), where the Research Director and the analyst's dossier read them.

    python3 theses/bin/news_pack.py [--week-of YYYY-MM-DD] [--today YYYY-MM-DD]
                                    [--site DIR | --ref REF] [--no-fetch] [--same-asof]
                                    [--out DIR] [--quiet]

WHERE THE HEADLINES COME FROM. The daily pipeline writes each company's latest
Google News headlines (at most 15, relevance-filtered since NEWS_FIX_DATE) to
docs/news/{TICKER}.json and its closes to docs/prices/{TICKER}.json, both
published on the gh-pages branch. By default this script fetches that branch
into refs/remotes/origin/gh-pages (git fetch, depth 1) and reads the files from
git. --no-fetch reads the ref as it is; --site DIR reads a folder laid out like
the site (news/, prices/); if git has no copy at all, the scoped names are
fetched one by one from the published site and the universe-wide volume scan is
skipped (the pack says so).

WHICH NAMES. Covered names (theses/ledger/events.csv), names a model book holds,
the style books' rules candidates, the names in this week's director plan if it
exists, today's analyst assignments from that plan, the screen's top 15 names
off cooldown, and the 25 names whose headline volume is most abnormal. "This
week" is the week of --week-of: by default the current week on a weekday, and
the week ahead on a Saturday or Sunday (desk_week_of).

ABNORMAL VOLUME. A name's 7-day count is the number of its headlines dated in
the 7 days to the pack's as-of time. Its baseline is the mean of the pipeline's
daily 7-day count on the panel (data/fundamentals, the news_count_7d column),
over panel dates on or after NEWS_FIX_DATE and at least 7 days before the as-of
date, so the baseline window never overlaps the current one. That column is
contaminated before NEWS_FIX_DATE and is never read for those dates; the
analyst never reads it at all. With little history: 3 or more readings give
the name's own baseline ("own"); 1 or 2 give a thin one ("own, thin"); none
(the first week after the fix, or a new listing) fall back to the median of
the latest panel reading across all names ("universe median"). The ratio is
(count + 1) / (baseline + 1), so a quiet name going from 0 to 1 is not news.
Only names with 3 or more headlines this week, at least one of them tier 1
or 2, are ranked: a burst of auto-generated quote pages is not news. Both sides are capped
at the pipeline's 15 headlines per fetch, so a name that is always busy cannot
look abnormal: the ratio finds quiet names that got loud.

SOURCE TIERS are in theses/news_sources.json, with the rule. Unknown is tier 3.

DUPLICATES. Within one name, titles are normalised (lower case, punctuation
dropped) and two headlines are the same story when their normalised titles are
at least DUP_RATIO alike (difflib) or share at least DUP_JACCARD of their words.
The highest tier copy is kept (then the earliest); the others are listed on it.

PRICE CLAIMS. A title that says a stock moved a percentage ("down 6.1%",
"falls 8%", "a 3% gain", "Stock Moves -1.64%") is compared with the stored
close-to-close move. The session is the headline's US Eastern date (a weekend
headline takes the Friday before); the claim may describe that session or the
one before it, so both moves are tried and the closer one counts. Only exact
stored closes are used: if the session's close is not stored, or the stored
close before it is not the previous session's, the claim is "unverifiable".
Which days were sessions is read from the stored closes of a few large names
and the S&P 500 series (CALENDAR_TICKERS), because the pipeline sometimes
misses a day for some names and a two-day move is not a one-day move. Nothing
is interpolated. A claim is a mismatch when neither move is within
MISMATCH_PP percentage points of it and both moves could be computed; when
one could not, it is "unverifiable" instead. Claims over a period ("since", "this week",
"year to date") or outside market hours ("premarket", "after hours"), and
claims about a figure rather than the share price ("revenue up 12%") are not
checked.

LM and VADER are kept per headline exactly as the pipeline stored them. The
per-name averages use tier 1 and 2 headlines only.
"""
import csv, difflib, hashlib, json, re, subprocess, sys
from datetime import date, datetime, time as dtime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (REPO, THESES, LEDGER, NEWS_FIX_DATE, NEWS_LATEST, fetch_site, is_operating,
                    read_csv_rows)

EASTERN = ZoneInfo("America/New_York")
SOURCES_FILE = THESES / "news_sources.json"
OUT_DIR = NEWS_LATEST
FUND_DIR = REPO / "data" / "fundamentals"
GIT_REF = "refs/remotes/origin/gh-pages"

LOOKBACK_DAYS = 14          # headlines listed per name
WINDOW_DAYS = 7             # the volume window
TOP_ABNORMAL = 25
MIN_ABNORMAL_COUNT = 3
MIN_BASELINE_OBS = 3
MISMATCH_PP = 1.5
MAX_SESSION_GAP_DAYS = 5
# Names whose stored closes, with the benchmark's, say which days were sessions.
CALENDAR_TICKERS = ("AAPL", "MSFT", "JPM", "XOM", "KO")
DUP_RATIO = 0.88
DUP_JACCARD = 0.8
BATCH_SIZE = 40
MD_HEADLINES_PER_NAME = 6

WIN_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
                *(f"LPT{i}" for i in range(1, 10))}


def site_filename(ticker):
    """The pipeline's _news_filename: a Windows device name gets a leading _."""
    base = (ticker or "").upper()
    return f"_{base}.json" if base in WIN_RESERVED else f"{base}.json"


# --------------------------------------------------------------- site access

class DirSite:
    """A folder laid out like the site: news/{T}.json, prices/{T}.json."""
    def __init__(self, root):
        self.root = Path(root)
        self.label = f"folder {root}"

    def read(self, path):
        try:
            return (self.root / path).read_bytes()
        except OSError:
            return None

    def news_files(self):
        d = self.root / "news"
        return sorted(p.name for p in d.glob("*.json")) if d.is_dir() else None


class GitSite:
    """The site as committed on a git ref, read with one cat-file process."""
    def __init__(self, ref=GIT_REF, repo=REPO):
        self.ref, self.repo = ref, str(repo)
        sha = subprocess.run(["git", "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"],
                             cwd=self.repo, capture_output=True, text=True)
        if sha.returncode != 0:
            raise RuntimeError(f"no git ref {ref}")
        self.sha = sha.stdout.strip()
        self.label = f"git {ref} at {self.sha[:10]}"
        self._proc = None

    def _cat(self):
        if self._proc is None:
            self._proc = subprocess.Popen(["git", "cat-file", "--batch"], cwd=self.repo,
                                          stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        return self._proc

    def read(self, path):
        p = self._cat()
        p.stdin.write(f"{self.sha}:{path}\n".encode())
        p.stdin.flush()
        head = p.stdout.readline().decode().split()
        if len(head) < 3 or head[1] != "blob":
            return None
        data = p.stdout.read(int(head[2]))
        p.stdout.read(1)
        return data

    def news_files(self):
        r = subprocess.run(["git", "ls-tree", "--name-only", f"{self.sha}:news"],
                           cwd=self.repo, capture_output=True, text=True)
        if r.returncode != 0:
            return None
        return sorted(n for n in r.stdout.split() if n.endswith(".json"))

    def close(self):
        if self._proc is not None:
            self._proc.stdin.close()
            self._proc.wait()
            self._proc = None


class NetSite:
    """The published site, file by file. Cannot list, so no universe scan."""
    label = "the published site, one file at a time"

    def read(self, path):
        return fetch_site(path)

    def news_files(self):
        return None


def git_fetch_pages(repo=REPO):
    """Refresh refs/remotes/origin/gh-pages. Returns an error string or ''."""
    r = subprocess.run(["git", "fetch", "--quiet", "--depth", "1", "origin",
                        f"+refs/heads/gh-pages:{GIT_REF}"],
                       cwd=str(repo), capture_output=True, text=True)
    return "" if r.returncode == 0 else (r.stderr.strip() or f"git fetch exited {r.returncode}")


def open_site(site_dir=None, ref=None, fetch=True, log=None):
    log = log if log is not None else []
    if site_dir:
        return DirSite(site_dir)
    if fetch and not ref:
        err = git_fetch_pages()
        if err:
            log.append(f"git fetch of gh-pages failed ({err}); using the copy git already has, if any")
    try:
        return GitSite(ref or GIT_REF)
    except (RuntimeError, OSError) as exc:
        log.append(f"no git copy of the site ({exc}); reading scoped names from the published site")
        return NetSite()


def read_json(site, path):
    raw = site.read(path)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None


# --------------------------------------------------------------- source tiers

def load_sources(path=None):
    cfg = json.loads(Path(path or SOURCES_FILE).read_text(encoding="utf-8"))
    tiers = []
    for k in sorted(cfg["tiers"], key=int):
        t = cfg["tiers"][k]
        tiers.append((int(k), {_norm_src(n) for n in t.get("names", [])},
                      [d.lower() for d in t.get("domains", [])]))
    pats = [re.compile(p, re.I) for p in cfg.get("tier3_title_patterns", [])]
    return {"tiers": tiers, "patterns": pats, "raw_patterns": cfg.get("tier3_title_patterns", [])}


def _norm_src(s):
    s = (s or "").strip().lower()
    return s[4:] if s.startswith("www.") else s


def _domain_ok(host, dom):
    return host == dom or host.endswith("." + dom)


def tier_of(source, link, title, cfg):
    """(tier, rule). The rule is the text of the match, for the record."""
    s = _norm_src(source)
    tier, rule = None, ""
    for n, names, _ in cfg["tiers"]:
        if s and s in names:
            tier, rule = n, f"source {source!r}"
            break
    if tier is None:
        hosts = []
        if s and "." in s and " " not in s:
            hosts.append(s)
        host = (urlparse(link or "").hostname or "").lower()
        if host and not host.endswith("news.google.com"):
            hosts.append(host[4:] if host.startswith("www.") else host)
        for n, _, doms in cfg["tiers"]:
            hit = next((f"{h} ~ {d}" for h in hosts for d in doms if _domain_ok(h, d)), None)
            if hit:
                tier, rule = n, f"domain {hit}"
                break
    if tier is None:
        return 3, "unknown source"
    if tier == 2:
        for p in cfg["patterns"]:
            if p.search(title or ""):
                return 3, f"{rule}, title pattern {p.pattern!r}"
    return tier, rule


EM_DASH, EN_DASH = chr(0x2014), chr(0x2013)


def dash_free(s):
    """The archive's rule: no em or en dashes, in headlines quoted here too."""
    s = re.sub(r"\s*" + EM_DASH + r"\s*", ", ", s or "")
    return re.sub(r"\s*" + EN_DASH + r"\s*", " - ", s)


def clean_title(title, source):
    t = (title or "").strip()
    src = (source or "").strip()
    if src and t.lower().endswith(" - " + src.lower()):
        t = t[: -(len(src) + 3)].rstrip()
    return dash_free(t)


# --------------------------------------------------------------- duplicates

def _norm_title(t):
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9%.]+", " ", (t or "").lower())).strip()


def same_story(a, b):
    na, nb = _norm_title(a), _norm_title(b)
    if not na or not nb:
        return False
    if na == nb or difflib.SequenceMatcher(None, na, nb).ratio() >= DUP_RATIO:
        return True
    wa, wb = set(na.split()), set(nb.split())
    return min(len(wa), len(wb)) >= 5 and len(wa & wb) / len(wa | wb) >= DUP_JACCARD


def dedupe(items):
    """(kept, removed_count). items carry title, tier, ts; best copy first."""
    order = sorted(items, key=lambda h: (h["tier"], h["ts"], h["id"]))
    kept = []
    removed = 0
    for h in order:
        twin = next((k for k in kept if same_story(k["title"], h["title"])), None)
        if twin is None:
            kept.append(dict(h, duplicates=[]))
        else:
            twin["duplicates"].append({"id": h["id"], "source": h["source"], "tier": h["tier"]})
            removed += 1
    return kept, removed


# --------------------------------------------------------------- price claims

_UP = (r"rises?|rose|rising|gains?|gained|jumps?|jumped|surges?|surged|soars?|soared|climbs?|climbed|"
       r"rall(?:y|ies|ied)|advances?|advanced|pops?|popped|spikes?|spiked|up|higher|skyrockets?|"
       r"skyrocketed|leaps?|leapt|rebounds?|rebounded|adds?|added|increases?|increased|rockets?|rocketed|"
       r"zooms?|zoomed|powers?|powered|edges? up|edged up")
_DOWN = (r"falls?|fell|falling|drops?|dropped|declines?|declined|slides?|slid|sinks?|sank|sunk|plunges?|"
         r"plunged|tumbles?|tumbled|slumps?|slumped|dips?|dipped|slips?|slipped|loses?|lost|sheds?|down|"
         r"lower|crash(?:es|ed)?|plummets?|plummeted|retreats?|retreated|sags?|sagged|decreases?|"
         r"decreased|craters?|cratered|tanks?|tanked|nosedives?|nosedived|edges? down|edged down|sell-off")
_UP_NOUN = r"gain|rise|jump|surge|rally|climb|advance|increase|pop|spike|rebound|higher|upside move"
_DOWN_NOUN = r"drop|fall|decline|plunge|slide|slump|loss|tumble|dip|decrease|selloff|sell-off|lower|crash"
_NUM = r"(?P<num>\d{1,3}(?:\.\d+)?)\s?(?:%|percent\b)"
_QUAL = (r"(?:by\s+|about\s+|nearly\s+|almost\s+|roughly\s+|around\s+|over\s+|more\s+than\s+|"
         r"as\s+much\s+as\s+|another\s+|a\s+further\s+|up\s+to\s+|some\s+|close\s+to\s+)*")
_SIGN = r"(?P<sign>[+\-−])?"
CLAIM_VERB = re.compile(rf"\b(?P<verb>{_UP}|{_DOWN})\s+{_QUAL}{_SIGN}{_NUM}", re.I)
CLAIM_NOUN = re.compile(rf"(?<![\w.]){_SIGN}{_NUM}\s+(?P<noun>{_UP_NOUN}|{_DOWN_NOUN})s?\b", re.I)
CLAIM_SIGNED = re.compile(rf"\b(?:moves?|moved|trades?|traded|ends?|ended|closes?|closed|shares|stock)\s+"
                          rf"(?P<sign>[+\-−]){_NUM}", re.I)
_DOWN_RE = re.compile(rf"^(?:{_DOWN}|{_DOWN_NOUN})s?$", re.I)
PERIOD = re.compile(r"\b(?:since|ytd|year|years|yr|annual|week|weeks|weekly|month|months|monthly|quarter|"
                    r"quarters|days|sessions|so far|past|decade|all-time|2024|2025|2026|2027|lifetime|yoy|qoq|mom|q[1-4]|h[12]|fy\d*)\b"
                    r"|year-to-date|\by\/y\b|\bq\/q\b|\bin (?:a|one|two|three|four|five|six|\d+) ", re.I)
FORWARD = re.compile(r"\b(?:could|may|might|potential|expects?|expected|forecasts?|predicts?|targets?|"
                     r"upside|downside|would|can|poised|set to|eyes|aims?|projected|implied|buy in)\b", re.I)
# Titles whose percentages are over several sessions whatever the verb says.
MULTI_DAY = re.compile(r"\b(?:streak|straight|consecutive|in a row|undervalued|overvalued|valued|valuation|"
                       r"fair value|from (?:its|the|a) (?:peak|high|low)|off (?:its|the) (?:high|low)|"
                       r"drawdown|rally since|run|bargain|cheap)\b", re.I)
TITLE_NOT_PRICE = re.compile(r"\bshort interest\b|\bshort volume\b", re.I)
EXTENDED = re.compile(r"pre-?market|after[- ]?hours|after the bell|extended trading|overnight|before the bell", re.I)
# A percentage about a figure, not the share price.
NOT_PRICE = re.compile(
    r"\b(?:revenue|revenues|sales|profit|profits|earnings|eps|income|margin|margins|dividend|dividends|"
    r"payout|deliveries|shipments|orders|production|output|volume|volumes|traffic|bookings|guidance|"
    r"forecast|outlook|stake|ownership|rate|rates|yield|yields|inflation|cost|costs|"
    r"prices|target|targets|index|s&p|nasdaq|dow|market|markets|sector|etf|bitcoin|gold|oil|backlog|"
    r"users|subscribers|comps|same-store|loss|losses|ebitda|cash|debt|spending|capex|demand|jobs|"
    r"headcount|workforce|staff|premiums|assets|aum|deposits|loans|surprise|estimate|estimates|"
    r"consensus|expectations|interest|short|float|options|bid|offer|tariff|tariffs)\b", re.I)
_STOP = {"inc", "corp", "corporation", "company", "holdings", "group", "the", "and", "ltd", "plc", "co",
         "technologies", "international", "industries", "global", "systems", "class", "common"}


def _name_tokens(ticker, name):
    toks = [t for t in re.split(r"[^A-Za-z0-9&]+", name or "") if len(t) >= 3 and t.lower() not in _STOP]
    return toks[:2]


def _mentions(text, ticker, name):
    if ticker and re.search(r"(?<![A-Za-z0-9])" + re.escape(ticker) + r"(?![A-Za-z0-9])", text):
        return True
    # Case-sensitive: "price target increase" is not about Target.
    return any(re.search(r"\b" + re.escape(t) + r"\b", text) for t in _name_tokens(ticker, name))


def find_claims(title):
    """[(start, end, signed_percent, text)] for each percentage move in a title."""
    out = []
    for rx in (CLAIM_SIGNED, CLAIM_VERB, CLAIM_NOUN):
        for m in rx.finditer(title):
            if any(m.start() < e and s < m.end() for s, e, _, _ in out):
                continue
            v = float(m.group("num"))
            sign = m.groupdict().get("sign")
            word = (m.groupdict().get("verb") or m.groupdict().get("noun") or "").lower()
            if word:
                neg = bool(_DOWN_RE.match(word))
            else:
                neg = sign in ("-", "\u2212")
            out.append((m.start(), m.end(), -v if neg else v, m.group(0)))
    return sorted(out)


def price_claim(title, ticker, name):
    """The one checkable share-price claim in a title about this company, as
    {"claimed", "text"} or {"skip": reason, "text"}, or None when the title
    has no percentage move at all."""
    claims = find_claims(title)
    if not claims:
        return None
    usable, skipped = [], []
    prev_end = 0
    own = {w.lower() for w in re.findall(r"[\w&'-]+", name or "")}
    for s, e, v, text in claims:
        left = title[prev_end:s]
        right = re.split(r"[;|]|:\s| - ", title[e:], maxsplit=1)[0][:45]
        prev_end = e
        clause = re.split(r"[;,|]|:\s|\s(?:as|and|after|while|but|with|on)\s", left, flags=re.I)[-1]
        lead = " ".join([w for w in re.findall(r"[\w&'-]+", clause) if w.lower() not in own][-4:])
        tail = " ".join(re.findall(r"[\w&'-]+", right)[:3]) if re.match(r"\s*(?:in|of)\s", right, re.I) else ""
        if NOT_PRICE.search(lead) or NOT_PRICE.search(tail):
            skipped.append("the percentage is about a figure, not the share price")
        elif FORWARD.search(left) or FORWARD.search(right):
            skipped.append("a forecast, not a move")
        elif PERIOD.search(right) or PERIOD.search(lead):
            skipped.append("a move over a period, not one session")
        elif not _mentions(left, ticker, name):
            skipped.append("the move is not tied to this company in the title")
        else:
            usable.append((left, v, text))
    first = usable[0][2] if usable else claims[0][3]
    if TITLE_NOT_PRICE.search(title):
        return {"skip": "the percentage is about a figure, not the share price", "text": first}
    if MULTI_DAY.search(title):
        return {"skip": "a move over a period, not one session", "text": first}
    if not usable:
        return {"skip": skipped[0], "text": first}
    if EXTENDED.search(title):
        return {"skip": "a move outside market hours", "text": first}
    _, v, text = usable[0]
    return {"claimed": v, "text": text.strip()}


def close_series(blob):
    """[(date_str, close)] sorted, positive closes only."""
    rows = (blob or {}).get("closes") or []
    out = []
    for r in rows:
        try:
            d, c = str(r[0]), float(r[1])
        except (TypeError, ValueError, IndexError):
            continue
        if c > 0:
            out.append((d, c))
    return sorted(out)


def session_for(d):
    """The market session a headline dated d (US Eastern) speaks about: the
    same day, or the Friday before a weekend day."""
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def session_calendar(series_list):
    """Every date any of the given close series has: the market's sessions,
    as far as the stored data knows them. One series can miss a session the
    pipeline failed to collect; the union rarely does."""
    days = set()
    for closes in series_list:
        days.update(d for d, _ in closes)
    return sorted(days)


def check_claim(claim, headline_date, closes, calendar=None):
    """Fill claim with status match | mismatch | unverifiable.

    A move is the change between two stored closes on consecutive sessions:
    the calendar says which session came before, and if the stored close
    before a session is not that one, the move is not used."""
    idx = {d: i for i, (d, _) in enumerate(closes)}
    cal = calendar or [d for d, _ in closes]
    prev_session = {cal[i]: cal[i - 1] for i in range(1, len(cal))}
    r = session_for(headline_date).isoformat()
    if r not in idx:
        why = ("no stored closes for this company" if not closes else
               f"no stored close for {r}" + (" yet" if r > closes[-1][0] else ""))
        return dict(claim, status="unverifiable", reason=why)
    moves, missing = [], []
    for i in (idx[r], idx[r] - 1):
        d1 = closes[i][0] if i >= 0 else None
        if i <= 0:
            missing.append(d1 or "the session before")
            continue
        (d0, c0), (_, c1) = closes[i - 1], closes[i]
        if prev_session.get(d1, d0) != d0 or \
                (date.fromisoformat(d1) - date.fromisoformat(d0)).days > MAX_SESSION_GAP_DAYS:
            missing.append(d1)
            continue
        moves.append({"session": d1, "from": d0, "move_pct": round((c1 / c0 - 1) * 100, 2)})
    if not moves:
        return dict(claim, status="unverifiable",
                    reason=f"the close of the session before {r} is not stored")
    best = min(moves, key=lambda m: abs(m["move_pct"] - claim["claimed"]))
    gap = round(abs(best["move_pct"] - claim["claimed"]), 2)
    out = dict(claim, actual=moves, closest=best, gap_pp=gap)
    if gap <= MISMATCH_PP:
        return dict(out, status="match")
    if missing:
        # The move the claim may describe cannot be computed, so no mismatch
        # can be shown.
        return dict(out, status="unverifiable",
                    reason=f"no one-session move can be computed for {', '.join(missing)}")
    return dict(out, status="mismatch")


# --------------------------------------------------------------- volume

def panel_history(fund_dir=None, since=NEWS_FIX_DATE):
    """(latest_date, latest_rows, {ticker: [(date, news_count_7d)]}) from the
    monthly panel files. Counts are read only for dates on or after `since`."""
    d = Path(fund_dir or FUND_DIR)
    files = sorted(p for p in d.glob("????-??.csv") if p.stem >= since[:7]) if d.is_dir() else []
    latest, rows, hist = "", {}, {}
    for f in files:
        with f.open(encoding="utf-8", newline="") as fh:
            for r in csv.DictReader(fh):
                dt, t = r.get("date") or "", r.get("ticker") or ""
                if not dt or not t:
                    continue
                if dt > latest:
                    latest, rows = dt, {}
                if dt == latest:
                    rows[t] = r
                if dt >= since:
                    try:
                        c = float(r.get("news_count_7d") or "")
                    except ValueError:
                        continue
                    hist.setdefault(t, []).append((dt, c))
    # One pass: the latest date's rows are reset whenever a later date
    # appears, so rows need not be in date order.
    return latest, list(rows.values()), hist


def baseline_for(obs, asof_date, universe_median):
    """(baseline, n_obs, method). obs is [(date, count)] from the panel."""
    cut = (asof_date - timedelta(days=WINDOW_DAYS)).isoformat()
    usable = [c for d, c in obs if d <= cut]
    if len(usable) >= MIN_BASELINE_OBS:
        return sum(usable) / len(usable), len(usable), "own"
    if usable:
        return sum(usable) / len(usable), len(usable), "own, thin"
    return universe_median, 0, "universe median"


def median(xs):
    xs = sorted(xs)
    if not xs:
        return 0.0
    m = len(xs) // 2
    return xs[m] if len(xs) % 2 else (xs[m - 1] + xs[m]) / 2


# --------------------------------------------------------------- the pack

def headline_id(ticker, title, source, ts):
    h = hashlib.sha1(f"{title}|{source}|{ts}".encode("utf-8")).hexdigest()[:10]
    return f"{ticker}-{h}"


def to_headlines(ticker, items, cfg, since_ts, until_ts):
    """Raw file items to dated, tiered headlines in [since_ts, until_ts]."""
    out = []
    for it in items if isinstance(items, list) else []:
        if not isinstance(it, dict):
            continue
        ts = it.get("ts") or 0
        if not isinstance(ts, (int, float)) or ts <= 0 or not (since_ts <= ts <= until_ts):
            continue
        raw_title, source = it.get("title") or "", (it.get("source") or "").strip()
        title = clean_title(raw_title, source)
        if not title:
            continue
        tier, rule = tier_of(source, it.get("link"), title, cfg)
        dt = datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(EASTERN)
        out.append({"id": headline_id(ticker, raw_title, source, int(ts)), "ticker": ticker,
                    "date": dt.date().isoformat(), "time_et": dt.strftime("%H:%M"), "ts": int(ts),
                    "title": title, "source": dash_free(source), "tier": tier, "tier_rule": rule,
                    "link": it.get("link") or "", "lm": it.get("lm"), "vader": it.get("vader")})
    return out


def count_window(items, since_ts, until_ts, cfg=None):
    """(headlines, tier 1 and 2 headlines) dated in [since_ts, until_ts]."""
    n = n12 = 0
    for it in items if isinstance(items, list) else []:
        ts = it.get("ts") if isinstance(it, dict) else None
        if isinstance(ts, (int, float)) and since_ts <= ts <= until_ts:
            n += 1
            if cfg is not None:
                src = it.get("source") or ""
                n12 += tier_of(src, it.get("link"), clean_title(it.get("title"), src), cfg)[0] <= 2
    return n, n12


def _avg(vals):
    vals = [v for v in vals if isinstance(v, (int, float)) and not isinstance(v, bool)]
    return round(sum(vals) / len(vals), 4) if vals else None


def build(asof, scope, site, rows, hist, cfg=None, week_of=None, log=None):
    """The pack as a dict.

    asof   an aware datetime: the end of the volume window
    scope  {ticker: [reason, ...]} before the abnormal-volume names are added
    site   a DirSite, GitSite or NetSite
    rows   the latest panel rows (for names, sectors and the operating filter)
    hist   {ticker: [(date, news_count_7d)]} post NEWS_FIX_DATE"""
    cfg = cfg or load_sources()
    log = list(log or [])
    panel = {r.get("ticker"): r for r in rows}
    fix_ts = datetime.combine(date.fromisoformat(NEWS_FIX_DATE), dtime(0), EASTERN).timestamp()
    until_ts = asof.timestamp()
    win_ts = max(fix_ts, until_ts - WINDOW_DAYS * 86400)
    look_ts = max(fix_ts, until_ts - LOOKBACK_DAYS * 86400)
    asof_date = asof.astimezone(EASTERN).date()
    latest = [c for obs in hist.values() for d, c in obs[-1:]]
    uni_median = median(latest)

    news_cache = {}

    def news(t):
        if t not in news_cache:
            news_cache[t] = read_json(site, f"news/{site_filename(t)}")
        return news_cache[t]

    # --- abnormal volume across every name with a news file ---------------
    volume = {}
    files = site.news_files()
    universe = []
    if files is None:
        log.append("the site copy cannot be listed, so abnormal volume is computed for the scoped names only")
        universe = sorted(scope)
    else:
        by_file = {site_filename(t): t for t in panel}
        universe = sorted(by_file[f] for f in files if f in by_file)
    for t in universe:
        items = news(t)
        if items is None:
            continue
        n7, n12 = count_window(items, win_ts, until_ts, cfg)
        base, nobs, method = baseline_for(hist.get(t, []), asof_date, uni_median)
        volume[t] = {"count_7d": n7, "tier12_7d": n12, "baseline": round(base, 2), "baseline_obs": nobs,
                     "method": method, "ratio": round((n7 + 1) / (base + 1), 2)}
    ranked = sorted((t for t, v in volume.items()
                     if v["count_7d"] >= MIN_ABNORMAL_COUNT and v["tier12_7d"] >= 1
                     and (t not in panel or is_operating(panel[t]))),
                    key=lambda t: (-volume[t]["ratio"], -volume[t]["count_7d"], t))
    top = ranked[:TOP_ABNORMAL]
    for i, t in enumerate(top, 1):
        volume[t]["rank"] = i
        scope.setdefault(t, []).append(f"abnormal volume, rank {i}")

    # --- the session calendar, for the price-claim check -------------------
    ref_series = [close_series(read_json(site, f"prices/{site_filename(t)}")) for t in CALENDAR_TICKERS]
    market = read_json(site, "prices/_MARKET.json") or {}
    ref_series.append(close_series({"closes": market.get("benchmark") or []}))
    price_cache = {}

    def closes_for(t):
        if t not in price_cache:
            price_cache[t] = close_series(read_json(site, f"prices/{site_filename(t)}"))
        return price_cache[t]

    # --- per name ----------------------------------------------------------
    names, all_heads = [], []
    totals = {"headlines_in_window": 0, "tier1": 0, "tier2": 0, "tier3": 0, "duplicates_removed": 0,
              "price_claims_found": 0, "price_claims_checked": 0, "price_claims_matched": 0,
              "price_claims_mismatched": 0, "price_claims_unverifiable": 0, "price_claims_not_checked": 0}
    no_file = []
    for t in sorted(scope):
        r = panel.get(t) or {}
        items = news(t)
        if items is None:
            no_file.append(t)
        heads = to_headlines(t, items, cfg, look_ts, until_ts)
        totals["headlines_in_window"] += len(heads)
        kept, removed = dedupe(heads)
        totals["duplicates_removed"] += removed
        for h in kept:
            h["in_7d"] = h["ts"] >= win_ts
            h["flags"] = []
            pc = price_claim(h["title"], t, r.get("name") or "")
            if pc is None:
                h["price_claim"] = None
                continue
            totals["price_claims_found"] += 1
            if "skip" in pc:
                h["price_claim"] = {"status": "not_checked", "reason": pc["skip"], "text": pc["text"]}
                totals["price_claims_not_checked"] += 1
                continue
            closes = closes_for(t)
            res = check_claim(pc, date.fromisoformat(h["date"]), closes,
                              session_calendar(ref_series + [closes]))
            h["price_claim"] = res
            if res["status"] == "unverifiable":
                totals["price_claims_unverifiable"] += 1
            else:
                totals["price_claims_checked"] += 1
                if res["status"] == "mismatch":
                    totals["price_claims_mismatched"] += 1
                    h["flags"].append("price claim mismatch")
                else:
                    totals["price_claims_matched"] += 1
        kept.sort(key=lambda h: (h["tier"], -h["ts"]))
        counts = {f"tier{n}": sum(1 for h in kept if h["tier"] == n) for n in (1, 2, 3)}
        for k, v in counts.items():
            totals[k] += v
        t12 = [h for h in kept if h["tier"] <= 2]
        if kept and not t12:
            for h in kept:
                h["flags"].append("tier 3 only")
        names.append({"ticker": t, "name": r.get("name", ""), "sector": (r.get("sector") or "").strip(),
                      "why": scope[t], "on_panel": bool(r), "news_file": items is not None,
                      "counts": counts, "duplicates_removed": removed,
                      "volume": volume.get(t), "tier12_headlines": len(t12),
                      "lm_avg_tier12": _avg(h["lm"] for h in t12),
                      "vader_avg_tier12": _avg(h["vader"] for h in t12),
                      "flagged": [h["id"] for h in kept if h["flags"]],
                      "headlines": kept})
        all_heads += kept

    # --- what the News Desk labels: tier 1 and 2 only ----------------------------
    targets = []
    for n in names:
        t12 = [h["id"] for h in n["headlines"] if h["tier"] <= 2]
        targets += t12
    batches = [targets[i:i + BATCH_SIZE] for i in range(0, len(targets), BATCH_SIZE)]

    why_counts = {}
    for n in names:
        for w in n["why"]:
            key = w.split(",")[0].split(":")[0]
            why_counts[key] = why_counts.get(key, 0) + 1
    return {
        "generated_for": {"asof": asof.astimezone(timezone.utc).isoformat(timespec="seconds"),
                          "asof_et": asof.astimezone(EASTERN).isoformat(timespec="minutes"),
                          "week_of": week_of.isoformat() if week_of else "",
                          "news_fix_date": NEWS_FIX_DATE},
        "site": site.label,
        "rules": {"window_days": WINDOW_DAYS, "lookback_days": LOOKBACK_DAYS, "top_abnormal": TOP_ABNORMAL,
                  "min_abnormal_count": MIN_ABNORMAL_COUNT, "min_baseline_obs": MIN_BASELINE_OBS,
                  "universe_median_7d": round(uni_median, 2), "mismatch_pp": MISMATCH_PP,
                  "dup_ratio": DUP_RATIO, "dup_jaccard": DUP_JACCARD, "batch_size": BATCH_SIZE,
                  "sources": "theses/news_sources.json",
                  "method": "theses/bin/news_pack.py, module docstring"},
        "totals": dict(totals, names_in_scope=len(names), scope_reasons=why_counts,
                       names_without_news_file=len(no_file), universe_scanned=len(volume),
                       label_targets=len(targets), label_batches=len(batches)),
        "notes": log,
        "abnormal_top": [dict(volume[t], ticker=t, name=(panel.get(t) or {}).get("name", "")) for t in top],
        "names": names,
        "label_batches": batches,
    }


def _tone(v):
    return "n/a" if v is None else f"{v:+.2f}"


def summary(pack):
    g, tot = pack["generated_for"], pack["totals"]
    L = [f"# News as of {g['asof_et'][:10]}, for the week of {g['week_of'] or '(no week given)'}", "",
         f"As of {g['asof_et']} US Eastern, from {pack['site']}. Headlines are leads, never facts: "
         f"a plan may cite one (with its tier and date) as a reason to look at a name, and never as "
         f"evidence of a business fact. Weigh tier 1 and 2 only. Sources and tiers: "
         f"`theses/news_sources.json`. Method: `theses/bin/news_pack.py`.", ""]
    for n in pack.get("notes") or []:
        L.append(f"- Note: {n}")
    if pack.get("notes"):
        L.append("")
    L += [f"- Names in scope: {tot['names_in_scope']} "
          f"({', '.join(f'{k} {v}' for k, v in sorted(tot['scope_reasons'].items()))})",
          f"- Headlines in the last {pack['rules']['lookback_days']} days, after removing "
          f"{tot['duplicates_removed']} duplicates: tier 1 {tot['tier1']}, tier 2 {tot['tier2']}, tier 3 {tot['tier3']}",
          f"- Price claims: {tot['price_claims_found']} found, {tot['price_claims_checked']} checked against stored "
          f"closes, {tot['price_claims_mismatched']} off by more than {pack['rules']['mismatch_pp']} points, "
          f"{tot['price_claims_unverifiable']} unverifiable, {tot['price_claims_not_checked']} not checkable",
          f"- For labelling: {tot['label_targets']} headlines in {tot['label_batches']} batches", ""]
    L += ["## Names", "",
          "| Ticker | Why | T1 | T2 | T3 | 7d vs baseline | LM (T1-2) | VADER (T1-2) | Flags |",
          "|---|---|---|---|---|---|---|---|---|"]
    for n in pack["names"]:
        v = n.get("volume") or {}
        vol = (f"{v['count_7d']} vs {v['baseline']:g} ({v['method']})" if v else "no file")
        c = n["counts"]
        L.append(f"| {n['ticker']} | {'; '.join(n['why'])} | {c['tier1']} | {c['tier2']} | {c['tier3']} | "
                 f"{vol} | {_tone(n['lm_avg_tier12'])} | {_tone(n['vader_avg_tier12'])} | "
                 f"{len(n['flagged']) or ''} |")
    L += ["", "## Headlines, tier 1 and 2", "",
          "Newest first within each tier. A flag means: do not act on this headline alone.", ""]
    for n in pack["names"]:
        hs = [h for h in n["headlines"] if h["tier"] <= 2]
        t3only = not hs and n["headlines"]
        if t3only:
            hs = n["headlines"][:3]
        if not hs:
            continue
        L.append(f"### {n['ticker']} {n['name']}".rstrip() + (" (tier 3 only)" if t3only else ""))
        L.append("")
        for h in hs[:MD_HEADLINES_PER_NAME]:
            flag = f" **{', '.join(h['flags'])}**" if h["flags"] else ""
            pc = h.get("price_claim") or {}
            claim = ""
            if pc.get("status") in ("match", "mismatch"):
                claim = (f" (claims {pc['claimed']:+g}%, stored close {pc['closest']['move_pct']:+g}% "
                         f"on {pc['closest']['session']})")
            L.append(f"- `{h['id']}` {h['date']}, tier {h['tier']}, {h['source']}: {h['title']}{claim}{flag}")
        if len(hs) > MD_HEADLINES_PER_NAME:
            L.append(f"- and {len(hs) - MD_HEADLINES_PER_NAME} more in news.json")
        L.append("")
    mism = [h for n in pack["names"] for h in n["headlines"]
            if (h.get("price_claim") or {}).get("status") == "mismatch"]
    L += ["## Price claims that do not match the stored closes", ""]
    if mism:
        for h in mism:
            pc = h["price_claim"]
            L.append(f"- {h['ticker']} {h['date']}, tier {h['tier']}, {h['source']}: \"{h['title']}\" claims "
                     f"{pc['claimed']:+g}%; stored close-to-close "
                     + ", ".join(f"{m['move_pct']:+g}% on {m['session']}" for m in pc["actual"]))
    else:
        L.append("- None.")
    return "\n".join(L) + "\n"


# --------------------------------------------------------------- scope

def scope_from_inputs(inp, plan_tickers=()):
    """{ticker: [reason]} from a news_scope block (compute_scope_inputs)."""
    ns = (inp or {}).get("news_scope") or {}
    out = {}
    for t in ns.get("covered", []):
        out.setdefault(t, []).append("covered")
    for t, books in (ns.get("held") or {}).items():
        out.setdefault(t, []).append("held: " + ", ".join(books))
    for t, books in (ns.get("candidates") or {}).items():
        out.setdefault(t, []).append("rules candidate: " + ", ".join(books))
    for t in ns.get("screen_top", []):
        out.setdefault(t, []).append("screen top")
    for t in plan_tickers:
        out.setdefault(t, []).append("in this week's plan")
    for t in ns.get("today_assigned", []):
        out.setdefault(t, []).append("assigned today")
    return out


def _plan(day):
    import director_check as DC
    p = DC.plan_path_for(day)
    if not p.exists():
        return []
    _, assignments, _ = DC.parse_plan(p.read_text(encoding="utf-8"))
    return [a for a in assignments or [] if isinstance(a, dict) and a.get("ticker")]


def plan_tickers(week_of):
    """Every name in the director's plan for the week of week_of."""
    return [(a.get("ticker") or "").upper() for a in _plan(week_of)]


def today_assigned(today):
    """The names the plan that covers today assigns the analyst today."""
    return [(a.get("ticker") or "").upper() for a in _plan(today)
            if str(a.get("date") or "") == today.isoformat()]


def desk_week_of(today):
    """The week the News Desk gathers for: this week on a weekday, the week
    ahead on a Saturday or Sunday (the Sunday run feeds the director's plan)."""
    return today - timedelta(days=today.weekday()) if today.weekday() < 5 else \
        today + timedelta(days=7 - today.weekday())


def compute_scope_inputs(week_of, rows, today=None):
    """The names to gather: covered, held, the style books' rules candidates,
    the screen's top names off cooldown and today's plan assignments, computed
    the way director_inputs computes them. Each part that fails is left out
    with a message, never raised."""
    import director_inputs as DI
    events = read_csv_rows(LEDGER / "events.csv")
    out = {"covered": sorted(DI.current_views(events)), "held": {}, "candidates": {}, "screen_top": [],
           "today_assigned": []}
    try:
        for book, tickers in DI.pm_holdings().items():
            for t in tickers:
                out["held"].setdefault(t, []).append(book)
    except Exception as exc:
        print(f"news_pack: holdings not read ({type(exc).__name__}: {exc})", file=sys.stderr)
    try:
        cands, _, _ = DI.style_candidates()
        for book, tickers in cands.items():
            for t in tickers:
                out["candidates"].setdefault(t, []).append(book)
    except Exception as exc:
        print(f"news_pack: style candidates not read ({type(exc).__name__}: {exc})", file=sys.stderr)
    if today is not None:
        try:
            import director_check as DC
            screen, _ = DI.screen_pack(rows, events, today, DC.load_config())
            out["screen_top"] = [s["ticker"] for s in screen.get("top_off_cooldown", [])]
        except Exception as exc:
            print(f"news_pack: the screen's top names not read ({type(exc).__name__}: {exc})",
                  file=sys.stderr)
        try:
            out["today_assigned"] = today_assigned(today)
        except Exception as exc:
            print(f"news_pack: today's assignments not read ({type(exc).__name__}: {exc})", file=sys.stderr)
    return out


def run(week_of, asof, news_scope=None, out_dir=None, site_dir=None, ref=None, fetch=True,
        fund_dir=None, rows=None, today=None):
    """Build and write news.json and news.md. Returns the pack."""
    log = []
    out_dir = Path(out_dir or OUT_DIR)
    latest, local_rows, hist = panel_history(fund_dir)
    if rows is None:
        rows = local_rows
    if not rows:
        from common import load_panel
        _, rows = load_panel()
        log.append("no panel in data/fundamentals: names come from the fetched panel, and every "
                   "baseline is the universe median")
    if news_scope is None:
        news_scope = compute_scope_inputs(week_of, rows, today or asof.astimezone(EASTERN).date())
    scope = scope_from_inputs({"news_scope": news_scope}, plan_tickers(week_of))
    site = open_site(site_dir, ref, fetch, log)
    try:
        pack = build(asof, scope, site, rows, hist, week_of=week_of, log=log)
    finally:
        if hasattr(site, "close"):
            site.close()
    pack["generated_for"]["panel_date"] = latest
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "news.json").write_text(json.dumps(pack, indent=1), encoding="utf-8")
    (out_dir / "news.md").write_text(summary(pack), encoding="utf-8")
    return pack


def headline_line(pack):
    t = pack["totals"]
    return (f"news_pack: {t['names_in_scope']} names, tier 1/2/3 headlines {t['tier1']}/{t['tier2']}/"
            f"{t['tier3']}, {t['duplicates_removed']} duplicates removed, price claims "
            f"{t['price_claims_checked']} checked and {t['price_claims_mismatched']} mismatched, "
            f"{t['label_targets']} headlines to label in {t['label_batches']} batches")


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)

    def opt(name):
        return args[args.index(name) + 1] if name in args else None
    today = date.fromisoformat(opt("--today")) if opt("--today") else datetime.now(tz=EASTERN).date()
    week_of = date.fromisoformat(opt("--week-of")) if opt("--week-of") else desk_week_of(today)
    out_dir = Path(opt("--out") or OUT_DIR)
    if "--same-asof" in args:
        prev = json.loads((out_dir / "news.json").read_text(encoding="utf-8"))
        asof = datetime.fromisoformat(prev["generated_for"]["asof"])
    elif opt("--today"):
        asof = datetime.combine(today, dtime(23, 59, 59), EASTERN)
    else:
        asof = datetime.now(tz=timezone.utc)
    pack = run(week_of, asof, out_dir=out_dir, site_dir=opt("--site"), ref=opt("--ref"),
               fetch="--no-fetch" not in args, today=today)
    if "--quiet" in args:
        # What the News Desk reports, without the whole summary (news.md has it).
        print("Names in scope: " + ", ".join(n["ticker"] for n in pack["names"]))
        where = out_dir.resolve()
        where = where.relative_to(REPO) if where.is_relative_to(REPO) else where
        print(f"Wrote {where}/news.json and news.md")
        print(headline_line(pack))
    else:
        print(summary(pack))
        print(headline_line(pack), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
