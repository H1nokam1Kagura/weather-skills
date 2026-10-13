"""Controller-owned recovery semantics (D18, D23, D29 parts, D31).

Everything here is a durable controller record, written from facts the controller itself
observed (classified exception types, the durable checkpoint, its own counters). Nothing is
parsed from exception text and nothing is synthesized: a fact the controller did not observe
stays missing (null), never defaulted.

Frozen shapes (EVIDENCE_CONTRACT.json structures v2_4.resume, v2_4.interruptions,
v2_5.resume_verification, v2_5.provenance):
- controller_trace.interruptions[*] {index, failure_class, at_state, pending_operation}
- controller_trace.resume[*] {result, verification{verdict, checked_items, failed_items},
  budget_override_applied, initiated_by, override_requested, operator_record_id,
  interruption_index, refusal_record{request, illegal_fields, governing_limit}}
- controller_trace.diagnoses[*] {index, interruption_indexes, cause, mechanical, evidenced,
  evidence[], fix_action, fix_widens_authority}
- controller_trace.mechanical_repairs[*] {index, diagnosis_index, action, widens_authority,
  result}
- controller_trace.operator_notifications[*] {index, channel, stop_record{failure_classes},
  diagnosis_index} (+ the native recorded/emitted lifecycle)
- controller_trace.transport_retries[*] {step, attempt, before_write}
Each record also carries the controller-global dispatch ``sequence`` (null when dispatch
evidence mode is off: never invented).

D31 (CR-07 O1): the run lock stays kernel-only, so a stale lock cannot exist and
``lock_stale`` is never classified. The mechanical-repair executor is a closed vocabulary that
is EMPTY in this SUT; CAP-OWNED-STALE-LOCK-SETUP is not implemented and E14-P2-CTL stays
blocked by contract.
"""

import copy
import json

from .catalog import build_manifest, tree_hash
from .contracts import GATE_REASONS, INCOMPLETE_GATES, Refused, digest, request_goal, tag
from .persistence import atomic_json, read_checkpoint, write_checkpoint

SCHEMA = "workflow.recovery/1"
# UC-15-resume-r2: limits.max_resume_attempts is the AUTOMATIC-resume limit.
MAX_AUTOMATIC_RESUMES = 2
TRANSIENT_CLASSES = frozenset({"connection_reset", "timeout"})
INITIATORS = ("automatic", "operator_ledger", "evidenced_repair")
# v2_5.resume_verification: an accepted resume must cover these items (and does: see verify).
REQUIRED_ITEMS = ("request", "source", "build_runtime", "artifacts", "evidence")
INTEGRITY_ITEMS = ("admission", "evidence", "source", "build_runtime", "request", "artifacts")
NOTIFICATION_CHANNEL = "run_outbox"
NOTIFICATION_DIR = "notifications"
REQUEST_FIELDS = frozenset(
    {"action", "initiated_by", "operator_record_id", "requested_by", "run_id"}
)
OVERRIDE_FIELDS = ("overrides", "budget_override")
GOVERNING_LIMIT = (
    "original run limits are immutable on resume (max_model_usd, max_total_seconds, "
    "max_repair_attempts, max_resume_attempts); consumed counters are never reset"
)
# Closed mechanical fix vocabulary: action -> executor(controller, diagnosis) -> SUCCESS|FAIL.
# Empty under the kernel-only lock (D31): no fault this SUT can exhibit has an
# authority-preserving machine fix. Never extended at runtime.
REPAIR_ACTIONS = {}
# Fixed diagnosis vocabulary, keyed by the latest interruption's failure class.
DIAGNOSES = {
    "write_denied": {
        "cause": "write_denied",
        "mechanical": True,
        "fix_action": "grant_write_access",
        "fix_widens_authority": True,  # a write grant beyond the authorized output scope
    },
    "connection_reset": {
        "cause": "repeated_transient_interruption",
        "mechanical": False,
        "fix_action": "operator_resume",
        "fix_widens_authority": True,  # beyond the automatic-resume limit
    },
    "timeout": {
        "cause": "repeated_transient_interruption",
        "mechanical": False,
        "fix_action": "operator_resume",
        "fix_widens_authority": True,
    },
}
UNCLASSIFIED_DIAGNOSIS = {
    "cause": "unclassified_failure",
    "mechanical": False,
    "fix_action": "operator_investigation",
    "fix_widens_authority": True,
}


class Supervisor:
    """Opaque capability: only the controller's own run loop may initiate automatic resumes."""

    __slots__ = ()


SUPERVISOR = Supervisor()


def new_ledger():
    return {
        "schema": SCHEMA,
        "interruptions": [],
        "resume": [],
        "diagnoses": [],
        "mechanical_repairs": [],
        "operator_notifications": [],
        "transport_retries": [],
        "counters": {
            "automatic_resumes_used": 0,
            "operator_resumes_used": 0,
            "evidenced_repair_resumes_used": 0,
            "unattributed_resumes_used": 0,
            "resume_attempts_used": 0,
        },
        "limits": {"max_automatic_resumes": MAX_AUTOMATIC_RESUMES},
        "stop": None,
    }


def ledger(context):
    return context.setdefault("recovery", new_ledger())


def detached(value):
    return json.loads(json.dumps(value))


# ------------------------------------------------------------------ classification


def classify(exc):
    """failure_class from the exception TYPE only; None for a controller decision."""
    from .contracts import NeedsInfo

    if isinstance(exc, (Refused, NeedsInfo)):
        return None
    if isinstance(exc, (ConnectionResetError, ConnectionAbortedError)):
        return "connection_reset"
    if isinstance(exc, TimeoutError):
        return "timeout"
    if isinstance(exc, PermissionError):
        return "write_denied"
    return "unclassified"


def pending_name(pending):
    if not pending:
        return None
    for key in ("skill", "role", "kind"):
        if isinstance(pending.get(key), str) and pending[key]:
            return pending[key]
    return None


def exception_facts(exc):
    """Observed attributes of the exception object (never its message text)."""
    facts = {"error_type": type(exc).__name__}
    if isinstance(exc, OSError):
        facts["errno"] = exc.errno if isinstance(exc.errno, int) else None
        facts["filename"] = exc.filename if isinstance(exc.filename, str) else None
    return facts


# ------------------------------------------------------------------ durable record helpers


def _append(controller, name, kind, record):
    """Append to a ledger list with its dispatch sequence, then checkpoint (atomic)."""
    records = ledger(controller.context)[name]
    record["index"] = len(records)
    event = controller.dispatch_event(kind, **{"record": detached(record)})
    record["sequence"] = event["sequence"] if event else None
    records.append(record)
    controller.checkpoint()
    return record


def _payload_event(root, value, kind, **details):
    """dispatch_event on a checkpoint payload (no live controller: refused resume)."""
    context = value["context"].get("dispatch_evidence")
    if context is None:
        return None
    context["sequence"] += 1
    record = {
        "schema": context["schema"],
        "run_id": context["run_id"],
        "sequence": context["sequence"],
        "resume_phase": value["resumes"],
        "kind": kind,
        **details,
    }
    name = f"dispatch-event-{context['sequence']:06}.json"
    atomic_json(root / name, record)
    value["evidence"][name] = tree_hash(root / name)
    return record


def record_interruption(controller, exc):
    """Interruption ledger entry, written at failure time from the classified exception."""
    durable = read_checkpoint(controller.root)
    failure_class = classify(exc)
    record = {
        "failure_class": failure_class,
        "at_state": durable["state"],
        "pending_operation": pending_name(durable["pending"]),
        "pending_record": detached(durable["pending"]),
        "transient": failure_class in TRANSIENT_CLASSES,
        **exception_facts(exc),
    }
    return _append(controller, "interruptions", "interruption", record)


def latest_unanswered(context):
    """The latest interruption, unless a resume decision already answered it."""
    book = ledger(context)
    if not book["interruptions"]:
        return None
    latest = book["interruptions"][-1]
    if any(r.get("interruption_index") == latest["index"] for r in book["resume"]):
        return None
    return latest


# ------------------------------------------------------------------ itemized verification


def _item(results, item, failed, refusal):
    results.append({"item": item, "failed": failed, "refusal": refusal})


def verify(controller, stop_at_first=False):
    """Itemized re-verification of the controller's bound inputs (integrity()).

    Returns {"checked_items", "failed_items", "refusals"}; each failed item names exactly
    what differed (e.g. ``artifact:onset``, ``evidence:request.json``). The same checks and
    gate codes integrity() always raised, evaluated one item at a time.
    """
    from .admission import identity as admission_identity
    from .admission import record_for

    c = controller
    bound = bool(c.binding)
    checked, failed, refusals = [], [], []

    def done():
        return stop_at_first and refusals

    def fail(name, refusal):
        failed.append(name)
        refusals.append(refusal)

    def guarded(gate, fn):
        try:
            return fn(), None
        except Refused as exc:
            return None, tag(exc, gate)

    checked.append("admission")
    changed, error = guarded(
        "source_check",
        lambda: (
            record_for(c.admission, c.source) != c.admission_record
            or admission_identity(c.admission_record) != c.admission_hash
        ),
    )
    if error is not None:
        fail("admission", error)
    elif changed:
        fail(
            "admission",
            Refused(
                "source admission policy changed",
                gate="review_binding" if bound else "source_check",
                reason="approval_invalidated" if bound else None,
            ),
        )
    if done():
        return {"checked_items": checked, "failed_items": failed, "refusals": refusals}
    checked.append("evidence")
    for name, expected in c.evidence.items():
        observed, error = guarded("gate_evidence", lambda n=name: tree_hash(c.root / n))
        if error is not None:
            fail("evidence:" + name, error)
        elif observed != expected:
            fail(
                "evidence:" + name,
                Refused(
                    "controller evidence changed: " + name,
                    gate="gate_evidence",
                    reason="artifact_identity_mismatch",
                ),
            )
        if done():
            return {"checked_items": checked, "failed_items": failed, "refusals": refusals}
    checked.append("source")
    changed, error = guarded("source_check", lambda: tree_hash(c.source) != c.source_hash)
    if error is not None:
        fail("source", error)
    elif changed:
        fail(
            "source",
            Refused(
                "source changed since admission",
                gate="review_binding" if bound else "source_check",
                reason="approval_invalidated" if bound else None,
            ),
        )
    if done():
        return {"checked_items": checked, "failed_items": failed, "refusals": refusals}
    checked.append("build_runtime")
    build_gate = "review_binding" if bound else "plan_validation"
    manifest = _runner_attr("build_manifest", build_manifest)
    changed, error = guarded(build_gate, lambda: digest(manifest(c.repo)) != c.build_hash)
    if error is not None:
        fail("build_runtime", error)
    elif changed:
        fail(
            "build_runtime",
            Refused(
                "code/documentation changed since admission; review is stale",
                gate=build_gate,
                reason="approval_invalidated" if bound else None,
            ),
        )
    if done():
        return {"checked_items": checked, "failed_items": failed, "refusals": refusals}
    checked.append("request")
    changed, error = guarded(
        "authorization",
        lambda: digest(c.request) != c.request_hash or request_goal(c.request) != c.goal,
    )
    if error is not None:
        fail("request", error)
    elif changed:
        fail(
            "request",
            Refused(
                "request/authorization changed since admission",
                gate="review_binding" if bound else "authorization",
                reason="approval_invalidated" if bound else None,
            ),
        )
    if done():
        return {"checked_items": checked, "failed_items": failed, "refusals": refusals}
    checked.append("artifacts")
    for name, value in c.artifacts.items():
        observed, error = guarded("gate_evidence", lambda n=name: tree_hash(c.paths[n]))
        if error is not None:
            fail("artifact:" + name, error)
        elif observed != value:
            fail(
                "artifact:" + name,
                Refused(
                    f"artifact changed since its producing step: {name}",
                    gate="gate_evidence",
                    reason="artifact_identity_mismatch",
                ),
            )
        if done():
            return {"checked_items": checked, "failed_items": failed, "refusals": refusals}
    checked.append("binding")
    if c.binding and c.current_binding() != c.binding:
        fail(
            "binding",
            Refused(
                "plan or review binding changed", gate="review_binding", reason="stale_binding"
            ),
        )
    return {"checked_items": checked, "failed_items": failed, "refusals": refusals}


def _runner_attr(name, default):
    # Tests and the CLI patch runner.build_manifest; verification must see the same function.
    from . import runner

    return getattr(runner, name, default)


# ------------------------------------------------------------------ resume requests


def parse_request(request, supervisor=None):
    """Normalize a resume request. Returns (attribution, override, problems)."""
    if request is None:
        return {"initiated_by": None, "operator_record_id": None, "requested_by": None}, None, []
    problems = []
    if not isinstance(request, dict):
        return (
            {"initiated_by": None, "operator_record_id": None, "requested_by": None},
            None,
            ["request:not_an_object"],
        )
    initiated = request.get("initiated_by")
    if initiated is not None and initiated not in INITIATORS:
        problems.append("attribution:unknown_initiator")
        initiated = None
    if initiated == "automatic" and supervisor is not SUPERVISOR:
        problems.append("attribution:automatic_not_supervised")
    if request.get("action", "resume") != "resume":
        problems.append("request:action")
    record_id = request.get("operator_record_id")
    if record_id is not None and (not isinstance(record_id, str) or not record_id):
        problems.append("attribution:operator_record_id")
        record_id = None
    override = {}
    for field in OVERRIDE_FIELDS:
        if field in request:
            value = request[field]
            if isinstance(value, dict):
                override.update({k: v for k, v in value.items() if k != "justification"})
            else:
                override[field] = value
    for field in sorted(set(request) - REQUEST_FIELDS - set(OVERRIDE_FIELDS)):
        override[field] = request[field]  # an unrecognized field is a request to change state
    requested_by = request.get("requested_by")
    return (
        {
            "initiated_by": initiated,
            "operator_record_id": record_id,
            "requested_by": requested_by if isinstance(requested_by, str) else None,
        },
        override or None,
        problems,
    )


def new_decision(request, attribution, override, interruption):
    record = {
        "result": None,
        "verification": {"verdict": None, "checked_items": [], "failed_items": []},
        "budget_override_applied": False,  # an override is never applied
        "initiated_by": attribution["initiated_by"],
        "override_requested": override is not None,
        "operator_record_id": attribution["operator_record_id"],
        "interruption_index": interruption["index"] if interruption else None,
        "requested_by": attribution["requested_by"],
    }
    if override is not None:
        record["refusal_record"] = {
            "request": detached(request),
            "illegal_fields": sorted(override),
            "governing_limit": GOVERNING_LIMIT,
        }
    return record


def operator_authorization(controller):
    """The accepted user resume record_id the operator channel binds to this resume, or None.

    Uses the operator channel's stable hook (Controller.operator_resume_authorization, which
    reads the ledger at the before_resume boundary and hands each accepted record out once).
    Returns (record_id, problem); an absent channel accepts no operator resume.
    """
    authorize = getattr(controller, "operator_resume_authorization", None)
    if authorize is None:
        return None, "operator_record:ledger_unavailable"
    record_id = authorize()
    # False = no ledger was opened for this run (unavailable), None = consulted, none accepted.
    if record_id is False:
        return None, "operator_record:ledger_unavailable"
    if not isinstance(record_id, str) or not record_id:
        return None, "operator_record:none_accepted"
    return record_id, None


def check_operator_record(controller, record_id):
    """An operator_ledger request binds exactly the accepted record the channel hands out."""
    bound, problem = operator_authorization(controller)
    if problem:
        return None, problem
    if record_id is not None and record_id != bound:
        return None, "operator_record:" + record_id
    return bound, None


def transition_request(state, request, result, reasons):
    """controller_trace.transition_requests entry for an operator resume request (E10/E11):
    the result is the controller's own resume decision, never the requester's claim."""
    return {
        "request": "resume",
        "from_state": state,
        "to_state": request.get("to_state"),
        "requested_by": request.get("requested_by"),
        "result": "ACCEPTED" if result == "ACCEPTED" else "REJECTED",
        "requested_via": "resume_request",
        "reasons": list(reasons),
        **({"run_id": request["run_id"]} if "run_id" in request else {}),
    }


def is_operator_request(request):
    return isinstance(request, dict) and request.get("requested_by") == "operator"


def check_repair_binding(context, interruption):
    """evidenced_repair: the LATEST diagnosis of the interruption and its LATEST repair."""
    book = ledger(context)
    if interruption is None:
        return "repair_binding:no_interruption"
    diagnoses = [
        d for d in book["diagnoses"] if interruption["index"] in d.get("interruption_indexes", [])
    ]
    if not diagnoses:
        return "repair_binding:no_diagnosis"
    latest = diagnoses[-1]
    if not evidenced_mechanical(latest):
        return "repair_binding:diagnosis_not_mechanical"
    repairs = [r for r in book["mechanical_repairs"] if r["diagnosis_index"] == latest["index"]]
    if not repairs:
        return "repair_binding:no_repair"
    repair = repairs[-1]
    if not (
        repair["result"] == "SUCCESS"
        and repair["action"] == latest["fix_action"]
        and repair["widens_authority"] is False
    ):
        return "repair_binding:repair_not_success"
    return None


def evidenced_mechanical(diagnosis):
    evidence = diagnosis.get("evidence")
    return (
        diagnosis.get("mechanical") is True
        and diagnosis.get("evidenced") is True
        and diagnosis.get("fix_widens_authority") is False
        and isinstance(evidence, list)
        and bool(evidence)
        and all(isinstance(x, str) and x.strip() for x in evidence)
    )


def refusal_terminal(refusal):
    gate = refusal.gate or "resume_verification"
    outcome = refusal.outcome or ("INCOMPLETE" if gate in INCOMPLETE_GATES else "BLOCKED")
    reason = refusal.reason or GATE_REASONS.get(gate, gate + "_failed")
    return {"outcome": outcome, "reason": reason, "failed_gate": gate}


def record_refused(root, value, decision, refusal, request=None):
    """REFUSED decision + terminal record, written durably on the checkpoint before raising."""
    book = ledger(value["context"])
    decision["index"] = len(book["resume"])
    decision["result"] = "REFUSED"
    event = _payload_event(root, value, "resume", record=detached(decision))
    decision["sequence"] = event["sequence"] if event else None
    book["resume"].append(decision)
    if is_operator_request(request):
        failed = decision["verification"]["failed_items"]
        reasons = failed or ["override_refused" if decision["override_requested"] else "refused"]
        record = transition_request(value["state"], request, "REJECTED", reasons)
        requests = value["context"].setdefault("transition_requests", [])
        record["sequence"] = len(requests) + 1
        event = _payload_event(root, value, "transition_request", transition_request=dict(record))
        record["event_seq"] = event["sequence"] if event else None
        requests.append(record)
    terminal = refusal_terminal(refusal)
    mapping = {"source": "resume_refusal", "resume_index": decision["index"]}
    value["context"]["terminal"] = {**terminal, "mapping": mapping}
    _payload_event(root, value, "terminal", terminal=terminal, terminal_mapping=mapping)
    write_checkpoint(root, value)
    atomic_json(
        root / "result.json",
        {
            "status": "INCOMPLETE",
            "state": value["state"],
            "error": str(refusal),
            "stop_kind": "resume_refused",
            "terminal": terminal,
            "terminal_mapping": mapping,
        },
    )


def record_accepted(controller, decision, request=None):
    book = ledger(controller.context)
    counters = book["counters"]
    key = {
        "automatic": "automatic_resumes_used",
        "operator_ledger": "operator_resumes_used",
        "evidenced_repair": "evidenced_repair_resumes_used",
    }.get(decision["initiated_by"], "unattributed_resumes_used")
    counters[key] += 1
    counters["resume_attempts_used"] += 1
    book["stop"] = None
    decision["index"] = len(book["resume"])
    decision["result"] = "ACCEPTED"
    event = controller.dispatch_event("resume", record=detached(decision))
    decision["sequence"] = event["sequence"] if event else None
    book["resume"].append(decision)
    if is_operator_request(request):
        record = transition_request(controller.state, request, "ACCEPTED", [])
        requests = controller.context.setdefault("transition_requests", [])
        record["sequence"] = len(requests) + 1
        event = controller.dispatch_event("transition_request", transition_request=dict(record))
        record["event_seq"] = event["sequence"] if event else None
        requests.append(record)
    controller.checkpoint()
    return decision


# ------------------------------------------------------------------ supervisor


def automatic_eligible(controller, interruption):
    book = ledger(controller.context)
    durable = read_checkpoint(controller.root)
    return (
        interruption["failure_class"] in TRANSIENT_CLASSES
        and durable["pending"] is None
        and controller.pending is None
        and durable["state"] != "RELEASED"
        and book["counters"]["automatic_resumes_used"] < MAX_AUTOMATIC_RESUMES
    )


def needs_diagnosis(controller, interruption):
    book = ledger(controller.context)
    exhausted = (
        interruption["failure_class"] in TRANSIENT_CLASSES
        and book["counters"]["automatic_resumes_used"] >= MAX_AUTOMATIC_RESUMES
    )
    return exhausted or len(book["interruptions"]) >= 2


def diagnose(controller):
    """Deterministic diagnosis from recorded interruption facts only."""
    book = ledger(controller.context)
    interruptions = book["interruptions"]
    latest = interruptions[-1]
    rule = DIAGNOSES.get(latest["failure_class"], UNCLASSIFIED_DIAGNOSIS)
    evidence = []
    for item in interruptions:
        evidence.append(
            f"interruption {item['index']}: failure_class={item['failure_class']}, "
            f"at_state={item['at_state']}, pending_operation={item['pending_operation']}, "
            f"error_type={item['error_type']}"
        )
    if latest.get("errno") is not None:
        evidence.append(f"interruption {latest['index']}: errno={latest['errno']}")
    if latest.get("filename"):
        evidence.append(f"interruption {latest['index']}: filename={latest['filename']}")
    record = {
        "interruption_indexes": [i["index"] for i in interruptions],
        "cause": rule["cause"],
        "mechanical": rule["mechanical"],
        "evidenced": bool(evidence) and all(e.strip() for e in evidence),
        "evidence": evidence,
        "fix_action": rule["fix_action"],
        "fix_widens_authority": rule["fix_widens_authority"],
    }
    return _append(controller, "diagnoses", "diagnosis", record)


def execute_repair(controller, diagnosis_index):
    """Bounded mechanical repair of the LATEST diagnosis; None when no repair may run."""
    book = ledger(controller.context)
    if not book["diagnoses"] or book["diagnoses"][-1]["index"] != diagnosis_index:
        raise Refused("mechanical repair must act on the latest diagnosis", gate="interrupted")
    diagnosis = book["diagnoses"][-1]
    if not evidenced_mechanical(diagnosis):
        return None  # widening, non-mechanical or unevidenced: never repaired by machine
    executor = REPAIR_ACTIONS.get(diagnosis["fix_action"])
    if executor is None:
        return None  # outside the closed vocabulary (empty under D31): notify instead
    try:
        result = executor(controller, copy.deepcopy(diagnosis))
    except Exception:
        result = "FAIL"
    record = {
        "diagnosis_index": diagnosis["index"],
        "action": diagnosis["fix_action"],
        "widens_authority": False,
        "result": "SUCCESS" if result == "SUCCESS" else "FAIL",
    }
    return _append(controller, "mechanical_repairs", "mechanical_repair", record)


def notify(controller, diagnosis):
    """recorded -> emitted (run-local outbox) -> after_operator_notification; at most once
    per diagnosis, so a duplicate or late notification is never double-counted."""
    book = ledger(controller.context)
    for existing in book["operator_notifications"]:
        if existing["diagnosis_index"] == diagnosis["index"]:
            return existing
    record = {
        "channel": NOTIFICATION_CHANNEL,
        "stop_record": {
            "failure_classes": [i["failure_class"] for i in book["interruptions"]],
            "interruption_indexes": [i["index"] for i in book["interruptions"]],
            "at_state": book["interruptions"][-1]["at_state"],
        },
        "diagnosis_index": diagnosis["index"],
        "recorded": True,
        "emitted": False,
        "delivery": None,
    }
    record = _append(controller, "operator_notifications", "operator_notification", record)
    name = f"{NOTIFICATION_DIR}/notification-{record['index']:03}.json"
    message = {
        "schema": "workflow.operator-notification/1",
        "run_root": str(controller.root),
        "notification": detached(record),
        "diagnosis": detached(diagnosis),
        "action_required": "operator decision; this run waits and starts nothing further",
    }
    try:
        (controller.root / NOTIFICATION_DIR).mkdir(exist_ok=True)
        controller.save(name, message)
        delivery = {"status": "DELIVERED", "path": name, "sha256": controller.evidence[name]}
    except OSError as exc:
        delivery = {"status": "FAILED", "path": name, "error_type": type(exc).__name__}
    record["delivery"] = delivery
    record["emitted"] = delivery["status"] == "DELIVERED"
    controller.dispatch_event(
        "operator_notification_emitted",
        index=record["index"],
        emitted=record["emitted"],
        delivery=detached(delivery),
    )
    book["stop"] = {"kind": "notify_and_wait", "notification_index": record["index"]}
    controller.checkpoint()
    if not record["emitted"]:
        return record
    try:
        occurrence = sum(
            1 for n in book["operator_notifications"] if n["recorded"] and n["emitted"]
        )
        controller.emit(
            "after_operator_notification",
            {
                "target": "operator_notification",
                "occurrence": occurrence,
                "recorded": True,
                "emitted": True,
                "notification": detached(record),
            },
        )
    finally:
        # The operator channel's wait boundary, read only after recorded+emitted are durable.
        consult = getattr(controller, "consult_operator_ledger", None)
        if consult is not None:
            consult("operator_notification")
    return record


def record_transport_retry(controller, role, exc, invocation_id=None):
    """One durable retry record per attempt; refused after RELEASED (AR25-012)."""
    if controller.state == "RELEASED" or (
        (controller.context.get("terminal") or {}).get("outcome") == "RELEASED"
    ):
        return False
    pending = controller.pending or {}
    if pending.get("kind") != "model" or pending.get("role") != role:
        return False
    book = ledger(controller.context)
    attempt_id = pending.get("attempt_id")
    context = getattr(controller.roles, "dispatch_context", None) or {}
    if not isinstance(invocation_id, str) or not invocation_id:
        return False  # an unidentified invocation cannot be bound to its retry
    if any(r["invocation_id"] == invocation_id for r in book["transport_retries"]):
        return False  # one retry per invocation, never a second
    record = {
        "step": role,
        "attempt": sum(r["step"] == role for r in book["transport_retries"]) + 1,
        "before_write": True,
        "attempt_id": attempt_id,
        "occurrence": context.get("occurrence"),
        "invocation_id": invocation_id,
        "input_hash": pending.get("input_hash"),
        "failure_class": classify(exc),
        "error_type": type(exc).__name__,
    }
    _append(controller, "transport_retries", "transport_retry", record)
    return True


def transport_call(roles, role, call, invocation_id=None):
    """Retry once, inside the same attempt, on a classified PRE-WRITE transport fault.

    Pre-write is observed, not assumed: the process manager's launch serial did not move,
    so no child was started and nothing was read or written. A fault after launch, a
    second fault, or a fault the controller cannot record durably is never retried.
    """
    processes = roles.processes
    before = getattr(processes, "serial", None)
    try:
        return call()
    except (ConnectionResetError, ConnectionAbortedError) as exc:
        if type(before) is not int or getattr(processes, "serial", None) != before:
            raise
        recorder = getattr(roles, "transport_retry_recorder", None)
        if recorder is None or not recorder(role, exc, invocation_id):
            raise
    return call()
