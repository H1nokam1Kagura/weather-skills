"""run_case's stop-and-resume route for harness_stop_controller cases (Wave 7, sub-wave C).

WAVE7-BRIEF.md "Mechanism". Only a case whose injection_schedule carries an interrupt with
``mode: harness_stop_controller`` (E10-P1, E11-P1) takes this route; every other case runs the
single in-process controller exactly as before (E14's intermittent interruptions included:
the controller resumes those itself).

1. Fork. ``run_case`` forks; the child is controller incarnation 1 and runs the controller
   with ``InjectionHooks(stopper=...)``. The parent builds no controller before the stop.
2. Hand-off. At the stop boundary the hook calls the stopper with the e2e-injections.stop/1
   packet; the stopper adds the harness-side state the parent needs (launch observer records,
   the substitution rebinds, the controller's own artifact paths), writes ONE JSON line to an
   ``os.pipe`` and blocks until it is killed. Nothing in the child runs past the stop.
3. Kill. With ``profile["interrupt_broker"]`` (container route: a root, untraced broker), the
   parent asks the broker to stop the child (``request_stop(pid)``: freeze the tree, read the
   output byte counts, SIGKILL); without one (in-process route, Linux pytest) the parent does
   the same itself with the broker's own stdlib ``stop_tree`` over /proc, or a bare SIGKILL
   where /proc is absent (byte counts then unobserved, MISSING). The parent reaps the child.
4. Resume. The parent observes the onset artifact (mid-process stop only: onset_partial),
   applies the entries deferred at the stop boundary (``apply_deferred``), appends a
   HARNESS-AUTHORED user resume record to the run's operator ledger (disclosed; it stands for
   the frozen ``request_transition`` resume entry, and the controller alone decides it), and
   calls ``Controller.request_resume(...)`` with ``initiated_by: operator_ledger`` as
   incarnation 2, with fresh hooks that ``import_state`` the child's hook state.

Fork hygiene: the child closes the inherited descriptors the parent owns (the pipe's read
end, the harness projection-inputs channel, the broker channel) and always leaves through
``os._exit`` (no inherited buffer is flushed twice, no parent cleanup runs). The parent bounds
every wait (INCARNATION_TIMEOUT_S, REAP_TIMEOUT_S). Without ``os.fork`` (Windows) the route is
refused before any side effect (``fork_available``).

D21 (CR-05, decisions/DECISIONS.json): a case is admitted only when the capability each
scheduled operation implies is declared (``implied_missing``), stricter than frozen.
"""

import copy
import json
import os
import select
import signal
import threading
import time
import warnings
from pathlib import Path
from types import SimpleNamespace

from adapter_support import resume_projection
from tools.workflow import operator_ledger
from tools.workflow.catalog import tree_hash
from tools.workflow.contracts import Refused
from tools.workflow.e2e_injections import (
    STOP_MODE,
    STOP_SCHEMA,
    InjectionHooks,
    midprocess_stop_form,
    unsupported_requirements,
)
from tools.workflow.persistence import read_checkpoint

ROUTE_SCHEMA = resume_projection.ROUTE_SCHEMA
MESSAGE_SCHEMA = "weather-adapter.incarnation-message/1"
# The nine wave-7 records. Their methods exist, but each is declared only by a reviewed D4
# record in CAPABILITY-PRODUCERS.json: without one it stays undeclared (WAVE7-BRIEF "Do not
# sign CAP-INTERRUPT, CAP-RESUME or CAP-RESUME-ATTRIBUTION early").
D4_GATED = frozenset(
    {
        "CAP-INTERRUPT",
        "CAP-RESUME",
        "CAP-RESUME-ATTRIBUTION",
        "CAP-MIDPROCESS",
        "CAP-AUTO-RESUME",
        "CAP-DIAGNOSIS",
        "CAP-OPERATOR-NOTIFY",
        "CAP-MECHANICAL-REPAIR",
        "CAP-DENY-WRITE",
    }
)
# D21 / CR-05: the capability a scheduled operation implies (frozen ADAPTER-REQUIREMENTS method
# table: deny_write -> CAP-DENY-WRITE, request_transition -> CAP-TRANSITION-REQUEST,
# mutate_fixture -> CAP-FIXTURE-OP, interrupt -> CAP-INTERRUPT, inject_metric -> CAP-METRIC),
# plus CAP-MIDPROCESS for the frozen mid-process stop form.
IMPLIED_CAPABILITIES = {
    "deny_write": "CAP-DENY-WRITE",
    "request_transition": "CAP-TRANSITION-REQUEST",
    "mutate_fixture": "CAP-FIXTURE-OP",
    "interrupt": "CAP-INTERRUPT",
    "inject_metric": "CAP-METRIC",
}
MIDPROCESS_CAPABILITY = "CAP-MIDPROCESS"
INCARNATION_TIMEOUT_S = 3600.0  # incarnation 1 runs the controller up to the stop boundary
REAP_TIMEOUT_S = 30.0
STOP_BLOCK_S = 600.0  # the child waits at most this long to be killed, then leaves
EXIT_FINISHED, EXIT_ERROR, EXIT_ORPHANED, EXIT_UNKILLED, EXIT_STOP_FAILED = 0, 70, 71, 72, 73
HARNESS_RESUME_RECORD_ID = "HARNESS-OPERATOR-RESUME-{index}"
HARNESS_RESUME_TEXT = (
    "resume (harness-authored record for frozen schedule entry {index}: request_transition "
    "action resume, requested_by operator, delivered after the controller stop)"
)
DISCLOSURES = (
    "Incarnation 1 is a fork of the traced root; incarnation 2 is the traced root itself "
    "(Controller.request_resume). HARNESS_CONTROLLER_PID maps to the traced root (OI-05).",
    "The operator resume record is harness-authored (the frozen schedule's request_transition "
    "resume entry, requested_by operator), appended to run/operator-ledger.jsonl after the "
    "stop; acceptance is only the controller's operator_actions decision.",
    "controller_killed is the controller's inference from a checkpoint with no terminal and "
    "no unanswered interruption (recovery.record_observed_stop).",
    "A killed process's output_bytes is harness-observed before SIGKILL (OI-04), labelled "
    "output_bytes_source harness_observed_before_kill; unobserved stays MISSING.",
)


class IncarnationError(RuntimeError):
    """The forked incarnation could not be driven to its stop; a harness failure."""


def _refusal(message):
    from adapter_support.weather_adapter import AdapterRefusal

    return AdapterRefusal(message)


# ------------------------------------------------------------------ static checks (no effects)


def stop_entries(case):
    """Indexes of the schedule's harness_stop_controller interrupts."""
    out = []
    for index, spec in enumerate((case or {}).get("injection_schedule") or []):
        payload = spec.get("payload") if isinstance(spec, dict) else None
        if isinstance(payload, dict) and payload.get("mode") == STOP_MODE:
            out.append(index)
    return out


def stop_scheduled(case):
    return bool(stop_entries(case))


def fork_available():
    return os.name == "posix" and callable(getattr(os, "fork", None))


def stop_delivery(case):
    """True when run_case will run the hooks with a stopper (the forked route)."""
    return stop_scheduled(case) and fork_available()


def implied_capabilities(case):
    """[{index, operation, capability}] every scheduled operation implies (D21 / CR-05)."""
    out = []
    for index, spec in enumerate((case or {}).get("injection_schedule") or []):
        if not isinstance(spec, dict):
            continue
        capability = IMPLIED_CAPABILITIES.get(spec.get("operation"))
        if capability is not None:
            out.append({"index": index, "operation": spec["operation"], "capability": capability})
        if midprocess_stop_form(spec):
            out.append(
                {"index": index, "operation": "interrupt", "capability": MIDPROCESS_CAPABILITY}
            )
    return out


def implied_missing(case, implemented):
    """The implied capabilities the adapter does not declare (sorted)."""
    return sorted({e["capability"] for e in implied_capabilities(case)} - set(implemented))


def refuse_unsupported_schedule(case, stop_delivery):
    """InjectionHooks' static findings; stop_delivery=True only when a stopper is supplied."""
    findings = unsupported_requirements(case, stop_delivery=stop_delivery)
    if findings:
        raise _refusal(f"case schedule needs injection channels that are unavailable: {findings}")


def refuse_unsupported_route(case, shadow_configured):
    """The forked route's own preconditions, checked before any side effect."""
    if not stop_scheduled(case):
        return
    if not fork_available():
        raise _refusal(
            "harness_stop_controller needs a real stop of a forked controller incarnation "
            "(os.fork); unavailable on this platform"
        )
    if len(stop_entries(case)) != 1:
        raise _refusal("one harness_stop_controller per case is supported")
    if shadow_configured:
        raise _refusal("the shadow policy is not supported across a controller stop")


# ------------------------------------------------------------------ incarnation 1 (child side)


class ControllerTap:
    """Outermost hook of incarnation 1: remembers the controller it is called with, so the
    stop packet carries that controller's own artifact paths, then delegates unchanged."""

    def __init__(self, hooks):
        self.hooks = hooks
        self.controller = None

    def __call__(self, point, event, controller):
        self.controller = controller
        self.hooks(point, event, controller)

    def paths(self):
        paths = getattr(self.controller, "paths", None)
        return None if paths is None else {k: str(v) for k, v in paths.items()}


class StopChannel:
    """The stopper the stop-delivery hooks call. ``send``, ``block`` and ``failed`` are bound
    by the incarnation route (fork: the pipe, wait-to-be-killed, os._exit)."""

    def __init__(self):
        self.send = self.block = self.failed = None
        self.observer = self.schedule_hooks = self.tap = None

    def state_message(self, kind):
        return {
            "schema": MESSAGE_SCHEMA,
            "kind": kind,
            "pid": os.getpid(),
            "observer_records": self.observer.records,
            "rebinds": self.schedule_hooks.rebinds,
            "paths": self.tap.paths(),
        }

    def stopper(self, packet):
        try:
            message = self.state_message("stop")
            message["packet"] = packet
            self.send(message)
        except BaseException as exc:  # noqa: BLE001 - nothing may run past a harness stop
            self.failed(exc)
        self.block()


def arm(schedule_hooks, case):
    """Replace the case hooks' InjectionHooks with a stop-delivery one, before any boundary
    is dispatched. Returns the StopChannel, or None when the case schedules no stop."""
    if not stop_scheduled(case):
        return None
    old = getattr(schedule_hooks, "injection", None)
    if old is None or old.records or old.prepared or old.counts:
        raise _refusal("stop delivery must be armed on unused case schedule hooks")
    channel = StopChannel()
    schedule_hooks.injection = InjectionHooks(
        old.case,
        symbols=old.symbols,
        disposable_root=old.disposable_root,
        stopper=channel.stopper,
    )
    return channel


def _write_message(fd, message):
    data = (json.dumps(message, allow_nan=False) + "\n").encode("utf-8")
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view) :]


def _block_until_killed(parent):
    """Wait to be killed; leave (never return) if the parent died or nothing came."""
    deadline = time.monotonic() + STOP_BLOCK_S
    while time.monotonic() < deadline:
        if os.getppid() != parent:
            os._exit(EXIT_ORPHANED)
        time.sleep(0.05)
    os._exit(EXIT_UNKILLED)


class ForkedIncarnation:
    """Incarnation 1 as a forked child of the traced root (the real route)."""

    route = "fork"

    def __init__(self):
        self.pid = self.read_fd = None
        self.threads_at_fork = None
        self.exit = None

    def start(self, channel, run, close_fds):
        read_fd, write_fd = os.pipe()
        parent = os.getpid()
        self.threads_at_fork = threading.active_count()
        with warnings.catch_warnings():
            # Python 3.12+ warns on fork with threads alive; the count is recorded instead.
            warnings.filterwarnings("ignore", message=".*fork.*", category=DeprecationWarning)
            pid = os.fork()
        if pid == 0:  # pragma: no cover - the child never returns into the caller
            self._child(channel, run, close_fds, read_fd, write_fd, parent)
        os.close(write_fd)
        self.pid, self.read_fd = pid, read_fd

    @staticmethod
    def _child(channel, run, close_fds, read_fd, write_fd, parent):  # pragma: no cover
        code = EXIT_ERROR
        try:
            os.close(read_fd)
            for fd in close_fds:
                try:
                    os.close(fd)
                except OSError:
                    pass
            channel.send = lambda message: _write_message(write_fd, message)
            channel.block = lambda: _block_until_killed(parent)
            channel.failed = lambda exc: os._exit(EXIT_STOP_FAILED)
            _write_message(write_fd, run())
            code = EXIT_FINISHED
        except BaseException as exc:  # noqa: BLE001 - reported to the parent, then os._exit
            try:
                _write_message(
                    write_fd,
                    {
                        "schema": MESSAGE_SCHEMA,
                        "kind": "error",
                        "pid": os.getpid(),
                        "error": repr(exc)[:4000],
                    },
                )
            except BaseException:  # noqa: BLE001
                pass
        finally:
            os._exit(code)

    def receive(self, timeout=None):
        """-> (message, None) or (None, reason). Bounded by INCARNATION_TIMEOUT_S."""
        deadline = time.monotonic() + (INCARNATION_TIMEOUT_S if timeout is None else timeout)
        buf = b""
        try:
            while b"\n" not in buf:
                left = deadline - time.monotonic()
                if left <= 0:
                    return None, "timeout waiting for incarnation 1"
                ready, _, _ = select.select([self.read_fd], [], [], min(left, 1.0))
                if not ready:
                    continue
                chunk = os.read(self.read_fd, 1 << 16)
                if not chunk:
                    return None, "incarnation 1 closed the channel without a message"
                buf += chunk
        finally:
            os.close(self.read_fd)
            self.read_fd = None
        try:
            message = json.loads(buf.split(b"\n", 1)[0].decode("utf-8"))
        except ValueError as exc:
            return None, f"unparseable incarnation message: {exc}"
        if not isinstance(message, dict) or message.get("schema") != MESSAGE_SCHEMA:
            return None, "incarnation message has an unknown schema"
        return message, None

    def kill(self, broker):
        """Stop incarnation 1 and its descendants; -> the kill record (never raises)."""
        from container_harness import interrupt_broker as ib

        if broker is not None:
            ack = broker.request_stop(self.pid)
            record = {
                "route": "interrupt_broker",
                "pid": self.pid,
                "ok": ack.get("ok"),
                "verified": ack.get("verified"),
                "descendants": ack.get("descendants") or [],
                "killed": ack.get("killed") or [],
                "output_bytes": ack.get("output_bytes") or {},
                "stop_index": ack.get("stop_index"),
                "errors": list(ack.get("errors") or []),
            }
        elif os.path.isdir("/proc"):
            rec = ib.stop_tree(self.pid, os.getpid(), ib.ProcFS())
            record = {
                "route": "in_process",
                "pid": self.pid,
                "ok": not rec["errors"] and self.pid in rec["killed"],
                "descendants": rec["descendants"],
                "stopped": rec["stopped"],
                "killed": rec["killed"],
                "output_bytes": {
                    p: {n: v.get(n) for n in ("stdout", "stderr")}
                    for p, v in rec["output_bytes"].items()
                },
                "errors": rec["errors"],
            }
        else:
            errors = []
            try:
                os.kill(self.pid, signal.SIGKILL)
            except OSError as exc:
                errors.append(f"SIGKILL {self.pid}: {exc}")
            record = {
                "route": "in_process_without_procfs",
                "pid": self.pid,
                "ok": not errors,
                "descendants": [],
                "killed": [],
                "output_bytes": {},
                "errors": errors + ["no /proc: descendants and output bytes unobservable"],
            }
        record["output_bytes_basis"] = ib.OUTPUT_BYTES_BASIS
        return record

    def reap(self, kill_on_timeout=True):
        """waitpid the child, bounded; a child still alive after REAP_TIMEOUT_S is SIGKILLed."""
        deadline = time.monotonic() + REAP_TIMEOUT_S
        fallback = False
        while True:
            pid, status = os.waitpid(self.pid, os.WNOHANG)
            if pid == self.pid:
                break
            if time.monotonic() > deadline:
                if fallback or not kill_on_timeout:
                    self.exit = {"reaped": False, "fallback_sigkill": fallback}
                    return self.exit
                os.kill(self.pid, signal.SIGKILL)
                fallback, deadline = True, time.monotonic() + REAP_TIMEOUT_S
            time.sleep(0.02)
        self.exit = {
            "reaped": True,
            "signaled": os.WIFSIGNALED(status),
            "signal": os.WTERMSIG(status) if os.WIFSIGNALED(status) else None,
            "exit_code": os.WEXITSTATUS(status) if os.WIFEXITED(status) else None,
            "fallback_sigkill": fallback,
        }
        return self.exit


# Route used by drive(); a test may substitute an in-process double (never evidence).
INCARNATION_ROUTE = ForkedIncarnation


def _close_fds(profile):
    fds = []
    channel = profile.get("projection_inputs_channel")
    fileno = getattr(channel, "fileno", None)
    if callable(fileno):
        try:
            fds.append(fileno())
        except (OSError, ValueError):
            pass
    broker = profile.get("interrupt_broker")
    for name in ("request_fd", "ack_fd"):
        if isinstance(getattr(broker, name, None), int):
            fds.append(getattr(broker, name))
    return sorted(set(fds))


# ------------------------------------------------------------------ the route (parent side)


def onset_observation(paths):
    """Mid-process stop: the onset artifact as the parent finds it after the kill."""
    onset = Path(paths["onset"])
    present = onset.exists()
    out = {
        "present_at_interrupt": present,
        "observed_by": "harness parent, after the kill of incarnation 1 and before any resume",
        "path": str(onset),
    }
    if present:
        try:
            out["content_hash"] = tree_hash(onset)
        except Exception as exc:  # noqa: BLE001 - recorded: the hash stays unobserved
            out["hash_error"] = repr(exc)
    return out


def harness_resume_record(index, run_id):
    return {
        "record_id": HARNESS_RESUME_RECORD_ID.format(index=index),
        "speaker": "user",
        "action": "resume",
        "run_id": run_id,
        "text": HARNESS_RESUME_TEXT.format(index=index),
    }


def drive(channel, build, *, case, schedule_plan, prepared, profile, run_dir, operator_run_id):
    """Run incarnation 1 to its stop, kill it, resume as incarnation 2.

    ``build(hooks=...)`` constructs incarnation 1's Controller (run_case's arguments). Returns
    SimpleNamespace(controller, observer, schedule_hooks, processes, roles, record), where
    ``controller`` is incarnation 2 (or, when no resume was accepted, a stand-in carrying the
    controller's own artifact paths) and ``record`` the harness resume-route record."""
    from adapter_support import evidence_projection, role_substitution
    from adapter_support import operator_ledger_projection as olp
    from tools.workflow.runner import Controller

    run_dir = Path(run_dir)
    channel.tap = ControllerTap(channel.observer)

    def incarnation_one():
        controller = build(hooks=channel.tap)
        error = None
        try:
            controller.run()
        except Exception as exc:  # noqa: BLE001 - the controller recorded its own terminal
            error = repr(exc)[:2000]
        message = channel.state_message("finished")
        message["error"] = error
        try:
            message["state"] = channel.schedule_hooks.injection.export_state()
        except Exception as exc:  # noqa: BLE001 - reported; the parent refuses to continue
            message["state"], message["state_error"] = None, repr(exc)
        return message

    incarnation = INCARNATION_ROUTE()
    incarnation.start(channel, incarnation_one, _close_fds(profile))
    message, reason = incarnation.receive()
    record = {
        "schema": ROUTE_SCHEMA,
        "route": incarnation.route,
        "root_pid": os.getpid(),
        "incarnations": [
            {"incarnation": 1, "pid": incarnation.pid, "parent_pid": os.getpid()},
        ],
        "threads_at_fork": incarnation.threads_at_fork,
        "disclosures": list(DISCLOSURES),
    }
    kind = message.get("kind") if message else None
    if kind != "stop":
        if kind in (None, "error"):
            record["kill"] = incarnation.kill(profile.get("interrupt_broker"))
        record["exit"] = incarnation.reap()
        if kind is None:
            raise IncarnationError(f"incarnation 1 sent no stop packet: {reason}")
        if kind == "error":
            raise IncarnationError("incarnation 1 failed: " + str(message.get("error")))
    else:
        packet = message.get("packet")
        if not isinstance(packet, dict) or packet.get("schema") != STOP_SCHEMA:
            record["kill"] = incarnation.kill(profile.get("interrupt_broker"))
            record["exit"] = incarnation.reap()
            raise IncarnationError("incarnation 1 sent a stop message without a stop packet")
        record["kill"] = incarnation.kill(profile.get("interrupt_broker"))
        record["exit"] = incarnation.reap()
        record["stop"] = packet["stop"]
        record["deferred"] = packet["deferred"]
        if message.get("pid") != incarnation.pid:
            raise IncarnationError("the stop packet came from another process")
    paths = message.get("paths")
    if not isinstance(paths, dict) or "onset" not in paths:
        raise IncarnationError("incarnation 1 reported no controller artifact paths")
    if kind == "stop" and record["stop"].get("trigger") == "process_launch":
        record["onset_partial"] = onset_observation(paths)

    # Incarnation 2's harness side: fresh roles, processes and hooks continuing the schedule.
    roles = profile["roles_factory"](run_dir)
    processes = profile["processes_factory"](run_dir)
    hooks = role_substitution.case_hooks(schedule_plan, roles, prepared["case_root"])
    olp.operator_run_id_symbol(hooks, operator_run_id)
    state = packet["state"] if kind == "stop" else message.get("state")
    if state is None:
        raise IncarnationError(
            "incarnation 1 hook state was not exported: " + str(message.get("state_error"))
        )
    hooks.injection.import_state(state)
    hooks.rebinds = copy.deepcopy(message.get("rebinds") or [])
    observer = evidence_projection.HarnessLaunchObserver(chained=hooks)
    observer.records = list(message.get("observer_records") or [])
    checkpoint = read_checkpoint(run_dir)
    stand_in = SimpleNamespace(
        root=run_dir,
        paths={k: Path(v) for k, v in paths.items()},
        source=Path(paths["source"]),
        context=checkpoint["context"],
    )
    controller = stand_in
    if kind == "stop":
        record["deferred_applied"] = hooks.injection.apply_deferred(stand_in)
    if kind == "stop" and hooks.injection.resume_requested:
        index = next(
            r["index"]
            for r in hooks.injection.records
            if r["operation"] == "request_transition" and r.get("deferred_after_stop") is not None
        )
        ledger = (checkpoint["context"].get("operator_channel") or {}).get("ledger_path")
        if not ledger:
            raise IncarnationError("the controller opened no operator ledger for this run")
        resume_record = harness_resume_record(index, operator_run_id)
        record["operator_resume_record"] = {
            **resume_record,
            "ledger_path": ledger,
            "record_hash": operator_ledger.append_record(ledger, resume_record),
            "authored_by": "harness (disclosed)",
        }
        request = {
            "action": "resume",
            "initiated_by": "operator_ledger",
            "requested_by": "operator",
            "run_id": operator_run_id,
        }
        record["resume_request"] = request
        record["incarnations"].append(
            {"incarnation": 2, "pid": os.getpid(), "parent_pid": os.getppid()}
        )
        try:
            controller = Controller.request_resume(
                run_dir, processes, roles, request, hooks=observer
            )
        except Refused as exc:
            record["resume_result"] = {"result": "REFUSED", "gate": exc.gate}
        else:
            record["resume_result"] = {"result": "ACCEPTED"}
            try:
                controller.run()
            except Exception:  # noqa: BLE001 - the controller recorded its own terminal
                pass
    return SimpleNamespace(
        controller=controller,
        observer=observer,
        schedule_hooks=hooks,
        processes=processes,
        roles=roles,
        record=json.loads(json.dumps(record, allow_nan=False, default=str)),
    )
