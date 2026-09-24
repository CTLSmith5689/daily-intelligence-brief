#!/usr/bin/env python3
"""Check the headline labels the News Desk writes.

The News Desk routine (theses/NEWS_DESK.md), on the Haiku model, labels the
news pack's headlines one batch at a time and writes each batch to a file;
this script shows a batch, checks and merges the batch files, and checks the
merged file against news.json. Both files are in theses/news/latest/
(NEWS_LATEST in common.py). No code here calls a model.

    python3 theses/bin/news_labels_check.py --show-batch N      # the headlines of batch N
    python3 theses/bin/news_labels_check.py --merge DIR         # check DIR/batch-NN.json, merge the good ones
    python3 theses/bin/news_labels_check.py                     # check news_labels.json

Exit code 0 = pass, 1 = at least one FAIL, 2 = usage.

A batch file, and news_labels.json's "labels", map a headline id to:

    {"relevant_to_company": true,
     "event_type": "earnings",          # one of EVENT_TYPES
     "tone": -1,                        # an integer from -2 to +2
     "checkable_claim": "shares fell 6%" or null}

--merge checks each batch file against the ids of its batch in news.json: a
file must label every id of its batch and nothing else. Good batches are
written into theses/news/latest/news_labels.json; failed ones are listed by
number, to be labelled again. A label already in that file is kept while its
headline is still in news.json: an id is a hash of the title, the source and
the time, and a label reads nothing else, so it stays right from one run to
the next. A good batch overwrites it.
"""
import json, re, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import NEWS_LATEST

NEWS = NEWS_LATEST / "news.json"
LABELS = NEWS_LATEST / "news_labels.json"
EVENT_TYPES = ("earnings", "guidance", "m_and_a", "legal_regulatory", "rating_change",
               "management", "product", "macro", "noise")
KEYS = ("relevant_to_company", "event_type", "tone", "checkable_claim")
MAX_CLAIM = 200
EM_DASH, EN_DASH = chr(0x2014), chr(0x2013)


def load_news(path=None):
    return json.loads(Path(path or NEWS).read_text(encoding="utf-8"))


def headlines_by_id(news):
    return {h["id"]: dict(h, company=n.get("name", "")) for n in news.get("names", [])
            for h in n.get("headlines", [])}


def check_label(hid, lab):
    """Fails for one label."""
    where = f"{hid}"
    if not isinstance(lab, dict):
        return [f"{where}: the label is not an object"]
    fails = []
    missing = [k for k in KEYS if k not in lab]
    extra = [k for k in lab if k not in KEYS]
    if missing:
        fails.append(f"{where}: missing {', '.join(missing)}")
    if extra:
        fails.append(f"{where}: unknown keys {', '.join(extra)}")
    if "relevant_to_company" in lab and not isinstance(lab["relevant_to_company"], bool):
        fails.append(f"{where}: relevant_to_company must be true or false")
    if "event_type" in lab and lab["event_type"] not in EVENT_TYPES:
        fails.append(f"{where}: event_type {lab['event_type']!r} is not one of {', '.join(EVENT_TYPES)}")
    t = lab.get("tone")
    if "tone" in lab and (isinstance(t, bool) or not isinstance(t, int) or not -2 <= t <= 2):
        fails.append(f"{where}: tone {t!r} must be a whole number from -2 to 2")
    c = lab.get("checkable_claim")
    if "checkable_claim" in lab and c is not None:
        if not isinstance(c, str) or not c.strip():
            fails.append(f"{where}: checkable_claim must be short text or null")
        elif len(c) > MAX_CLAIM:
            fails.append(f"{where}: checkable_claim is {len(c)} characters; keep it under {MAX_CLAIM}")
        elif EM_DASH in c or EN_DASH in c:
            fails.append(f"{where}: checkable_claim contains an em or en dash")
    return fails


def check_labels(labels, news, expect_ids=None):
    """(fails, warns). labels is {id: label}. expect_ids, when given, is the
    exact set of ids the labels must cover (a batch)."""
    fails, warns = [], []
    if not isinstance(labels, dict):
        return ["the labels are not an object keyed by headline id"], []
    known = headlines_by_id(news)
    for hid, lab in labels.items():
        if hid not in known:
            fails.append(f"{hid}: not a headline id in news.json")
            continue
        fails += check_label(hid, lab)
    if expect_ids is not None:
        want = set(expect_ids)
        missing = sorted(want - set(labels))
        extra = sorted(set(labels) - want)
        if missing:
            fails.append(f"{len(missing)} ids of the batch have no label: {', '.join(missing[:5])}"
                         + (" ..." if len(missing) > 5 else ""))
        if extra:
            fails.append(f"{len(extra)} labels are for ids outside the batch: {', '.join(extra[:5])}"
                         + (" ..." if len(extra) > 5 else ""))
    else:
        targets = [i for b in news.get("label_batches", []) for i in b]
        unlabelled = [i for i in targets if i not in labels]
        if unlabelled:
            warns.append(f"{len(unlabelled)} of {len(targets)} headlines to label have no label")
    return fails, warns


def read_labels_file(path):
    """{id: label} from a labels or batch file. A file may be the mapping
    itself or {"labels": mapping}."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(data, dict) and isinstance(data.get("labels"), dict):
        return data["labels"]
    return data


def show_batch(news, n):
    batches = news.get("label_batches", [])
    if not 1 <= n <= len(batches):
        print(f"there is no batch {n}: news.json has {len(batches)}", file=sys.stderr)
        return 2
    known = headlines_by_id(news)
    for hid in batches[n - 1]:
        h = known[hid]
        print(json.dumps({"id": hid, "company": f"{h['ticker']} {h['company']}".strip(),
                          "source": h["source"], "title": h["title"]}, ensure_ascii=False))
    return 0


def merge(news, folder, out_path=None):
    """Check every batch-NN.json in folder; merge the good ones into news_labels.json."""
    out_path = Path(out_path or LABELS)
    batches = news.get("label_batches", [])
    g = news.get("generated_for", {})
    known = headlines_by_id(news)
    merged = {}
    if out_path.exists():
        # Labels of headlines still in the pack are kept (the module docstring
        # says why); the rest are dropped.
        try:
            prev = json.loads(out_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            prev = {}
        if isinstance(prev, dict) and isinstance(prev.get("labels"), dict):
            merged = {k: v for k, v in prev["labels"].items() if k in known and not check_label(k, v)}
    failed = []
    for n in range(1, len(batches) + 1):
        f = Path(folder) / f"batch-{n:02d}.json"
        if not f.exists():
            print(f"[FAIL] batch {n}: {f} is missing")
            failed.append(n)
            continue
        try:
            labels = read_labels_file(f)
        except ValueError as exc:
            print(f"[FAIL] batch {n}: not valid JSON ({exc})")
            failed.append(n)
            continue
        fails, _ = check_labels(labels, news, batches[n - 1])
        if fails:
            print(f"[FAIL] batch {n}: {len(fails)} problem{'s' if len(fails) != 1 else ''}")
            for x in fails[:10]:
                print(f"  FAIL  {x}")
            failed.append(n)
            continue
        merged.update(labels)
        print(f"[pass] batch {n}: {len(labels)} labels")
    out_path.write_text(json.dumps({"week_of": g.get("week_of", ""), "news_asof": g.get("asof", ""),
                                    "labels": dict(sorted(merged.items()))}, indent=1),
                        encoding="utf-8")
    print(f"news_labels_check: {len(merged)} labels in {out_path}. "
          + (f"Label these batches again: {', '.join(map(str, failed))}." if failed else "Every batch passed."))
    return 1 if failed else 0


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)

    def opt(name):
        return args[args.index(name) + 1] if name in args and args.index(name) + 1 < len(args) else None
    news_path = opt("--news") or NEWS
    try:
        news = load_news(news_path)
    except (OSError, ValueError) as exc:
        print(f"news_labels_check: cannot read {news_path}: {exc}", file=sys.stderr)
        return 1
    if "--show-batch" in args:
        try:
            return show_batch(news, int(opt("--show-batch")))
        except (TypeError, ValueError):
            print(__doc__)
            return 2
    if "--merge" in args:
        if not opt("--merge"):
            print(__doc__)
            return 2
        return merge(news, opt("--merge"), opt("--labels"))
    path = Path(opt("--labels") or LABELS)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"[FAIL] {path}: {exc}")
        return 1
    fails, warns = [], []
    if not isinstance(data, dict) or not isinstance(data.get("labels"), dict):
        fails.append('the file must be {"week_of": ..., "labels": {id: label}}')
    else:
        wk = news.get("generated_for", {}).get("week_of", "")
        if data.get("week_of") != wk:
            fails.append(f"week_of {data.get('week_of')!r} does not match news.json's {wk!r}")
        asof = news.get("generated_for", {}).get("asof", "")
        if data.get("news_asof", asof) != asof:
            fails.append(f"news_asof {data.get('news_asof')!r} does not match news.json's {asof!r}: "
                         f"the labels are for an older pack; run --merge again")
        f2, warns = check_labels(data["labels"], news)
        fails += f2
    n = len(data.get("labels") or {}) if isinstance(data, dict) else 0
    print(f"[{'FAIL' if fails else 'warn' if warns else 'pass'}] {path}: {n} labels")
    for x in fails:
        print(f"  FAIL  {x}")
    for x in warns:
        print(f"  warn  {x}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
