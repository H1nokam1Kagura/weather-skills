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


def admission(rows):
    return {
        "schema": "simkit.gha-admission/1",
        "adapter": "test",
        "selector": "test",
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
