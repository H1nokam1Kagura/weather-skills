"""Observed review projection, deliberately not an admitted full-case adapter."""

import json

from simkit.adapter import Adapter

from tools.workflow import review_evidence
from tools.workflow.contracts import digest, request_goal
from tools.workflow.persistence import read_checkpoint
from tools.workflow.runner import Controller


class EvidenceMappingError(ValueError):
    pass


class WeatherAdapter(Adapter):
    name = "weather-observed-review-diagnostic"

    # Extend introspection for the frozen capability absent from base Simkit's
    # method table. A manifest string is never an implementation. No placeholder
    # method is provided: this capability remains unavailable in this adapter.
    extension_methods = {"CAP-BOUND-SOURCES": "capture_bound_sources"}

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
        names = sorted(name for name in checkpoint["evidence"]
                       if name.startswith("review-capture-") and name.endswith(".json"))
        reviewer_receipts = [receipt for receipt in checkpoint["role_receipts"]
                             if receipt["role"] == "reviewer"]
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
                raise EvidenceMappingError("required observed capture field missing/malformed") from exc
            snapshots.append(snapshot)
            provenance.append({
                "path": name,
                "native_file_tree_hash": checkpoint["evidence"][name],
                "run_id": record["run_id"],
                "reviewer_occurrence": occurrence,
                "input_hash": record["input_hash"],
                "stdin_hash": record["stdin_hash"],
                "accepted_review": record["accepted_review"],
                "native_receipt": receipt,
            })
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
        if (record["schema"] != "workflow.review-capture/1"
                or record["role"] != "reviewer" or record["run_id"] != run_id
                or type(record["reviewer_occurrence"]) is not int
                or record["reviewer_occurrence"] != occurrence
                or record["dispatch_status"] != "RETURNED"
                or record["receipt"] != receipt
                or type(record["accepted_review"]) is not bool):
            raise EvidenceMappingError("review dispatch identity or receipt mismatch")
        payload, envelope, output = record["payload"], record["review_evidence"], record["output"]
        stdin = record["stdin"]
        if (json.dumps(payload) != stdin or json.loads(stdin) != payload
                or review_evidence.text_hash(stdin) != record["stdin_hash"]
                or receipt["dispatch"]["stdin_hash"] != record["stdin_hash"]
                or digest(payload) != record["input_hash"]
                or receipt["input_hash"] != record["input_hash"]
                or receipt["output_hash"] != digest(output)
                or not receipt["invocation_id"] or not receipt["session_id"]):
            raise EvidenceMappingError("review transport/receipt hash mismatch")
        dispatch = receipt["dispatch"]
        if review_evidence.text_hash(dispatch["system_prompt"]) != dispatch["system_prompt_hash"]:
            raise EvidenceMappingError("review system prompt hash mismatch")
        if (payload["review_evidence"] != envelope
                or envelope["schema"] != review_evidence.SCHEMA
                or envelope["sources"] != sources
                or review_evidence.sources_from_docs(payload["documentation"]) != sources):
            raise EvidenceMappingError("review source envelope mismatch")
        review_evidence.validate_claims(envelope["plan_claims"], envelope["sources"])
        goal = request_goal(payload["request"])
        hashes = {
            "request_hash": digest(payload["request"]), "goal_hash": digest(goal),
            "plan_hash": digest(payload["plan"]), "manifest_hash": checkpoint["build_hash"],
            "source_hash": checkpoint["source_hash"],
        }
        if (hashes["request_hash"] != checkpoint["request_hash"]
                or any(envelope[key] != value for key, value in hashes.items())):
            raise EvidenceMappingError("reviewed binding components mismatch")
        binding = digest({
            "request": hashes["request_hash"], "goal": goal, "plan": payload["plan"],
            "build": hashes["manifest_hash"], "source": hashes["source_hash"],
            "admission": checkpoint["admission_hash"],
            "review_evidence": {"schema": envelope["schema"],
                                "plan_claims": envelope["plan_claims"], "sources": sources},
        })
        if any(value != binding for value in (
                payload["binding_hash"], envelope["binding_hash"], output["binding_hash"])):
            raise EvidenceMappingError("reviewed binding mismatch")
        if record["accepted_review"]:
            committed = record["committed_transition"]
            index = committed["index"]
            if type(index) is not int or not 0 <= index < len(checkpoint["events"]):
                raise EvidenceMappingError("committed review index invalid")
            event = checkpoint["events"][index]
            if (committed["event"] != event or event["to"] != "REVIEW_APPROVED"
                    or committed["binding_hash"] != binding or output["verdict"] != "APPROVE"
                    or event["evidence"] != output or event["evidence_hash"] != digest(output)):
                raise EvidenceMappingError("accepted review lacks exact durable transition")
        # Only contract fields observed in the actual input. No support labels,
        # approved_binding, terminal result, seq, phase or empty streams invented.
        return json.loads(json.dumps({
            "reviewer_occurrence": occurrence, **hashes,
            "plan_claims": envelope["plan_claims"], "sources": envelope["sources"],
        }))
