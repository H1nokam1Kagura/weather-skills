"""replicate_compare: agreement across repeat runs is read from the results, never invented."""

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import replicate_compare as rc  # noqa: E402

CASE = "DEV-E2E-V2_7-E02-P2-CTL"


def write_run(root, terminal, gate, failing, plan, with_case=True, status="EXECUTED"):
    root.mkdir(parents=True)
    assertions = {a: "PASS" for a in ("J1", "J2")} | {a: "FAIL" for a in failing}
    results = {
        "run_url": str(root),
        "cases": [
            {"case_id": CASE, "status": status, "terminal": terminal, "assertions": assertions}
        ],
    }
    (root / "RESULTS.json").write_text(json.dumps(results), encoding="utf-8")
    if with_case:
        record = root / "case" / CASE / "harness-out" / "runner_record.json"
        record.parent.mkdir(parents=True)
        trace = {"terminal": {"failed_gate": gate}, "plan_validations": [{"plan_hash": plan}]}
        record.write_text(json.dumps({"evidence": {"controller_trace": trace}}), encoding="utf-8")
    return root


def test_identical_runs_agree_and_need_no_more_runs(tmp_path):
    a = write_run(tmp_path / "a", "FAIL", "repair_exhausted", ["U1"], "p1")
    b = write_run(tmp_path / "b", "FAIL", "repair_exhausted", ["U1"], "p1")
    per_case = rc.compare([rc.load_run(a), rc.load_run(b)])
    c = per_case[CASE]
    assert c["outcome_agrees"] and c["flip_rate"] == 0.0 and c["same_plan"] is True
    assert c["failing_jaccard_mean"] == 1.0 and c["varying_assertions"] == []
    assert rc.summary(per_case)["needs_more_runs"] == []


def test_a_flip_and_varying_assertions_are_flagged(tmp_path):
    a = write_run(tmp_path / "a", "FAIL", "repair_exhausted", ["U1", "TS"], "p1")
    b = write_run(tmp_path / "b", "PASS", None, [], "p1")
    c3 = write_run(tmp_path / "c", "FAIL", "repair_exhausted", ["U1"], "p2")
    per_case = rc.compare([rc.load_run(a), rc.load_run(b), rc.load_run(c3)])
    c = per_case[CASE]
    assert not c["outcome_agrees"] and abs(c["flip_rate"] - 2 / 3) < 1e-9
    assert c["varying_assertions"] == ["TS", "U1"] and c["failing_jaccard_min"] == 0.0
    assert c["same_plan"] is False
    assert rc.summary(per_case)["needs_more_runs"] == [CASE]


def test_missing_case_artifact_leaves_the_plan_missing_not_equal(tmp_path):
    a = write_run(tmp_path / "a", "FAIL", "repair_exhausted", [], "p1")
    b = write_run(tmp_path / "b", "FAIL", "repair_exhausted", [], "p1", with_case=False)
    c = rc.compare([rc.load_run(a), rc.load_run(b)])[CASE]
    assert c["same_plan"] is None and c["all_clean"]


def test_single_runs_and_unexecuted_cases_are_not_compared(tmp_path):
    a = write_run(tmp_path / "a", "FAIL", "x", [], "p1")
    b = write_run(tmp_path / "b", "FAIL", "x", [], "p1", status="NOT_IMPLEMENTED")
    assert rc.compare([rc.load_run(a)]) == {}
    assert rc.compare([rc.load_run(a), rc.load_run(b)]) == {}


def test_main_writes_both_reports(tmp_path):
    a = write_run(tmp_path / "a", "FAIL", "g", ["U1"], "p1")
    b = write_run(tmp_path / "b", "FAIL", "g", ["U1"], "p1")
    assert rc.main(["x", str(tmp_path / "out"), str(a), str(b)]) == 0
    assert (
        json.loads((tmp_path / "out" / "REPLICATES.json").read_text())["summary"]["cases_compared"]
        == 1
    )
    assert "never a score" in (tmp_path / "out" / "REPLICATES.md").read_text()
