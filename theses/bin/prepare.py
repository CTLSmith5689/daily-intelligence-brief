#!/usr/bin/env python3
"""Everything the analyst agent needs, assembled before the model is invoked.

There is deliberately no pre-built "bundle" written by the pipeline. The agent
clones this repo and runs this script, which is why the screen and the dossier
builder are committed Python rather than something the model improvises. It also
means the preparation is versioned alongside the notes it produced: you can go
back to any run and re-derive exactly what the analyst was looking at.

Writes theses/runs/{DATE}/ containing a manifest and one dossier per slot, then
prints what the agent should do next. Costs zero model tokens.

    python3 theses/bin/prepare.py [--slots N] [--date YYYY-MM-DD]

If the Research Director has written a plan for this week (theses/director/,
checked by director_check.py) and it passes, today's assignments take the first
slots, in the plan's order, and the screen fills the rest. With no plan, a plan
that fails the check, or no assignment for today, the run is exactly the
screen's, as it was before the director existed. The manifest's "director" key
says which of those happened.
"""
import json, os, subprocess, sys, time
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

# One clock for the whole pipeline. A US equity pipeline has exactly one
# meaningful day boundary and it is not UTC.
EASTERN = ZoneInfo("America/New_York")
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import THESES, LEDGER, read_csv_rows, fetch_site
import screen

BIN = Path(__file__).resolve().parent


def director_assignments(run_date, check=None):
    """({"path": ..., ...}, [assignment, ...]) for this run.

    path is "no_plan", "invalid_plan", "no_assignments_today" or "director".
    Only the last returns assignments. Nothing here may stop a run: any error
    reading or checking the plan is an invalid plan, and the screen runs alone."""
    # Found without importing anything new, so a run with no plan touches no
    # code the run did not touch before the director existed.
    try:
        day = date.fromisoformat(str(run_date))
    except ValueError:
        return {"path": "no_plan", "plan": "",
                "note": f"Run date {run_date!r} is not a date; no plan was looked for."}, []
    sunday = day - timedelta(days=day.weekday() + 1)
    plan_path = THESES / "director" / f"{sunday.isoformat()}.md"
    rel = f"theses/director/{plan_path.name}"
    if not plan_path.exists():
        return {"path": "no_plan", "plan": rel,
                "note": "No director plan for this week; every slot comes from the screen."}, []
    try:
        import director_check as DC
        fails, warns, plan = (check or DC.check_plan)(plan_path)
    except Exception as exc:          # a broken plan must never break the run
        fails, warns, plan = [f"director_check raised {type(exc).__name__}: {exc}"], [], None
    if fails:
        return {"path": "invalid_plan", "plan": rel, "fails": fails[:20],
                "note": "The plan failed director_check.py and was ignored; every slot "
                        "comes from the screen."}, []
    today = [a for a in (plan or {}).get("assignments", []) if a.get("date") == day.isoformat()]
    if not today:
        return {"path": "no_assignments_today", "plan": rel, "warnings": warns[:20],
                "note": "The plan passed but assigns nothing today; every slot comes from "
                        "the screen."}, []
    return {"path": "director", "plan": rel, "warnings": warns[:20],
            "assigned": [a["ticker"] for a in today]}, today


def merge_slots(assigned, screened, total, per_sector=1):
    """The director's names first, in order, then the screen's, up to `total`.

    A screen name is skipped when the director already took it, and, while
    there are others to choose from, when the director took its sector: the
    screen's own one-name-per-sector rule, applied across both."""
    out = [{"ticker": a["ticker"], "slot": "director", "reason": a["reason"],
            "kind": a["kind"], "desk": a["desk"], "sector": a["sector"],
            "price": a.get("price")} for a in assigned[:total]]
    taken = {s["ticker"] for s in out}
    sectors = {s["sector"] for s in out if s["sector"]}
    rest = [s for s in screened if s.get("ticker") not in taken]
    first = [s for s in rest if not (per_sector <= 1 and s.get("sector") in sectors)]
    second = [s for s in rest if s not in first]
    for s in first + second:
        if len(out) >= total:
            break
        out.append(s)
    return out


def hit_rate():
    """The analyst's record so far, injected into every run.

    A fresh session cannot remember whether it has been any good. Putting the
    record in front of it on every run is the only thing that carries that
    across sessions, and the conviction breakdown matters more than the
    headline: an analyst whose 4s and 5s do no better than its 2s has no edge,
    however well the notes read."""
    scores = read_csv_rows(LEDGER / "scores.csv")
    preds = {p["prediction_id"]: p for p in read_csv_rows(LEDGER / "predictions.csv")}
    if not scores:
        return {"scored": 0, "note": "No prediction has matured yet. There is no track record "
                                     "to calibrate against, so be correspondingly humble."}
    by_conv, out = {}, {"scored": len(scores)}
    right = sum(1 for s in scores if s.get("outcome") == "right")
    out["hit_rate"] = round(100 * right / len(scores))
    for s in scores:
        c = (preds.get(s.get("prediction_id"), {}) or {}).get("conviction")
        if c:
            b = by_conv.setdefault(c, {"n": 0, "right": 0})
            b["n"] += 1
            b["right"] += s.get("outcome") == "right"
    out["by_conviction"] = {c: f"{v['right']}/{v['n']}" for c, v in sorted(by_conv.items())}
    hi = sum(v["right"] for c, v in by_conv.items() if c in ("4", "5"))
    hi_n = sum(v["n"] for c, v in by_conv.items() if c in ("4", "5"))
    lo = sum(v["right"] for c, v in by_conv.items() if c in ("1", "2", "3"))
    lo_n = sum(v["n"] for c, v in by_conv.items() if c in ("1", "2", "3"))
    if hi_n >= 5 and lo_n >= 5:
        hr, lr = hi / hi_n, lo / lo_n
        out["calibration"] = (
            f"High conviction {hr*100:.0f}% vs low conviction {lr*100:.0f}%. "
            + ("Conviction is carrying information." if hr > lr + 0.05 else
               "CONVICTION IS NOT CARRYING INFORMATION. The confident calls are doing no "
               "better than the tentative ones, which means the confidence is decorative. "
               "Weight this heavily when assigning conviction in this run."))
    return out


def main():
    args = sys.argv[1:]
    slots = int(args[args.index("--slots") + 1]) if "--slots" in args else None
    # Eastern, not UTC. The rest of the pipeline stamps everything in Eastern
    # (lambda_function uses datetime.now(EASTERN) for the panel date), and the
    # analyst session writes notes dated in its own local time. With UTC here,
    # any run between 20:00 and midnight Eastern put the dossiers in tomorrow's
    # runs/ directory while the note it produced carried today's date, so the
    # run and its output no longer reconciled.
    run_date = (args[args.index("--date") + 1] if "--date" in args
                else datetime.now(tz=EASTERN).date().isoformat())

    # The dossiers read price history and headlines from the published site. When
    # it cannot be reached they still build, only without those sections, and the
    # run looks healthy: on 2026-09-13 a cloud session's proxy refused github.io,
    # all four dossiers came back with no close series and no headlines, and this
    # script reported that none had failed. So check once, before doing any work.
    if fetch_site("prices/_MARKET.json") is None:
        print("prepare: FAILED. The published site's price files cannot be reached from this "
              "session, from GitHub Pages or from raw.githubusercontent.com, so every dossier "
              "would be built without price history or headlines. Stopping before any work.",
              file=sys.stderr)
        return 1

    env = dict(os.environ)
    if slots:
        env["THESES_SLOTS"] = str(slots)

    director, assigned = director_assignments(run_date)
    print(f"prepare: director path {director['path']} ({director['plan']})", file=sys.stderr)
    total = slots or screen.CFG["slots_per_run"]
    if assigned:
        # Ask the screen for enough names to fill around the director's, so a
        # screen name dropped for a shared sector still leaves one to take.
        env["THESES_SLOTS"] = str(total + len(assigned))

    t0 = time.time()
    print(f"prepare: screening for {run_date} ...", file=sys.stderr)
    out = subprocess.run([sys.executable, str(BIN / "screen.py")],
                         capture_output=True, text=True, env=env)
    if out.returncode != 0:
        print(f"prepare: screen failed\n{out.stderr}", file=sys.stderr)
        return 1
    plan = json.loads(out.stdout)
    if assigned:
        plan["slots"] = merge_slots(assigned, plan.get("slots", []), total,
                                    screen.CFG.get("max_per_sector_per_run", 1))

    run_dir = THESES / "runs" / run_date
    run_dir.mkdir(parents=True, exist_ok=True)

    built, failed = [], []
    for s in plan.get("slots", []):
        t = s["ticker"]
        print(f"prepare:   dossier {t} ...", file=sys.stderr)
        d = subprocess.run([sys.executable, str(BIN / "dossier.py"), t],
                           capture_output=True, text=True, env=env)
        if d.returncode != 0 or len(d.stdout) < 500:
            failed.append(t)
            continue
        (run_dir / f"{t}.md").write_text(d.stdout, encoding="utf-8")
        built.append({**s, "dossier": f"{t}.md", "dossier_chars": len(d.stdout)})

    manifest = {
        "run_date": run_date,
        "panel_date": plan.get("panel_date"),
        "prepared_at": datetime.now(tz=timezone.utc).isoformat(timespec="seconds"),
        "universe": {"panel_rows": plan.get("panel_rows"),
                     "core": plan.get("core_universe"),
                     "scorable": plan.get("scorable"),
                     "peer_groups": plan.get("peer_groups")},
        "track_record": hit_rate(),
        "open_predictions": plan.get("open_predictions", 0),
        "slots": built,
        "dossier_failed": failed,
        "director": director,
        "elapsed_s": round(time.time() - t0, 1),
    }
    (run_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    print(json.dumps(manifest, indent=2))
    print(f"\nprepare: {len(built)} dossiers in {manifest['elapsed_s']}s -> {run_dir}",
          file=sys.stderr)
    if failed:
        print(f"prepare: dossier failed for {failed}; those slots have no context and "
              f"MUST NOT be written up blind.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
