"""Separate SIMKIT adapter for verification profiles; no inherited onset approvals.

Implements the frozen runner's duck-typed adapter lifecycle. Its observed evidence
schema is separate from cached-onset v2.7 symbols and cannot enter their score.
"""

import copy
import json
import subprocess
from pathlib import Path

from tools.check_capability_signatures import check, rereview_flagged
from tools.verification_profiles import PROFILES
from tools.verification_profiles.contracts import require
from tools.verification_profiles.lifecycle import reopen
from tools.workflow.catalog import safe_path, tree_hash
from tools.workflow.contracts import digest
from tools.workflow.persistence import run_lock

CAPABILITY = "CAP-VERIFICATION-PROFILES-V1"
DIAGNOSTIC = "CAP-VERIFICATION-DIAGNOSTIC-V1"
RECORD = "adapter_support/VERIFICATION-PRODUCERS.json"
CASESET = "evals/controller/verification-v1/cases.json"


def cases(repo, diagnostic=False):
    value = json.loads((Path(repo) / CASESET).read_text(encoding="utf-8"))
    require(value["schema"] == "verification.cases/1", "unknown verification caseset")
    result = copy.deepcopy(value["cases"])
    for case in result:
        require(case["profile"] in PROFILES, "unregistered case profile")
        if diagnostic:
            case["id"] = "DIAG-" + case["id"]
            case["adapter_requirements"] = [DIAGNOSTIC]
    return result


def project(repo, root):
    """Only checked checkpoint, role files and score/replay artifacts become evidence."""
    root = safe_path(root)
    with run_lock(root):
        controller = reopen(repo, root)
        state = controller.state
        require(state["schema"] == "verification.checkpoint/1", "unknown checkpoint schema")
        roles = []
        require(
            len(state.get("role_artifacts", [])) == len(state["role_receipts"]),
            "role evidence is incomplete",
        )
        for reference, receipt in zip(
            state.get("role_artifacts", []), state["role_receipts"], strict=True
        ):
            evidence = json.loads((root / reference["name"]).read_text(encoding="utf-8"))
            require(
                evidence["receipt"] == receipt and evidence["role"] == receipt["role"],
                "role evidence differs from checkpoint",
            )
            roles.append({**evidence, "evidence_hash": reference["hash"]})
        artifacts = []
        for entry in state["ledger"]:
            if entry["status"] != "VERIFIED":
                continue
            report = json.loads(
                (Path(entry["artifact"]) / "scores.json").read_text(encoding="utf-8")
            )
            require(report["metrics"] == entry["scores"], "score evidence differs from checkpoint")
            artifacts.append(
                {
                    "product": entry["product"],
                    "window_days": entry["window_days"],
                    "artifact": entry["artifact"],
                    "artifact_hash": entry["artifact_hash"],
                    "replay_hash": entry["replay_hash"],
                    "report": report,
                }
            )
        terminal = state["status"] in ("SYNTHETIC_COMPLETE", "SYNTHETIC_PARTIAL")
        if terminal:
            expected = digest({"binding": controller.binding, "ledger": state["ledger"]})
            require(
                bool(roles)
                and roles[-1]["role"] == "verifier"
                and roles[-1]["output"] == {"verdict": "PASS", "binding_hash": expected},
                "terminal verifier evidence missing or stale",
            )
        return {
            "schema": "verification.evidence/1",
            "status": state["status"],
            "profile": state["profile"],
            "binding_hash": controller.binding,
            "checkpoint_hash": tree_hash(root / "checkpoint.json"),
            "request": controller.request,
            "sources": controller.grant.record,
            "controller_trace": state["events"],
            "invocation_receipts": roles,
            "product_ledger": state["ledger"],
            "probes": state["probes"],
            "artifacts": artifacts,
            "elapsed_seconds": state["elapsed_seconds"],
            "role_mode": "substituted",
            "production_release": False,
            "classification": "UNRATIFIED_SYNTHETIC_PROCESS_EVIDENCE",
            "not_observed": [
                "live_agent_independence",
                "remote_acquisition",
                "os_process_containment",
                "exhaustive_filesystem_capture",
            ],
            "frozen_onset_score_eligible": False,
        }


class VerificationAdapter:
    name = "verification-v1-observed"

    def __init__(self, repo, root=None, diagnostic=False):
        self.repo = safe_path(repo)
        self.root = safe_path(root) if root is not None else None
        self.diagnostic = diagnostic is True

    def admission(self):
        records = json.loads((self.repo / RECORD).read_text(encoding="utf-8"))
        record = records["capabilities"][CAPABILITY]
        if (
            records.get("schema") != "verification.producers/1"
            or record.get("d4_gated") is not True
        ):
            return {
                "status": "NOT_IMPLEMENTED",
                "reason": "malformed capability schema or missing mandatory D4 gate",
            }
        if (
            record.get("status") != "PRODUCER_MAPPED"
            or rereview_flagged(record)
            or not all(
                isinstance(record.get(k), str) and record[k].strip()
                for k in ("reviewer", "reviewed_at", "reviewed_commit")
            )
        ):
            return {
                "status": "NOT_IMPLEMENTED",
                "reason": "independent producer review/signature pending",
            }
        failures, flagged = check(records, repo=self.repo)
        if failures or flagged:
            return {
                "status": "NOT_IMPLEMENTED",
                "reason": "producer review is stale",
                "failures": failures,
            }
        paths = [p["file"] for p in record["producers"]]
        diff = subprocess.run(
            ["git", "-C", str(self.repo), "diff", "--exit-code", "HEAD", "--", *paths],
            capture_output=True,
            check=False,
        )
        if diff.returncode:
            return {"status": "NOT_IMPLEMENTED", "reason": "reviewed producers have local changes"}
        return {"status": "ADMITTED", "reviewed_commit": record["reviewed_commit"]}

    def implemented_capabilities(self):
        if self.diagnostic:
            return {DIAGNOSTIC}
        return {CAPABILITY} if self.admission()["status"] == "ADMITTED" else set()

    def require_admission(self):
        require(
            self.diagnostic or self.admission()["status"] == "ADMITTED",
            "verification profile is NOT_IMPLEMENTED until independent capability review/signing; diagnostic execution must be explicit",
        )

    def can_run(self):
        return self.root is not None

    def profile_status(self):
        return {
            "profiles": [
                {
                    "id": profile,
                    "admission": self.admission(),
                    "execution_scope": "cached synthetic / substituted roles",
                }
                for profile in PROFILES
            ],
            "production_release": False,
            "case_schema": "verification.cases/1",
        }

    def capture_evidence(self, root):
        return project(self.repo, root)

    def run_case(self, case, profile):
        from tools.verification_profiles.evaluation import execute_case

        self.require_admission()
        require(self.can_run(), "case execution root is required")
        expected = {c["id"]: c for c in cases(self.repo, self.diagnostic)}
        require(case == expected.get(case.get("id")), "case definition changed or unknown")
        require(
            profile == {"id": "verification-suite/v1", "profiles": list(PROFILES)},
            "wrong suite profile",
        )
        require(
            set(case["adapter_requirements"]) <= self.implemented_capabilities(),
            "case capability not admitted",
        )
        return execute_case(self.repo, self.root / case["id"], case)
