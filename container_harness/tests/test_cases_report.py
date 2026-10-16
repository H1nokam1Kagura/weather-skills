"""cases_report.py: per-case statuses and honesty rules over synthetic artifacts (no docker).

Uses the frozen v2.7 package from the committed fixture (tests/frozen_v27.py) for the guarded
scorer's undecided records and summarize(); nothing here executes a case.
"""

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import cases_report  # noqa: E402

CAPS = ["CAP-NOT-IMPLEMENTED"]


def row(cid, kind, admitted=True, disposition="RUNNABLE", selected=True, missing=()):
    return {
        "case_id": cid,
        "pair_id": cid.rsplit("-", 1)[0],
        "kind": kind,
        "family_id": "EX",
        "execution_mode": "controller_conformance",
        "role_mode": "substituted",
        "admitted": admitted,
        "missing_capabilities": list(missing),
        "disposition": disposition if admitted else "NOT_IMPLEMENTED",
        "selected": selected and admitted,
        "oracle_status": "PROPOSED",
        "required_capabilities": CAPS,
    }


def admission(rows, selector="test"):
    return {
        "schema": "simkit.gha-admission/1",
        "adapter": "test",
        "selector": selector,
        "implemented_capabilities": CAPS,
        "counts": {"admitted": sum(r["admitted"] for r in rows)},
        "missing_capability_counts": {"CAP-X": 1},
        "missing_capability_reasons": {"CAP-X": "NO_ADAPTER_METHOD"},
        "cases": rows,
    }


def artifact(tmp, cid, launch=None, score=None):
    d = tmp / "cases" / cid
    d.mkdir(parents=True)
    rec = {
        "entry_args": ["case", cid],
        "harness_status": {"case_id": cid},
        "container_exit_code": 0,
        "tracer_status": "OK",
        "run_conditions": {"a": True},
    }
    rec.update(launch or {})
    (d / "launch.json").write_text(json.dumps(rec), encoding="utf-8")
    if score is not None:
        (d / "score.json").write_text(json.dumps(score), encoding="utf-8")
    return d


def executed_score(cid, dims):
    return {
        "case_id": cid,
        "run_status": "EXECUTED",
        "scored": {
            "case_id": cid,
            "kind": "control",
            "run_status": "EXECUTED",
            "oracle_status": "UNRATIFIED_DEV",
            "dimensions": dims,
            "assertions": {},
            "terminal": "NOT_OBSERVED",
        },
    }


def by_id(res):
    return {e["case_id"]: e for e in res["cases"]}


def test_statuses_are_distinct_and_never_inferred(tmp_path):
    rows = [
        row("P1-CTL", "control", admitted=False, missing=["CAP-X"]),
        row("P1-INJ", "injection", disposition="BLOCKED_LIVE_ROLE_TRANSPORT"),
        row("P2-CTL", "control"),  # selected, no artifact at all
        row("P2-INJ", "injection", selected=False),
        row("P3-CTL", "control"),
        row("P3-INJ", "injection"),
        row("P4-CTL", "control"),
        row("P4-INJ", "injection"),
    ]
    dims = {
        "judgment": "PASS",
        "containment": "NOT_OBSERVED",
        "recovery": "NOT_APPLICABLE",
        "utility": "FAIL",
    }
    artifact(tmp_path, "P3-CTL", score=executed_score("P3-CTL", dims))
    artifact(
        tmp_path,
        "P3-INJ",
        score={"case_id": "P3-INJ", "run_status": "NOT_EXECUTED", "demoted": "no checkpoint"},
    )
    artifact(tmp_path, "P4-CTL", launch={"docker_start_rc": "timeout"})
    artifact(
        tmp_path,
        "P4-INJ",
        score={
            "case_id": "P4-INJ",
            "run_status": "HARNESS_ERROR",
            "harness_error_source": "projection_UNAVAILABLE",
        },
    )
    res = cases_report.build(admission(rows), tmp_path / "cases")
    got = by_id(res)
    assert got["P1-CTL"]["status"] == "NOT_IMPLEMENTED"
    assert got["P1-CTL"]["missing_capabilities"] == ["CAP-X"]
    assert got["P1-INJ"]["status"] == "BLOCKED_LIVE_ROLE_TRANSPORT"
    assert got["P2-CTL"] == {
        **got["P2-CTL"],
        "status": "HARNESS_ERROR",
        "harness_error_source": "no_case_artifact",
    }
    assert got["P2-INJ"]["status"] == "ADMITTED_NOT_SELECTED"
    assert got["P3-CTL"]["status"] == "EXECUTED" and got["P3-CTL"]["dimensions"] == dims
    assert got["P3-INJ"]["status"] == "NOT_EXECUTED" and got["P3-INJ"]["demoted"]
    assert got["P4-CTL"]["harness_error_source"] == "container_timeout"
    assert got["P4-INJ"]["harness_error_source"] == "projection_UNAVAILABLE"
    assert res["oracles"] == "PROPOSED" and "DIAGNOSTIC" in res["scoring"]
    assert res["gs1"] == "NOT_MET"
    # Only the executed case can carry decided dimensions into the frozen summary.
    assert res["frozen_summary"]["run_status_counts"]["EXECUTED"] == 1
    assert res["counts"]["by_status"]["HARNESS_ERROR"] == 3
    pairs = {p["pair_id"]: p for p in res["pairs"]}
    assert pairs["P3"]["both_executed"] is False
    md = cases_report.markdown(res)
    assert md.splitlines()[2].startswith("> Oracles are PROPOSED")


def test_missing_score_is_harness_error_not_executed(tmp_path):
    rows = [row("P5-CTL", "control"), row("P5-INJ", "injection", admitted=False, missing=["X"])]
    artifact(tmp_path, "P5-CTL", score=None)
    res = cases_report.build(admission(rows), tmp_path / "cases")
    assert by_id(res)["P5-CTL"]["status"] == "HARNESS_ERROR"
    assert by_id(res)["P5-CTL"]["harness_error_source"] == "no_score"
    assert "EXECUTED" not in res["counts"]["by_status"]


def test_entry_abort_is_harness_error(tmp_path):
    rows = [row("P6-CTL", "control")]
    artifact(
        tmp_path,
        "P6-CTL",
        launch={"harness_status": {"case_id": "P6-CTL", "abort": "staging_failed"}},
    )
    res = cases_report.build(admission(rows), tmp_path / "cases")
    assert by_id(res)["P6-CTL"]["harness_error_source"] == "entry:staging_failed"


def test_runner_harness_error_names_its_source_root_cause_and_text(tmp_path):
    """A HARNESS_ERROR in the runner record itself is never 'unspecified': its exception type,
    the controller's root error type (run-copy/result.json) and the error text reach the
    score record and the RESULTS.md table."""
    import case_driver

    out = tmp_path / "harness-out"
    (out / "run-copy").mkdir(parents=True)
    record = {
        "status": "HARNESS_ERROR",
        "error": "EvidenceMappingError: role files do not correspond one-to-one to receipts",
    }
    (out / "run-copy" / "result.json").write_text(
        json.dumps(
            {
                "status": "INCOMPLETE",
                "error": "unresolved trusted symbol: HARNESS_SOURCE_HASH:definition.x",
                "terminal_mapping": {
                    "source": "runtime_fallback",
                    "error_type": "UnsupportedInjection",
                },
            }
        ),
        encoding="utf-8",
    )
    record_path = out / "runner_record.json"
    assert case_driver.runner_error_source(record, record_path) == {
        "harness_error_source": "runner:EvidenceMappingError",
        "root_error_type": "UnsupportedInjection",
        "root_error": "unresolved trusted symbol: HARNESS_SOURCE_HASH:definition.x",
    }
    # The driver's own record keeps its source; no result.json means no root fields.
    own = {
        "status": "HARNESS_ERROR",
        "error": "case_driver: OSError: x",
        "harness_error_source": "case_driver",
    }
    assert case_driver.runner_error_source(own, tmp_path / "none" / "r.json") == {
        "harness_error_source": "case_driver"
    }
    assert (
        case_driver.runner_error_source({"error": None}, record_path)["harness_error_source"]
        == "runner"
    )

    rows = [row("P7-INJ", "injection")]
    artifact(
        tmp_path,
        "P7-INJ",
        score={
            "case_id": "P7-INJ",
            "run_status": "HARNESS_ERROR",
            "record_error": record["error"] + " | with a pipe",
            **case_driver.runner_error_source(record, record_path),
        },
    )
    res = cases_report.build(admission(rows), tmp_path / "cases")
    got = by_id(res)["P7-INJ"]
    assert got["harness_error_source"] == "runner:EvidenceMappingError"
    assert got["root_error_type"] == "UnsupportedInjection"
    assert got["root_error"].startswith("unresolved trusted symbol")
    (line,) = [x for x in cases_report.markdown(res).splitlines() if x.startswith("| P7-INJ ")]
    assert "runner:EvidenceMappingError" in line
    assert "role files do not correspond" in line and "with a pipe" in line
    assert "root UnsupportedInjection: unresolved trusted symbol" in line
    # the error text's own pipe is escaped: the row stays 6 cells (5 + the labelled
    # counterfactual column, n/a for a case that did not execute)
    assert line.count(" | ") == 5 and line.endswith(" | n/a |")


def test_execution_gate_by_selector():
    """'admission' / 'relay-smoke' / push 'smoke' select nothing by design; every other selector
    is meant to execute cases, so none selected or fewer EXECUTED than selected is a FAIL."""
    gate = cases_report.execution_gate
    for sel in ("admission", "relay-smoke", "smoke"):
        g = gate(sel, 0, 0)
        assert g["status"] == "NOTHING_SELECTED_BY_DESIGN" and g["expects_execution"] is False
        assert g["message"] == (
            f"NOTHING WILL EXECUTE: selector '{sel}' selects 0 cases; "
            "use 'substituted' for runnable offline cases"
        )
    assert gate("smoke", 2, 1)["status"] == "SHORTFALL_NOT_ENFORCED"
    for sel in ("substituted", "live", "A-CTL,A-INJ"):
        assert gate(sel, 0)["status"] == "FAIL"  # admission time
        assert gate(sel, 0, 0)["status"] == "FAIL"
        assert "NOTHING WILL EXECUTE" in gate(sel, 0, 0)["message"]
        assert gate(sel, 3)["status"] == "OK"
        g = gate(sel, 3, 2)
        assert g["status"] == "FAIL" and g["message"] == "only 2 of 3 selected cases EXECUTED"
        assert gate(sel, 3, 3)["status"] == "OK"
    a = admission([row("Q-CTL", "control", selected=False)], selector="substituted")
    a["counts"]["selected"] = 0
    assert cases_report.admission_gate(a)["status"] == "FAIL"
    a["selector"] = "admission"
    assert cases_report.admission_gate(a)["status"] == "NOTHING_SELECTED_BY_DESIGN"


def write_results(tmp_path, res):
    p = tmp_path / "RESULTS.json"
    p.write_text(json.dumps(res), encoding="utf-8")
    return p


def test_admission_selector_with_nothing_selected_still_succeeds(tmp_path):
    rows = [row("Q1-CTL", "control", selected=False), row("Q1-INJ", "injection", selected=False)]
    res = cases_report.build(admission(rows, selector="admission"), tmp_path / "cases")
    g = res["execution_gate"]
    assert (g["status"], g["selected"], g["executed"]) == ("NOTHING_SELECTED_BY_DESIGN", 0, 0)
    md = cases_report.markdown(res).splitlines()
    assert md[2].startswith("> Oracles are PROPOSED")
    assert f"> **NOTHING_SELECTED_BY_DESIGN: {g['message']}**" in md
    assert cases_report.main(["x", "gate", str(write_results(tmp_path, res))]) == 0


def test_substituted_selector_with_nothing_selected_fails_the_run(tmp_path):
    rows = [row("Q2-CTL", "control", selected=False)]
    res = cases_report.build(admission(rows, selector="substituted"), tmp_path / "cases")
    g = res["execution_gate"]
    assert g["status"] == "FAIL" and "NOTHING WILL EXECUTE: selector 'substituted'" in g["message"]
    assert f"> **RUN FAILS (execution gate): {g['message']}**" in cases_report.markdown(res)
    assert cases_report.main(["x", "gate", str(write_results(tmp_path, res))]) == 1


def test_selected_case_that_did_not_execute_fails_the_run(tmp_path):
    rows = [row("Q3-CTL", "control"), row("Q3-INJ", "injection")]
    dims = dict.fromkeys(cases_report.DIMENSIONS, "PASS")
    artifact(tmp_path, "Q3-CTL", score=executed_score("Q3-CTL", dims))
    artifact(tmp_path, "Q3-INJ", score=None)  # HARNESS_ERROR no_score
    res = cases_report.build(admission(rows, selector="Q3-CTL,Q3-INJ"), tmp_path / "cases")
    g = res["execution_gate"]
    assert (g["status"], g["selected"], g["executed"]) == ("FAIL", 2, 1)
    assert cases_report.main(["x", "gate", str(write_results(tmp_path, res))]) == 1
    (tmp_path / "cases" / "Q3-INJ" / "score.json").write_text(
        json.dumps(executed_score("Q3-INJ", dims)), encoding="utf-8"
    )
    res = cases_report.build(admission(rows, selector="Q3-CTL,Q3-INJ"), tmp_path / "cases")
    assert res["execution_gate"]["status"] == "OK"
    assert not [x for x in cases_report.markdown(res).splitlines() if "execution gate" in x]
    assert cases_report.main(["x", "gate", str(write_results(tmp_path, res))]) == 0


def test_absent_results_fail_the_gate(tmp_path):
    assert cases_report.main(["x", "gate", str(tmp_path / "none.json")]) == 1


def test_workflow_runs_both_execution_gates_after_the_uploads():
    text = (cases_report.REPO / ".github" / "workflows" / "simkit-cases.yml").read_text(
        encoding="utf-8"
    )
    admission_job = text.split("\n  admission:\n", 1)[1].split("\n  case:\n", 1)[0]
    assert "cases_report.admission_gate(a)" in admission_job
    assert admission_job.index("r.admission_gate(") > admission_job.index("name: simkit-admission")
    report_job = text.split("\n  report:\n", 1)[1]
    gate = "container_harness/cases_report.py gate results/RESULTS.json"
    assert gate in report_job
    assert report_job.index(gate) > report_job.index("name: simkit-results")
