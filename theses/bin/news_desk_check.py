#!/usr/bin/env python3
"""The News Desk's two housekeeping steps (theses/NEWS_DESK.md).

    python3 theses/bin/news_desk_check.py --archive          # dated copy, and prune old copies
    python3 theses/bin/news_desk_check.py --changes          # what is staged
    python3 theses/bin/news_desk_check.py --changes REV      # REV..HEAD

--archive copies news.json, news.md and news_labels.json from theses/news/latest/
into theses/news/{DATE}/, where DATE is the pack's as-of date in US Eastern
time, and deletes dated folders NEWS_KEEP_DAYS days older than that or more.
Everything under theses/news/ is derived from the published site and can be
rebuilt, so deleting an old copy loses nothing the ledger needs.

--changes fails if the News Desk's commit touches anything outside theses/news/,
anything there other than the three pack files in latest/ or a dated folder, or
deletes a file in latest/. Exit code 0 = pass, 1 = FAIL, 2 = usage.

No code here calls a model.
"""
import json, re, shutil, sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import REPO, NEWS_DIR, NEWS_LATEST, NEWS_FILES, NEWS_KEEP_DAYS

EASTERN = ZoneInfo("America/New_York")
DATED = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _rel_prefix(p):
    try:
        return Path(p).resolve().relative_to(REPO.resolve()).as_posix() + "/"
    except ValueError:
        return Path(p).as_posix() + "/"


NEWS_PREFIX = _rel_prefix(NEWS_DIR)
_FILES = "|".join(re.escape(f) for f in NEWS_FILES)
ALLOWED = re.compile(r"^" + re.escape(NEWS_PREFIX) + r"(?P<dir>latest|\d{4}-\d{2}-\d{2})/(?:" + _FILES + r")$")


def pack_date(latest=None):
    """The pack's as-of date, US Eastern, or None when there is no pack."""
    try:
        g = json.loads((Path(latest or NEWS_LATEST) / "news.json").read_text(encoding="utf-8"))["generated_for"]
        return datetime.fromisoformat(g["asof"]).astimezone(EASTERN).date()
    except (OSError, ValueError, KeyError, TypeError):
        return None


def archive(latest=None, news_dir=None, keep_days=NEWS_KEEP_DAYS):
    """(dated folder, [pruned folder names]). Raises FileNotFoundError when
    latest/ has no readable news.json."""
    latest, news_dir = Path(latest or NEWS_LATEST), Path(news_dir or NEWS_DIR)
    day = pack_date(latest)
    if day is None:
        raise FileNotFoundError(f"{latest / 'news.json'} is missing or unreadable; run news_pack.py first")
    dest = news_dir / day.isoformat()
    dest.mkdir(parents=True, exist_ok=True)
    for f in NEWS_FILES:
        src = latest / f
        if src.exists():
            shutil.copyfile(src, dest / f)
        elif (dest / f).exists():
            (dest / f).unlink()
    cut = day - timedelta(days=keep_days)
    pruned = []
    for d in sorted(news_dir.iterdir()):
        if d.is_dir() and DATED.match(d.name) and date.fromisoformat(d.name) <= cut:
            shutil.rmtree(d)
            pruned.append(d.name)
    return dest, pruned


def check_changes(changes):
    """Fails for any change the News Desk's commit may not make. changes is
    [(status, path)] as git prints them (A, M, D)."""
    fails = []
    for status, p in changes:
        status = (status or "?")[0]
        m = ALLOWED.match(p)
        if not p.startswith(NEWS_PREFIX):
            fails.append(f"{p}: the News Desk may change only {NEWS_PREFIX}")
        elif not m:
            fails.append(f"{p}: {NEWS_PREFIX} holds only latest/ and dated folders, each with "
                         f"{', '.join(NEWS_FILES)}")
        elif status == "D" and m.group("dir") == "latest":
            fails.append(f"{p}: deletes a file in latest/; only old dated folders are deleted")
    return fails


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if args[:1] == ["--archive"]:
        try:
            dest, pruned = archive()
        except FileNotFoundError as exc:
            print(f"[FAIL] {exc}")
            return 1
        print(f"[pass] copied the pack to {_rel_prefix(dest)[:-1]}; "
              + (f"deleted {len(pruned)} old dated folders: {', '.join(pruned)}" if pruned
                 else "no dated folder was old enough to delete"))
        return 0
    if args[:1] == ["--changes"]:
        import director_check as DC
        base = args[1] if len(args) > 1 else None
        changes, _, _ = DC.git_changes(base)
        fails = check_changes(changes)
        what = "staged changes" if base is None else f"changes {base}..HEAD"
        print(f"[{'FAIL' if fails else 'pass'}] {what}: {len(changes)} file{'s' if len(changes) != 1 else ''}")
        for f in fails:
            print(f"  FAIL  {f}")
        if not changes:
            print("  warn  nothing is staged: run git add theses/news/ first")
        return 1 if fails else 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
