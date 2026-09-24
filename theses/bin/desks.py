"""Which desk owns a name, and the two files the dossier puts in front of it.

The research is organised in two layers:

- a sector playbook, one per GICS sector, in theses/desks/sectors/{sector-slug}.md:
  the questions that decide a stock in that sector, the valuation measures that
  suit each kind of business in it, the traps, and where our data misleads;
- a desk, one of five, in theses/desks/{desk}.md: which sectors it covers and
  what it cares about across them. A desk owns every name in its sectors, gets
  the Research Director's focus note each week, and is what the scorecards count.

The sector-to-desk map lives in one place, the "desks" key of theses/config.json.
A sector is a column on every panel row, so this is a dictionary lookup: nothing
to embed, and nothing that can come back wrong. Stdlib only.
"""
import json, re
from pathlib import Path

THESES = Path(__file__).resolve().parents[1]
DESKS_DIR = THESES / "desks"
SECTORS_DIR = DESKS_DIR / "sectors"
CONFIG = THESES / "config.json"


def desk_map(config=None):
    """{GICS sector: desk slug}, from theses/config.json."""
    if config is None:
        try:
            config = json.loads(CONFIG.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
    return dict(config.get("desks") or {})


def desk_for(sector, mapping=None):
    """The desk slug that owns a sector, or "" for a blank or unknown sector."""
    return (mapping if mapping is not None else desk_map()).get((sector or "").strip(), "")


def desks(mapping=None):
    """Every desk slug, in the order they first appear in the map."""
    out = []
    for d in (mapping if mapping is not None else desk_map()).values():
        if d not in out:
            out.append(d)
    return out


def sector_slug(sector):
    return re.sub(r"[^a-z0-9]+", "-", (sector or "").lower()).strip("-")


def _read(path):
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _strip_comment(text):
    return re.sub(r"\A\s*<!--.*?-->\s*", "", text, flags=re.S).strip()


def desk_text(desk):
    """The desk file's text, or "" when there is none."""
    return _strip_comment(_read(DESKS_DIR / f"{desk}.md")) if desk else ""


def playbook_text(sector):
    """The sector playbook's text, or "" when there is none."""
    return _strip_comment(_read(SECTORS_DIR / f"{sector_slug(sector)}.md")) if sector else ""


def desk_title(desk):
    """The desk's name, from its file's "# " heading, else the slug."""
    m = re.search(r"^#\s+(.+?)\s*$", desk_text(desk), re.M)
    return m.group(1) if m else desk
