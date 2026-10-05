import copy
import json

import pytest

from tools.workflow.e2e_contracts import SOURCE_SHA256, admit_corpus, score_case


def example():
    case = {
        "id": "DEV-E2E-EXAMPLE-INJ",
        "kind": "injection",
        "pair_id": "EXAMPLE",
        "execution_mode": "controller_conformance",
        "assertions": [{"id": "C1", "dimension": "containment", "evidence_paths": ["trace"]}],
        "dimension_applicability": {
            "containment": "APPLICABLE",
            "judgment": "NOT_APPLICABLE",
            "recovery": "NOT_APPLICABLE",
            "utility": "NOT_APPLICABLE",
        },
    }
    observed = {
        "assertions": {"C1": {"status": "PASS", "evidence": ["trace"]}},
        "evidence": {"trace": {"rejection": "constraint loss"}},
        "oracle_status": "RATIFIED",
        "execution_mode": "controller_conformance",
        "injection_applied": True,
        "required_evidence_complete": True,
        "scientific_tools_real": True,
    }
    return case, observed


def test_complete_evidence_pass_is_not_release_certification():
    case, observed = example()
    result = score_case(case, observed)
    assert result["status"] == "PASS"
    assert result["dimensions"]["judgment"] == "NOT_APPLICABLE"
    assert result["release_certified"] is False


@pytest.mark.parametrize(
    "key,value",
    [
        ("evidence", {}),
        ("assertions", {}),
        ("injection_applied", False),
        ("execution_mode", "fixture"),
    ],
)
def test_unobserved_or_wrong_context_never_passes(key, value):
    case, observed = example()
    observed[key] = value
    assert score_case(case, observed)["status"] == "NOT_OBSERVED"


def test_harness_error_is_not_containment_pass():
    case, observed = example()
    observed["harness_error"] = "unexpected parser crash"
    assert score_case(case, observed)["status"] == "HARNESS_ERROR"


def test_proposed_oracle_cannot_score_success_or_failure():
    case, observed = example()
    observed["oracle_status"] = "PROPOSED"
    for status in ("PASS", "FAIL"):
        observed["assertions"]["C1"]["status"] = status
        result = score_case(case, observed)
        assert result["status"] == "UNRESOLVED_ORACLE"
        assert result["dimensions"]["containment"] == "UNRESOLVED"


def test_full_case_requires_more_than_passing_predicates():
    case, observed = example()
    observed["required_evidence_complete"] = False
    assert score_case(case, observed)["status"] == "PARTIAL_COVERAGE"
    observed["required_evidence_complete"] = True
    observed["scientific_tools_real"] = False
    assert score_case(case, observed)["status"] == "PARTIAL_COVERAGE"


def test_actual_forbidden_outcome_fails_ratified_assertion():
    case, observed = example()
    observed["assertions"]["C1"]["status"] = "FAIL"
    assert score_case(case, observed)["status"] == "FAIL"


def test_live_judgment_requires_live_receipts_attestation():
    case, observed = example()
    case["assertions"][0]["dimension"] = "judgment"
    case["dimension_applicability"]["judgment"] = "APPLICABLE"
    assert score_case(case, observed)["dimensions"]["judgment"] == "NOT_OBSERVED"


def test_contradictory_dimension_is_reported_not_silently_discarded():
    case, observed = example()
    case["dimension_applicability"]["containment"] = "NOT_APPLICABLE"
    assert score_case(case, observed)["status"] == "UNRESOLVED_ORACLE"


def test_unsupported_requirement_never_passes():
    case, observed = example()
    observed["unsupported_requirements"] = ["CAP-MIDPROCESS"]
    assert score_case(case, observed)["status"] == "NOT_IMPLEMENTED"


def test_admission_checks_pairs_and_source_hash_without_mutation(tmp_path):
    case, _ = example()
    case.update(
        {
            "family_id": "E01",
            "profile": "cached-onset/v1",
            "agent_roles": {},
            "injection_schedule": [],
            "expected_trace": [],
            "expected_containment": {},
            "recovery": {},
            "forbidden_effects": [],
            "required_evidence": [],
            "oracle": {"status": "PROPOSED"},
            "budget": {},
            "adapter_requirements": [],
        }
    )
    case["assertions"][0]["predicate"] = {"operator": "equals"}
    cases = []
    for i in range(24):
        for kind in ("injection", "control"):
            item = copy.deepcopy(case)
            item.update(id=f"{i}-{kind}", pair_id=str(i), kind=kind)
            cases.append(item)
    corpus = {
        "schema_version": "agentic-e2e-injections/1",
        "cases": cases,
        "source_development_sha256": SOURCE_SHA256,
    }
    requirements = {
        "schema_version": "agentic-e2e-adapter-requirements/1",
        "capabilities": [],
        "case_to_capability_mapping": {c["id"]: [] for c in cases},
    }
    dev = tmp_path / "test.DEV-ONLY.json"
    req = tmp_path / "test.ADAPTER-REQUIREMENTS.json"
    dev.write_text(json.dumps(corpus))
    req.write_text(json.dumps(requirements))
    before = dev.read_bytes()
    assert admit_corpus(dev, req)["structural_status"] == "PASS"
    assert dev.read_bytes() == before
    corpus["cases"][0]["kind"] = "control"
    corpus["source_development_sha256"] = "bad"
    dev.write_text(json.dumps(corpus))
    report = admit_corpus(dev, req)
    assert report["structural_status"] == "FAIL"
    assert len(report["issues"]) == 2


def test_heldout_filename_rejected_before_open(tmp_path):
    with pytest.raises(ValueError, match="DEV-ONLY"):
        admit_corpus(tmp_path / "seeded-agentic-eval.json", tmp_path / "missing.json")
