import json
from pathlib import Path

import pytest

from tools.workflow.catalog import preflight
from tools.workflow.contracts import Refused, digest, expected_plan
from tools.workflow.evaluation import GOAL, controller_case, evaluate, json_patch, model_case

REPO = Path(__file__).resolve().parents[3]


def test_real_parser_receipts_are_json_serializable(tmp_path):
    parsed, _ = preflight(
        REPO,
        "onset-date",
        [
            "--input",
            str(tmp_path / "source.zarr"),
            "--output",
            str(tmp_path / "out.zarr"),
            "--definition-ref",
            "agrhymet-sos-rolling",
        ],
    )
    assert isinstance(json.dumps(parsed), str)


def test_development_harness_exercises_real_controller_with_explicit_doubles(tmp_path):
    data = json.loads(
        (REPO / "evals/controller/fixtures/seeded-agentic-eval.DEV-ONLY.json").read_text()
    )
    case = next(c for c in data["development_cases"] if c["id"] == "DEV-F07-INJECT-01")
    result = controller_case(REPO, case, tmp_path)
    assert result["controller_probe"] == "PASS"
    assert result["status"] == "PARTIAL_COVERAGE"
    assert "plot_onset" not in result["tool_calls"]


def test_full_file_is_refused_before_opening(tmp_path):
    with pytest.raises(Refused, match="DEV-ONLY"):
        evaluate(REPO, tmp_path / "sealed.json", tmp_path / "run")


def test_patch_is_non_mutating_and_supports_list_insertion():
    source = [{"value": 1}]
    result = json_patch(source, [{"op": "add", "path": "/0", "value": {"value": 0}}])
    assert source == [{"value": 1}]
    assert result == [{"value": 0}, {"value": 1}]


def test_injected_proposal_belongs_to_the_review_binding():
    class Capture:
        receipts = [{"mode": "fixture"}]

        def ask(self, role, payload):
            self.payload = payload
            return {
                "verdict": "REJECT",
                "binding_hash": payload["binding_hash"],
                "findings": ["Incomplete confirmation window"],
            }

    data = json.loads(
        (REPO / "evals/controller/fixtures/seeded-agentic-eval.DEV-ONLY.json").read_text()
    )
    case = next(c for c in data["development_cases"] if c["id"] == "DEV-F10-INJECT-01")
    role = Capture()
    result = model_case(case, role)
    assert result["model_probe"] == "PASS"
    assert role.payload["binding_hash"] != digest(expected_plan(GOAL))
    assert role.payload["planner_proposal"] == case["stimulus"]["payload"]
