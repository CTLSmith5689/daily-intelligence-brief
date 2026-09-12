#!/usr/bin/env python3
"""Gate a written thesis before it is allowed into the archive.

These are not style preferences. Each check corresponds to a specific way that
AI-written research goes wrong, and each is mechanical so it cannot be argued
with at 3am by a model that would rather ship something.

    python3 theses/bin/validate.py theses/notes/YELP/2026-09-14-initiation.md
    python3 theses/bin/validate.py theses/runs/2026-09-14/          # whole run

Exit code 0 = all pass. 1 = at least one FAIL. Warnings never fail the run.
"""
import re, sys
from pathlib import Path

REQUIRED_FM = ["thesis_id", "ticker", "kind", "written_on", "panel_date", "entry_price",
               "direction", "conviction", "horizon_days", "target_price", "review_by",
               "falsifier", "data_caveats"]
DIRECTIONS = {"long", "short", "avoid", "watch", "no view"}
KINDS = {"initiation", "update", "revision", "close"}
SECTIONS = ["WHAT IS PRICED IN", "WHERE I DIFFER", "WHAT I DON'T KNOW"]


def parse(text):
    m = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.S)
    if not m:
        return None, text
    fm, body = {}, m.group(2)
    key, buf = None, []
    for line in m.group(1).splitlines():
        if re.match(r"^\s*-\s+", line) and key:
            fm.setdefault(key + "__list", []).append(line.strip()[1:].strip())
            continue
        mm = re.match(r"^([A-Za-z_][\w]*):\s*(.*)$", line)
        if mm:
            key, val = mm.group(1), mm.group(2).strip()
            # Inline flow lists: "[]" and "[a, b]". Without this an empty inline
            # list reads as the string "[]" and the caller reports the wrong
            # defect, which is worse than reporting none.
            if val.startswith("[") and val.endswith("]"):
                inner = val[1:-1].strip()
                fm[key] = [x.strip() for x in inner.split(",") if x.strip()] if inner else []
            else:
                fm[key] = val
    for k in list(fm):
        if k.endswith("__list"):
            fm[k[:-6]] = fm.pop(k)
    return fm, body


def check(path):
    text = Path(path).read_text(encoding="utf-8")
    fm, body = parse(text)
    fails, warns = [], []
    F, W = fails.append, warns.append

    if fm is None:
        return ["no YAML front-matter: the PM agent reads front-matter, not prose"], []

    for k in REQUIRED_FM:
        if k not in fm:
            F(f"missing required field: {k}")
        elif fm[k] in ("", None) and not isinstance(fm[k], list):
            # An empty LIST is a real value with its own diagnostic below.
            # Reporting it twice buries the useful message under the generic one.
            F(f"required field is blank: {k}")

    # The defence against a note that asserts more than it can know.
    cav = fm.get("data_caveats")
    if isinstance(cav, list):
        if len(cav) == 0:
            F("data_caveats is empty. No thesis built on this pipeline knows everything: "
              "there are no consensus estimates anywhere in it, which alone is a caveat.")
        elif len(cav) == 1:
            W("only one data caveat. Check the dossier's own caveat list was carried over.")
    elif cav:
        F("data_caveats must be a list, not a string")

    fal = fm.get("falsifier", "")
    if fal and len(fal) < 40:
        F(f"falsifier is {len(fal)} chars and is almost certainly not checkable. It needs a "
          f"condition, a threshold and a source.")
    if fal and not re.search(r"\d", fal):
        F("falsifier contains no number. 'the thesis deteriorates' is not falsifiable.")

    d = (fm.get("direction") or "").strip().strip('"')
    if d and d not in DIRECTIONS:
        F(f"direction {d!r} not one of {sorted(DIRECTIONS)}")
    k = (fm.get("kind") or "").strip().strip('"')
    if k and k not in KINDS:
        F(f"kind {k!r} not one of {sorted(KINDS)}")

    try:
        c = int(str(fm.get("conviction", "")).strip())
        if not 1 <= c <= 5:
            F(f"conviction {c} outside 1-5")
    except (TypeError, ValueError):
        F("conviction is not an integer")

    for f in ("entry_price", "target_price"):
        try:
            float(str(fm.get(f, "")).strip())
        except (TypeError, ValueError):
            F(f"{f} is not numeric")

    # A directional call with nothing said about why is the failure this whole
    # design exists to prevent.
    if d and d != "no view" and not fm.get("key_claim"):
        F(f"direction is {d!r} but there is no key_claim. A direction without a claim is a "
          f"guess wearing a number.")

    up = body.upper()
    for s in SECTIONS:
        if s not in up and s.replace("'", "’") not in up:
            F(f"body is missing the section: {s}")

    if "—" in text:
        n = text.count("—")
        F(f"{n} em dash{'es' if n > 1 else ''}. Repo brand voice forbids them.")

    words = len(re.findall(r"\b\w+\b", body))
    if words < 220:
        F(f"body is {words} words. Too short to have shown any reasoning.")
    elif words > 1200:
        W(f"body is {words} words, over the 900-word target.")

    # Cheap proxy for recitation: prose that is mostly field names read back.
    cites = len(re.findall(r"`[a-z_0-9]+`", body))
    if cites == 0:
        F("no field is cited anywhere in the body. Every quantitative claim must name the "
          "field and value it came from.")
    elif cites > 0 and words / max(cites, 1) < 18:
        W(f"{cites} field citations in {words} words: dense enough that this may be reciting "
          f"the panel rather than reasoning about it.")

    return fails, warns


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    target = Path(sys.argv[1])
    if target.is_dir():
        # A run directory holds dossiers as well as notes. A dossier has no
        # front-matter and is not meant to: it is the input. Scanning a
        # directory checks what claims to be a note; naming a file explicitly
        # checks that file, front-matter or not.
        files = [f for f in sorted(target.rglob("*.md"))
                 if f.read_text(encoding="utf-8").lstrip().startswith("---")]
        if not files:
            print(f"validate: no notes with front-matter under {target} "
                  f"({len(list(target.glob('*.md')))} markdown files there are dossiers or "
                  f"other input).")
            return 0
    else:
        files = [target]
    if not files:
        print(f"validate: nothing to check in {target}")
        return 0

    bad = 0
    for f in files:
        fails, warns = check(f)
        status = "FAIL" if fails else ("warn" if warns else "pass")
        print(f"\n[{status}] {f.name}")
        for m in fails:
            print(f"   FAIL  {m}")
        for m in warns:
            print(f"   warn  {m}")
        bad += bool(fails)
    print(f"\n{len(files) - bad}/{len(files)} passed")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
