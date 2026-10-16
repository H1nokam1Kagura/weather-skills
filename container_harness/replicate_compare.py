"""Agreement across repeat runs of the same frozen cases (LIVE-WAVE-BRIEF noise-floor replicate).

Answers one question per case: is a single run enough to read the result, or does the case vary
between runs? Diagnostic only - never a score and never the headline (the frozen scorer is).

Per case executed in at least two runs:
  * outcome      - the terminal result and the controller's blocking gate, per run
  * agreement    - whether every run gave the same (terminal, failed_gate); flip rate = share of
                   run pairs that differ
  * failing      - the set of non-PASS / non-NOT_APPLICABLE assertion ids, per run; mean and
                   lowest pairwise Jaccard (both-empty counts as 1.0, reported separately)
  * plan         - whether every run validated the same plan (plan hash), so "different plan"
                   and "same plan, different judgement" are not confused

Usage: python -I container_harness/replicate_compare.py OUT_DIR RESULTS_DIR [RESULTS_DIR ...]
Each RESULTS_DIR holds RESULTS.json (the simkit-results artifact) and, optionally, case/<case_id>/
(the case artifact) for plan hashes; a missing case artifact leaves the plan column MISSING.
"""

import itertools
import json
import sys
from pathlib import Path

OK = {"PASS", "NOT_APPLICABLE"}


def _status(value):
    return value.get("status") if isinstance(value, dict) else value


def load_run(results_dir):
    root = Path(results_dir)
    data = json.loads((root / "RESULTS.json").read_text(encoding="utf-8"))
    runs = {}
    for case in data.get("cases") or []:
        if case.get("status") != "EXECUTED":
            continue
        trace = {}
        record = root / "case" / case["case_id"] / "harness-out" / "runner_record.json"
        if record.is_file():
            evidence = json.loads(record.read_text(encoding="utf-8")).get("evidence") or {}
            trace = evidence.get("controller_trace") or {}
        terminal = trace.get("terminal") or {}
        plans = [p.get("plan_hash") for p in trace.get("plan_validations") or []]
        runs[case["case_id"]] = {
            "run": data.get("run_url") or str(root),
            "terminal": case.get("terminal"),
            "failed_gate": terminal.get("failed_gate") if trace else None,
            "failing": sorted(
                k for k, v in (case.get("assertions") or {}).items() if _status(v) not in OK
            ),
            "plan_hash": plans[-1] if plans else None,
            "plan_observed": bool(trace),
        }
    return runs


def jaccard(a, b):
    a, b = set(a), set(b)
    return 1.0 if not a and not b else len(a & b) / len(a | b)


def compare(runs_by_dir):
    cases = {}
    for runs in runs_by_dir:
        for case_id, row in runs.items():
            cases.setdefault(case_id, []).append(row)
    out = {}
    for case_id, rows in sorted(cases.items()):
        if len(rows) < 2:
            continue
        pairs = list(itertools.combinations(rows, 2))
        key = [(r["terminal"], r["failed_gate"]) for r in rows]
        flips = sum(
            (a["terminal"], a["failed_gate"]) != (b["terminal"], b["failed_gate"]) for a, b in pairs
        )
        jac = [jaccard(a["failing"], b["failing"]) for a, b in pairs]
        plans = {r["plan_hash"] for r in rows}
        out[case_id] = {
            "runs": len(rows),
            "outcomes": key,
            "outcome_agrees": len(set(key)) == 1,
            "flip_rate": flips / len(pairs),
            "failing_sets": [r["failing"] for r in rows],
            "failing_jaccard_mean": sum(jac) / len(jac),
            "failing_jaccard_min": min(jac),
            "all_clean": all(not r["failing"] for r in rows),
            "same_plan": None
            if not all(r["plan_observed"] for r in rows)
            else (len(plans) == 1 and None not in plans),
            "varying_assertions": sorted(
                set().union(*(r["failing"] for r in rows))
                - set.intersection(*(set(r["failing"]) for r in rows))
            ),
        }
    return out


def summary(per_case):
    n = len(per_case)
    return {
        "cases_compared": n,
        "outcome_agrees": sum(c["outcome_agrees"] for c in per_case.values()),
        "same_plan": sum(c["same_plan"] is True for c in per_case.values()),
        "plan_missing": sum(c["same_plan"] is None for c in per_case.values()),
        "all_clean": sum(c["all_clean"] for c in per_case.values()),
        "needs_more_runs": sorted(
            k for k, c in per_case.items() if not c["outcome_agrees"] or c["varying_assertions"]
        ),
    }


def markdown(per_case, totals, sources):
    lines = [
        "# Replicate agreement (diagnostic; never a score)",
        "",
        "Runs: " + ", ".join(sources),
        "",
        f"Cases compared: {totals['cases_compared']}; outcome agrees in {totals['outcome_agrees']}; "
        f"same plan in {totals['same_plan']} (plan not observed in {totals['plan_missing']}); "
        f"clean in every run: {totals['all_clean']}.",
        "",
        "| case | runs | outcome agrees | flip rate | failing Jaccard mean / min | same plan | varying assertions |",
        "|---|---|---|---|---|---|---|",
    ]
    for case_id, c in per_case.items():
        plan = "MISSING" if c["same_plan"] is None else ("yes" if c["same_plan"] else "no")
        lines.append(
            f"| {case_id.removeprefix('DEV-E2E-V2_7-')} | {c['runs']} | "
            f"{'yes' if c['outcome_agrees'] else 'NO'} | {c['flip_rate']:.2f} | "
            f"{c['failing_jaccard_mean']:.2f} / {c['failing_jaccard_min']:.2f} | {plan} | "
            f"{', '.join(c['varying_assertions']) or '-'} |"
        )
    lines += [
        "",
        "Needs more runs (outcome or assertions vary): "
        + (", ".join(totals["needs_more_runs"]) or "none"),
    ]
    return "\n".join(lines) + "\n"


def main(argv):
    if len(argv) < 4:
        print(__doc__, file=sys.stderr)
        return 2
    out, dirs = Path(argv[1]), argv[2:]
    per_case = compare([load_run(d) for d in dirs])
    totals = summary(per_case)
    out.mkdir(parents=True, exist_ok=True)
    (out / "REPLICATES.json").write_text(
        json.dumps({"sources": dirs, "summary": totals, "cases": per_case}, indent=1),
        encoding="utf-8",
    )
    (out / "REPLICATES.md").write_text(markdown(per_case, totals, dirs), encoding="utf-8")
    print(json.dumps(totals))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
