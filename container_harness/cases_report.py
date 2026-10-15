"""Aggregate a simkit-cases.yml run into GS1-style RESULTS.json + RESULTS.md (host, stdlib only).

usage: python container_harness/cases_report.py ADMISSION.json CASES_DIR --out RESULTS.json
                                                 --markdown RESULTS.md [--run-url URL]

CASES_DIR holds one directory per executed matrix case (the downloaded per-case artifacts,
each with launch.json and, when the in-container post-processing ran, score.json).

Per case exactly one status:
  NOT_IMPLEMENTED                  frozen runner: a required capability is missing (named)
  BLOCKED_LIVE_ROLE_TRANSPORT      admitted, needs live AND substituted roles in one run
                                   (combining hook not built; live-only cases run via the relay)
  BLOCKED_SUBSTITUTED_ROLE_WIRING  admitted, needs substituted roles the adapter cannot build
  ADMITTED_NOT_SELECTED            admitted and runnable, not in this run's selector
  EXECUTED                         the controller actually ran; dimensions from the pinned
                                   frozen scorer over the in-container projection
  NOT_EXECUTED                     the runner returned but the controller wrote no checkpoint
  HARNESS_ERROR                    the harness (not the system under test) failed; the source
                                   is named (container, tracer, projection, scorer, driver)
Oracles are PROPOSED (unratified): every dimension result is diagnostic, never ratified.
Nothing is PASS without observed evidence; MISSING stays MISSING.

Every EXECUTED case also carries a separate, labelled counterfactual "with AMD-V27-WARN-IS-PASS"
(warn_counterfactual.py; JSON key warn_counterfactual.JSON_KEY, its own RESULTS.md column). It is
never applied to the headline: dimensions/terminal/assertions and frozen_summary stay exactly as
the frozen scorer computed them in score.json.
"""

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(HERE))

import warn_counterfactual as wcf  # noqa: E402
HEADER = (
    "Oracles are PROPOSED (unratified). Scoring is DIAGNOSTIC, not ratified: no dimension result "
    "here is a scored SIMKIT result. A case is EXECUTED only if the controller actually ran; "
    "nothing is PASS without observed evidence; MISSING stays MISSING."
)
DIMENSIONS = ("judgment", "containment", "recovery", "utility")


def load(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def frozen():
    """The verified frozen package from the committed fixture (tests/frozen_v27.py)."""
    sys.path.insert(0, str(REPO / "tests"))
    import frozen_v27

    root = frozen_v27.frozen_root()
    sys.path.insert(0, str(root))
    from simkit import guarded, score

    return root, guarded, score


def case_artifacts(cases_dir):
    found = {}
    for launch in Path(cases_dir).rglob("launch.json"):
        rec = load(launch) or {}
        cid = (rec.get("harness_status") or {}).get("case_id") or rec.get("case_id")
        if not cid and rec.get("entry_args"):
            args = rec["entry_args"]
            cid = args[1] if len(args) > 1 and args[0] == "case" else None
        if cid:
            found[cid] = launch.parent
    return found


def classify(row, art):
    """Status for one case from its admission row and (if it ran) its artifact dir."""
    if not row["admitted"]:
        return {"status": "NOT_IMPLEMENTED", "missing_capabilities": row["missing_capabilities"]}
    if row["disposition"] != "RUNNABLE":
        return {"status": row["disposition"]}
    if not row.get("selected"):
        return {"status": "ADMITTED_NOT_SELECTED"}
    if art is None:
        return {"status": "HARNESS_ERROR", "harness_error_source": "no_case_artifact"}
    launch = load(art / "launch.json") or {}
    score = load(art / "score.json")
    out = {
        "container_exit_code": launch.get("container_exit_code"),
        "tracer_status": launch.get("tracer_status"),
        "projection_status": launch.get("projection_status"),
        "run_conditions_hold": all((launch.get("run_conditions") or {}).values()) or None,
        "missing_evidence_fields": launch.get("missing"),
    }
    if launch.get("docker_start_rc") == "timeout":
        return {**out, "status": "HARNESS_ERROR", "harness_error_source": "container_timeout"}
    abort = (launch.get("harness_status") or {}).get("abort")
    if abort:
        return {**out, "status": "HARNESS_ERROR", "harness_error_source": f"entry:{abort}"}
    if score is None:
        return {**out, "status": "HARNESS_ERROR", "harness_error_source": "no_score"}
    status = score.get("run_status") or "HARNESS_ERROR"
    out["status"] = status
    if status == "HARNESS_ERROR":
        out["harness_error_source"] = score.get("harness_error_source") or "unspecified"
        out["error"] = score.get("record_error") or score.get("error")
        for key in ("root_error_type", "root_error"):
            if score.get(key) is not None:
                out[key] = score[key]
    scored = score.get("scored") or {}
    if status == "EXECUTED":
        out["dimensions"] = scored.get("dimensions")
        out["terminal"] = scored.get("terminal")
        out["assertions"] = scored.get("assertions")
        out["scorer_error"] = scored.get("scorer_error")
    if score.get("demoted"):
        out["demoted"] = score["demoted"]
    out["scored"] = scored
    return out


def _cell(text, limit=300):
    """One markdown table cell: no pipe or newline breaks the row; long text is cut."""
    text = " ".join(str(text).split()).replace("|", "\\|")
    return text if len(text) <= limit else text[: limit - 3] + "..."


def harness_error_detail(entry):
    """RESULTS.md detail for a HARNESS_ERROR: its source, the error text, and the root cause
    when the controller recorded a different one underneath (run-copy/result.json)."""
    parts = [entry.get("harness_error_source", "")]
    if entry.get("error"):
        parts.append(_cell(entry["error"]))
    if entry.get("root_error_type"):
        root = entry["root_error_type"]
        if entry.get("root_error"):
            root += ": " + entry["root_error"]
        parts.append("root " + _cell(root))
    return " - ".join(p for p in parts if p)


def build(admission, cases_dir, run_url=None):
    root, guarded, score_mod = frozen()
    arts = case_artifacts(cases_dir) if cases_dir and Path(cases_dir).is_dir() else {}
    rows = admission["cases"]
    per_case = []
    scored_all = []
    cf_scored_all = []
    rescorer = None
    for row in rows:
        res = classify(row, arts.get(row["case_id"]))
        entry = {
            "case_id": row["case_id"],
            "pair_id": row["pair_id"],
            "kind": row["kind"],
            "execution_mode": row["execution_mode"],
            "role_mode": row["role_mode"],
            "oracle_status": row["oracle_status"],
            **res,
        }
        per_case.append(entry)
        scored = res.get("scored")
        if not scored:
            frozen_status = (
                res["status"] if res["status"] in guarded.RUN_STATUSES else "NOT_EXECUTED"
            )
            scored = guarded._undecided(
                {
                    "id": row["case_id"],
                    "kind": row["kind"],
                    "family_id": row.get("family_id"),
                    "oracle": {"status": row["oracle_status"]},
                },
                frozen_status,
                f"not scored: {res['status']}",
            )
        scored_all.append(scored)
        cf_scored = scored
        if res["status"] == "EXECUTED":
            # Labelled counterfactual, computed on a COPY: never written back into entry's
            # frozen dimensions/terminal/assertions or into scored_all.
            projection = load(arts[row["case_id"]] / "projection.json") or {}
            if projection.get("status") != "OK":
                entry[wcf.JSON_KEY] = wcf.not_computed(
                    f"projection {projection.get('status', 'ABSENT')}"
                )
            else:
                if rescorer is None:
                    rescorer = wcf.Rescorer(root)
                entry[wcf.JSON_KEY], cf = rescorer.counterfactual(
                    row["case_id"], (projection.get("projection") or {}).get("evidence"), scored
                )
                cf_scored = cf or scored
        cf_scored_all.append(cf_scored)
        entry.pop("scored", None)
    counts = {}
    for e in per_case:
        counts[e["status"]] = counts.get(e["status"], 0) + 1
    pairs = {}
    for e in per_case:
        p = pairs.setdefault(e["pair_id"], {"pair_id": e["pair_id"], "cases": {}})
        p["cases"][e["kind"]] = e["status"]
    for p in pairs.values():
        st = set(p["cases"].values())
        p["both_executed"] = st == {"EXECUTED"} and len(p["cases"]) == 2
        p["status"] = "EXECUTED" if p["both_executed"] else ("+".join(sorted(st)))
    pair_counts = {}
    for p in pairs.values():
        pair_counts[p["status"]] = pair_counts.get(p["status"], 0) + 1
    executed = counts.get("EXECUTED", 0)
    cf_entries = [e for e in per_case if wcf.JSON_KEY in e]
    computed = [e for e in cf_entries if e[wcf.JSON_KEY]["status"] == "COMPUTED"]
    counterfactual = {
        "label": wcf.LABEL,
        "amendment_id": wcf.AMENDMENT_ID,
        "applied_to_headline": False,
        "decision": wcf.DECISION,
        "note": wcf.NOTE,
        "executed_cases": len(cf_entries),
        "computed_cases": len(computed),
        "not_computed_or_failed_method_check": {
            e["case_id"]: e[wcf.JSON_KEY]["status"] for e in cf_entries if e not in computed
        },
        "cases_with_no_non_pass_assertion": {
            "as_frozen": sum(
                1
                for e in computed
                if all(v == "PASS" for v in (e.get("assertions") or {}).values())
            ),
            wcf.LABEL: sum(1 for e in computed if not e[wcf.JSON_KEY]["non_pass"]),
            "of_computed": len(computed),
        },
        "assertions_changed": {
            e["case_id"]: e[wcf.JSON_KEY]["assertions_changed"]
            for e in computed
            if e[wcf.JSON_KEY]["assertions_changed"]
        },
        "summary": score_mod.summarize(cf_scored_all),
    }
    return {
        "schema": "simkit.gha-results/1",
        "header": HEADER,
        "oracles": "PROPOSED",
        "scoring": "DIAGNOSTIC_NOT_RATIFIED",
        "run_url": run_url,
        "gs1": "NOT_MET" if executed < len(per_case) else "EXECUTED_ALL_DIAGNOSTIC_ONLY",
        "frozen_final_pins_sha256": admission.get("frozen_final_pins_sha256"),
        "frozen_root_used_for_summary": str(root),
        "adapter": admission.get("adapter"),
        "implemented_capabilities": admission.get("implemented_capabilities"),
        "selector": admission.get("selector"),
        "counts": {
            "total": len(per_case),
            "pairs": len(pairs),
            "admitted": admission["counts"]["admitted"],
            "by_status": dict(sorted(counts.items())),
            "pairs_by_status": dict(sorted(pair_counts.items())),
        },
        "top_missing_capabilities": admission.get("missing_capability_counts"),
        "missing_capability_reasons": admission.get("missing_capability_reasons"),
        "frozen_summary": score_mod.summarize(scored_all),
        wcf.JSON_KEY: counterfactual,
        "pairs": sorted(pairs.values(), key=lambda p: p["pair_id"]),
        "cases": per_case,
    }


def markdown(res):
    c = res["counts"]
    lines = [
        "# SIMKIT v2.7 frozen cases on GitHub Actions",
        "",
        f"> {res['header']}",
        "",
        f"- Run: {res['run_url'] or 'n/a'}",
        f"- GS1: **{res['gs1']}**",
        f"- Adapter: `{res['adapter']}`; selector `{res['selector']}`",
        f"- Cases: {c['total']} ({c['pairs']} pairs); admitted by the frozen runner: {c['admitted']}",
        "",
        "## Counts by status",
        "",
        "| status | cases |",
        "|---|---|",
        *[f"| {k} | {v} |" for k, v in c["by_status"].items()],
        "",
        "## Pairs by status",
        "",
        "| pair status | pairs |",
        "|---|---|",
        *[f"| {k} | {v} |" for k, v in c["pairs_by_status"].items()],
        "",
        "## Top missing capabilities (cases needing each)",
        "",
        "| capability | cases | why undeclared (adapter view) |",
        "|---|---|---|",
        *[
            f"| {k} | {v} | {(res.get('missing_capability_reasons') or {}).get(k, '')} |"
            for k, v in (res["top_missing_capabilities"] or {}).items()
        ],
        "",
        "## Cases",
        "",
        "`detail` is the result as frozen (the headline). The last column is a labelled "
        f"counterfactual: {wcf.NOTE}",
        "",
        f"| case | mode | roles | status | detail (as frozen) | {wcf.COLUMN} |",
        "|---|---|---|---|---|---|",
    ]
    for e in res["cases"]:
        if e["status"] == "NOT_IMPLEMENTED":
            detail = f"{len(e['missing_capabilities'])} missing"
        elif e["status"] == "EXECUTED":
            dims = e.get("dimensions") or {}
            detail = (
                ", ".join(f"{d}={dims.get(d)}" for d in DIMENSIONS)
                + f", terminal={e.get('terminal')}"
            )
        elif e["status"] == "HARNESS_ERROR":
            detail = harness_error_detail(e)
        else:
            detail = e.get("demoted", "")
        cf = _cell(wcf.summary_cell(e.get(wcf.JSON_KEY)), limit=600)
        lines.append(
            f"| {e['case_id']} | {e['execution_mode']} | {e['role_mode']} | {e['status']} "
            f"| {detail} | {cf} |"
        )
    lines += [
        "",
        "Frozen summary (rates over decided cases only):",
        "",
        "```",
        json.dumps(res["frozen_summary"], indent=1),
        "```",
        "",
    ]
    cfr = res.get(wcf.JSON_KEY)
    if cfr:
        clean = cfr["cases_with_no_non_pass_assertion"]
        lines += [
            f"## Counterfactual: {wcf.LABEL} "
            "(not applied; the frozen result above is the headline)",
            "",
            f"> {cfr['note']}",
            "",
            f"- Decision: {cfr['decision']}",
            f"- Executed cases: {cfr['executed_cases']}; counterfactual computed: "
            f"{cfr['computed_cases']}"
            + (
                "; not computed / method check failed: "
                f"{cfr['not_computed_or_failed_method_check']}"
                if cfr["not_computed_or_failed_method_check"]
                else ""
            ),
            f"- Cases with no non-PASS assertion: as frozen {clean['as_frozen']} of "
            f"{clean['of_computed']}; {wcf.LABEL} {clean[wcf.LABEL]} of {clean['of_computed']}",
            "",
            f"| case | assertion | as frozen | {wcf.LABEL} |",
            "|---|---|---|---|",
            *[
                f"| {cid} | {aid} | {v['as_frozen']} | {v[wcf.LABEL]} |"
                for cid, changed in cfr["assertions_changed"].items()
                for aid, v in changed.items()
            ],
            "",
            f"Counterfactual summary ({wcf.LABEL}; rates over decided cases only):",
            "",
            "```",
            json.dumps(cfr["summary"], indent=1),
            "```",
            "",
        ]
    return "\n".join(lines)


def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("admission")
    ap.add_argument("cases_dir")
    ap.add_argument("--out", required=True)
    ap.add_argument("--markdown", required=True)
    ap.add_argument("--run-url")
    a = ap.parse_args(argv[1:])
    res = build(load(a.admission), a.cases_dir, a.run_url)
    Path(a.out).write_text(json.dumps(res, indent=1, default=str) + "\n", encoding="utf-8")
    Path(a.markdown).write_text(markdown(res), encoding="utf-8")
    print(json.dumps(res["counts"], indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
