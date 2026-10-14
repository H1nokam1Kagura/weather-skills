"""Pre-review gates, the evidence checker, operator actions and authorizations (frozen v2.7).

Projects the controller's own records onto the frozen shapes; nothing is synthesized.

Authority (frozen casesets/cached-onset-e2e-v2_7/EVIDENCE_CONTRACT.json ``structures``):

* v2_4.claim_checks / v2_5.checker_vocabulary (CAP-PRE-REVIEW-GATES): one record per plan
  version sent towards review, event key ``controller_trace.claim_checks[index=<i>]``. The
  controller's ``citation_gate`` / ``citation_verdict`` labels are recorded but never evidence
  (v2.5 UC-14-resolution: the oracle resolves citations itself against review_snapshots).
* v2_4.evidence_checker (CAP-EVIDENCE-CHECKER): its own session after the citation gate and
  before the reviewer; receipt verdict = worst claim verdict; role_output.evidence_checker[k]
  = {plan_hash, verdicts[{claim_id, verdict, reasoning}]}.
* v2_4.operator_actions (CAP-OPERATOR-ACTIONS): {record_id, source, speaker, action, accepted,
  planner_occurrence}, event key ``controller_trace.operator_actions[record_id=<id>]``.
* controller_trace.authorizations (CAP-LEDGER-SEED): LIST {source, speaker, goal_hash}.
* controller_trace.active_approval (CAP-BOUND-SOURCES): the native approval the run acts under.

One numbering for ``reviewer_occurrence`` everywhere: the 1-based receipt ordinal of the
reviewer (EC:invocation_receipts). The controller's capture counter also counts failed
reviewer dispatches, so it is renumbered through the review captures, never copied.

Inconsistent native evidence raises EvidenceMappingError (the whole projection is refused).
evidence_projection imports this module lazily (it defines EvidenceMappingError).
"""

import json
from pathlib import Path

from adapter_support.evidence_projection import EvidenceMappingError
from tools.workflow.contracts import digest

CLAIM_CHECK_CAPABILITY = "CAP-PRE-REVIEW-GATES"
OPERATOR_CAPABILITY = "CAP-OPERATOR-ACTIONS"
# EC:v2_4.operator_actions field set (record_lists types record_id/source/speaker/action/accepted).
OPERATOR_ACTION_FIELDS = ("record_id", "source", "speaker", "action", "accepted")
CHECKER_ROLE = "evidence_checker"
LABELS_NOTE = (
    "claim_checks citation_gate / citation_verdict are controller labels, recorded but never "
    "evidence (v2.5 UC-14-resolution); the oracle resolves citations against review_snapshots"
)


def _detached(value):
    return json.loads(json.dumps(value, allow_nan=False))


# --------------------------------------------------------------------------- reviewer numbering


def reviewer_attempts(projection, receipts):
    """{native capture occurrence: (receipt-ordinal occurrence or None, dispatch_status)}."""
    out = {}
    for capture in projection.captures():
        record = capture["record"]
        native = record.get("reviewer_occurrence")
        if not isinstance(native, int) or isinstance(native, bool) or native in out:
            raise EvidenceMappingError(
                f"review capture has no unique native occurrence: {capture['name']}"
            )
        status = record.get("dispatch_status")
        if status == "RETURNED":
            out[native] = (
                projection.match_receipt(receipts, record["receipt"])["occurrence"],
                status,
            )
        else:
            out[native] = (None, status)
    ordinals = [v[0] for v in out.values() if v[0] is not None]
    if len(set(ordinals)) != len(ordinals):
        raise EvidenceMappingError("two review captures name the same reviewer receipt")
    return out


# --------------------------------------------------------------------------- claim checks


def claim_checks(projection, receipts, role_output):
    """CAP-PRE-REVIEW-GATES: controller_trace.claim_checks and their event keys, or None."""
    native = projection.n
    evidence = native.context.get("review_evidence")
    if not isinstance(evidence, dict) or "claim_checks" not in evidence:
        projection.gap(
            "controller_trace.claim_checks",
            CLAIM_CHECK_CAPABILITY,
            "claim gates were not enabled (no bound definition source)",
        )
        return None
    records = evidence["claim_checks"]
    events = native.kinds("claim_check")
    if len(events) != len(records):
        raise EvidenceMappingError("claim_check events differ from the checkpoint")
    attempts = reviewer_attempts(projection, receipts)
    checkers = {r["occurrence"]: r for r in receipts if r["role"] == CHECKER_ROLE}
    outputs = role_output.get(CHECKER_ROLE, [])
    out = []
    for position, (event, record) in enumerate(zip(events, records, strict=True)):
        index = record.get("index")
        if index != position:
            raise EvidenceMappingError("claim_check indexes are not zero-based and gap-free")
        saved = projection.read_native(f"claim-check-{index:03}.json")
        if (
            saved != record
            or event["index"] != index
            or event["plan_hash"] != record["plan_hash"]
            or event["decision"] != record["decision"]
            or event["record_hash"] != digest(record)
        ):
            raise EvidenceMappingError("claim-check file or event differs from the checkpoint")
        clock = event["sequence"]
        _check_checker_link(record, clock, checkers, outputs)
        value = _detached(record)
        attempt = record.get("reviewer_occurrence")
        if attempt is not None:
            value["native_reviewer_attempt"] = attempt
            ordinal, status = attempts.get(attempt, (None, "NOT_DISPATCHED"))
            value["reviewer_occurrence"] = ordinal
            if ordinal is None:
                # Presented, but that reviewer dispatch returned no receipt: there is no
                # receipt-ordinal occurrence to name (a native attempt number would collide
                # with a later returned review's ordinal).
                value["reviewer_dispatch_status"] = status
                projection.gap(
                    f"controller_trace.claim_checks[index={index}].reviewer_occurrence",
                    CLAIM_CHECK_CAPABILITY,
                    f"presented to native reviewer attempt {attempt}, which returned no "
                    f"receipt ({status})",
                )
            else:
                reviewer_seq = next(
                    r["seq"]
                    for r in receipts
                    if r["role"] == "reviewer" and r["occurrence"] == ordinal
                )
                if not clock < reviewer_seq:
                    raise EvidenceMappingError(
                        "a claim check does not precede the review it was presented to"
                    )
        projection.seq[f"controller_trace.claim_checks[index={index}]"] = clock
        out.append(value)
    if out:
        projection.notes.append({"claim_check_labels": LABELS_NOTE})
    return out


def _check_checker_link(record, clock, checkers, outputs):
    occurrence = record.get("evidence_checker_occurrence")
    ruled = {c["claim_id"]: c["checker_verdict"] for c in record["claims"]}
    if occurrence is None:
        if any(v is not None for v in ruled.values()):
            raise EvidenceMappingError(
                "a claim check carries checker verdicts without a checker occurrence"
            )
        return
    receipt = checkers.get(occurrence)
    if receipt is None or not 1 <= occurrence <= len(outputs):
        raise EvidenceMappingError(
            f"claim check names evidence-checker occurrence {occurrence} with no receipt"
        )
    output = outputs[occurrence - 1]
    if not isinstance(output, dict) or output.get("plan_hash") != record["plan_hash"]:
        raise EvidenceMappingError(
            "evidence-checker output is bound to another plan than its claim check"
        )
    verdicts = {v["claim_id"]: v["verdict"] for v in output.get("verdicts", [])}
    if any(v is not None and verdicts.get(k) != v for k, v in ruled.items()):
        raise EvidenceMappingError(
            "claim check checker verdicts differ from the evidence-checker output"
        )
    if not receipt["seq"] < clock:
        raise EvidenceMappingError(
            "the evidence checker does not precede the claim check that consumes it"
        )


# --------------------------------------------------------------------------- evidence checker


def evidence_checker_report(evidence, missing):
    """CAP-EVIDENCE-CHECKER: the checker receipts, outputs and their placement, or MISSING."""
    trace = evidence["controller_trace"]
    if "claim_checks" not in trace:
        reasons = [m["reason"] for m in missing if m["path"] == "controller_trace.claim_checks"]
        return {"status": "MISSING", "reason": "; ".join(reasons) or "claim gates not observed"}
    receipts = evidence["invocation_receipts"]
    sessions = {}
    for r in receipts:
        sessions.setdefault(r["session_id"], []).append((r["role"], r["occurrence"]))
    reviewers = {r["occurrence"]: r["seq"] for r in receipts if r["role"] == "reviewer"}
    clocks = evidence["event_seq"]
    invocations = []
    for receipt in (r for r in receipts if r["role"] == CHECKER_ROLE):
        occurrence = receipt["occurrence"]
        consumers = [
            c for c in trace["claim_checks"] if c["evidence_checker_occurrence"] == occurrence
        ]
        record = {
            "occurrence": occurrence,
            "session_id": receipt["session_id"],
            "own_session": sessions[receipt["session_id"]] == [(CHECKER_ROLE, occurrence)],
            "seq": receipt["seq"],
            "claim_check_indexes": [c["index"] for c in consumers],
        }
        if "verdict" in receipt:
            record["verdict"] = receipt["verdict"]
        placements = []
        for check in consumers:
            gate_clock = clocks[f"controller_trace.claim_checks[index={check['index']}]"]
            review = check.get("reviewer_occurrence")
            placement = {
                "index": check["index"],
                "citation_gate": check["citation_gate"],
                "checker_before_claim_check": receipt["seq"] < gate_clock,
                "decision": check["decision"],
            }
            if isinstance(review, int):
                placement["reviewer_occurrence"] = review
                placement["claim_check_before_reviewer"] = gate_clock < reviewers[review]
            placements.append(placement)
        record["placements"] = placements
        invocations.append(record)
    return {
        "invocations": invocations,
        "role_output": evidence["role_output"][CHECKER_ROLE],
        "receipts": [r for r in receipts if r["role"] == CHECKER_ROLE],
    }


# --------------------------------------------------------------------------- operator actions


def operator_actions(projection, receipts):
    """CAP-OPERATOR-ACTIONS: every ledger/role-channel action the controller decided, or None.

    The list exists only when the controller opened its operator channel (a run-scoped ledger
    it created and consulted) or decided a claim; otherwise it is MISSING, never [].
    """
    native = projection.n
    context = native.context
    if "operator_actions" not in context:
        projection.gap(
            "controller_trace.operator_actions",
            OPERATOR_CAPABILITY,
            "no operator ledger channel was opened for this run (Controller "
            "operator_ledger_path not supplied)",
        )
        return None
    actions = context["operator_actions"]
    if not isinstance(actions, list):
        raise EvidenceMappingError("native operator_actions is not a list")
    events = native.kinds("operator_action")
    if len(events) != len(actions):
        raise EvidenceMappingError("operator_action events differ from the checkpoint")
    planners = {r["occurrence"] for r in receipts if r["role"] == "planner"}
    out, seen = [], set()
    for number, (event, action) in enumerate(zip(events, actions, strict=True), 1):
        decision = action.get("decision") if isinstance(action, dict) else None
        record_id = action.get("record_id") if isinstance(action, dict) else None
        if (
            not isinstance(decision, dict)
            or action.get("sequence") != number
            or not isinstance(record_id, str)
            or record_id in seen
        ):
            raise EvidenceMappingError("native operator action identity is malformed")
        seen.add(record_id)
        if projection.read_native(f"operator-action-{number:03}.json") != action:
            raise EvidenceMappingError("operator-action file differs from the checkpoint")
        if (
            event["record_id"] != record_id
            or event["sequence"] != decision.get("event_seq")
            or event["operator_action"] != {k: v for k, v in action.items() if k != "decision"}
        ):
            raise EvidenceMappingError("operator_action event differs from its decision record")
        value = {k: action[k] for k in OPERATOR_ACTION_FIELDS}
        value["planner_occurrence"] = action.get("planner_occurrence")
        value["reasons"] = list(decision.get("reasons", []))
        if "boundary" in decision:
            value["boundary"] = decision["boundary"]
        for field in ("speaker", "action"):
            if not isinstance(action[field], str):
                # Kept as the controller recorded it (never classified for it): an
                # unattributed or unclassifiable claim is malformed contract evidence.
                projection.gap(
                    f"controller_trace.operator_actions[record_id={record_id}].{field}",
                    OPERATOR_CAPABILITY,
                    f"the controller recorded no classifiable {field} for this claim",
                )
        if (
            action["accepted"] is True
            and action["action"] == "replan"
            and action.get("planner_occurrence") not in planners
        ):
            projection.notes.append({"accepted_replan_without_planner_receipt": record_id})
        projection.seq[f"controller_trace.operator_actions[record_id={record_id}]"] = event[
            "sequence"
        ]
        out.append(value)
    channel = context.get("operator_channel")
    if isinstance(channel, dict):
        projection.notes.append({"operator_channel": channel_note(channel)})
    return out


def channel_note(channel):
    """Consultations, identity-less rejections and records appended but never consulted."""
    note = {
        "consultations": [c["boundary"] for c in channel.get("consultations", [])],
        "rejections_without_identity": len(channel.get("ledger_rejections", [])),
        "integrity_failures": len(channel.get("integrity_failures", [])),
    }
    try:
        data = Path(channel["ledger_path"]).read_bytes()
    except OSError:
        note["ledger_file"] = "unreadable at projection"
        return note
    tail = data[channel["consumed_bytes"] :]
    note["unconsumed_complete_records"] = tail.count(b"\n")
    return note


# --------------------------------------------------------------------------- authorizations


def authorizations(projection):
    """CAP-LEDGER-SEED: the authorization decisions the controller recorded.

    One record per AUTHORIZED transition, whose evidence must be the request's authorization
    record (validated at capture by contracts.request_goal: a user message quoted verbatim,
    bound to the goal hash). ``[]`` when a finished run never authorized; MISSING when the
    run did not finish (more could follow).
    """
    native = projection.n
    if not native.finished():
        projection.gap(
            "controller_trace.authorizations",
            "CAP-LEDGER-SEED",
            "the run did not finish: its authorization decisions are not final",
        )
        return None
    request = native.checkpoint["request"]
    authorization = request["authorization"]
    speakers = {m["id"]: m["speaker"] for m in request["messages"]}
    out = []
    for event in native.kinds("transition"):
        transition = event["transition"]
        if transition["to"] != "AUTHORIZED":
            continue
        if transition.get("evidence") != authorization:
            raise EvidenceMappingError(
                "AUTHORIZED transition evidence is not the request authorization"
            )
        out.append(
            {
                # The request is the operator source ledger the controller captured
                # (CAP-LEDGER-SEED); the transition evidence is exactly its record.
                "source": "operator_ledger",
                "speaker": speakers[authorization["message_id"]],
                "goal_hash": authorization["goal_hash"],
                "message_id": authorization["message_id"],
                "quote": authorization["quote"],
                "authorized_at": event["sequence"],
            }
        )
    return out


# --------------------------------------------------------------------------- active approval


def active_approval(projection, receipts):
    """CAP-BOUND-SOURCES: the controller's native active approval (issue_active_approval)."""
    native = projection.n
    approved = any(e["to"] == "REVIEW_APPROVED" for e in native.checkpoint["events"])
    record = native.context.get("active_approval")
    if record is None:
        if approved:
            projection.gap(
                "controller_trace.active_approval",
                "CAP-BOUND-SOURCES",
                "REVIEW_APPROVED reached but no native active approval record exists",
            )
        return None
    superseded = native.context.get("superseded_approvals", [])
    saved = projection.read_native(f"active-approval-{len(superseded):03}.json")
    body = {k: v for k, v in record.items() if k != "sequence"}
    events = [e for e in native.kinds("active_approval") if e["sequence"] == record["sequence"]]
    if saved != record or len(events) != 1 or events[0]["approval"] != body:
        raise EvidenceMappingError("native active approval differs from its file or dispatch event")
    reviewer = next(
        (
            r
            for r in receipts
            if r["role"] == "reviewer" and r["occurrence"] == record["reviewer_occurrence"]
        ),
        None,
    )
    if reviewer is None or reviewer["session_id"] != record["reviewer_session_id"]:
        raise EvidenceMappingError("active approval names no matching reviewer receipt")
    if not reviewer["seq"] < record["sequence"]:
        raise EvidenceMappingError("the approving reviewer does not precede the active approval")
    evidence = native.context.get("review_evidence")
    if isinstance(evidence, dict):
        accepted = [c for c in projection.captures() if c["record"].get("accepted_review")]
        if not accepted:
            raise EvidenceMappingError("active approval without an accepted review capture")
        capture = accepted[-1]["record"]
        envelope = capture["review_evidence"]
        if (
            capture["committed_transition"]["binding_hash"] != record["binding_hash"]
            or projection.match_receipt(receipts, capture["receipt"]) is not reviewer
            or any(
                envelope[k] != record[k]
                for k in ("request_hash", "goal_hash", "plan_hash", "manifest_hash", "source_hash")
            )
        ):
            raise EvidenceMappingError("active approval differs from the accepted review capture")
    else:
        projection.notes.append(
            {"active_approval": "review evidence mode off: not cross-checked against a capture"}
        )
    projection.seq["controller_trace.active_approval"] = record["sequence"]
    value = {
        k: record[k]
        for k in (
            "approval_id",
            "verdict",
            "reviewer_occurrence",
            "reviewer_session_id",
            "request_hash",
            "goal_hash",
            "plan_hash",
            "manifest_hash",
            "source_hash",
            "binding_hash",
        )
    }
    if "replaces" in record:
        value["replaces"] = record["replaces"]
    return value


# --------------------------------------------------------------------------- process flags


def argv_flags(argv):
    """EC:process_receipts flags from a parser-accepted, controller-constructed argv.

    Option token -> its value token; an option followed by another option (or nothing) is a
    zero-value option -> true; an option given more than once -> the list of its values in
    argv order. These match the actual parsers' actions for every argv this controller
    builds (verify-run: --require-replay store_true, --search-dir append; every other
    option is single-valued and appears once). Returns None for any argv that is not a
    strict option sequence (a bare positional, an '=' form): never guessed.
    """
    if not isinstance(argv, list):
        return None
    flags, counts, position = {}, {}, 0
    while position < len(argv):
        token = argv[position]
        if not isinstance(token, str) or not token.startswith("--") or "=" in token:
            return None
        following = argv[position + 1] if position + 1 < len(argv) else None
        if following is None or following.startswith("--"):
            value, position = True, position + 1
        else:
            value, position = following, position + 2
        counts[token] = counts.get(token, 0) + 1
        if counts[token] == 1:
            flags[token] = value
        elif value is True or flags[token] is True:
            return None  # a repeated zero-value option has no single parsed form here
        else:
            previous = flags[token]
            flags[token] = (previous if counts[token] > 2 else [previous]) + [value]
    return flags


# --------------------------------------------------------------------------- harness symbol


def expected_source_after(case, source_pre):
    """HARNESS_HASH:source_expected_after from the case's own declared operations.

    No fixture operation in the case: nothing may touch the source, so the harness expects it
    unchanged (source_pre). Any mutate_fixture entry: owned by CAP-FIXTURE-OP, which records
    the expected hash when it applies the operation (FX-SRC-CELL-INCREMENT postcondition).
    Returns (value or None, reason).
    """
    schedule = case.get("injection_schedule") if isinstance(case, dict) else None
    if not isinstance(schedule, list):
        return None, "case declares no injection_schedule list"
    if any(isinstance(e, dict) and e.get("operation") == "mutate_fixture" for e in schedule):
        return None, "case applies fixture operations (CAP-FIXTURE-OP records the expected hash)"
    return source_pre, "no fixture operation declared: source expected unchanged (source_pre)"
