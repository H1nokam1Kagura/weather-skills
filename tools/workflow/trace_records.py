"""Small controller-native trace records the evidence projection needs and nothing else made.

Frozen shapes (casesets/cached-onset-e2e-v2_7/EVIDENCE_CONTRACT.json):
- structures.event_seq: ``controller_trace.states[<STATE>]`` carries a controller-global clock
  for every entered state, the initial CAPTURED state included (v2_4.run_evidence: "a clock for
  a state never entered" / a missing clock is NOT_OBSERVED).
- structures.controller_trace: ``goal_check {verdict, failed_fields}``.

Both records are durable (checkpointed context and/or an atomic evidence file) and emit a
``dispatch_event`` on the controller-global sequence when dispatch evidence mode is on.
"""

import json
import os

from .contracts import FIELDS, Refused, retain_goal

GOAL_CHECK_FILE = "goal-check.json"


def record_initial_state(controller):
    """Clock the initial state at the start of the run, before any other dispatch event.

    The state is entered at construction, where no dispatch event is ever emitted, so the
    first event of a fresh run is an equally faithful clock for it. A resumed controller, or
    one that already emitted events (e.g. a direct ask() before run()), gets no record: an
    initial-state clock is never invented after other events.
    """
    context = controller.context.get("dispatch_evidence")
    if (
        context is None
        or context["sequence"] != 0
        or controller.events
        or controller.state != controller.sequence[0]
    ):
        return None
    # The controller's own process id: the root of the tree it starts (process_tree /
    # HARNESS_CONTROLLER_PID) even when no child process is ever launched.
    event = controller.dispatch_event(
        "initial_state", state=controller.state, controller_pid=os.getpid()
    )
    controller.checkpoint()
    return event


def _canonical(value):
    return json.dumps(value, sort_keys=True, allow_nan=False, default=repr)


def mismatched_fields(candidate, trusted_goal):
    """Goal fields where a compiled goal differs from the trusted goal, or None if unknown.

    A missing key is not an explicit null and an extra key is a change (v2_5.provenance);
    values compare by canonical JSON so a boolean never equals a number.
    """
    if not isinstance(candidate, dict):
        return None
    failed = [
        k
        for k in sorted(FIELDS)
        if k not in candidate or _canonical(candidate[k]) != _canonical(trusted_goal[k])
    ]
    failed += sorted(k for k in candidate if k not in FIELDS)
    return failed or None


def retain_compiled_goal(controller, candidate):
    """contracts.retain_goal, recording the failed goal check before the refusal propagates.

    ``failed_fields`` is None when the refusal cannot be attributed to named fields (the
    projection then keeps goal_check MISSING rather than inventing a field list).
    """
    try:
        retain_goal(candidate, controller.goal)
    except Refused as exc:
        record = {
            "verdict": "FAIL",
            "failed_fields": mismatched_fields(candidate, controller.goal),
            "compiler_occurrence": sum(
                r.get("role") == "compiler" for r in controller.roles.receipts
            ),
            "gate": exc.gate,
            "reason": str(exc),
        }
        controller.context["goal_check"] = record
        controller.save(GOAL_CHECK_FILE, record)
        controller.dispatch_event("goal_check", goal_check=dict(record))
        controller.checkpoint()
        raise
