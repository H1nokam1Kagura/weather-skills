"""run_case's notify-and-wait route for after_operator_notification cases (Wave 8, sub-wave 5).

WAVE8-BRIEF.md "Build A", part A1. Only a case whose injection_schedule carries an entry on the
``after_operator_notification`` hook takes this route: of the 66 frozen v2.7 cases, E14-P3-CTL and
E14-P3-INJ. Every other case keeps its route exactly as before (resume_driver.arm returns None
for it, or the harness_stop_controller StopChannel).

1. Arm. ``arm(schedule_hooks, case)`` swaps the case hooks' InjectionHooks for one built with
   ``notification_boundary=True``, before any boundary is dispatched, so the dependent
   dispatcher decides the after_operator_notification entry at the controller's own boundary
   (recovery.notify: recorded -> emitted -> hook -> consult_operator_ledger) instead of marking
   it NOT_IMPLEMENTED. Returns the NotifyChannel; run_case sets ``observer`` and
   ``schedule_hooks`` on it and hands it to resume_driver.drive, which hands it here.
2. Run. ``drive`` runs the single in-process controller with run_case's hooks (the observer
   chained to the case schedule), exactly as the plain route does. After two automatic resumes
   a third interruption is diagnosed, the operator is notified, and the run stops and waits.
3. Operator resume. The harness reads the durable checkpoint. Only when the recovery stop is
   ``notify_and_wait`` AND the operator channel holds an accepted user resume record that no
   resume has consumed yet (operator_ledger.take_accepted_resume) does it issue
   ``Controller.request_resume`` with ``initiated_by: operator_ledger`` naming that record
   (D1, D18). The controller decides it; the harness authors no ledger record here (in E14-P3-CTL
   the record is the frozen FX-OPERATOR-RECORD, appended at the notification boundary). An
   accepted resume runs again in the same process; a refused one is final (the controller
   recorded its decision and terminal). Without an accepted, unconsumed record (E14-P3-INJ:
   the role-channel claim is rejected) nothing is requested: a harness request there would turn
   the frozen INCOMPLETE into BLOCKED.

No fork and no harness route record: one controller process throughout, so ``record`` is None
(run_case writes ``resume_route: None``; container_harness/project_in_container.py drops it).
"""

from pathlib import Path
from types import SimpleNamespace

from adapter_support import shadow_projection
from tools.workflow.contracts import Refused
from tools.workflow.e2e_injections import NOTIFICATION_HOOK, InjectionHooks
from tools.workflow.persistence import read_checkpoint

ROUTE = "notify"
NOTIFY_STOP = "notify_and_wait"
INITIATED_BY = "operator_ledger"


def _refusal(message):
    from adapter_support.weather_adapter import AdapterRefusal

    return AdapterRefusal(message)


# ------------------------------------------------------------------ static checks (no effects)


def notification_entries(case):
    """Indexes of the schedule's after_operator_notification entries."""
    return [
        index
        for index, spec in enumerate((case or {}).get("injection_schedule") or [])
        if isinstance(spec, dict) and spec.get("hook") == NOTIFICATION_HOOK
    ]


def notification_scheduled(case):
    return bool(notification_entries(case))


def shadow_configured(case):
    """The case names a shadow_policy role (shadow_projection.shadow_plan's own test)."""
    roles = (case or {}).get("agent_roles") or {}
    names = set(roles.get("live") or []) | set(roles.get("substituted") or [])
    return shadow_projection.ROLE in names


def refuse_unsupported_route(case, shadow_configured_):
    """The notify route's own preconditions, checked before any side effect (plan_case)."""
    if notification_scheduled(case) and shadow_configured_:
        raise _refusal("the shadow policy is not supported across an operator notification")


# ------------------------------------------------------------------ arm / drive


class NotifyChannel:
    """resume_driver.drive's channel for the notify route. ``observer`` and ``schedule_hooks``
    are set by run_case, as on resume_driver.StopChannel."""

    route = ROUTE

    def __init__(self):
        self.observer = self.schedule_hooks = None


def arm(schedule_hooks, case):
    """Open the notification boundary on the case hooks' unused InjectionHooks. Returns the
    NotifyChannel, or None when the case schedules no after_operator_notification entry."""
    if not notification_scheduled(case):
        return None
    if shadow_configured(case):
        raise _refusal("the shadow policy is not supported across an operator notification")
    old = getattr(schedule_hooks, "injection", None)
    if old is None or old.records or old.prepared or old.counts:
        raise _refusal("the notification boundary must be opened on unused case schedule hooks")
    schedule_hooks.injection = InjectionHooks(
        old.case,
        symbols=old.symbols,
        disposable_root=old.disposable_root,
        notification_boundary=True,
    )
    return NotifyChannel()


def pending_operator_resume(checkpoint):
    """The accepted user resume record to bind, or None: only at a notify_and_wait stop and
    only for an accepted record no resume has consumed (operator_ledger.take_accepted_resume's
    own order: the earliest)."""
    context = (checkpoint or {}).get("context") or {}
    stop = (context.get("recovery") or {}).get("stop") or {}
    if stop.get("kind") != NOTIFY_STOP:
        return None
    channel = context.get("operator_channel") or {}
    consumed = set(channel.get("consumed_resumes") or [])
    for record_id in channel.get("accepted_resumes") or []:
        if record_id not in consumed:
            return record_id
    return None


def operator_resume_request(record_id, operator_run_id):
    return {
        "action": "resume",
        "initiated_by": INITIATED_BY,
        "operator_record_id": record_id,
        "requested_by": "operator",
        "run_id": operator_run_id,
    }


def _run(controller):
    try:
        controller.run()
    except Exception:  # noqa: BLE001 - the controller recorded its own terminal
        pass


def drive(channel, build, *, run_dir, operator_run_id):
    """Run the controller; after a notify-and-wait stop, issue the operator resume the ledger
    accepted (if any) and run again. Returns resume_driver.drive's namespace with record None."""
    from adapter_support.resume_driver import IncarnationError
    from tools.workflow.runner import Controller

    run_dir = Path(run_dir)
    observer = channel.observer
    controller = build()  # run_case's partial: hooks=observer already
    _run(controller)
    issued = []
    while True:
        record_id = pending_operator_resume(read_checkpoint(run_dir))
        if record_id is None:
            break
        if record_id in issued:  # pragma: no cover - take_accepted_resume consumes it
            raise IncarnationError(f"operator resume record {record_id} was not consumed")
        issued.append(record_id)
        request = operator_resume_request(record_id, operator_run_id)
        try:
            controller = Controller.request_resume(
                run_dir, controller.processes, controller.roles, request, hooks=observer
            )
        except Refused:
            break  # the refusal and its terminal are durable; nothing follows it
        _run(controller)
    return SimpleNamespace(
        controller=controller,
        observer=observer,
        schedule_hooks=channel.schedule_hooks,
        processes=controller.processes,
        roles=controller.roles,
        record=None,
    )
