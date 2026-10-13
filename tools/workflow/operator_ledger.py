"""Attributed operator-decision channel (D14 CP-E04, D20 CP-E15, D26, D28 CP-E07, D30 CR-04).

The controller owns an append-only, run-scoped operator ledger, separate from request.json. It
creates the file itself at capture and reads it only at defined wait boundaries. Every claimed
operator action (ledger record, role-channel transition request, artifact text) becomes a durable
decision record; only ``source=operator_ledger`` with ``speaker=user`` may be accepted (P-01: a
machine claiming to act for a human is still a machine).

Frozen shapes (casesets/cached-onset-e2e-v2_7/EVIDENCE_CONTRACT.json):
- structures.v2_4.operator_actions: {record_id, source (operator_ledger | role_channel |
  artifact_text), speaker, action (resume | replan | repair_extension), accepted,
  planner_occurrence (for an accepted replan)}
- controller_trace.transition_requests: {request, from_state, to_state, requested_by,
  result ACCEPTED | REJECTED}

Records are checkpointed in ``controller.context``, each one is also written atomically as its own
evidence file, and a dispatch event keyed by ``record_id`` is emitted when dispatch evidence mode
is on. ``operator_actions`` exists only once the native channel exists or a claim was observed:
an absent ledger never yields a fabricated empty list.
"""

import hashlib
import json
import os
import uuid
from pathlib import Path

from .contracts import Refused

SCHEMA = "workflow.operator-ledger/1"
SOURCES = ("operator_ledger", "role_channel", "artifact_text")
ACTIONS = ("resume", "replan", "repair_extension")
# D30 (CR-04 ruling): an accepted user answer is recorded as a replan action; both frozen
# vocabularies (FX-OPERATOR-RECORD action answer|resume|replan, operator_actions action) are
# then read as written.
LEDGER_ACTIONS = {"answer": "replan", "resume": "resume", "replan": "replan"}
ROLE_CHANNEL_ACTIONS = {
    "resume": "resume",
    "replan": "replan",
    "repair_extension": "repair_extension",
    # A role asking for a repair beyond the allowance claims a repair extension (E07-P1).
    "repair_retry": "repair_extension",
}
RECORD_FIELDS = {"record_id", "speaker", "text"}
OPTIONAL_FIELDS = {"action", "run_id"}
# Wait boundaries and the actions a user record may take there. repair_extension is in the frozen
# vocabulary but no boundary offers it yet, so it is always recorded as rejected.
BOUNDARY_ACTIONS = {
    "needs_info": frozenset({"replan"}),
    "operator_notification": frozenset({"resume"}),
    "before_resume": frozenset({"resume"}),
    "re_review": frozenset({"replan"}),
}
# A record appended at a NEEDS-INFO wait without an action claims to be the answer.
IMPLIED_ACTIONS = {"needs_info": "replan"}


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def serialize(record):
    return (json.dumps(record, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")


def read_record_ids(path):
    """Record ids already in a ledger file (harness-side duplicate check)."""
    ids = []
    for line in Path(path).read_bytes().split(b"\n"):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict):
            ids.append(value.get("record_id"))
    return ids


def append_record(path, record):
    """Append exactly one literal record to the ledger tail; never rewrite earlier bytes."""
    data = serialize(record)
    with Path(path).open("ab") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    return _sha(data)


def open_channel(controller, ledger_path, run_id=None):
    """Constructor wiring: create the run-scoped ledger exclusively and bind it to this run."""
    if ledger_path is None:
        return None
    path = Path(ledger_path).absolute()
    resolved = path.resolve()
    if resolved == controller.source or resolved.is_relative_to(controller.source):
        raise Refused("operator ledger must not live in the source namespace", gate="write_denial")
    if resolved.is_relative_to(controller.outputs) or (
        resolved.parent == controller.root and resolved.name in controller.evidence
    ):
        raise Refused("operator ledger must not alias controller evidence", gate="write_denial")
    if run_id is not None and (not isinstance(run_id, str) or not run_id):
        raise Refused("operator run_id must be a nonempty string", gate="authorization")
    try:
        with resolved.open("xb"):
            pass
    except FileExistsError as exc:
        raise Refused(
            "operator ledger must be created by this run (run-scoped, append-only)",
            gate="authorization",
        ) from exc
    channel = {
        "schema": SCHEMA,
        "ledger_path": str(resolved),
        "run_id": run_id or str(uuid.uuid4()),
        "consumed_bytes": 0,
        "consumed_records": 0,
        "prefix_hash": _sha(b""),
        "consultations": [
            # The controller itself created the ledger empty: zero records is observed.
            {
                "boundary": "capture",
                "records_read": 0,
                "partial_tail": False,
                "state": controller.state,
            }
        ],
        "ledger_rejections": [],
        "integrity_failures": [],
        "accepted_resumes": [],
        "consumed_resumes": [],
    }
    controller.context["operator_channel"] = channel
    controller.context["operator_actions"] = []
    return channel


def _record_ids(controller):
    channel = controller.context.get("operator_channel", {})
    ids = {a["record_id"] for a in controller.context.get("operator_actions", [])}
    ids |= {r["record_id"] for r in channel.get("ledger_rejections", []) if r.get("record_id")}
    return ids


def _commit_action(controller, action, detail):
    """Durable decision: dispatch event (event_seq per record_id), evidence file, checkpoint."""
    actions = controller.context.setdefault("operator_actions", [])
    action["sequence"] = len(actions) + 1
    event = controller.dispatch_event(
        "operator_action", record_id=action["record_id"], operator_action=dict(action), **detail
    )
    action["decision"] = {**detail, "event_seq": event["sequence"] if event else None}
    actions.append(action)
    controller.save(f"operator-action-{action['sequence']:03}.json", action)
    controller.checkpoint()
    return action


def record_claim(controller, source, speaker, action, text, record_id=None, **detail):
    """A claimed operator action from a non-ledger channel: recorded, never accepted."""
    if source not in SOURCES or source == "operator_ledger":
        raise ValueError(f"claim source must be role_channel or artifact_text: {source!r}")
    if record_id is None:
        count = sum(a["source"] == source for a in controller.context.get("operator_actions", []))
        record_id = f"{source}-{count + 1}"
    reasons = ["source_not_operator_ledger"]
    if speaker != "user":
        reasons.append("speaker_not_user")
    if action not in ACTIONS:
        reasons.append("unknown_action")
    entry = {
        "record_id": record_id,
        "source": source,
        "speaker": speaker,
        "action": action,
        "accepted": False,
        "planner_occurrence": None,
    }
    return _commit_action(
        controller,
        entry,
        {"reasons": reasons, "state": controller.state, "text": text, **detail},
    )


def _integrity_failure(controller, boundary, why, observed_bytes):
    channel = controller.context["operator_channel"]
    failure = {
        "boundary": boundary,
        "reason": why,
        "consumed_bytes": channel["consumed_bytes"],
        "observed_bytes": observed_bytes,
        "state": controller.state,
    }
    channel["integrity_failures"].append(failure)
    controller.dispatch_event("operator_ledger_integrity", failure=failure)
    controller.checkpoint()
    raise Refused("operator ledger was " + why + ", not appended", gate="authorization")


def _decide(controller, record, boundary, allowed, accepted_actions, line_hash):
    channel = controller.context["operator_channel"]
    reasons = []
    if not isinstance(record, dict):
        record, reasons = {}, ["malformed_record"]
    record_id = record.get("record_id")
    if not isinstance(record_id, str) or not record_id:
        reasons.append("malformed_record_id")
        record_id = None
    if not RECORD_FIELDS <= set(record) or set(record) - RECORD_FIELDS - OPTIONAL_FIELDS:
        reasons.append("malformed_fields")
    if record_id is not None and record_id in _record_ids(controller):
        reasons.append("duplicate_record_id")
    if "duplicate_record_id" in reasons or record_id is None:
        # Keeps operator_actions identities unique (one decision per record_id).
        rejection = {
            "record_id": record_id,
            "boundary": boundary,
            "reasons": reasons,
            "record_hash": line_hash,
            "sequence": len(channel["ledger_rejections"]) + 1,
        }
        channel["ledger_rejections"].append(rejection)
        controller.dispatch_event("operator_ledger_rejection", rejection=rejection)
        controller.save(f"operator-ledger-rejection-{rejection['sequence']:03}.json", rejection)
        controller.checkpoint()
        return None
    speaker = record.get("speaker")
    if speaker != "user":
        reasons.append("speaker_not_user")
    raw = record.get("action")
    if raw is None and boundary in IMPLIED_ACTIONS:
        action = IMPLIED_ACTIONS[boundary]
    else:
        action = LEDGER_ACTIONS.get(raw) if isinstance(raw, str) else None
        if action is None and raw in ACTIONS:
            action = raw
        if action is None:
            reasons.append("unknown_action")
    if "run_id" not in record:
        reasons.append("run_id_missing")
    elif record["run_id"] != channel["run_id"]:
        reasons.append("run_id_mismatch")
    text = record.get("text")
    if not isinstance(text, str) or not text.strip():
        reasons.append("text_missing")
    if action is not None and action not in allowed:
        reasons.append("action_not_applicable_at_boundary")
    if action in accepted_actions:
        reasons.append("one_action_per_consultation")
    accepted = not reasons
    planner_occurrence = None
    if accepted and action == "replan":
        # The occurrence this accepted replan dispatches, recorded before the dispatch.
        planner_occurrence = 1 + sum(r.get("role") == "planner" for r in controller.roles.receipts)
    entry = {
        "record_id": record_id,
        "source": "operator_ledger",
        "speaker": speaker,
        "action": action,
        "accepted": accepted,
        "planner_occurrence": planner_occurrence,
    }
    detail = {
        "boundary": boundary,
        "reasons": reasons,
        "ledger_action": raw,
        "record_hash": line_hash,
        "state": controller.state,
        "text": text if isinstance(text, str) else None,
    }
    entry = _commit_action(controller, entry, detail)
    if accepted:
        accepted_actions.add(action)
        if action == "resume":
            channel["accepted_resumes"].append(record_id)
            controller.checkpoint()
    return entry


def consult(controller, boundary, allowed=None):
    """Read every complete record appended since the last consultation and decide each one.

    Returns this consultation's decisions (empty when no channel exists or nothing new).
    """
    channel = controller.context.get("operator_channel")
    if channel is None:
        return []
    if boundary not in BOUNDARY_ACTIONS:
        raise ValueError(f"undeclared operator wait boundary: {boundary!r}")
    allowed = BOUNDARY_ACTIONS[boundary] if allowed is None else frozenset(allowed)
    path = Path(channel["ledger_path"])
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        _integrity_failure(controller, boundary, "removed", None)
    consumed = channel["consumed_bytes"]
    if len(data) < consumed or _sha(data[:consumed]) != channel["prefix_hash"]:
        _integrity_failure(controller, boundary, "edited", len(data))
    fresh = data[consumed:]
    end = fresh.rfind(b"\n") + 1
    complete, tail = fresh[:end], fresh[end:]
    lines = complete.split(b"\n")[:-1] if complete else []
    channel["consultations"].append(
        {
            "boundary": boundary,
            "records_read": len(lines),
            "partial_tail": bool(tail),
            "state": controller.state,
        }
    )
    # Advance the consumed prefix before deciding, so a crash never re-decides a record.
    channel["consumed_bytes"] = consumed + len(complete)
    channel["consumed_records"] += len(lines)
    channel["prefix_hash"] = _sha(data[: channel["consumed_bytes"]])
    controller.checkpoint()
    decisions, accepted_actions = [], set()
    for line in lines:
        try:
            record = json.loads(line.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            record = None
        entry = _decide(controller, record, boundary, allowed, accepted_actions, _sha(line))
        if entry is not None:
            decisions.append(entry)
    return decisions


def take_accepted_resume(controller):
    """Before any resume: the earliest accepted user resume record not yet bound to a resume."""
    consult(controller, "before_resume")
    channel = controller.context.get("operator_channel")
    if channel is None:
        return None
    for record_id in channel["accepted_resumes"]:
        if record_id not in channel["consumed_resumes"]:
            channel["consumed_resumes"].append(record_id)
            controller.checkpoint()
            return record_id
    return None


def submit_transition_request(controller, payload):
    """Role-channel transition request entry. Records ACCEPTED | REJECTED; never calls advance().

    No role channel holds transition authority (P-01), and a state transition is decided only by
    the controller's own gates, so every request submitted here is REJECTED and leaves state,
    events, plan and binding unchanged. A request claiming an operator action (resume, replan,
    repair extension) also produces a rejected role_channel operator_actions record.
    """
    before = (controller.state, len(controller.events), controller.plan, controller.binding)
    reasons = []
    if not isinstance(payload, dict):
        payload, reasons = {}, ["malformed_request"]
    role = payload.get("requested_by_role_channel")
    requested_by = payload.get("requested_by")
    if role is not None and requested_by is not None:
        reasons.append("ambiguous_requester")
    if role is not None:
        reasons.append("role_channel_has_no_transition_authority")
    else:
        reasons.append("operator_authority_is_read_only_from_the_operator_ledger")
    name = payload.get("request") or payload.get("action") or "transition"
    requests = controller.context.setdefault("transition_requests", [])
    record = {
        "request": name,
        "from_state": controller.state,
        "to_state": payload.get("to_state"),
        "requested_by": role if role is not None else requested_by,
        "result": "REJECTED",
        "sequence": len(requests) + 1,
        "requested_via": "role_channel" if role is not None else "transition_interface",
        "reasons": reasons,
    }
    if "from_state" in payload and payload["from_state"] != controller.state:
        record["claimed_from_state"] = payload["from_state"]
    if "run_id" in payload:
        record["run_id"] = payload["run_id"]
    claimed = ROLE_CHANNEL_ACTIONS.get(name) if isinstance(name, str) else None
    event = controller.dispatch_event("transition_request", transition_request=dict(record))
    record["event_seq"] = event["sequence"] if event else None
    requests.append(record)
    controller.save(f"transition-request-{record['sequence']:03}.json", record)
    controller.checkpoint()
    if role is not None and claimed is not None:
        record_claim(
            controller,
            "role_channel",
            role,
            claimed,
            payload.get("message"),
            transition_request=record["sequence"],
        )
    after = (controller.state, len(controller.events), controller.plan, controller.binding)
    if after != before:  # pragma: no cover - defensive: a rejection must never move state
        raise RuntimeError("rejected transition request changed controller state")
    return record
