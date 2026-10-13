"""Observed review projection, deliberately not an admitted full-case adapter."""

import json
import re
from pathlib import Path

from simkit.adapter import Adapter

from adapter_support.fixture_staging import stage_fixture
from tools.workflow import review_evidence
from tools.workflow.catalog import safe_path, tree_hash
from tools.workflow.contracts import digest, request_goal
from tools.workflow.e2e_injections import InjectionHooks
from tools.workflow.persistence import read_checkpoint
from tools.workflow.runner import Controller

SOURCE_HASH_ALGORITHM = "tools.workflow.catalog.tree_hash"
REPORT_HASH_ALGORITHM = "tools.workflow.review_evidence.text_hash"
SUPPORTED_REPAIR_LIMITS = (0, 1)


class EvidenceMappingError(ValueError):
    pass


class AdapterRefusal(ValueError):
    """A case input this adapter cannot faithfully apply; never guessed or filled."""


class WeatherAdapter(Adapter):
    name = "weather-observed-review-diagnostic"

    # Extend introspection for the frozen capability absent from base Simkit's
    # method table. A manifest string is never an implementation. No placeholder
    # method is provided: this capability remains unavailable in this adapter.
    extension_methods = {
        "CAP-BOUND-SOURCES": "capture_bound_sources",
        "CAP-SCHEDULE-DEPENDENCIES": "dispatch_dependent_schedule",
        "CAP-REVIEW-APPROVAL-HOOK": "dispatch_after_accepted_review",
    }

    def implemented_capabilities(self):
        implemented = super().implemented_capabilities()
        for capability, method_name in self.extension_methods.items():
            if callable(getattr(type(self), method_name, None)):
                implemented.add(capability)
        return implemented

    def diagnostic_capability_status(self):
        return {
            "CAP-BOUND-SOURCES": {
                "status": "NOT_IMPLEMENTED",
                "reasons": [
                    "failed dispatch lacks complete invocation/receipt correlation",
                ],
            },
            "CAP-RECEIPTS": {
                "status": "NOT_IMPLEMENTED",
                "reasons": ["native receipts lack controller-global seq and resume phase"],
            },
            "CAP-HARNESS-OBS": {
                "status": "NOT_IMPLEMENTED",
                "reasons": ["review projection is not exhaustive harness observation"],
            },
        }

    def controller_goal_hash(self, goal):
        """CAP-CONTROLLER-HASH: the controller's own authorization goal digest.

        The caller passes the reference goal it authorizes; this never reads a
        controller's compiled goal (that is HARNESS_GOAL_HASH:current). NaN and
        other non-JSON values are refused by the controller routine itself.
        """
        if not isinstance(goal, dict):
            raise AdapterRefusal("an explicit goal object is required")
        return digest(goal)

    def configure_repair(self, case):
        """CAP-REPAIR-CONFIG: the Controller(max_repairs=...) value for one case.

        Only the frozen budget field is read. Anything outside the supported
        0/1 limits, including bool, is refused rather than coerced.
        """
        try:
            value = case["budget"]["max_repair_attempts"]
        except (KeyError, TypeError) as exc:
            raise AdapterRefusal("case budget.max_repair_attempts is required") from exc
        if type(value) is not int or value not in SUPPORTED_REPAIR_LIMITS:
            raise AdapterRefusal("max_repair_attempts must be the integer 0 or 1")
        return value

    def prepare_fixture(
        self, case, case_root, *, source, generator_provenance, presented_fetch_version
    ):
        """CAP-DISPOSABLE-FIXTURE: stage one pre-generated synthetic source per case.

        The generator is not imported here: the caller supplies its output and a
        pinned identity (generator, generator_sha256), recorded as caller-stated.
        Layout under a new case_root: source/source.zarr (staged; its parent holds
        nothing else, since verify-run searches source.parent), harness/ (manifest)
        and run/ (fresh, empty). source_pre uses the controller's own tree hash.
        Run provenance and synthetic invisibility are NOT declared here.
        """
        if not isinstance(case, dict) or not isinstance(case.get("id"), str) or not case["id"]:
            raise AdapterRefusal("a case with an explicit id is required")
        provenance = dict(generator_provenance)
        if not re.fullmatch(r"[0-9a-f]{64}", str(provenance.get("generator_sha256", ""))):
            raise AdapterRefusal("a pinned generator_sha256 is required")
        raw = Path(case_root)
        if not raw.is_absolute() or ".." in raw.parts:
            raise AdapterRefusal("case_root must be absolute with no '..' segments")
        root = safe_path(raw)
        if root.exists():
            raise FileExistsError("case_root must be new; reuse is refused")
        for ancestor in root.parents:
            if (ancestor / "harness" / "manifest.json").exists():
                raise AdapterRefusal("case_root lies inside another prepared case root")
        root.mkdir(exist_ok=False)
        source_directory, harness, run = root / "source", root / "harness", root / "run"
        source_directory.mkdir(exist_ok=False)
        staged = source_directory / "source.zarr"
        manifest = stage_fixture(
            source,
            staged,
            harness,
            generator_provenance=provenance,
            presented_fetch_version=presented_fetch_version,
        )
        run.mkdir(exist_ok=False)
        return {
            "schema": "weather-adapter.disposable-fixture/1",
            "case_id": case["id"],
            "case_root": str(root),
            "source": str(staged),
            "source_directory": str(source_directory),
            "harness": str(harness),
            "run": str(run),
            "source_pre": {"algorithm": SOURCE_HASH_ALGORITHM, "value": tree_hash(staged)},
            "generator_identity": {
                "generator": manifest["generator_provenance"]["generator"],
                "generator_sha256": manifest["generator_provenance"]["generator_sha256"],
                "stated_by": "caller",
            },
            "manifest": manifest,
            "not_declared": ["CAP-RUN-PROVENANCE"],
        }

    def patch_gate_report(self, hooks, event, controller):
        """CAP-GATE-REPORT-PATCH: deliver after_tool json_patch via InjectionHooks.

        event["process"] is the dict the controller's read_gate later parses, so
        the patched stdout is the delivered report. The raw report stays in the
        process manager's own record; both hashes are returned so evidence can
        name which is which. A schedule whose after_tool entries are anything
        but JSON Patch arrays is refused: delegating would apply them too.
        """
        if not isinstance(hooks, InjectionHooks):
            raise AdapterRefusal("the existing InjectionHooks delivery path is required")
        for spec in hooks.schedule:
            if spec["hook"] == "after_tool" and (
                spec["operation"] != "json_patch" or not isinstance(spec.get("payload"), list)
            ):
                raise AdapterRefusal("after_tool entry is not a gate-report JSON Patch")
        process = event.get("process") if isinstance(event, dict) else None
        if not isinstance(process, dict) or not isinstance(process.get("stdout"), str):
            raise AdapterRefusal("an observed after_tool process with stdout is required")
        raw = review_evidence.text_hash(process["stdout"])
        first = len(hooks.records)
        hooks("after_tool", event, controller)
        return {
            "hash_algorithm": REPORT_HASH_ALGORITHM,
            "raw_report_hash": raw,
            "delivered_report_hash": review_evidence.text_hash(process["stdout"]),
            "raw_report_record": "process manager process record (unpatched stdout)",
            "delivered_report_record": "after_tool process consumed by read_gate",
            "records": json.loads(json.dumps(hooks.records[first:])),
        }

    def dispatch_dependent_schedule(self, hooks):
        """CAP-SCHEDULE-DEPENDENCIES: per-entry dispositions from InjectionHooks.

        Delivery happens at the controller's own hook points; this only returns
        the observed DELIVERED / INACTIVE / NOT_IMPLEMENTED / HARNESS_ERROR record.
        """
        if not isinstance(hooks, InjectionHooks):
            raise AdapterRefusal("the existing InjectionHooks delivery path is required")
        return hooks.dependency_report()

    def dispatch_after_accepted_review(self, hooks):
        """CAP-REVIEW-APPROVAL-HOOK: dispositions of after_review_approval entries.

        The controller emits the hook only after a durably committed accepted
        approval; rejected or uncommitted reviews consume no occurrence.
        """
        report = self.dispatch_dependent_schedule(hooks)
        return {
            "capability": "CAP-REVIEW-APPROVAL-HOOK",
            "accepted_approvals": report["accepted_approvals"],
            "entries": [e for e in report["entries"] if e["hook"] == "after_review_approval"],
        }

    def resolve_review_diagnostic_symbol(self, controller, symbol, reviewer_occurrence):
        """Resolve one observed symbol; this is not the broad CAP-SYMBOLS API."""
        expected = "HARNESS_SOURCE_HASH:" + review_evidence.DEFINITION_SOURCE_ID
        if symbol != expected or type(reviewer_occurrence) is not int:
            raise EvidenceMappingError("unsupported diagnostic symbol or occurrence")
        diagnostic = self.capture_review_diagnostics(controller)
        for snapshot in diagnostic["evidence"]["harness_observations"]["review_snapshots"]:
            if snapshot["reviewer_occurrence"] == reviewer_occurrence:
                for source in snapshot["sources"]:
                    if source["source_id"] == review_evidence.DEFINITION_SOURCE_ID:
                        return source["source_hash"]
        raise EvidenceMappingError("definition source was not observed in that review")

    def capture_review_diagnostics(self, controller):
        """Project only observed fields; never invoke a case, model, or tool.

        Complete returned-review records are required for this diagnostic path.
        A failed attempt is not silently dropped and does not become an invocation
        receipt. No Simkit invocation_receipts stream is synthesized from the
        narrower native receipt schema.
        """
        if not isinstance(controller, Controller):
            raise EvidenceMappingError("a real controller observation is required")
        controller.integrity()
        checkpoint = read_checkpoint(controller.root)
        if checkpoint["context"] != controller.context:
            raise EvidenceMappingError("controller context differs from durable checkpoint")
        context = checkpoint["context"].get("review_evidence")
        if not isinstance(context, dict):
            raise EvidenceMappingError("review evidence mode was not observed")
        original_sources = review_evidence.sources_from_docs(controller.docs())
        if context.get("sources") != original_sources:
            raise EvidenceMappingError("bound sources differ from original reviewed bytes")
        names = sorted(
            name
            for name in checkpoint["evidence"]
            if name.startswith("review-capture-") and name.endswith(".json")
        )
        reviewer_receipts = [
            receipt for receipt in checkpoint["role_receipts"] if receipt["role"] == "reviewer"
        ]
        if not names or len(names) != len(reviewer_receipts):
            raise EvidenceMappingError("complete dispatch/receipt correlation unavailable")
        if context.get("reviewer_occurrence") != len(names):
            raise EvidenceMappingError("review occurrence coverage mismatch")
        snapshots, provenance = [], []
        for occurrence, (name, receipt) in enumerate(zip(names, reviewer_receipts, strict=True), 1):
            if "/" in name or "\\" in name:
                raise EvidenceMappingError("capture path must be a run-local file")
            record = json.loads((controller.root / name).read_text(encoding="utf-8"))
            try:
                snapshot = self._project_record(
                    record, receipt, occurrence, checkpoint, original_sources, context["run_id"]
                )
            except (KeyError, TypeError, IndexError) as exc:
                raise EvidenceMappingError(
                    "required observed capture field missing/malformed"
                ) from exc
            snapshots.append(snapshot)
            provenance.append(
                {
                    "path": name,
                    "native_file_tree_hash": checkpoint["evidence"][name],
                    "run_id": record["run_id"],
                    "reviewer_occurrence": occurrence,
                    "input_hash": record["input_hash"],
                    "stdin_hash": record["stdin_hash"],
                    "accepted_review": record["accepted_review"],
                    "native_receipt": receipt,
                }
            )
        events = checkpoint["events"]
        states = ([events[0]["from"]] + [event["to"] for event in events]) if events else []
        return {
            "kind": "DIRECT_DIAGNOSTIC_NOT_ADMITTED_SIMKIT_CASE",
            "evidence": {
                "harness_observations": {"review_snapshots": snapshots},
                "controller_trace": {"states": states, "final_state": checkpoint["state"]},
            },
            "provenance": provenance,
            "capabilities": self.diagnostic_capability_status(),
            "unsupported_semantics": list(review_evidence.UNSUPPORTED),
        }

    @staticmethod
    def _project_record(record, receipt, occurrence, checkpoint, sources, run_id):
        if (
            record["schema"] != "workflow.review-capture/1"
            or record["role"] != "reviewer"
            or record["run_id"] != run_id
            or type(record["reviewer_occurrence"]) is not int
            or record["reviewer_occurrence"] != occurrence
            or record["dispatch_status"] != "RETURNED"
            or record["receipt"] != receipt
            or type(record["accepted_review"]) is not bool
        ):
            raise EvidenceMappingError("review dispatch identity or receipt mismatch")
        payload, envelope, output = record["payload"], record["review_evidence"], record["output"]
        stdin = record["stdin"]
        if (
            json.dumps(payload) != stdin
            or json.loads(stdin) != payload
            or review_evidence.text_hash(stdin) != record["stdin_hash"]
            or receipt["dispatch"]["stdin_hash"] != record["stdin_hash"]
            or digest(payload) != record["input_hash"]
            or receipt["input_hash"] != record["input_hash"]
            or receipt["output_hash"] != digest(output)
            or not receipt["invocation_id"]
            or not receipt["session_id"]
        ):
            raise EvidenceMappingError("review transport/receipt hash mismatch")
        dispatch = receipt["dispatch"]
        if review_evidence.text_hash(dispatch["system_prompt"]) != dispatch["system_prompt_hash"]:
            raise EvidenceMappingError("review system prompt hash mismatch")
        if (
            payload["review_evidence"] != envelope
            or envelope["schema"] != review_evidence.SCHEMA
            or envelope["sources"] != sources
            or review_evidence.sources_from_docs(payload["documentation"]) != sources
        ):
            raise EvidenceMappingError("review source envelope mismatch")
        review_evidence.validate_claims(envelope["plan_claims"], envelope["sources"])
        goal = request_goal(payload["request"])
        hashes = {
            "request_hash": digest(payload["request"]),
            "goal_hash": digest(goal),
            "plan_hash": digest(payload["plan"]),
            "manifest_hash": checkpoint["build_hash"],
            "source_hash": checkpoint["source_hash"],
        }
        if hashes["request_hash"] != checkpoint["request_hash"] or any(
            envelope[key] != value for key, value in hashes.items()
        ):
            raise EvidenceMappingError("reviewed binding components mismatch")
        binding = digest(
            {
                "request": hashes["request_hash"],
                "goal": goal,
                "plan": payload["plan"],
                "build": hashes["manifest_hash"],
                "source": hashes["source_hash"],
                "admission": checkpoint["admission_hash"],
                "review_evidence": {
                    "schema": envelope["schema"],
                    "plan_claims": envelope["plan_claims"],
                    "sources": sources,
                },
            }
        )
        if any(
            value != binding
            for value in (payload["binding_hash"], envelope["binding_hash"], output["binding_hash"])
        ):
            raise EvidenceMappingError("reviewed binding mismatch")
        if record["accepted_review"]:
            committed = record["committed_transition"]
            index = committed["index"]
            if type(index) is not int or not 0 <= index < len(checkpoint["events"]):
                raise EvidenceMappingError("committed review index invalid")
            event = checkpoint["events"][index]
            if (
                committed["event"] != event
                or event["to"] != "REVIEW_APPROVED"
                or committed["binding_hash"] != binding
                or output["verdict"] != "APPROVE"
                or event["evidence"] != output
                or event["evidence_hash"] != digest(output)
            ):
                raise EvidenceMappingError("accepted review lacks exact durable transition")
        # Only contract fields observed in the actual input. No support labels,
        # approved_binding, terminal result, seq, phase or empty streams invented.
        return json.loads(
            json.dumps(
                {
                    "reviewer_occurrence": occurrence,
                    **hashes,
                    "plan_claims": envelope["plan_claims"],
                    "sources": envelope["sources"],
                }
            )
        )
