"""Shared helpers for the offline test suite.

Nothing here touches the network. Everything that would write into the
repository is redirected into a temporary directory by patching the module
constant that names the location, the same constant the pipeline itself reads.

Run the helpers directly to regenerate the em dash baseline after deliberately
removing occurrences:

    python tests/helpers.py --write-em-dash-baseline
"""
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

import lambda_function as LF  # noqa: E402

EM_DASH = chr(0x2014)  # written as a code point so this file never contains one
EM_DASH_BASELINE = REPO / "tests" / "em_dash_baseline.json"

# Paths whose contents the pipeline or the analyst delivery writes, not a
# person editing the repository: third-party filing text, headline titles,
# rendered pages, caches, and notes that ingest.py commits from Drive. An em
# dash arriving there is data, and failing on it would fail on the news.
EM_DASH_EXEMPT_PREFIXES = (
    "data/", "docs/", "state/",
    "theses/notes/", "theses/runs/", "theses/ledger/", "theses/positions/",
)


@contextlib.contextmanager
def patched(obj, **attrs):
    """Temporarily replace attributes on a module, restoring them afterwards."""
    saved = {k: getattr(obj, k) for k in attrs}
    try:
        for k, v in attrs.items():
            setattr(obj, k, v)
        yield
    finally:
        for k, v in saved.items():
            setattr(obj, k, v)


@contextlib.contextmanager
def quiet():
    """Swallow the pipeline's progress prints so test output stays readable."""
    with contextlib.redirect_stdout(io.StringIO()):
        yield


@contextlib.contextmanager
def temp_dir():
    path = Path(tempfile.mkdtemp(prefix="dib-test-"))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def trading_days(n, end=None):
    """n weekday dates ending on `end` (default today, UTC), oldest first.

    Ending today keeps the series inside the pipeline's ten-day staleness
    cutoff, which is measured from the real clock."""
    day = end or datetime.now(timezone.utc).date()
    out = []
    while len(out) < n:
        if day.weekday() < 5:
            out.append(day.isoformat())
        day -= timedelta(days=1)
    return out[::-1]


def closes_from_returns(returns, start=100.0):
    """Price path from simple daily returns."""
    px = [start]
    for r in returns:
        px.append(px[-1] * (1 + r))
    return px


def zigzag_returns(n, seed=7):
    """Deterministic, non-degenerate daily returns (a small LCG, no random
    module state shared with anything else)."""
    out, x = [], seed
    for _ in range(n):
        x = (1103515245 * x + 12345) % (2 ** 31)
        out.append((x / 2 ** 31 - 0.5) * 0.04)
    return out


def write_price_file(prices_dir, ticker, dates, closes, volumes=None):
    blob = {"updated": datetime.now(timezone.utc).isoformat(),
            "closes": [[d, p] for d, p in zip(dates, closes)]}
    if volumes is not None:
        blob["volumes"] = list(volumes)
    (prices_dir / LF._news_filename(ticker)).write_text(json.dumps(blob), encoding="utf-8")


def write_market_file(prices_dir, dates, bench_closes, rf_percent=4.0):
    payload = {"updated": datetime.now(timezone.utc).isoformat(),
               "risk_free_symbol": "^IRX",
               "risk_free": [[d, rf_percent] for d in dates],
               "benchmark_symbol": "^GSPC",
               "benchmark": [[d, p] for d, p in zip(dates, bench_closes)]}
    (prices_dir / LF.MARKET_FILE).write_text(json.dumps(payload), encoding="utf-8")


# ── Synthetic XBRL ─────────────────────────────────────────────────────────

def _d(y, m, day):
    return date(y, m, day).isoformat()


def calendar_quarters(year):
    """(start, end) of the four calendar quarters of `year`."""
    return [(_d(year, 1, 1), _d(year, 3, 31)), (_d(year, 4, 1), _d(year, 6, 30)),
            (_d(year, 7, 1), _d(year, 9, 30)), (_d(year, 10, 1), _d(year, 12, 31))]


def income_records(year, q_vals, form_q="10-Q", form_k="10-K", tag_q4=False):
    """How an income-statement concept is filed: three-month columns for Q1..Q3
    in the 10-Qs, the full year in the 10-K, and Q4 only if the filer tags it."""
    qs = calendar_quarters(year)
    recs = []
    for i in range(3):
        recs.append({"start": qs[i][0], "end": qs[i][1], "val": q_vals[i],
                     "form": form_q, "filed": _d(year, 3 * i + 5, 1)})
    fy_filed = _d(year + 1, 2, 15)
    recs.append({"start": qs[0][0], "end": qs[3][1], "val": sum(q_vals),
                 "form": form_k, "filed": fy_filed})
    if tag_q4:
        recs.append({"start": qs[3][0], "end": qs[3][1], "val": q_vals[3],
                     "form": form_k, "filed": fy_filed})
    return recs


def ytd_records(year, q_vals):
    """How a cash-flow concept is filed: year to date. Q1 is three months, the
    Q2 10-Q carries six, Q3 nine, and the 10-K twelve. No other quarter is ever
    a ninety-day period on the page."""
    qs = calendar_quarters(year)
    recs, running = [], 0
    for i in range(4):
        running += q_vals[i]
        recs.append({"start": qs[0][0], "end": qs[i][1], "val": running,
                     "form": "10-K" if i == 3 else "10-Q",
                     "filed": _d(year + 1, 2, 15) if i == 3 else _d(year, 3 * i + 5, 1)})
    return recs


def instant_records(values_by_end, form="10-Q"):
    return [{"end": end, "val": val, "form": form, "filed": end} for end, val in values_by_end]


def quarter_ends(years):
    return [q[1] for y in years for q in calendar_quarters(y)]


def facts_node(records, unit="USD"):
    return {"units": {unit: records}}


def rich_facts(years=(2023, 2024, 2025)):
    """A company-facts payload that exercises every branch of
    compute_edgar_factors: income concepts, year-to-date cash flows, per-share
    figures, instants and cover-page shares. Values grow gently so every
    plausibility clamp passes."""
    def series(base, growth, wobble=0.0):
        vals = {}
        k = 0
        for y in years:
            row = []
            for q in range(4):
                row.append(round(base * (1 + growth) ** k * (1 + wobble * ((-1) ** q)), 4))
                k += 1
            vals[y] = row
        return vals

    rev = series(1000.0, 0.02, 0.01)
    gp = {y: [round(v * 0.42, 4) for v in rev[y]] for y in years}
    op = {y: [round(v * (0.20 + 0.002 * i), 4) for i, v in enumerate(rev[y])] for y in years}
    ni = {y: [round(v * 0.15, 4) for v in rev[y]] for y in years}
    cfo = {y: [round(v * 0.18, 4) for v in rev[y]] for y in years}
    capex = {y: [round(v * 0.05, 4) for v in rev[y]] for y in years}
    eps = series(1.00, 0.02, 0.02)
    da = {y: [round(v * 0.04, 4) for v in rev[y]] for y in years}

    def income(vals):
        return [r for y in years for r in income_records(y, vals[y])]

    def ytd(vals):
        return [r for y in years for r in ytd_records(y, vals[y])]

    ends = quarter_ends(years)
    usgaap = {
        "Revenues": facts_node(income(rev)),
        "GrossProfit": facts_node(income(gp)),
        "OperatingIncomeLoss": facts_node(income(op)),
        "NetIncomeLoss": facts_node(income(ni)),
        "NetCashProvidedByUsedInOperatingActivities": facts_node(ytd(cfo)),
        "PaymentsToAcquirePropertyPlantAndEquipment": facts_node(ytd(capex)),
        "EarningsPerShareBasic": facts_node(income(eps), "USD/shares"),
        "EarningsPerShareDiluted": facts_node(income(eps), "USD/shares"),
        "DepreciationDepletionAndAmortization": facts_node(ytd(da)),
        "Assets": facts_node(instant_records([(e, 20000.0 + 100 * i) for i, e in enumerate(ends)])),
        "StockholdersEquity": facts_node(instant_records([(e, 8000.0 + 50 * i) for i, e in enumerate(ends)])),
        "CashAndCashEquivalentsAtCarryingValue": facts_node(instant_records([(ends[-1], 900.0)])),
        "ShortTermInvestments": facts_node(instant_records([(ends[-1], 100.0)])),
        "LongTermDebtNoncurrent": facts_node(instant_records([(ends[-1], 3000.0)])),
        "LongTermDebtCurrent": facts_node(instant_records([(ends[-1], 200.0)])),
        "ShortTermBorrowings": facts_node(instant_records([(ends[-1], 50.0)])),
    }
    dei = {"EntityCommonStockSharesOutstanding":
           facts_node(instant_records([(ends[-1], 500.0)]), "shares")}
    return {"us-gaap": usgaap, "dei": dei}, {"rev": rev, "cfo": cfo, "capex": capex}


# ── Repository scanning ────────────────────────────────────────────────────

def tracked_files():
    """Paths git tracks, relative to the repo root, or None without git."""
    try:
        res = subprocess.run(["git", "-C", str(REPO), "ls-files", "-z"],
                             capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    if res.returncode != 0:
        return None
    return [p for p in res.stdout.decode("utf-8", "replace").split("\0") if p]


def read_text_or_none(rel):
    """File contents as text, or None for binaries and unreadable files."""
    try:
        raw = (REPO / rel).read_bytes()
    except OSError:
        return None
    if b"\0" in raw[:8192]:
        return None
    return raw.decode("utf-8", "replace")


def em_dash_counts(files):
    counts = {}
    for rel in files:
        if rel.startswith(EM_DASH_EXEMPT_PREFIXES):
            continue
        text = read_text_or_none(rel)
        if text is None:
            continue
        n = text.count(EM_DASH)
        if n:
            counts[rel] = n
    return counts


def _write_em_dash_baseline():
    files = tracked_files()
    if files is None:
        sys.exit("git ls-files failed; run this from a checkout.")
    counts = em_dash_counts(files)
    EM_DASH_BASELINE.write_text(json.dumps(dict(sorted(counts.items())), indent=2) + "\n",
                                encoding="utf-8")
    print(f"Wrote {EM_DASH_BASELINE.relative_to(REPO)}: {sum(counts.values())} "
          f"occurrences in {len(counts)} files.")


# ── Workflow parsing ───────────────────────────────────────────────────────

def workflow_run_blocks(text):
    """Every `run:` value in a workflow file, as the shell would receive it.

    A deliberately small reader for the two shapes these workflows use: a
    literal block (`run: |`), a folded block (`run: >-`) and a one-line value.
    It exists so the inline Python can be checked on a runner without PyYAML;
    where PyYAML is installed, a test confirms the two readers agree."""
    lines = text.split("\n")
    out = []
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.lstrip(" ")
        prefix = ""
        if stripped.startswith("- run:"):
            prefix = "- run:"
        elif stripped.startswith("run:"):
            prefix = "run:"
        if not prefix:
            i += 1
            continue
        key_indent = len(line) - len(stripped) + (2 if prefix.startswith("-") else 0)
        value = stripped[len(prefix):].strip()
        if value and value[0] in "|>":
            style, chomp = value[0], value[1:2]
            body = []
            j = i + 1
            block_indent = None
            while j < len(lines):
                cur = lines[j]
                if cur.strip() == "":
                    body.append("")
                    j += 1
                    continue
                ind = len(cur) - len(cur.lstrip(" "))
                if ind <= key_indent:
                    break
                if block_indent is None:
                    block_indent = ind
                body.append(cur[block_indent:])
                j += 1
            while body and body[-1] == "":
                body.pop()
            if style == "|":
                s = "\n".join(body)
            else:
                # Folding: a single line break between non-empty lines becomes a
                # space; more-indented lines keep their breaks.
                s, prev_more = "", False
                for k, b in enumerate(body):
                    more = b.startswith((" ", "\t"))
                    if k == 0:
                        s = b
                    elif b == "" or more or prev_more:
                        s += "\n" + b
                    else:
                        s += ("" if s.endswith("\n") else " ") + b
                    prev_more = more
            if chomp != "-":
                s += "\n"
            out.append(s)
            i = j
        else:
            out.append(value)
            i += 1
    return out


def python_snippets(run_text):
    """(kind, source) for each piece of Python a run block would execute:
    `python -c "..."` arguments and `python3 - <<'TAG'` heredocs."""
    found = []
    # Heredocs fed to the interpreter.
    lines = run_text.split("\n")
    k = 0
    while k < len(lines):
        ln = lines[k]
        if ("python" in ln and " - " in ln and "<<" in ln):
            tag = ln.split("<<", 1)[1].strip().split()[0].strip("'\"-")
            body = []
            k += 1
            while k < len(lines) and lines[k].strip() != tag:
                body.append(lines[k])
                k += 1
            found.append(("heredoc", "\n".join(body)))
        k += 1
    # python -c "...": a double-quoted shell word.
    pos = 0
    while True:
        at = run_text.find(" -c \"", pos)
        if at < 0:
            break
        head = run_text[max(0, run_text.rfind("\n", 0, at) + 1):at]
        start = at + len(" -c \"")
        j, buf = start, []
        while j < len(run_text):
            ch = run_text[j]
            if ch == "\\" and j + 1 < len(run_text) and run_text[j + 1] in "\"\\$`":
                buf.append(run_text[j + 1])
                j += 2
                continue
            if ch == "\"":
                break
            buf.append(ch)
            j += 1
        if "python" in head:
            found.append(("-c", "".join(buf)))
        pos = j + 1
    return found


if __name__ == "__main__":
    if "--write-em-dash-baseline" in sys.argv:
        _write_em_dash_baseline()
    else:
        print(__doc__)
