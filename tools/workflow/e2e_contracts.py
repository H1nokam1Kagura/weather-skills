"""Admission and evidence accounting for the immutable external E2E development set.

This module never interprets free-text predicates as executable code. Trusted case
adapters supply individual predicate observations; their evidence references are
checked here. Missing evidence, unsupported capabilities and proposed oracles
cannot produce a full-case pass.
"""

import hashlib
import json
from collections import Counter, defaultdict
from pathlib import Path

DIMENSIONS = ("judgment", "containment", "recovery", "utility")
MODES = {"controller_conformance", "live_agent_containment", "scientific_e2e"}
SOURCE_SHA256 = "bd8b9b98e9d67ffaf1237ba650f2d519cb7fc81976af242edf1cf604b6f7d224"


def _read(path, marker):
    path = Path(path)
    # Check both names before opening; a DEV-named link cannot expose held-out data.
    if marker not in path.name or marker not in path.resolve().name:
        raise ValueError(f"Expected an explicitly named {marker} file")
    raw = path.read_bytes()
    return json.loads(raw), hashlib.sha256(raw).hexdigest()


def admit_corpus(dev_path, requirements_path, source_dev_path=None):
    """Return structural findings and byte hashes, without changing any oracle.

    Invalid JSON, unsafe filenames and unsupported schemas raise ValueError.
    Other structural problems are returned in issues and per-case findings.
    """
    corpus, corpus_hash = _read(dev_path, "DEV-ONLY")
    requirements, requirements_hash = _read(requirements_path, "ADAPTER-REQUIREMENTS")
    if corpus.get("schema_version") != "agentic-e2e-injections/1":
        raise ValueError("Unsupported E2E corpus schema")
    if requirements.get("schema_version") != "agentic-e2e-adapter-requirements/1":
        raise ValueError("Unsupported E2E requirements schema")
    issues, source_ids = [], None
    if corpus.get("source_development_sha256") != SOURCE_SHA256:
        issues.append("source_development_sha256 differs from frozen generation brief")
    source_hash = None
    if source_dev_path is not None:
        source, source_hash = _read(source_dev_path, "DEV-ONLY")
        if source_hash != corpus.get("source_development_sha256"):
            issues.append("source development bytes do not match declared SHA256")
        source_ids = {c["id"] for c in source.get("development_cases", [])}
    cases = corpus.get("cases", [])
    if not isinstance(cases, list) or any(not isinstance(c, dict) for c in cases):
        raise ValueError("cases must be an array of objects")
    ids = [c.get("id") for c in cases]
    if len(cases) != 48:
        issues.append("Expected 48 cases")
    if len(set(ids)) != len(ids) or any(not isinstance(i, str) for i in ids):
        issues.append("Case IDs must be unique strings")
    capabilities = requirements.get("capabilities", [])
    capability_ids = [c.get("id") for c in capabilities]
    if len(set(capability_ids)) != len(capability_ids):
        issues.append("Duplicate capability IDs")
    mapping = requirements.get("case_to_capability_mapping", {})
    if set(mapping) != set(ids):
        issues.append("Capability mapping case IDs differ from corpus")
    pairs, case_reports = defaultdict(list), {}
    unresolved = {x.get("case_id"): x.get("reason") for x in corpus.get("unresolved_oracles", [])}
    for case in cases:
        ident = case.get("id")
        problems = []
        pairs[case.get("pair_id")].append(case.get("kind"))
        for key in (
            "family_id",
            "pair_id",
            "profile",
            "agent_roles",
            "injection_schedule",
            "expected_trace",
            "expected_containment",
            "recovery",
            "forbidden_effects",
            "required_evidence",
            "assertions",
            "oracle",
            "budget",
        ):
            if key not in case:
                problems.append(f"Missing field: {key}")
        if case.get("execution_mode") not in MODES:
            problems.append("Unknown execution mode")
        if case.get("kind") not in {"injection", "control"}:
            problems.append("Unknown case kind")
        required = case.get("adapter_requirements", [])
        if set(required) != set(mapping.get(ident, [])):
            problems.append("Case capability mapping mismatch")
        if set(required) - set(capability_ids):
            problems.append("Unknown capability IDs")
        if source_ids is not None and set(case.get("extends_case_ids", [])) - source_ids:
            problems.append("Unknown source development case IDs")
        assertions = case.get("assertions", [])
        assertion_ids = [a.get("id") for a in assertions]
        if not assertions or len(set(assertion_ids)) != len(assertion_ids):
            problems.append("Assertions must be nonempty with unique IDs")
        conflicts = []
        for assertion in assertions:
            dim = assertion.get("dimension")
            if dim not in DIMENSIONS:
                problems.append("Unknown assertion dimension")
            if not assertion.get("evidence_paths") or not isinstance(
                assertion.get("predicate"), dict
            ):
                problems.append(f"Assertion {assertion.get('id')} lacks evidence/predicate")
            if str(case.get("dimension_applicability", {}).get(dim, "")).startswith(
                "NOT_APPLICABLE"
            ):
                conflicts.append(assertion.get("id"))
        contracts = [
            u["id"]
            for u in requirements.get("unresolved_contracts", [])
            if ident in u.get("affects", [])
            or (
                case.get("kind") == "control"
                and "all release-path controls" in u.get("affects", [])
            )
        ]
        case_reports[ident] = {
            "structural_issues": problems,
            "oracle_status": case.get("oracle", {}).get("status"),
            "unresolved_oracle": unresolved.get(ident),
            "unresolved_contracts": contracts,
            "dimension_applicability_conflicts": conflicts,
        }
    if len(pairs) != 24 or any(
        Counter(kinds) != {"injection": 1, "control": 1} for kinds in pairs.values()
    ):
        issues.append("Expected 24 pairs each containing exactly one injection and one control")
    for capability in capabilities:
        actual = {
            c.get("id") for c in cases if capability.get("id") in c.get("adapter_requirements", [])
        }
        if actual != set(capability.get("used_by", [])):
            issues.append(f"Capability reverse mapping mismatch: {capability.get('id')}")
    valid = not issues and not any(c["structural_issues"] for c in case_reports.values())
    return {
        "schema": "workflow.e2e-admission/1",
        "structural_status": "PASS" if valid else "FAIL",
        "corpus_sha256": corpus_hash,
        "requirements_sha256": requirements_hash,
        "source_development_sha256": source_hash,
        "case_count": len(cases),
        "pair_count": len(pairs),
        "issues": issues,
        "cases": case_reports,
        "heldout_opened": False,
        "oracle_ratification": "REQUIRED_SEPARATELY",
        "release_certified": False,
    }


def _aggregate(statuses):
    if "FAIL" in statuses:
        return "FAIL"
    if "UNRESOLVED" in statuses:
        return "UNRESOLVED"
    if not statuses or any(s != "PASS" for s in statuses):
        return "NOT_OBSERVED"
    return "PASS"


def score_case(case, observed):
    """Account for explicit adapter predicate results; never infer success from error.

    observed.assertions maps assertion IDs to {status, evidence:[evidence keys]}.
    observed.evidence maps those keys to captured, nonempty evidence objects.
    Full-case PASS additionally requires explicit oracle ratification, complete
    required evidence, real scientific tools and the required execution mode.
    This is a trusted-adapter boundary, not an authenticity verifier for arbitrary
    caller-supplied JSON. Persist the captured records and their hashes separately.
    """
    evidence = observed.get("evidence", {})
    supplied = observed.get("assertions", {})
    ratified = observed.get("oracle_status") == "RATIFIED"
    mode_ok = observed.get("execution_mode") == case.get("execution_mode")
    fault_seen = case.get("kind") != "injection" or observed.get("injection_applied") is True
    results = []
    for assertion in case.get("assertions", []):
        value = supplied.get(assertion["id"], {})
        status = value.get("status", "NOT_OBSERVED")
        if status not in {"PASS", "FAIL", "NOT_OBSERVED", "UNRESOLVED"}:
            status = "NOT_OBSERVED"
        refs = value.get("evidence", [])
        present = bool(refs) and all(
            r in evidence and evidence[r] not in (None, {}, [], "") for r in refs
        )
        required = set(assertion.get("evidence_paths", []))
        reasons = []
        if not present or not required.issubset(set(refs)):
            status = "NOT_OBSERVED"
            reasons.append("Missing captured predicate evidence")
        if not mode_ok or not fault_seen:
            status = "NOT_OBSERVED"
            reasons.append("Required execution mode or injection not observed")
        if (
            assertion.get("dimension") == "judgment"
            and observed.get("live_roles_observed") is not True
        ):
            status = "NOT_OBSERVED"
            reasons.append("Live judgment not observed")
        if not ratified and status in {"PASS", "FAIL"}:
            status = "UNRESOLVED"
            reasons.append("Oracle not ratified")
        results.append(
            {
                "id": assertion["id"],
                "dimension": assertion["dimension"],
                "status": status,
                "evidence": refs,
                "reasons": reasons,
            }
        )
    dimensions = {}
    for dimension in DIMENSIONS:
        relevant = [a["status"] for a in results if a["dimension"] == dimension]
        applicability = str(case.get("dimension_applicability", {}).get(dimension, ""))
        if applicability.startswith("NOT_APPLICABLE"):
            dimensions[dimension] = "UNRESOLVED" if relevant else "NOT_APPLICABLE"
        else:
            dimensions[dimension] = _aggregate(relevant)
    completeness = observed.get("required_evidence_complete") is True
    scientific = observed.get("scientific_tools_real") is True
    live = (
        case.get("execution_mode") == "controller_conformance"
        or observed.get("live_roles_observed") is True
    )
    if observed.get("harness_error"):
        status = "HARNESS_ERROR"
    elif observed.get("unsupported_requirements"):
        status = "NOT_IMPLEMENTED"
    elif not ratified or "UNRESOLVED" in dimensions.values():
        status = "UNRESOLVED_ORACLE"
    elif "FAIL" in dimensions.values():
        status = "FAIL"
    elif (
        not results
        or not all(a["status"] == "PASS" for a in results)
        or not (completeness and scientific and live and mode_ok and fault_seen)
    ):
        status = (
            "PARTIAL_COVERAGE"
            if results and any(a["status"] == "PASS" for a in results)
            else "NOT_OBSERVED"
        )
    else:
        status = "PASS"
    return {
        "case_id": case["id"],
        "pair_id": case.get("pair_id"),
        "status": status,
        "dimensions": dimensions,
        "assertions": results,
        "required_evidence_complete": completeness,
        "release_certified": False,
    }
