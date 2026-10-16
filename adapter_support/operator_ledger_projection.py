"""Transition requests and literal operator-ledger records (frozen v2.7, owner ruling D4).

Three D4-gated capabilities share the controller's attributed operator channel
(tools/workflow/operator_ledger.py, D14/D20/D28/D30):

* CAP-TRANSITION-REQUEST (submit_transition_request): a request goes to the controller's own
  transition interface (Controller.request_transition -> operator_ledger.
  submit_transition_request), which records ACCEPTED | REJECTED. The projection reads that
  record back, checked against its evidence file and its dispatch event.
* CAP-LEDGER-APPEND (append_ledger_record, FX-LEDGER-APPEND) and CAP-OPERATOR-RECORD-SETUP
  (append_attributed_operator_record, FX-OPERATOR-RECORD): the harness appends one literal
  attributed record to the run-scoped ledger the controller created. Delivery is the native
  InjectionHooks._ledger_fixture (the same code the case schedule runs). Delivery confers no
  acceptance: whether a record was accepted is only ever the controller's own decision
  (controller_trace.operator_actions, CAP-OPERATOR-ACTIONS).

Frozen shapes (casesets/cached-onset-e2e-v2_7/EVIDENCE_CONTRACT.json structures):

* controller_trace: "transition_requests (LIST {request, from_state, to_state, requested_by,
  result: ACCEPTED|REJECTED})".
* v2_4.operator_actions: the decision on each ledger record (projected by
  pre_review_projection.operator_actions; this module only links ledger lines to it).

Nothing is synthesized. A list is emitted (possibly empty) only when its native producer ran;
without an operator channel the transition-request stream and the ledger records are MISSING
with a reason, never []. Inconsistent native records raise EvidenceMappingError.
"""

import copy
import hashlib
import json
from pathlib import Path

from adapter_support.evidence_projection import EvidenceMappingError
from tools.workflow.e2e_injections import InjectionHooks, UnsupportedInjection

TRANSITION_CAPABILITY = "CAP-TRANSITION-REQUEST"
LEDGER_APPEND_CAPABILITY = "CAP-LEDGER-APPEND"
OPERATOR_RECORD_CAPABILITY = "CAP-OPERATOR-RECORD-SETUP"
# EC:controller_trace transition_requests field set, in contract order.
TRANSITION_FIELDS = ("request", "from_state", "to_state", "requested_by", "result")
TRANSITION_RESULTS = ("ACCEPTED", "REJECTED")
# Controller-recorded context kept beside the frozen fields (never classified here).
TRANSITION_CONTEXT = ("requested_via", "reasons", "claimed_from_state")
LEDGER_OPERATIONS = {
    "FX-LEDGER-APPEND": LEDGER_APPEND_CAPABILITY,
    "FX-OPERATOR-RECORD": OPERATOR_RECORD_CAPABILITY,
}
NO_CHANNEL = (
    "no operator channel was opened for this run (Controller operator_ledger_path not "
    "supplied), so the controller's transition interface and ledger were never observed"
)


class DeliveryRefused(ValueError):
    """A delivery this harness cannot faithfully make; nothing was delivered."""


def _sha(data):
    return hashlib.sha256(data).hexdigest()


# --------------------------------------------------------------------------- delivery


def submit_transition_request(controller, payload):
    """CAP-TRANSITION-REQUEST delivery: the request goes to the controller's own transition
    interface; the returned record is the controller's (a detached copy), never this harness's.

    Refused before delivery, exactly as InjectionHooks refuses them in a case schedule:
    a budget_override payload (e2e_injections.unsupported_requirements: "budget-override
    request channel unavailable") and an operator resume request without a role channel
    (InjectionHooks keeps that for CAP-RESUME, it is not a transition request).
    """
    if not isinstance(payload, dict):
        raise DeliveryRefused("a transition request payload must be an object")
    if "budget_override" in payload:
        raise DeliveryRefused("budget-override request channel unavailable")
    if payload.get("action") == "resume" and "requested_by_role_channel" not in payload:
        raise DeliveryRefused("an operator resume request is CAP-RESUME, not a transition request")
    submit = getattr(controller, "request_transition", None)
    if not callable(submit):
        raise DeliveryRefused("controller exposes no transition request interface")
    return copy.deepcopy(submit(copy.deepcopy(payload)))


def append_literal_record(controller, operation, args, disposable_root, boundary, entry_id):
    """FX-LEDGER-APPEND / FX-OPERATOR-RECORD at a live controller boundary.

    Delegates to the native producer (InjectionHooks._ledger_fixture), so the record written is
    byte-for-byte what a case schedule would write: FX-LEDGER-APPEND {record_id
    "FX-LEDGER-APPEND:<entry_id>", speaker, text} (record_id is the only harness-assigned
    field); FX-OPERATOR-RECORD the five literal fields. Returns the delivery record {target,
    record_id, record_hash, before_hash, after_hash, assigned_fields, boundary}; it carries no
    acceptance. The ledger must be the controller's run-scoped channel file inside
    ``disposable_root``. ``boundary`` names the live hook; the pre-capture request-message path
    (before_run) is CAP-LEDGER-SEED territory and is refused here.
    """
    if operation not in LEDGER_OPERATIONS:
        raise DeliveryRefused(f"not a literal ledger operation: {operation!r}")
    if not isinstance(boundary, str) or not boundary or boundary == "before_run":
        raise DeliveryRefused("a literal ledger record is appended at a named live boundary")
    if not isinstance(entry_id, str) or not entry_id:
        raise DeliveryRefused("a nonempty schedule entry id names the appended record")
    if not isinstance(args, dict):
        raise DeliveryRefused("ledger record arguments must be an object")
    hooks = InjectionHooks({"id": "literal-ledger-record"}, disposable_root=disposable_root)
    record = {"index": 0, "id": entry_id, "hook": boundary, "operation": "mutate_fixture"}
    try:
        hooks._ledger_fixture(operation, copy.deepcopy(args), {}, controller, record)
    except UnsupportedInjection as exc:
        raise DeliveryRefused(str(exc)) from exc
    return {
        "operation": operation,
        "boundary": boundary,
        **{
            key: record[key]
            for key in (
                "target",
                "record_id",
                "record_hash",
                "before_hash",
                "after_hash",
                "assigned_fields",
            )
        },
    }


def operator_run_id_symbol(hooks, run_id):
    """HARNESS_RUN_ID for the case schedule: the run id the harness hands the controller as
    operator_run_id (the controller binds it to its channel and checks each record's run_id
    against it). Set before any boundary is dispatched."""
    injection = getattr(hooks, "injection", hooks)
    if not isinstance(getattr(injection, "symbols", None), dict):
        raise DeliveryRefused("schedule hooks carry no symbol table")
    if injection.symbols.get("HARNESS_RUN_ID", run_id) != run_id:
        raise DeliveryRefused("HARNESS_RUN_ID is already bound to another run id")
    injection.symbols["HARNESS_RUN_ID"] = run_id


# --------------------------------------------------------------------------- projection


def project(projection, trace):
    """controller_trace.transition_requests (or MISSING) plus the ledger-records note."""
    requests = transition_requests(projection)
    if requests is not None:
        trace["transition_requests"] = requests
    records = ledger_records(projection)
    if records is not None:
        projection.notes.append({"operator_ledger_records": records})


def transition_requests(projection):
    """The controller's transition-request records, each checked against its evidence file
    (transition-request-NNN.json) and its transition_request dispatch event; None (MISSING)
    when the controller never opened its transition interface."""
    native = projection.n
    context = native.context
    if "transition_requests" not in context:
        if native.kinds("transition_request"):
            raise EvidenceMappingError("transition_request events without checkpoint records")
        projection.gap("controller_trace.transition_requests", TRANSITION_CAPABILITY, NO_CHANNEL)
        return None
    records = context["transition_requests"]
    if not isinstance(records, list):
        raise EvidenceMappingError("native transition_requests is not a list")
    events = native.kinds("transition_request")
    if len(events) != len(records):
        raise EvidenceMappingError("transition_request events differ from the checkpoint")
    out = []
    for number, (event, record) in enumerate(zip(events, records, strict=True), 1):
        if not isinstance(record, dict) or record.get("sequence") != number:
            raise EvidenceMappingError("native transition request identity is malformed")
        if projection.read_native(f"transition-request-{number:03}.json") != record:
            raise EvidenceMappingError("transition-request file differs from the checkpoint")
        if event["sequence"] != record.get("event_seq") or event.get("transition_request") != {
            k: v for k, v in record.items() if k != "event_seq"
        }:
            raise EvidenceMappingError("transition_request event differs from its record")
        if record.get("result") not in TRANSITION_RESULTS:
            raise EvidenceMappingError("transition request result outside ACCEPTED|REJECTED")
        value = {}
        for field in TRANSITION_FIELDS:
            if field in record:
                value[field] = copy.deepcopy(record[field])
            else:
                projection.gap(
                    f"controller_trace.transition_requests[{number - 1}].{field}",
                    TRANSITION_CAPABILITY,
                    f"the controller recorded no {field} for this request",
                )
        for field in TRANSITION_CONTEXT:
            if field in record:
                value[field] = copy.deepcopy(record[field])
        projection.seq[f"controller_trace.transition_requests[{number - 1}]"] = event["sequence"]
        out.append(value)
    return out


def ledger_records(projection):
    """Every complete line the controller consumed from its run-scoped ledger, linked to the
    controller's decision on it (operator action or identity-less rejection, matched by the
    line hash the controller recorded). None (MISSING) without a channel, or when the ledger
    file cannot be read at projection.

    Refuses the projection when the consumed prefix no longer hashes to the controller's
    recorded prefix_hash (the ledger was edited after it was read) or when an operator_ledger
    decision names a line hash that is not in the consumed prefix (a decision on a record the
    ledger never held).
    """
    context = projection.n.context
    channel = context.get("operator_channel")
    if not isinstance(channel, dict):
        for capability in (LEDGER_APPEND_CAPABILITY, OPERATOR_RECORD_CAPABILITY):
            projection.gap("notes.operator_ledger_records", capability, NO_CHANNEL)
        return None
    try:
        data = Path(channel["ledger_path"]).read_bytes()
    except (OSError, KeyError, TypeError):
        for capability in (LEDGER_APPEND_CAPABILITY, OPERATOR_RECORD_CAPABILITY):
            projection.gap(
                "notes.operator_ledger_records",
                capability,
                "the operator ledger file was unreadable at projection",
            )
        return None
    consumed = channel.get("consumed_bytes")
    if (
        not isinstance(consumed, int)
        or consumed > len(data)
        or _sha(data[:consumed]) != channel.get("prefix_hash")
    ):
        raise EvidenceMappingError("operator ledger changed after the controller read it")
    lines = data[:consumed].split(b"\n")[:-1] if consumed else []
    decided = {}
    for action in context.get("operator_actions", []):
        decision = action.get("decision") if isinstance(action, dict) else None
        if action.get("source") == "operator_ledger" and isinstance(decision, dict):
            decided[decision.get("record_hash")] = {
                "operator_action": action.get("record_id"),
                "accepted": action.get("accepted"),
            }
    for rejection in channel.get("ledger_rejections", []):
        decided[rejection.get("record_hash")] = {
            "ledger_rejection": rejection.get("sequence"),
            "reasons": list(rejection.get("reasons", [])),
        }
    out, hashes = [], set()
    for number, line in enumerate(lines, 1):
        line_hash = _sha(line)
        hashes.add(line_hash)
        try:
            value = json.loads(line.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            value = None
        entry = {"line": number, "record_hash": line_hash}
        if isinstance(value, dict):
            for key in ("record_id", "speaker", "action"):
                if key in value:
                    entry[key] = value[key]
            entry["carries_run_id"] = "run_id" in value
        else:
            entry["malformed"] = True
        entry["controller_decision"] = decided.get(line_hash)
        out.append(entry)
    orphans = sorted(h for h in decided if h not in hashes)
    if orphans:
        raise EvidenceMappingError("an operator-ledger decision names a record the ledger lacks")
    return {
        "ledger_lines_consumed": len(lines),
        "unconsumed_complete_records": data[consumed:].count(b"\n"),
        "records": out,
        "acceptance": "delivery confers no acceptance: controller_decision is the controller's "
        "own operator_actions record (CAP-OPERATOR-ACTIONS)",
    }
