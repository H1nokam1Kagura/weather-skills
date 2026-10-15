"""Project a finished controller run directory onto the frozen v2.7 evidence shape.

Authority for every shape: frozen EVIDENCE_CONTRACT.json ``structures`` (cited per section
below as EC:<key>). The projection reads only what the controller and the harness recorded:
the durable checkpoint, the dispatch-event files on the controller-global sequence, the role
files, claim-check files, review captures, process start receipts and harness-side records.

Rules (WAVE3-BRIEF rule 1, owner ruling D4):

* Nothing is synthesized, defaulted or filled. A field whose native producer is absent is
  left out (key absent) and listed in ``missing`` with its owning capability.
* A list is emitted as ``[]`` only when its native producer exists and recorded nothing. A
  producer that does not exist yet yields MISSING, never ``[]``.
* An empty signal list is never exhaustive evidence (D4).
* Write and whole-tree signal evidence come only from the container harness (OWNER-RULINGS
  container_route_ruling_2026-10-13): its trace analyzer writes a harness-observations file
  (``simkit.container-harness-evidence/1``) carrying ``filesystem_snapshot.created_paths``,
  ``process_tree``, ``controller_trace.signalled_pids``, ``symbols.HARNESS_CONTROLLER_PID``,
  the whole-tree counts (``max_concurrent_children``, ``shell_launches``,
  ``data_fetch_attempts``) and the harness RAM sampler's ``min_free_ram_while_running_gib``.
  It is merged when supplied (``harness["observations_path"]``) and checked against the native
  records; without it those fields stay MISSING. Carrying the values does not declare
  CAP-EXHAUSTIVE-WRITES or CAP-SIGNAL-CAPTURE: that needs a reviewed D4 record.

Tampered or inconsistent native evidence refuses the whole projection (EvidenceMappingError);
it is never partially projected.
"""

import copy
import hashlib
import json
import math
import re
from pathlib import Path, PurePosixPath, PureWindowsPath

from tools.workflow import catalog, review_evidence
from tools.workflow.contracts import (
    FAILED_GATES,
    TERMINAL_OUTCOMES,
    digest,
    expected_plan,
    plan_version_hash,
    request_goal,
)
from tools.workflow.persistence import read_checkpoint
from tools.workflow.profiles import stages

SCHEMA = "weather-adapter.evidence-projection/1"
# EC:invocation_receipts + v2_4.role_sequence: the contract role vocabulary.
CONTRACT_ROLES = (
    "compiler_1",
    "compiler_2",
    "compiler_3",
    "planner",
    "evidence_checker",
    "reviewer",
    "verifier",
    "repairer",
    "shadow_policy",
)
GATE_TOOLS = ("check-artifact", "verify-run")
CATALOG_TOOLS = tuple(catalog.CATALOG)
# Records other wave-3 packages are building, keyed by their frozen controller_trace names
# (EC:controller_trace, v2_4.resume, v2_4.interruptions, v2_4.material_changes). Projected
# verbatim from context[<name>] (the recovery lists: context["recovery"][<name>], see
# RECOVERY_LEDGER_LISTS) when the native record exists; MISSING (key absent) otherwise,
# never []. operator_actions has its own checked projection (pre_review_projection);
# shadow_decisions has its own (shadow_projection.project, CAP-SHADOW).
PENDING_TRACE_LISTS = {
    "shadow_decisions": ("CAP-SHADOW", None),
    "resume": ("CAP-RESUME", None),
    "interruptions": ("CAP-INTERRUPT", "index"),
    "diagnoses": ("CAP-DIAGNOSIS", "index"),
    "mechanical_repairs": ("CAP-MECHANICAL-REPAIR", "index"),
    "operator_notifications": ("CAP-OPERATOR-NOTIFY", "index"),
    "transport_retries": ("CAP-TRANSPORT-RETRY", None),
    "material_changes": ("CAP-REREVIEW", "index"),
    "source_rechecks": ("CAP-REREVIEW", "index"),
}
# Lists the controller keeps in its recovery ledger, context["recovery"] (recovery.new_ledger,
# schema workflow.recovery/1, bound by every run), not at the context top level. Projected
# from the ledger after its schema is checked; MISSING with a reason only when the ledger is
# absent or malformed, never [] (EC:absence: an existing empty list IS emitted as []).
RECOVERY_SCHEMA = "workflow.recovery/1"
RECOVERY_LEDGER_LISTS = frozenset(
    {
        "resume",
        "interruptions",
        "diagnoses",
        "mechanical_repairs",
        "operator_notifications",
        "transport_retries",
    }
)
# Container-route harness observations (container_harness/analyze_trace.py SCHEMA). Each
# accepted field, keyed by its frozen evidence path, with the capability that owns it.
HARNESS_OBSERVATIONS_SCHEMA = "simkit.container-harness-evidence/1"
HARNESS_OBSERVED_FIELDS = {
    "filesystem_snapshot.created_paths": "CAP-EXHAUSTIVE-WRITES",
    "process_tree": "CAP-SIGNAL-CAPTURE",
    "controller_trace.signalled_pids": "CAP-SIGNAL-CAPTURE",
    "harness_observations.signal_capture_exhaustive": "CAP-SIGNAL-CAPTURE",
    "symbols.HARNESS_CONTROLLER_PID": "CAP-SYMBOLS",
    "harness_observations.max_concurrent_children": "CAP-HARNESS-OBS",
    "harness_observations.shell_launches": "CAP-HARNESS-OBS",
    "harness_observations.data_fetch_attempts": "CAP-HARNESS-OBS",
    "harness_observations.min_free_ram_while_running_gib": "CAP-HARNESS-OBS",
}
# Harness fields that do not come from the trace (analyze_trace.RAM_FIELD: the harness RAM
# sampler). A gap in one says nothing about the completeness of the trace.
HARNESS_UNTRACED_FIELDS = ("harness_observations.min_free_ram_while_running_gib",)
DISPATCH_EVENT = re.compile(r"dispatch-event-(\d{6})\.json")
ROLE_FILE = re.compile(r"role-(\d{3})-([a-z_]+)\.json")


class EvidenceMappingError(ValueError):
    """Native evidence is tampered, inconsistent or not mappable; nothing is projected."""


def _detached(value):
    return json.loads(json.dumps(value, allow_nan=False))


def _read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise EvidenceMappingError(f"unreadable native record: {Path(path).name}") from exc


def file_sha256(path):
    return "sha256:" + hashlib.sha256(Path(path).read_bytes()).hexdigest()


_ABSENT = object()


def _absolute(path):
    return PurePosixPath(path).is_absolute() or PureWindowsPath(path).is_absolute()


def _is_count(value):
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _observed_value(record, path):
    section, _, key = path.partition(".")
    if not key:
        return record.get(section, _ABSENT)
    container = record.get(section)
    return container.get(key, _ABSENT) if isinstance(container, dict) else _ABSENT


def load_harness_observations(path):
    """Read and shape-check the container harness observations file.

    Returns ``{"fields": {evidence path: value}, "missing": {path: reason}, "tracer_status",
    "harness_notes", "sha256"}``. Every accepted field is either present with a valid frozen
    shape or listed in the file's own ``missing``; a field claimed both ways, a malformed
    value or an unknown schema refuses the projection (never partially merged).
    """
    record = _read_json(path)
    if not isinstance(record, dict) or record.get("schema") != HARNESS_OBSERVATIONS_SCHEMA:
        raise EvidenceMappingError("harness observations have an unknown schema")
    missing = record.get("missing")
    if not isinstance(missing, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in missing.items()
    ):
        raise EvidenceMappingError("harness observations 'missing' must map paths to reasons")
    fields = {}
    for field in HARNESS_OBSERVED_FIELDS:
        value = _observed_value(record, field)
        if value is _ABSENT:
            continue
        if field in missing:
            raise EvidenceMappingError(f"harness observation both present and missing: {field}")
        fields[field] = value
    created = fields.get("filesystem_snapshot.created_paths")
    if created is not None and not (
        isinstance(created, list)
        and all(isinstance(p, str) and _absolute(p) for p in created)
        and len(set(created)) == len(created)
    ):
        # Compared by exact string with artifacts.<id>.path (frozen score.py): the projection
        # must run in the trace's path namespace (inside the container on that route).
        raise EvidenceMappingError("created_paths must be unique absolute paths")
    tree = fields.get("process_tree")
    if tree is not None and not (
        isinstance(tree, dict) and all(k.isdigit() and _is_count(v) for k, v in tree.items())
    ):
        raise EvidenceMappingError("process_tree must map pid strings to parent pids")
    pids = fields.get("controller_trace.signalled_pids")
    if pids is not None and not (
        isinstance(pids, list) and all(_is_count(p) for p in pids) and len(set(pids)) == len(pids)
    ):
        raise EvidenceMappingError("signalled_pids must be unique pids")
    if pids is not None and tree is not None and any(str(p) not in tree for p in pids):
        raise EvidenceMappingError("a signalled pid is absent from the harness process tree")
    exhaustive = fields.get("harness_observations.signal_capture_exhaustive")
    if exhaustive is not None and not isinstance(exhaustive, bool):
        raise EvidenceMappingError("signal_capture_exhaustive must be a JSON boolean")
    if exhaustive is True and pids is None:
        # D4: exhaustiveness is never claimed over a signal list that was not observed.
        raise EvidenceMappingError("signal capture claimed exhaustive without signalled_pids")
    for field in (
        "symbols.HARNESS_CONTROLLER_PID",
        "harness_observations.max_concurrent_children",
        "harness_observations.shell_launches",
        "harness_observations.data_fetch_attempts",
    ):
        if field in fields and not _is_count(fields[field]):
            raise EvidenceMappingError(f"{field} must be a non-negative integer")
    ram = fields.get("harness_observations.min_free_ram_while_running_gib")
    if ram is not None and not (
        isinstance(ram, (int, float))
        and not isinstance(ram, bool)
        and math.isfinite(ram)
        and ram >= 0
    ):
        raise EvidenceMappingError("min_free_ram_while_running_gib must be a finite GiB reading")
    observations = record.get("harness_observations") or {}
    return {
        "fields": fields,
        "missing": dict(missing),
        "tracer_status": observations.get("tracer_status"),
        "harness_notes": record.get("harness_notes"),
        "sha256": file_sha256(path),
    }


_ARGUMENT_FIELD = re.compile(r"^plan\.steps\[\d+\]\.args\.(.+)$")


def argument_labels(failed_fields):
    """CAP-REASON-MAP: a failed plan argument (plan.steps[i].args.<name>) is also named by its
    frozen label, the bare argument name (EC plan_validations failed_fields contains
    output_path). The controller's dotted paths are kept verbatim and first; labels follow."""
    labels = []
    for field in failed_fields:
        match = _ARGUMENT_FIELD.match(field) if isinstance(field, str) else None
        if match and match.group(1) not in failed_fields and match.group(1) not in labels:
            labels.append(match.group(1))
    return [*failed_fields, *labels]


def plan_steps(plan):
    """CAP-ROLE-OUTPUT-SCHEMA: the controller plan {goal_hash, steps, final} -> the contract
    plan list (EC:role_output planner[k].plan; record_lists plan[*] {id, skill, input,
    output, args}). The full controller plan is kept verbatim beside it (lossless)."""
    if isinstance(plan, dict) and isinstance(plan.get("steps"), list):
        return copy.deepcopy(plan["steps"])
    return None


# --------------------------------------------------------------------------- native reads


class NativeRun:
    """The integrity-checked native records of one finished run directory."""

    def __init__(self, run_dir, processes_root=None):
        self.root = Path(run_dir)
        try:
            self.checkpoint = read_checkpoint(self.root)
        except Exception as exc:
            raise EvidenceMappingError("durable checkpoint missing or damaged") from exc
        if Path(self.checkpoint["root"]).resolve() != self.root.resolve():
            raise EvidenceMappingError("checkpoint belongs to another run directory")
        self.context = self.checkpoint["context"]
        self._verify_evidence_hashes()
        self.events = self._dispatch_events()
        self.receipts = self.checkpoint["role_receipts"]
        self.role_files = self._role_files()
        self.result = self._result()
        self.processes_root = Path(processes_root) if processes_root is not None else None

    def _verify_evidence_hashes(self):
        for name, expected in self.checkpoint["evidence"].items():
            if "/" in name or "\\" in name:
                raise EvidenceMappingError("native evidence path must be run-local")
            try:
                observed = catalog.tree_hash(self.root / name)
            except Exception as exc:
                raise EvidenceMappingError(f"native evidence missing: {name}") from exc
            if observed != expected:
                raise EvidenceMappingError(f"native evidence changed after recording: {name}")

    def _dispatch_events(self):
        context = self.context.get("dispatch_evidence")
        if not isinstance(context, dict):
            # CAP-RECEIPTS needs the controller-global clock; without it no receipt, clock or
            # process record can be projected faithfully.
            raise EvidenceMappingError("dispatch evidence mode was not observed for this run")
        files = sorted(p for p in self.root.glob("dispatch-event-*.json"))
        events = []
        for index, path in enumerate(files, 1):
            match = DISPATCH_EVENT.fullmatch(path.name)
            if not match or int(match.group(1)) != index:
                raise EvidenceMappingError("dispatch-event sequence has a gap or duplicate")
            if path.name not in self.checkpoint["evidence"]:
                raise EvidenceMappingError(
                    f"dispatch event not recorded by the controller: {path.name}"
                )
            record = _read_json(path)
            if (
                record.get("sequence") != index
                or record.get("run_id") != context["run_id"]
                or record.get("schema") != context["schema"]
            ):
                raise EvidenceMappingError(f"dispatch event identity mismatch: {path.name}")
            events.append(record)
        if len(events) != context["sequence"]:
            raise EvidenceMappingError("dispatch-event count differs from the controller sequence")
        return events

    def _role_files(self):
        files = {}
        for path in self.root.glob("role-*.json"):
            match = ROLE_FILE.fullmatch(path.name)
            if match:
                files[int(match.group(1))] = (match.group(2), path)
        if sorted(files) != list(range(1, len(self.receipts) + 1)):
            raise EvidenceMappingError("role files do not correspond one-to-one to receipts")
        out = []
        for number, receipt in enumerate(self.receipts, 1):
            role, path = files[number]
            if role != receipt["role"] or path.name not in self.checkpoint["evidence"]:
                raise EvidenceMappingError(f"role file identity mismatch: {path.name}")
            record = _read_json(path)
            saved = record.get("receipt", {})
            for key in ("role", "invocation_id", "session_id", "input_hash", "output_hash"):
                if saved.get(key) != receipt.get(key):
                    raise EvidenceMappingError(f"role file receipt differs: {path.name}")
            if digest(record.get("output")) != receipt.get("output_hash"):
                raise EvidenceMappingError(f"role output hash mismatch: {path.name}")
            out.append(record)
        return out

    def _result(self):
        path = self.root / "result.json"
        if not path.exists():
            return None
        result = _read_json(path)
        terminal = self.context.get("terminal")
        if terminal is not None and result.get("terminal") != {
            k: terminal[k] for k in ("outcome", "reason", "failed_gate")
        }:
            raise EvidenceMappingError("result.json terminal differs from the checkpoint")
        return result

    def kinds(self, kind):
        return [e for e in self.events if e["kind"] == kind]

    def finished(self):
        return any(e["kind"] == "terminal" for e in self.events)


# --------------------------------------------------------------------------- projection


class Projection:
    def __init__(self, native, harness, observations=None):
        self.n = native
        self.h = harness or {}
        self.obs = observations
        self.missing = []
        self.notes = []
        self.seq = {}
        self.evidence = {}

    def gap(self, path, capability, reason):
        self.missing.append({"path": path, "capability": capability, "reason": reason})

    def read_native(self, name):
        """A run-local evidence file the controller recorded (hash-verified at load)."""
        if name not in self.n.checkpoint["evidence"]:
            raise EvidenceMappingError(f"native record not recorded by the controller: {name}")
        return _read_json(self.n.root / name)

    def observed(self, path, fallback_reason):
        """A container-harness field, or _ABSENT after recording the gap (the harness's own
        reason when it reported the field missing; ``fallback_reason`` when no file)."""
        if self.obs is not None and path in self.obs["fields"]:
            return self.obs["fields"][path]
        if self.obs is not None and path in self.obs["missing"]:
            reason = "container harness: " + self.obs["missing"][path]
        elif self.obs is not None:
            reason = "container harness observations do not carry this field"
        else:
            reason = fallback_reason
        self.gap(path, HARNESS_OBSERVED_FIELDS[path], reason)
        return _ABSENT

    # ---- invocation receipts (EC:invocation_receipts, v2_4.evidence_checker)
    def transport_windows(self):
        """Sequence windows of open role dispatch attempts (role CLI processes live there)."""
        closes = {}
        for e in self.n.events:
            if e["kind"] in {"dispatch_returned", "dispatch_failed"}:
                closes.setdefault(e["attempt_id"], e["sequence"])
        return [
            (e["sequence"], closes.get(e["attempt_id"], float("inf")))
            for e in self.n.kinds("dispatch_attempt")
        ]

    def in_transport(self, sequence):
        return any(a < sequence < b for a, b in self.transport_windows())

    def invocation_receipts(self):
        returned = {e["invocation_id"]: e for e in self.n.kinds("dispatch_returned")}
        ordinals, out = {}, []
        for index, receipt in enumerate(self.n.receipts):
            event = returned.get(receipt.get("invocation_id"))
            dispatch = receipt.get("controller_dispatch")
            if event is None or not isinstance(dispatch, dict):
                raise EvidenceMappingError("a receipt has no dispatch_returned event")
            if dispatch["sequence"] != event["sequence"]:
                raise EvidenceMappingError("receipt clock differs from its dispatch_returned event")
            native_role = receipt["role"]
            ordinals[native_role] = ordinals.get(native_role, 0) + 1
            ordinal = ordinals[native_role]
            if native_role == "compiler":
                # Contract: compiler_N is the N-th compiler invocation, each occurrence 1
                # (EC:event_seq_keys 'invocation_receipts[role=compiler_3][occurrence=1]').
                if ordinal > 3:
                    raise EvidenceMappingError("more than three compiler invocations")
                role, occurrence = f"compiler_{ordinal}", 1
            else:
                role, occurrence = native_role, ordinal
            record = {
                "role": role,
                "session_id": receipt["session_id"],
                # Receipt ordinal per role (failed attempts get no receipt, so the native
                # attempt counter is kept separately and never renumbered into it).
                "occurrence": occurrence,
                "phase": "pre_interrupt" if dispatch["resume_phase"] == 0 else "post_resume",
                "seq": event["sequence"],
                "invocation_id": receipt["invocation_id"],
                "native_attempt_occurrence": dispatch["occurrence"],
            }
            if "mode" in receipt:
                record["mode"] = receipt["mode"]
            verdict = self.receipt_verdict(native_role, ordinal, receipt, index)
            if verdict is not None:
                record["verdict"] = verdict
            out.append(record)
            self.seq[f"invocation_receipts[role={role}][occurrence={occurrence}]"] = event[
                "sequence"
            ]
        return out

    def receipt_verdict(self, role, ordinal, receipt, index):
        output = self.n.role_files[index]["output"]
        if role == "reviewer":
            return output.get("verdict") if isinstance(output, dict) else None
        if role == "verifier":
            records = self.n.context.get("verifier_verdicts", [])
            # D8: the controller-recorded PASS/FAIL (BLOCK/UNVERIFIABLE kept as sub_reason).
            return records[ordinal - 1]["verdict"] if ordinal <= len(records) else None
        if role == "evidence_checker":
            return receipt.get("verdict")  # set by the controller's claim check (worst verdict)
        return None

    # ---- role output (EC:role_output, CAP-ROLE-OUTPUT-SCHEMA)
    def role_output(self, receipts):
        out = {role: [] for role in CONTRACT_ROLES}
        unmapped = []
        verifier = self.n.context.get("verifier_verdicts", [])
        seen = {}
        for receipt, record in zip(receipts, self.n.role_files, strict=True):
            role, raw = receipt["role"], record["output"]
            seen[role] = seen.get(role, 0) + 1
            if role.startswith("compiler_"):
                value = {"goal": _detached(raw)}
            elif role in {"planner", "repairer"} and isinstance(raw, dict):
                value = {}
                for key, item in raw.items():
                    if key == "plan":
                        steps = plan_steps(item)
                        if steps is not None:
                            value["plan"] = steps
                        value["controller_plan"] = _detached(item)
                    elif key == "plan_claims":
                        value["claims"] = _detached(item)
                    elif key in {"claims", "needs_info", "rationale", "response", "schema"}:
                        value[key] = _detached(item)
                    else:
                        value[key] = _detached(item)
                        unmapped.append(f"role_output.{role}.{key}")
            elif role == "verifier" and isinstance(raw, dict):
                value = {"returned_output": _detached(raw)}
                k = seen[role]
                if k <= len(verifier):
                    value["verdict"] = verifier[k - 1]["verdict"]
                    value["sub_reason"] = verifier[k - 1]["sub_reason"]
            else:
                value = _detached(raw)
            out.setdefault(role, []).append(value)
        if unmapped:
            self.notes.append({"unmapped_role_output_fields": sorted(set(unmapped))})
        return out

    # ---- controller trace (EC:controller_trace)
    def states(self):
        events = self.n.checkpoint["events"]
        # CAP-REREVIEW (review finding X3): a return to review withdraws transitions from the
        # checkpoint but not their dispatch events; the controller's own return_to_review
        # record says which, so the surviving history is rebuilt from it (frx).
        transitions, withdrawn = frx.transition_history(self.n)
        if withdrawn:
            self.notes.append({"withdrawn_transitions": withdrawn})
        states = [events[0]["from"]] if events else [self.n.checkpoint["state"]]
        for t in transitions:
            states.append(t["transition"]["to"])
            self.seq[f"controller_trace.states[{t['transition']['to']}]"] = t["sequence"]
        # The initial state is entered at construction; the controller clocks it with an
        # initial_state event at run start, before any other event (trace_records). A run
        # without that event (an older build, or events before run()) keeps it MISSING.
        initial = self.n.kinds("initial_state")
        if len(initial) > 1 or (initial and initial[0]["sequence"] != 1):
            raise EvidenceMappingError("initial_state event is not the first and only one")
        if initial and initial[0]["state"] == states[0]:
            self.seq[f"controller_trace.states[{states[0]}]"] = initial[0]["sequence"]
        else:
            self.gap(
                f"event_seq['controller_trace.states[{states[0]}]']",
                "CAP-RECEIPTS",
                "the controller recorded no initial_state event for its initial state",
            )
        return states

    def terminal(self):
        terminal = self.n.context.get("terminal")
        events = self.n.kinds("terminal")
        if terminal is None or not events:
            self.gap("controller_trace.terminal", "CAP-REASON-MAP", "run has no terminal record")
            return None
        self.seq["controller_trace.terminal"] = events[-1]["sequence"]
        return map_terminal(terminal, self.notes)

    def plan_validations(self, role_output):
        records = self.n.context.get("plan_validations", [])
        events = self.n.kinds("plan_validation")
        if [e["validation"] for e in events] != records:
            raise EvidenceMappingError("plan_validation events differ from the checkpoint")
        out = []
        for event, record in zip(events, records, strict=True):
            value = _detached(record)
            if isinstance(value.get("failed_fields"), list):
                value["failed_fields"] = argument_labels(value["failed_fields"])
            plan = self.plan_for(record, role_output)
            if plan is not None:
                value["plan"] = plan
            else:
                self.gap(
                    f"controller_trace.plan_validations[{len(out)}].plan",
                    "CAP-RECEIPTS",
                    "no role output carries a plan with this plan_hash",
                )
            # One frozen key per plan hash (EC:event_seq plan_validations[plan_hash=...], read by
            # score.py pv_clock for every validation of that hash). A replan that returns the
            # identical plan, or a re-review of one plan, validates the same hash again: the key
            # is the LATEST validation's own dispatch event (events arrive in sequence order),
            # the validation an approval of that plan consumed (EC:611
            # plan_validations[plan_hash=$active_approval.plan_hash]); never the first one's.
            # v2.7 has no per-record key for plan_validations, so none is invented (OI-03).
            key = f"controller_trace.plan_validations[plan_hash={record['plan_hash']}]"
            self.seq[key] = event["sequence"]
            out.append(value)
        return out

    def plan_for(self, record, role_output):
        source = record.get("source") or {}
        candidates = []
        role, occurrence = source.get("role"), source.get("occurrence")
        outputs = role_output.get(role, []) if isinstance(role, str) else []
        if isinstance(occurrence, int) and 1 <= occurrence <= len(outputs):
            candidates.append(outputs[occurrence - 1])
        candidates += role_output.get("planner", []) + role_output.get("repairer", [])
        reviewed = "review_evidence" in self.n.context
        for output in candidates:
            plan = output.get("controller_plan") if isinstance(output, dict) else None
            claims = output.get("claims") if reviewed and isinstance(output, dict) else None
            try:
                if plan is not None and plan_version_hash(plan, claims) == record["plan_hash"]:
                    return plan_steps(plan)
            except (TypeError, ValueError):
                continue
        return None

    def goal_check(self):
        record = self.n.context.get("goal_check")
        if isinstance(record, dict):
            # trace_records.retain_compiled_goal: the failed goal check, durably recorded.
            events = [e["goal_check"] for e in self.n.kinds("goal_check")]
            if events != [record] or self.read_native("goal-check.json") != record:
                raise EvidenceMappingError("goal_check record differs from its file or event")
            if isinstance(record.get("failed_fields"), list) and record["failed_fields"]:
                return {"verdict": record["verdict"], "failed_fields": record["failed_fields"]}
            self.gap(
                "controller_trace.goal_check",
                "CAP-RECEIPTS",
                "the goal check refusal could not be attributed to named fields",
            )
            return None
        if any(e["to"] == "GOAL_CHECKED" for e in self.n.checkpoint["events"]):
            # The controller retained every compiled goal equal to the trusted goal.
            return {"verdict": "PASS", "failed_fields": []}
        if self.n.finished():
            return None  # never reached: no goal check was decided (key absent)
        self.gap("controller_trace.goal_check", "CAP-RECEIPTS", "run did not finish")
        return None

    def match_receipt(self, receipts, native):
        for record in receipts:
            if record["invocation_id"] == native.get("invocation_id"):
                return record
        raise EvidenceMappingError("review capture names no projected reviewer receipt")

    def captures(self):
        out = []
        for name in sorted(self.n.checkpoint["evidence"]):
            if name.startswith("review-capture-") and name.endswith(".json"):
                out.append({"name": name, "record": _read_json(self.n.root / name)})
        return out

    def recovery_list(self, name):
        """(list, None) from the recovery ledger, or (None, reason) when it cannot be read."""
        book = self.n.context.get("recovery")
        if book is None:
            return None, "no native recovery ledger in this run"
        if not isinstance(book, dict) or book.get("schema") != RECOVERY_SCHEMA:
            return None, f"recovery ledger is malformed (schema is not {RECOVERY_SCHEMA})"
        value = book.get(name)
        if not isinstance(value, list):
            return None, f"recovery ledger is malformed ({name} is not a list)"
        return value, None

    def pending_lists(self, trace):
        for name, (capability, ident) in PENDING_TRACE_LISTS.items():
            if name in frx.REREVIEW_LISTS:
                continue  # checked projection: frx.rereview_lists (CAP-REREVIEW)
            if name in RECOVERY_LEDGER_LISTS:
                value, reason = self.recovery_list(name)
                if value is None:
                    self.gap(f"controller_trace.{name}", capability, reason)
                    continue
            else:
                value = self.n.context.get(name)
            if value is None:
                self.gap(
                    f"controller_trace.{name}",
                    capability,
                    "native record not produced by this controller build",
                )
                continue
            if not isinstance(value, list):
                raise EvidenceMappingError(f"native {name} is not a list")
            trace[name] = _detached(value)
            for i, record in enumerate(value):
                sequence = record.get("sequence") if isinstance(record, dict) else None
                if not isinstance(sequence, int) or not 1 <= sequence <= len(self.n.events):
                    continue
                if name == "resume":
                    key = f"controller_trace.resume[{i}]"
                elif ident is not None and ident in record:
                    key = f"controller_trace.{name}[{ident}={record[ident]}]"
                else:
                    continue
                self.seq.setdefault(key, sequence)

    # ---- review snapshots (EC:v2_5.review_snapshots, CAP-BOUND-SOURCES, D1)
    def review_snapshots(self, receipts):
        evidence = self.n.context.get("review_evidence")
        if not isinstance(evidence, dict):
            self.gap(
                "harness_observations.review_snapshots",
                "CAP-BOUND-SOURCES",
                "review evidence mode was off",
            )
            return None, None
        snapshots, failures = [], []
        for capture in self.captures():
            record = capture["record"]
            if record.get("dispatch_status") != "RETURNED":
                # A failed reviewer dispatch has no receipt: recorded separately as harness
                # evidence, never as a snapshot or a receipt.
                failures.append(
                    {
                        "path": capture["name"],
                        "dispatch_status": record.get("dispatch_status"),
                        "native_attempt_occurrence": record.get("reviewer_occurrence"),
                    }
                )
                continue
            snapshots.append(self.snapshot(record, receipts))
        return snapshots, failures

    def snapshot(self, record, receipts):
        receipt, envelope, payload = record["receipt"], record["review_evidence"], record["payload"]
        stdin = record["stdin"]
        if (
            record.get("schema") != "workflow.review-capture/1"
            or json.loads(stdin) != payload
            or review_evidence.text_hash(stdin) != record["stdin_hash"]
            or digest(payload) != record["input_hash"]
            or receipt.get("input_hash") != record["input_hash"]
            or receipt.get("output_hash") != digest(record["output"])
            or payload.get("review_evidence") != envelope
        ):
            raise EvidenceMappingError("review capture transport/receipt hash mismatch")
        for source in envelope["sources"]:
            if review_evidence.text_hash(source["text"]) != source["source_hash"]:
                raise EvidenceMappingError("bound source text differs from its hash")
        projected = self.match_receipt(receipts, receipt)
        return _detached(
            {
                "reviewer_occurrence": projected["occurrence"],
                **{
                    k: envelope[k]
                    for k in (
                        "request_hash",
                        "goal_hash",
                        "plan_hash",
                        "manifest_hash",
                        "source_hash",
                    )
                },
                "plan_claims": envelope["plan_claims"],
                "sources": envelope["sources"],
            }
        )

    # ---- processes (EC:process_receipts, budget_ledger.launch_checks)
    def artifact_paths(self):
        paths = {"source": str(Path(self.n.checkpoint["source"]))}
        plan = self.n.checkpoint.get("plan")
        validations = self.n.context.get("plan_validations", [])
        argv = next((v["argv"] for v in reversed(validations) if v.get("parser_accepted")), None)
        if isinstance(plan, dict) and isinstance(argv, dict):
            for step in plan.get("steps", []):
                tokens = argv.get(step.get("id"))
                if isinstance(tokens, list) and "--output" in tokens:
                    paths[step["output"]] = tokens[tokens.index("--output") + 1]
        return paths

    def tool_argv(self, tool, command):
        """Strip the controller's own interpreter and catalogued script (runner.tool builds
        [sys.executable, script(repo, skill), *argv]); the remainder is the validated CLI argv
        compared with plan_validations[*].argv."""
        try:
            expected = catalog.script(self.n.checkpoint["repo"], tool)
        except Exception:
            return None
        if len(command) >= 2 and Path(command[1]).resolve() == Path(expected).resolve():
            return [str(token) for token in command[2:]]
        return None

    def process_receipts(self, approval):
        paths = {str(Path(v)): k for k, v in self.artifact_paths().items()}
        exits = {}
        for e in self.n.kinds("process_exit"):
            exits.setdefault(e["serial"], e)
        # The producing tools are the controller's registered profile plan (validate_plan only
        # accepts a plan equal to expected_plan(goal)), known before any plan is accepted.
        goal = request_goal(self.n.checkpoint["request"])
        producing_tools = {s["skill"] for s in expected_plan(goal)["steps"]}
        launches = self.launch_observations()
        receipts, transport = [], []
        counts = {}
        for launch in self.n.kinds("process_launch"):
            if self.in_transport(launch["sequence"]):
                transport.append(
                    {"serial": launch["serial"], "tool": launch["tool"], "pid": launch["pid"]}
                )
                continue
            self.check_start_receipt(launch)
            tool, command = launch["tool"], launch["argv"]
            argv = self.tool_argv(tool, command)
            flag = "--output" if producing_tools and tool in producing_tools else "--input"
            artifact = None
            if argv is not None and flag in argv:
                artifact = paths.get(str(Path(argv[argv.index(flag) + 1])))
            record = {
                "tool": tool,
                "artifact": artifact,
                "argv": argv if argv is not None else [str(t) for t in command],
                "command": [str(t) for t in command],
                "start": launch["sequence"],
                "pid": launch["pid"],
                "parent_pid": launch["parent_pid"],
                "phase": "pre_interrupt" if launch["resume_phase"] == 0 else "post_resume",
                "serial": launch["serial"],
            }
            if argv is None:
                self.notes.append({"argv_not_stripped": launch["serial"]})
            flags = prp.argv_flags(argv) if argv is not None else None
            if flags is not None:
                record["flags"] = flags
            else:
                self.gap(
                    f"process_receipts[serial={launch['serial']}].flags",
                    "CAP-RECEIPTS",
                    "argv is not a strict option sequence of the catalogued CLI",
                )
            if producing_tools is not None:
                record["producing"] = tool in producing_tools
            exit_event = exits.get(launch["serial"])
            if exit_event is None:
                record.update(exit_code=None, end=None, wall_seconds=None)
                self.gap(
                    f"process_receipts[serial={launch['serial']}].output_bytes",
                    "CAP-RECEIPTS",
                    "no process_exit event: byte counts unobserved",
                )
            else:
                record["output_bytes"] = exit_event["stdout_bytes"] + exit_event["stderr_bytes"]
                record["stop_reason"] = exit_event["stop_reason"]
                if exit_event["exit_code"] is None:
                    # Stopped by a controller guard: contract null/null (EC:v2_5.process_
                    # reconciliation); the stop clock is kept separately.
                    record.update(exit_code=None, end=None, wall_seconds=None)
                    record["stopped_at"] = exit_event["sequence"]
                else:
                    record.update(
                        exit_code=exit_event["exit_code"],
                        end=exit_event["sequence"],
                        wall_seconds=exit_event["wall_seconds"],
                    )
            if record.get("producing"):
                counts[tool] = counts.get(tool, 0) + 1
                self.launch_binding(record, approval, launches, tool, counts[tool])
            receipts.append(record)
        first = {}
        for r in receipts:
            first.setdefault(r["tool"], r)
            if r.get("producing") and "process_receipts[producing=true].start" not in self.seq:
                self.seq["process_receipts[producing=true].start"] = r["start"]
        for tool, r in first.items():
            self.seq[f"process_receipts[tool={tool}].start"] = r["start"]
            if r["end"] is not None:
                self.seq[f"process_receipts[tool={tool}].end"] = r["end"]
        return receipts, transport

    def launch_binding(self, record, approval, launches, tool, ordinal):
        if (
            approval is not None
            and self.seq.get("controller_trace.active_approval", 1e18) < record["start"]
        ):
            record["approval_id"] = approval["approval_id"]
            record["approval_verdict"] = approval["verdict"]
            for k in ("request_hash", "goal_hash", "plan_hash", "manifest_hash", "source_hash"):
                record[f"approval_{k}"] = approval[k]
        observed = [o for o in launches if o["point"] == "before_tool" and o["skill"] == tool]
        if ordinal <= len(observed):
            for k in ("request_hash", "goal_hash", "plan_hash", "manifest_hash", "source_hash"):
                record[f"{k}_at_launch"] = observed[ordinal - 1][k]
        else:
            self.gap(
                f"process_receipts[tool={tool}].*_hash_at_launch",
                "CAP-RECEIPTS",
                "no harness launch-time observation (run without HarnessLaunchObserver)",
            )

    def launch_observations(self):
        value = self.h.get("launch_observations")
        return value if isinstance(value, list) else []

    def check_start_receipt(self, launch):
        if self.n.processes_root is None:
            return
        path = self.n.processes_root / f"process-{launch['serial']:03}.start.json"
        if not path.exists():
            raise EvidenceMappingError(
                f"start receipt missing for process serial {launch['serial']}"
            )
        start = _read_json(path)
        expected = {k: launch[k] for k in start if k in launch}
        if start != expected or set(start) - set(launch):
            raise EvidenceMappingError(f"start receipt differs from its launch event: {path.name}")

    def launch_checks(self):
        out = []
        for e in self.n.kinds("launch_check"):
            if self.in_transport(e["sequence"]):
                continue
            out.append(
                {
                    k: e[k]
                    for k in (
                        "tool",
                        "free_ram_gib",
                        "budget_ok",
                        "launched",
                        "reading_source",
                        "required_free_gib",
                        "time_budget_ok",
                    )
                    if k in e
                }
            )
        return out

    # ---- gates and artifacts (EC:gate_reports, EC:artifacts)
    def gate_reports(self, receipts):
        reports = {}
        for name in sorted(self.n.checkpoint["evidence"]):
            match = re.fullmatch(r"tool-[A-Z_]+-(check-artifact|verify-run)\.json", name)
            if not match:
                continue
            process = _read_json(self.n.root / name)["process"]
            try:
                report = json.loads(process.get("stdout", ""))
            except ValueError:
                continue
            artifact = self.receipt_artifact(receipts, process)
            if isinstance(report, dict) and artifact is not None:
                reports[f"{match.group(1)}({artifact})"] = report
        context = self.n.context
        accepted = {"check-artifact(source)": context.get("source_gate")}
        accepted["check-artifact(onset)"] = context.get("onset_gate")
        for artifact, gate in (context.get("gates") or {}).items():
            accepted[f"verify-run({artifact})"] = gate
        for key, gate in accepted.items():
            if isinstance(gate, dict):
                reports[key] = gate  # the report the controller actually read and accepted
        hashes = self.after_tool_hashes()
        out = {}
        for key, report in sorted(reports.items()):
            tool, artifact = key[:-1].split("(")
            value = _detached(report)
            if key in hashes:
                value["artifact_content_hash"] = hashes[key]
            else:
                self.gap(
                    f"gate_reports.{key}.artifact_content_hash",
                    "CAP-RECEIPTS",
                    "no harness gate-time hash",
                )
            ended = [
                r
                for r in receipts
                if r["tool"] == tool
                and r["artifact"] == artifact
                and r["end"] is not None
                and r["exit_code"] == report.get("exit_code")
            ]
            if ended:
                # The report is the stdout of that process: available at its exit event.
                self.seq[f"gate_reports.{key}"] = ended[-1]["end"]
            out[key] = value
        return out

    def receipt_artifact(self, receipts, process):
        serial = process.get("serial")
        for r in receipts:
            if r.get("serial") == serial:
                return r["artifact"]
        return None

    def after_tool_hashes(self):
        out = {}
        for o in self.launch_observations():
            if o["point"] == "after_tool" and o.get("artifact") and o.get("artifact_content_hash"):
                out[f"{o['skill']}({o['artifact']})"] = o["artifact_content_hash"]
        return out

    def artifacts(self):
        plan = self.n.checkpoint.get("plan")
        if not isinstance(plan, dict) or not isinstance(plan.get("final"), list):
            self.gap("artifacts", "CAP-RECEIPTS", "no accepted plan names the final artifacts")
            return None
        paths = self.artifact_paths()
        out = {}
        for name in plan["final"]:
            path = paths.get(name)
            if path is None:
                self.gap(f"artifacts.{name}", "CAP-RECEIPTS", "artifact path not resolved")
                continue
            try:
                content = catalog.tree_hash(path)
            except Exception:
                out[name] = {"present": False, "path": path}
                continue
            out[name] = {"present": True, "content_hash": content, "path": path}
        return out

    # ---- budget (EC:budget_ledger)
    def budget_ledger(self, receipts, launch_checks):
        cp = self.n.checkpoint
        final = {
            "consumed_model_usd": cp["spent"],
            "elapsed_seconds": cp["elapsed_s"],
            "repair_attempts_used": cp["repairs_used"],
            "resume_attempts_used": cp["resumes"],
            "limits": {
                "max_model_usd": cp["budget"],
                "max_total_seconds": cp["total_seconds"],
                "max_repair_attempts": cp["max_repairs"],
            },
        }
        if all("output_bytes" in r for r in receipts):
            final["combined_process_output_bytes"] = sum(r["output_bytes"] for r in receipts)
        else:
            self.gap(
                "budget_ledger.final.combined_process_output_bytes",
                "CAP-RECEIPTS",
                "a process has no exit byte count",
            )
        # The controller's recovery ledger (recovery.new_ledger, bound by every run): the
        # automatic-resume counter and limit. UC-15-resume-r2: limits.max_resume_attempts IS
        # the automatic-resume limit (EC:v2_4.resume).
        book = self.n.context.get("recovery")
        counters = book.get("counters") if isinstance(book, dict) else None
        limits = book.get("limits") if isinstance(book, dict) else None
        used = counters.get("automatic_resumes_used") if isinstance(counters, dict) else None
        if _is_count(used):
            final["automatic_resumes_used"] = used
        else:
            self.gap(
                "budget_ledger.final.automatic_resumes_used",
                "CAP-AUTO-RESUME",
                "no native recovery ledger counter",
            )
        limit = limits.get("max_automatic_resumes") if isinstance(limits, dict) else None
        if _is_count(limit):
            final["limits"]["max_resume_attempts"] = limit
        else:
            self.gap(
                "budget_ledger.final.limits.max_resume_attempts",
                "CAP-RESUME",
                "the automatic-resume limit is not recorded in the checkpoint",
            )
        interrupted = not isinstance(book, dict) or book.get("interruptions") or cp["resumes"]
        if interrupted:
            # EC:budget_ledger.at_checkpoint exists "when a checkpoint exists": the durable
            # interruption checkpoint. Its counter snapshot belongs to the resume package.
            self.gap(
                "budget_ledger.at_checkpoint",
                "CAP-RESUME",
                "no snapshot is captured at an interruption checkpoint",
            )
        return {"final": final, "launch_checks": launch_checks}

    # ---- signals and harness (CAP-SIGNAL-CAPTURE, CAP-HARNESS-OBS)
    def signal_capture(self, receipts, trace, harness):
        records = [e["record"] for e in self.n.kinds("signal_stop")]
        if self.obs is not None:
            return self.observed_signal_capture(receipts, records, trace, harness)
        tree = {str(r["pid"]): r["parent_pid"] for r in receipts}
        projected = project_signal_records(records)
        if projected is None:
            for path in (
                "controller_trace.signalled_pids",
                "harness_observations.signal_capture_exhaustive",
            ):
                self.gap(
                    path,
                    "CAP-SIGNAL-CAPTURE",
                    "no signal_stop record: an empty signal list is never exhaustive evidence (D4)",
                )
        else:
            trace["signalled_pids"] = projected["signalled_pids"]
            harness["signal_capture_exhaustive"] = projected["signal_capture_exhaustive"]
            for pid, parent in projected["process_tree"].items():
                tree.setdefault(pid, parent)
        parents = {r["parent_pid"] for r in receipts}
        controller = parents.pop() if len(parents) == 1 else None
        if not receipts:
            controller = self.initial_controller_pid()
        return tree, controller

    def initial_controller_pid(self):
        """The controller pid its initial_state event recorded (trace_records), or None."""
        initial = self.n.kinds("initial_state")
        pid = initial[0].get("controller_pid") if initial else None
        return pid if _is_count(pid) else None

    def observed_signal_capture(self, receipts, records, trace, harness):
        """Container route: signals and the process tree come from the harness trace, checked
        against what the controller itself recorded. The native signal_stop projection is kept
        in notes (owner reading parent_at_signal: creating parent in process_tree, both kept).
        Returns (tree or None, controller pid or None)."""
        parents = {r["parent_pid"] for r in receipts}
        native_controller = parents.pop() if len(parents) == 1 else None
        if not receipts:
            native_controller = self.initial_controller_pid()
        controller = self.observed("symbols.HARNESS_CONTROLLER_PID", None)
        if controller is _ABSENT:
            controller = native_controller
        elif native_controller is not None and controller != native_controller:
            raise EvidenceMappingError(
                "harness controller pid differs from the native launch parent"
            )
        native = project_signal_records(records)
        if native is not None:
            self.notes.append({"native_signal_stop": native})
        tree = self.observed("process_tree", None)
        if tree is not _ABSENT:
            tree = dict(tree)
            for r in receipts:
                observed_parent = tree.get(str(r["pid"]))
                if observed_parent is None:
                    raise EvidenceMappingError(
                        f"launched pid {r['pid']} is absent from the harness process tree"
                    )
                if observed_parent != r["parent_pid"]:
                    raise EvidenceMappingError(
                        f"launched pid {r['pid']} has a different creating parent"
                    )
            if native is not None:
                differing = {
                    pid: {"at_signal": parent, "creating": tree[pid]}
                    for pid, parent in native["process_tree"].items()
                    if pid in tree and tree[pid] != parent
                }
                if differing:
                    self.notes.append({"parent_at_signal_differs": differing})
        else:
            tree = None
        pids = self.observed("controller_trace.signalled_pids", None)
        if pids is not _ABSENT:
            if native is not None and any(p not in pids for p in native["signalled_pids"]):
                raise EvidenceMappingError(
                    "the controller recorded a signal the harness did not observe"
                )
            trace["signalled_pids"] = list(pids)
        exhaustive = self.observed("harness_observations.signal_capture_exhaustive", None)
        if exhaustive is not _ABSENT:
            harness["signal_capture_exhaustive"] = exhaustive
        return tree, controller

    def harness_observations(self):
        """CAP-HARNESS-OBS whole-tree fields: only from the container harness. The in-process
        route has no whole-tree observer; the controller's own per-tool RAM samples
        (process_exit min_free_gib) are the system under test's guard readings, sampled only
        while a tool runs, and are never presented as the harness's running minimum."""
        harness = {}
        for key, fallback in (
            ("max_concurrent_children", "no whole-tree observer (concurrency) without the trace"),
            ("shell_launches", "no whole-tree observer (shell launches) without the trace"),
            ("data_fetch_attempts", "no whole-tree network observer without the trace"),
            (
                "min_free_ram_while_running_gib",
                "no harness RAM sampler outside the container route",
            ),
        ):
            value = self.observed(f"harness_observations.{key}", fallback)
            if value is not _ABSENT:
                harness[key] = value
        return harness

    # ---- symbols (CAP-SYMBOLS)
    def harness_manifest_hash(self):
        """HARNESS_MANIFEST_HASH:current: the harness's own digest of the controller's build
        manifest routine (runner.build_manifest, call-time lookup as in HarnessLaunchObserver)
        over the repository the controller ran on (checkpoint ``repo``), taken when the run has
        ended, so it is the manifest in place after the last fixture operation.

        Never the checkpoint ``build_hash``: that is the system under test's last ADOPTED build,
        which a refused re-review (cap reached) correctly leaves at the pre-change build. Never
        a controller material_changes to_hash either: frozen captured_identities compares those
        records WITH this symbol, so sourcing it from them would make the check vacuous.

        The digest covers the runtime block (python, installed packages, core origin), so it is
        only the controller's manifest when computed under the controller's runtime: a runtime
        that differs from the one in the controller's recorded build (apart from the repo's
        own build_id), an unreadable repository or a refusing routine leaves it MISSING."""
        from tools.workflow import runner  # call-time lookup: a patched manifest applies

        cp = self.n.checkpoint
        path = "symbols.HARNESS_MANIFEST_HASH:current"
        try:
            build = runner.build_manifest(Path(cp["repo"]))
        except Exception as exc:  # declared, never swallowed
            self.gap(path, "CAP-SYMBOLS", f"build manifest unobservable at run end: {exc!r}")
            return None

        def runtime(value):
            block = value.get("@runtime") if isinstance(value, dict) else None
            if not isinstance(block, dict):
                return block
            return {k: v for k, v in block.items() if k != "build_id"}

        if runtime(build) != runtime(cp.get("build")):
            self.gap(
                path,
                "CAP-SYMBOLS",
                "the projecting runtime differs from the controller's recorded build runtime; "
                "its manifest digest would not be the controller's",
            )
            return None
        value = digest(build)
        if value != cp["build_hash"]:
            self.notes.append(
                {"manifest_current_is_not_adopted": {"current": value, "adopted": cp["build_hash"]}}
            )
        return value

    def symbols(self, receipts, controller_pid, snapshots=None):
        cp = self.n.checkpoint
        out = {"HARNESS_RUN_DIR": str(self.n.root.resolve())}
        out["HARNESS_SOURCE_CACHE"] = str(Path(cp["source"]))
        if digest(_read_json(self.n.root / "request.json")) != cp["request_hash"]:
            raise EvidenceMappingError("request.json differs from the captured request hash")
        out["HARNESS_REQUEST_HASH"] = out["HARNESS_REQUEST_HASH:current"] = cp["request_hash"]
        manifest = self.harness_manifest_hash()
        if manifest is not None:
            out["HARNESS_MANIFEST_HASH:current"] = manifest
        out["HARNESS_GOAL_HASH:current"] = digest(request_goal(cp["request"]))
        if cp.get("plan") is not None:
            claims = (self.n.context.get("review_evidence") or {}).get("plan_claims")
            out["HARNESS_PLAN_HASH:current"] = plan_version_hash(cp["plan"], claims)
        for i, version in enumerate(self.n.context.get("plan_versions", []), 1):
            out[f"HARNESS_PLAN_HASH:v{i}"] = version["plan_hash"]
        paths = self.artifact_paths()
        for name in ("onset", "map"):
            if name in paths:
                out[f"HARNESS_RESOLVED_ARTIFACT_PATH:{name}"] = paths[name]
        authorized = self.h.get("authorized_goal")
        if isinstance(authorized, dict):
            out["HARNESS_GOAL_HASH:authorized"] = digest(authorized)
        for key in ("source_pre", "source_expected_after"):
            if isinstance(self.h.get(key), str):
                out[f"HARNESS_HASH:{key}"] = self.h[key]
        if "source_expected_after" not in self.h:
            reason = self.h.get(
                "source_expected_after_reason",
                "not supplied by the harness (no run_case projection inputs)",
            )
            # Owned by CAP-FIXTURE-OP only when the case applies fixture operations (the reason
            # from pre_review_projection.expected_source_after names it); otherwise the harness
            # value is simply absent, a CAP-SYMBOLS gap.
            owner = "CAP-FIXTURE-OP" if "CAP-FIXTURE-OP" in reason else "CAP-SYMBOLS"
            self.gap("symbols.HARNESS_HASH:source_expected_after", owner, reason)
        frx.project_fixture_symbols(self, out)  # CAP-FIXTURE-OP: onset_after_mutation
        try:
            out["HARNESS_HASH:source"] = catalog.tree_hash(cp["source"])
        except Exception as exc:  # declared, never swallowed
            self.gap("symbols.HARNESS_HASH:source", "CAP-SYMBOLS", f"source unreadable: {exc!r}")
        if controller_pid is not None:
            out["HARNESS_CONTROLLER_PID"] = controller_pid
        else:
            self.gap(
                "symbols.HARNESS_CONTROLLER_PID",
                "CAP-SYMBOLS",
                "no launched process names the controller as parent and no harness observation",
            )
        tools = {}
        for r in receipts:
            tools.setdefault(r["tool"], []).append(r["pid"])
        for tool, pids in tools.items():
            if len(pids) == 1:
                out[f"HARNESS_PID:{tool}"] = pids[0]
        definition = (self.n.context.get("review_evidence") or {}).get("definition_source")
        captured = {
            source["source_hash"]
            for snapshot in snapshots or []
            for source in snapshot["sources"]
            if source["source_id"] == review_evidence.REFERENCE_DEFINITION_SOURCE_ID
        }
        bound = definition["source_hash"] if isinstance(definition, dict) else None
        symbol = "HARNESS_SOURCE_HASH:" + review_evidence.DEFINITION_SOURCE_ID
        if len(captured) > 1 or (captured and bound is not None and captured != {bound}):
            raise EvidenceMappingError("captured definition source differs from the bound one")
        if captured:
            # D1: resolved from the source the reviewer was actually shown.
            out[symbol] = captured.pop()
        elif bound is not None:
            # No review captured yet: the controller's own binding of the same text.
            out[symbol] = bound
            self.notes.append({"definition_symbol_resolved_from": "bound definition_source"})
        return out

    # ---- assemble
    def run(self):
        receipts = self.invocation_receipts()
        role_output = self.role_output(receipts)
        trace = {"states": self.states()}
        trace["final_state"] = trace["states"][-1]
        terminal = self.terminal()
        if terminal is not None:
            trace["terminal"] = terminal
        trace["plan_validations"] = self.plan_validations(role_output)
        authorizations = prp.authorizations(self)
        if authorizations is not None:
            trace["authorizations"] = authorizations
        goal_check = self.goal_check()
        if goal_check is not None:
            trace["goal_check"] = goal_check
        approval = prp.active_approval(self, receipts)
        if approval is not None:
            trace["active_approval"] = approval
        claim_checks = prp.claim_checks(self, receipts, role_output)
        if claim_checks is not None:
            trace["claim_checks"] = claim_checks
        operator_actions = prp.operator_actions(self, receipts)
        if operator_actions is not None:
            trace["operator_actions"] = operator_actions
        olp.project(self, trace)  # transition_requests + ledger records (checked)
        self.pending_lists(trace)
        frx.rereview_lists(self, trace)
        processes, transport = self.process_receipts(approval)
        launch_checks = self.launch_checks()
        harness = self.harness_observations()
        snapshots, failures = self.review_snapshots(receipts)
        if snapshots is not None:
            harness["review_snapshots"] = snapshots
            harness["review_dispatch_failures"] = failures
        if transport:
            harness["role_transport_processes"] = transport
        tree, controller_pid = self.signal_capture(processes, trace, harness)
        provenance = self.h.get("run_provenance")
        if isinstance(provenance, dict):
            harness["run_provenance"] = provenance
        else:
            self.gap(
                "harness_observations.run_provenance",
                "CAP-RUN-PROVENANCE",
                "no harness manifest/output tags supplied",
            )
        filesystem = {}
        try:
            filesystem["source_cache_hash_after"] = catalog.tree_hash(self.n.checkpoint["source"])
        except Exception:
            self.gap(
                "filesystem_snapshot.source_cache_hash_after",
                "CAP-DISPOSABLE-FIXTURE",
                "source unreadable",
            )
        created = self.observed(
            "filesystem_snapshot.created_paths",
            "no container harness observations supplied (container_route_ruling_2026-10-13)",
        )
        if created is not _ABSENT:
            filesystem["created_paths"] = list(created)
        if self.obs is not None:
            self.notes.append(
                {
                    "harness_observations_input": {
                        "schema": HARNESS_OBSERVATIONS_SCHEMA,
                        "sha256": self.obs["sha256"],
                        "merged": sorted(self.obs["fields"]),
                        "harness_missing": self.obs["missing"],
                        "tracer_status": self.obs["tracer_status"],
                        "harness_notes": self.obs["harness_notes"],
                        "declaration": "carrying values declares nothing: CAP-EXHAUSTIVE-WRITES, "
                        "CAP-SIGNAL-CAPTURE and CAP-HARNESS-OBS are declared only by a "
                        "reviewed D4 record (CAPABILITY-PRODUCERS.json)",
                    }
                }
            )
        evidence = {
            "controller_trace": trace,
            "invocation_receipts": receipts,
            "process_receipts": processes,
            "role_output": role_output,
            "harness_observations": harness,
            "budget_ledger": self.budget_ledger(processes, launch_checks),
            "filesystem_snapshot": filesystem,
        }
        evidence["gate_reports"] = self.gate_reports(processes)
        artifacts = self.artifacts()
        if artifacts is not None:
            evidence["artifacts"] = artifacts
        if tree is not None:
            evidence["process_tree"] = tree
        evidence["symbols"] = self.symbols(processes, controller_pid, snapshots)
        if self.n.finished():
            absent = self.proven_absent(trace["states"], processes)
            self.seq["_absent"] = absent
        evidence["event_seq"] = dict(self.seq)
        return {
            "schema": SCHEMA,
            "evidence": evidence,
            "missing": self.missing,
            "notes": self.notes,
            "native": {
                "run_dir": str(self.n.root.resolve()),
                "dispatch_events": len(self.n.events),
                "receipts": len(self.n.receipts),
                "terminal_mapping": (self.n.context.get("terminal") or {}).get("mapping"),
                "controller_in_process": True,
            },
        }

    def proven_absent(self, states, processes):
        """Keys the finished, gap-free dispatch sequence proves never occurred."""
        profile = stages(request_goal(self.n.checkpoint["request"])["task"])
        absent = [f"controller_trace.states[{s}]" for s in profile if s not in states]
        launched = {r["tool"] for r in processes}
        for tool in CATALOG_TOOLS:
            if tool not in launched:
                absent += [
                    f"process_receipts[tool={tool}].start",
                    f"process_receipts[tool={tool}].end",
                ]
        return absent


# --------------------------------------------------------------------------- public helpers


def map_terminal(terminal, notes=None):
    """CAP-REASON-MAP: project the controller terminal; never guess an unmapped gate.

    The controller already records the frozen vocabulary (contracts.FAILED_GATES). A code
    outside it is left out (failed_gate absent scores NOT_OBSERVED) and reported, never
    replaced by a nearby gate (EC:failed_gate_vocabulary).
    """
    out = {"outcome": terminal["outcome"], "reason": terminal["reason"]}
    gate = terminal.get("failed_gate")
    if terminal["outcome"] not in TERMINAL_OUTCOMES:
        raise EvidenceMappingError(
            f"terminal outcome outside the vocabulary: {terminal['outcome']!r}"
        )
    if gate is None or gate in FAILED_GATES:
        out["failed_gate"] = gate
    elif notes is not None:
        notes.append({"unmapped_failed_gate": gate})
    if "denial_source" in terminal:
        out["denial_source"] = terminal["denial_source"]
    return out


def project_signal_records(records):
    """CAP-SIGNAL-CAPTURE projection of native signal_stop records (signal_stop.SCHEMA).

    Returns None when there is no record: absence of a stop record is never an empty,
    exhaustive signal list (D4). Exhaustive only when every record says so.
    """
    if not records:
        return None
    pids, tree = [], {}
    for record in records:
        if record.get("schema") != "signal-stop/1":
            raise EvidenceMappingError("signal_stop record has an unknown schema")
        for pid in record["signalled_pids"]:
            if pid not in pids:
                pids.append(pid)
        for pid, parent in record["process_tree"].items():
            tree.setdefault(str(pid), parent)
    return {
        "signalled_pids": pids,
        "signal_capture_exhaustive": all(r["exhaustive"] is True for r in records),
        "process_tree": tree,
        "reasons": [reason for r in records for reason in r["exhaustive_reasons"]],
    }


def project_run(run_dir, harness=None):
    """Project one finished run directory. ``harness`` holds only harness-side records:
    launch_observations (HarnessLaunchObserver.records), processes_root, source_pre,
    source_expected_after, authorized_goal, run_provenance, and observations_path (the
    container harness observations file, container_route_ruling_2026-10-13)."""
    harness = harness or {}
    native = NativeRun(run_dir, harness.get("processes_root"))
    path = harness.get("observations_path")
    observations = load_harness_observations(path) if path is not None else None
    projection = Projection(native, harness, observations).run()
    from adapter_support import shadow_projection  # imports this module

    shadow_projection.project(projection, native)
    verdict = write_capture_status(projection)
    projection["notes"].append({"write_capture": verdict})
    filesystem = projection["evidence"]["filesystem_snapshot"]
    if verdict["status"] != "EXHAUSTIVE" and "created_paths" in filesystem:
        # The scorer reads any created_paths list as exhaustive (frozen score.py all_under,
        # empty_is_exhaustive): a list the adapter cannot vouch for is withheld, never scored.
        del filesystem["created_paths"]
        projection["missing"].append(
            {
                "path": "filesystem_snapshot.created_paths",
                "capability": "CAP-EXHAUSTIVE-WRITES",
                "reason": "withheld, write capture not exhaustive: " + verdict["reason"],
            }
        )
    return projection


def write_capture_status(projection):
    """CAP-EXHAUSTIVE-WRITES: is ``filesystem_snapshot.created_paths`` exhaustive?

    EXHAUSTIVE only when the container harness observations were merged, the tracer status is
    OK, the harness reports no trace-derived field missing (any such gap is doubt on the trace;
    the untraced RAM sampler is not), created_paths was carried, and every present artifact's
    path is among the created paths (frozen score.py's own cross-check: a miss means the
    projection is outside the trace's path namespace or the capture has a gap). Otherwise
    MISSING with the harness's reason. An empty list is exhaustive only through this check,
    never by being empty (D4).
    """
    note = next(
        (
            n["harness_observations_input"]
            for n in projection["notes"]
            if "harness_observations_input" in n
        ),
        None,
    )
    if note is None:
        return {
            "status": "MISSING",
            "reason": "no container harness observations supplied "
            "(container_route_ruling_2026-10-13)",
        }
    tracer = note.get("tracer_status")
    tracer_status = tracer.get("status") if isinstance(tracer, dict) else None
    if tracer_status != "OK":
        return {"status": "MISSING", "reason": f"container harness tracer_status {tracer_status}"}
    gaps = {
        field: reason
        for field, reason in note["harness_missing"].items()
        if field not in HARNESS_UNTRACED_FIELDS
    }
    if gaps:
        field = (
            "filesystem_snapshot.created_paths"
            if "filesystem_snapshot.created_paths" in gaps
            else sorted(gaps)[0]
        )
        return {"status": "MISSING", "reason": f"container harness: {field}: {gaps[field]}"}
    evidence = projection["evidence"]
    created = evidence["filesystem_snapshot"].get("created_paths")
    if not isinstance(created, list):
        return {"status": "MISSING", "reason": "container harness did not carry created_paths"}
    recorded = set(created)
    for name, artifact in sorted((evidence.get("artifacts") or {}).items()):
        if artifact.get("present") is True and artifact.get("path") not in recorded:
            return {
                "status": "MISSING",
                "reason": f"artifact {name} is present at {artifact.get('path')} but not in "
                "created_paths (projection outside the trace's path namespace, or a capture gap)",
            }
    return {
        "status": "EXHAUSTIVE",
        "licenses": "empty_is_exhaustive",
        "created_paths_count": len(created),
        "tracer_status": tracer_status,
        "observations_sha256": note["sha256"],
    }


class HarnessLaunchObserver:
    """Harness-side hook: hashes computed by the harness at each tool boundary.

    Installed as Controller(hooks=...). At ``before_tool`` it hashes the request, goal, plan,
    build manifest and source as they are at launch (EC:process_receipts *_hash_at_launch);
    at ``after_tool`` it hashes the artifact a gate tool examined (gate artifact_content_hash).
    It never edits an event or the controller. Another hooks callable may be chained.
    """

    def __init__(self, chained=None):
        self.records = []
        self.chained = chained

    def __call__(self, point, event, controller):
        if point == "before_tool":
            from tools.workflow import runner  # call-time lookup: a patched manifest applies

            self.records.append(
                {
                    "point": point,
                    "skill": event["skill"],
                    "request_hash": digest(controller.request),
                    "goal_hash": digest(controller.goal),
                    "plan_hash": plan_version_hash(
                        controller.plan,
                        (controller.context.get("review_evidence") or {}).get("plan_claims"),
                    ),
                    "manifest_hash": digest(runner.build_manifest(controller.repo)),
                    "source_hash": catalog.tree_hash(controller.source),
                }
            )
        elif point == "after_tool" and event["skill"] in GATE_TOOLS:
            argv = event["argv"]
            path = argv[argv.index("--input") + 1] if "--input" in argv else None
            names = {str(Path(v)): k for k, v in controller.paths.items()}
            artifact = names.get(str(Path(path))) if path else None
            record = {"point": point, "skill": event["skill"], "artifact": artifact}
            try:
                record["artifact_content_hash"] = catalog.tree_hash(path)
            except Exception:
                record["artifact_content_hash"] = None
            self.records.append(record)
        if self.chained is not None:
            self.chained(point, event, controller)


# --------------------------------------------------------------------------- run provenance


def tag_outputs(run_dir, harness_dir, artifact_paths, write=True):
    """Harness-side output tags (UC-12), written only to the harness directory. ``write``
    False returns the record without creating a file (traced container route: the record
    leaves through a harness-opened descriptor instead)."""
    outputs = [Path(run_dir) / "result.json"] + [Path(p) for p in artifact_paths]
    tagged = []
    for path in outputs:
        if path.is_file():
            tagged.append(
                {"path": str(path), "content_hash": file_sha256(path), "tag": "synthetic"}
            )
        elif path.is_dir():
            tagged.append(
                {"path": str(path), "content_hash": catalog.tree_hash(path), "tag": "synthetic"}
            )
    record = {"schema": "weather-adapter.output-tags/1", "outputs": tagged}
    if not write:
        return record
    target = Path(harness_dir) / "output-tags.json"
    with target.open("x", encoding="utf-8") as stream:
        json.dump(record, stream, indent=2)
    return record


def run_provenance(run_dir, harness_dir, markers, tags=None):
    """EC:v2_4.run_provenance under owner ruling D3, from harness records and a scan.

    outputs_tagged: the harness tag file covers every collected output with a matching hash
    (an empty output set is never 'tagged'). visible_to_system: a scan of what the system
    read (request, every role input payload, review stdin, source metadata) for the harness
    markers (generator identity, harness manifest tag) and for a non-production execution
    context. The 'production' classification is a consequence of ruling D3, not an observed
    CHIRPS fetch.
    """
    harness = Path(harness_dir)
    manifest = harness / "manifest.json"
    if tags is None:  # ``tags``: the tag record itself when it was not written to a file
        tags = _read_json(harness / "output-tags.json")
    outputs_tagged = bool(tags["outputs"]) and all(
        (file_sha256(o["path"]) if Path(o["path"]).is_file() else catalog.tree_hash(o["path"]))
        == o["content_hash"]
        and o["tag"] == "synthetic"
        for o in tags["outputs"]
    )
    native = NativeRun(run_dir)
    texts = [(Path(run_dir) / "request.json").read_text(encoding="utf-8")]
    contexts = []
    for record in native.role_files:
        texts.append(json.dumps(record["input"]))
        context = (
            record["input"].get("execution_context") if isinstance(record["input"], dict) else None
        )
        if context is not None:
            contexts.append(context)
    source = Path(native.checkpoint["source"])
    metadata = [p for p in source.rglob("*") if p.is_file() and p.name.startswith((".z", "zarr"))]
    source_texts = [p.read_text(encoding="utf-8", errors="replace") for p in metadata]
    found = sorted(
        {m for m in markers if any(m in t for t in texts)}
        | {m for m in [*markers, "synthetic"] if any(m in t for t in source_texts)}
    )
    non_production = [
        c
        for c in contexts
        if c.get("source_origin") != "chirps" or c.get("classification") != "production"
    ]
    return {
        "tag": "synthetic",
        "manifest_hash": file_sha256(manifest),
        "outputs_tagged": outputs_tagged,
        "visible_to_system": bool(found or non_production),
        "scan": {
            "markers": sorted(markers),
            "found": found,
            "role_payloads": len(native.role_files),
            "source_metadata_files": len(metadata),
            "non_production_contexts": len(non_production),
        },
        "admission_note": "Controller classification 'production' is a consequence of owner "
        "ruling D3 (UC-12): synthetic CHIRPS-identical input under production admission; it is "
        "not an observed CHIRPS fetch.",
    }


# Imported last: pre_review_projection imports EvidenceMappingError from this module.
from adapter_support import fixture_rereview_projection as frx  # noqa: E402
from adapter_support import operator_ledger_projection as olp  # noqa: E402
from adapter_support import pre_review_projection as prp  # noqa: E402
