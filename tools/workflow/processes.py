"""Sequential subprocesses with bounded output, wall time and available-memory guard.

Evidence path (opt-in; nothing changes when no new option is passed):

* ``on_event(name, payload)`` receives, per ``run()``: ``launch_check`` for every launch
  decision (refused ones too, before the refusal is raised), ``process_launch`` right
  after Popen and before any wait, ``process_exit`` once the root has exited, then
  ``signal_stop`` when the per-PID stop ran. A durable start receipt
  ``process-NNN.start.json`` (same fields as ``process_launch``) is written before that
  event and before any wait. An exception from ``on_event`` never stops cleanup: it is
  logged, the child is stopped and reaped, and the first such exception is re-raised
  afterwards. One at ``launch_check`` is raised at once: no child exists yet. Once Popen
  returned, ``process_launch`` always precedes ``process_exit`` (also when the start
  receipt cannot be written), and a stop that itself fails falls back to taskkill/killpg,
  the job and ``kill()`` so the child never survives and ``process_exit`` still follows
  (stop_reason ``exception:<Type>+stop_failed``). A stopped child's ``process_exit``
  carries ``exit_code`` and ``wall_seconds`` both null (EVIDENCE_CONTRACT v2_5
  process_reconciliation); ``min_free_gib`` is null when no in-run sample was taken.
* Guard refusals carry ``gate="resource_guard"`` at the raise site (owner ruling
  role_cli_resource_gate_2026-10-14): the launch-check RAM floor and aggregate time budget,
  an unmeasurable RAM reading, and an in-run stop by the RAM guard or the wall-time guard
  (per-process timeout or aggregate deadline). The gate therefore survives every caller's
  call-site tagging, including Controller.ask for a role CLI. The output-budget stop stays
  untagged: it is a role-output (or tool) failure that the caller's call site classifies.
* ``memory_reader`` replaces ``free_gib`` at the launch check and in sampling; its
  readings are reported as-is, with ``reading_source`` ``observed`` or ``injected``.
* ``signal_capture=True`` (Windows) launches the root suspended inside a census job,
  pins it in an ``AncestryJournal`` before it runs, and replaces ``taskkill /T`` with
  ``signal_stop.stop_tree`` on every stop path. After a normal root exit the census is
  checked and surviving owned descendants are stopped the same way, so a tool leaving
  children behind is a ``signal_stop`` event, not hidden. See signal_stop's docstring
  for which targets count as signalled and the residual races.
"""

import ctypes
import json
import logging
import os
import re
import signal
import subprocess
import tempfile
import time
from pathlib import Path

from . import signal_stop as ss
from .contracts import Refused
from .persistence import atomic_json

LOG = logging.getLogger(__name__)
# In-run stop reasons that are the RAM floor or the wall-time guard (resource_guard).
RAM_GUARD = "RAM guard"
TIME_GUARD = "time budget exhausted"
OUTPUT_GUARD = "output budget exhausted"
RESOURCE_STOPS = frozenset({RAM_GUARD, TIME_GUARD})
_RECEIPT = re.compile(r"process-(\d+)(?:\.start)?\.json")


def _note(exc, text):
    try:
        exc.add_note(text)
    except BaseException:
        pass  # Never replace the original failure.


def free_gib():
    if os.name == "nt":
        buffer = ctypes.create_string_buffer(64)
        ctypes.c_ulong.from_buffer(buffer).value = 64
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(buffer):
            raise ctypes.WinError()
        return ctypes.c_ulonglong.from_buffer(buffer, 16).value / 2**30
    for line in Path("/proc/meminfo").read_text().splitlines():
        if line.startswith("MemAvailable:"):
            return int(line.split()[1]) / 1024**2
    raise Refused("available RAM cannot be measured", gate="resource_guard")


def stop(proc, observe=None, reason="unspecified"):
    observer_errors = []

    def emit(event, **details):
        if observe is None:
            return
        try:
            observe(event, proc, **details)
        except BaseException as exc:
            observer_errors.append({"event": event, "error_type": type(exc).__name__})

    def annotate(error):
        if observer_errors:
            try:
                error.add_note("Incomplete stop observation: " + json.dumps(observer_errors))
            except BaseException:
                pass  # Never replace the original backend/wait failure.

    mechanism = "taskkill_tree" if os.name == "nt" else "killpg"
    emit(
        "stop_request",
        reason=reason,
        mechanism=mechanism,
        requested_root_pid=proc.pid,
        actual_target_set=None,
    )
    try:
        if os.name == "nt":
            result = subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                capture_output=True,
                creationflags=subprocess.CREATE_NO_WINDOW,
                check=False,
            )
            backend_exit = result.returncode if observe is not None else None
            backend_output = (
                {
                    "stdout": result.stdout[:4096].decode("utf-8", errors="replace"),
                    "stderr": result.stderr[:4096].decode("utf-8", errors="replace"),
                    "truncated": len(result.stdout) > 4096 or len(result.stderr) > 4096,
                }
                if observe is not None
                else None
            )
        else:
            os.killpg(proc.pid, signal.SIGKILL)
            backend_exit = None
            backend_output = None
        emit(
            "stop_backend_return",
            mechanism=mechanism,
            backend_exit_code=backend_exit,
            backend_output=backend_output,
            actual_target_set=None,
        )
    except BaseException as exc:
        emit(
            "stop_backend_error",
            mechanism=mechanism,
            error_type=type(exc).__name__,
            actual_target_set=None,
        )
        annotate(exc)
        raise
    try:
        proc.wait(timeout=30)
    except BaseException as exc:
        emit("stop_wait_error", error_type=type(exc).__name__)
        annotate(exc)
        raise
    emit("stop_wait_return", exit_code=proc.returncode)
    if observe is not None:
        return {
            "observer_errors": observer_errors,
            "backend_exit_code": backend_exit,
            "exhaustive_signal_capture": False,
        }


class Processes:
    def __init__(
        self,
        root,
        timeout=900,
        ram_floor=2,
        total_seconds=1800,
        observer=None,
        on_event=None,
        memory_reader=None,
        signal_capture=False,
    ):
        self.root = Path(root)
        self.timeout = timeout
        self.ram_floor = ram_floor
        self.total_seconds = total_seconds
        self.deadline = time.monotonic() + total_seconds
        self.observer = observer
        self.observation_events = []
        self.on_event = on_event
        # None (or free_gib itself) is the observed default. It is resolved at call
        # time, so a patch of this module's free_gib still reaches it.
        self.memory_reader = None if memory_reader is free_gib else memory_reader
        if signal_capture and os.name != "nt":
            raise Refused(
                "signal_capture needs the Windows census job and per-PID stop",
                gate="resource_guard",
            )
        self.signal_capture = bool(signal_capture)
        # A start receipt shares the serial of its final record; both reserve it.
        self.serial = max(
            (
                int(m.group(1))
                for p in self.root.glob("process-*.json")
                if (m := _RECEIPT.fullmatch(p.name))
            ),
            default=0,
        )

    @property
    def _evidence(self):
        return self.on_event is not None or self.signal_capture

    def _read_memory(self):
        return free_gib() if self.memory_reader is None else self.memory_reader()

    def _emit(self, event, payload, deferred=None):
        if self.on_event is None:
            return
        try:
            self.on_event(event, payload)
        except BaseException as exc:
            LOG.exception("on_event raised for %s", event)
            if deferred is None:
                raise
            deferred.append(exc)

    def _observe(self, event, proc, **details):
        if self.observer is None:
            return
        record = {
            "sequence": len(self.observation_events) + 1,
            "monotonic_ns": time.monotonic_ns(),
            "event": event,
            "requested_root_pid": proc.pid,
            "details": details,
            "exhaustive_signal_capture": False,
        }
        self.observation_events.append(record)
        try:
            observation = self.observer(event, proc, details)
            # Detach and reject invalid evidence while still preserving cleanup.
            record["observation"] = json.loads(json.dumps(observation, allow_nan=False))
            record["observation_status"] = "RETURNED"
        except BaseException as exc:
            # Observers cannot take control of the process lifecycle, even by
            # raising SystemExit/KeyboardInterrupt. Missing evidence stays explicit.
            record["observation_status"] = "ERROR"
            record["observer_error_type"] = type(exc).__name__

    def _stop(self, proc, reason, capture=None):
        """Stop the child. Returns the per-PID record under signal capture, else None."""
        if capture is None:
            if self.observer is None:
                stop(proc)  # Preserve the legacy call contract and cleanup behavior.
            else:
                stop(proc, observe=self._observe, reason=reason)
            return None
        journal, job = capture
        self._observe(
            "stop_request",
            proc,
            reason=reason,
            mechanism="per_pid_terminate",
            requested_root_pid=proc.pid,
            actual_target_set=None,
        )
        record = ss.stop_tree(journal, job=job)
        record["trigger"] = reason
        self._observe(
            "stop_backend_return",
            proc,
            mechanism="per_pid_terminate",
            backend_exit_code=None,
            backend_output=None,
            actual_target_set=list(record["signalled_pids"]),
        )
        proc.wait(timeout=30)
        self._observe("stop_wait_return", proc, exit_code=proc.returncode)
        return record

    def _launch(self, argv, popen_kwargs):
        if not self.signal_capture:
            return subprocess.Popen(argv, **popen_kwargs), None
        backend = ss.Win32Backend()
        pinned = []

        def pin(proc):  # the root is a job member and suspended: no descendant exists
            pinned.append(ss.AncestryJournal(proc, backend=backend))

        proc, job = ss.launch_in_census_job(argv, backend=backend, on_suspended=pin, **popen_kwargs)
        return proc, (pinned[0].start(), job)

    @staticmethod
    def _survivors(job):
        try:
            return bool(job.backend.job_pids(job.handle))
        except ss.SignalStopError:
            return True  # census unreadable: run the stop so its record says why

    def _emit_exit(self, serial, proc, killed, sizes, started, reason, deferred):
        # EVIDENCE_CONTRACT.md v2_5.process_reconciliation: an integer exit_code has a
        # numeric end and vice versa; null/null only on a stopped run, with null
        # wall_seconds. A controller-stopped child therefore carries both as null.
        self._emit(
            "process_exit",
            {
                "serial": serial,
                "pid": proc.pid,
                "exit_code": None if killed else proc.returncode,
                "stdout_bytes": sizes[0],
                "stderr_bytes": sizes[1],
                "wall_seconds": None if killed else time.monotonic() - started,
                "stop_reason": reason,
            },
            deferred,
        )

    @staticmethod
    def _fallback_kill(proc, capture):
        """Last-resort cleanup after a failed stop: never let the child survive.

        Every step is guarded so the caller can still emit process_exit and re-raise
        its original failure. Returns the error type names observed, for a note.
        """
        errors = []
        steps = []
        if os.name == "nt":
            steps.append(
                lambda: subprocess.run(
                    ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                    capture_output=True,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                    check=False,
                    timeout=30,
                )
            )
        else:
            steps.append(lambda: os.killpg(proc.pid, signal.SIGKILL))
        if capture is not None:
            steps.append(
                lambda: capture[1].backend.terminate_job(capture[1].handle, ss.TERMINATE_EXIT_CODE)
            )
        steps.append(proc.kill)
        steps.append(lambda: proc.wait(timeout=30))
        for step in steps:
            try:
                step()
            except BaseException as exc:
                errors.append(type(exc).__name__)
        return errors

    def run(self, argv, cwd, env, stdin="", tool=None):
        observation_start = len(self.observation_events)
        tool = tool if tool is not None else Path(str(argv[0])).name
        reading = self._read_memory()
        budget_ok = reading >= self.ram_floor + 1
        time_ok = time.monotonic() < self.deadline
        self._emit(
            "launch_check",
            {
                "tool": tool,
                "free_ram_gib": reading,
                "budget_ok": budget_ok,
                "launched": budget_ok and time_ok,
                "reading_source": "observed" if self.memory_reader is None else "injected",
                "required_free_gib": self.ram_floor + 1,
                "time_budget_ok": time_ok,
            },
        )
        if not budget_ok:
            raise Refused("insufficient free RAM to start a child", gate="resource_guard")
        if not time_ok:
            raise Refused("aggregate time budget exhausted", gate="resource_guard")
        self.serial += 1
        serial = self.serial
        samples = []
        reason = None
        killed = False
        record = None
        deferred = []  # on_event failures held until the child is cleaned up
        with (
            tempfile.TemporaryFile() as output,
            tempfile.TemporaryFile() as error,
            tempfile.TemporaryFile() as input_stream,
        ):
            input_stream.write(stdin.encode("utf-8"))
            input_stream.seek(0)
            kwargs = (
                {"creationflags": subprocess.CREATE_NO_WINDOW}
                if os.name == "nt"
                else {
                    "start_new_session": True,
                }
            )

            def sizes():  # exact byte counts, measured before any decoding
                return os.fstat(output.fileno()).st_size, os.fstat(error.fileno()).st_size

            started = time.monotonic()
            started_ns = time.monotonic_ns()
            proc, capture = self._launch(
                argv,
                dict(
                    cwd=cwd,
                    env=env,
                    stdin=input_stream,
                    stdout=output,
                    stderr=error,
                    shell=False,
                    **kwargs,
                ),
            )
            launch = {
                "serial": serial,
                "tool": tool,
                "argv": [str(a) for a in argv],
                "pid": proc.pid,
                "parent_pid": os.getpid(),
                "started_monotonic_ns": started_ns,
            }
            launch_emitted = False  # a launched PID gets process_launch before process_exit
            try:
                try:
                    if self._evidence:
                        self.root.mkdir(parents=True, exist_ok=True)
                        try:
                            atomic_json(self.root / f"process-{serial:03}.start.json", launch)
                        except BaseException as exc:
                            _note(exc, "start receipt write failed after Popen")
                            raise
                    launch_emitted = True  # set before the call: a raising handler saw it
                    self._emit("process_launch", launch)
                    self._observe("launch", proc)
                    while proc.poll() is None:
                        samples.append(self._read_memory())
                        if samples[-1] < self.ram_floor:
                            reason = RAM_GUARD
                        if (
                            time.monotonic() - started > self.timeout
                            or time.monotonic() > self.deadline
                        ):
                            reason = TIME_GUARD
                        if sum(sizes()) > 4_000_000:
                            reason = OUTPUT_GUARD
                        if reason:
                            record = self._stop(proc, reason, capture)
                            killed = True
                            break
                        time.sleep(1)
                except BaseException as exc:
                    failure = "exception:" + type(exc).__name__
                    if not launch_emitted:
                        # The child exists (Popen returned): its launch is evidence even
                        # when the start receipt could not be written.
                        launch_emitted = True
                        self._emit("process_launch", launch, deferred)
                    # Under capture the tree is stopped even if the root already exited.
                    if proc.poll() is None or capture is not None:
                        killed = True
                        try:
                            record = self._stop(proc, failure, capture)
                        except BaseException as stop_exc:
                            record = None
                            fallback = self._fallback_kill(proc, capture)
                            failure += "+stop_failed"
                            _note(
                                exc,
                                f"stop failed ({type(stop_exc).__name__}); fallback cleanup "
                                f"errors: {fallback or 'none'}",
                            )
                    self._emit_exit(serial, proc, killed, sizes(), started, failure, deferred)
                    if record is not None:
                        self._emit("signal_stop", {"serial": serial, "record": record}, deferred)
                    if deferred:
                        _note(exc, f"on_event also failed {len(deferred)} time(s)")
                    raise
                if capture is not None and not killed and self._survivors(capture[1]):
                    record = self._stop(proc, "surviving descendants after root exit", capture)
            finally:
                if capture is not None:
                    capture[0].close()
                    capture[1].close()  # KILL_ON_JOB_CLOSE: last-resort safety net
            self._observe("process_exit", proc, exit_code=proc.returncode)
            measured = sizes()
            if sum(measured) > 4_000_000:
                reason = OUTPUT_GUARD
            output.seek(0)
            error.seek(0)
            result = {
                "argv": argv,
                "exit_code": proc.returncode,
                "stdout": output.read(4_000_001).decode("utf-8", errors="replace"),
                "stderr": error.read(4_000_001).decode("utf-8", errors="replace"),
                "stop_reason": reason,
                "elapsed_s": time.monotonic() - started,
                # No in-run sample is a missing minimum, never a post-exit reading
                # (W2 review processes.py:405); a reader failing here would also skip
                # process_exit for a launched PID.
                "min_free_gib": min(samples) if samples else None,
            }
            if self.observer is not None:
                result["process_observations"] = self.observation_events[observation_start:]
                result["exhaustive_signal_capture"] = (
                    record is not None and record["exhaustive"] is True
                )
            if self._evidence:
                result.update(
                    serial=serial,
                    tool=tool,
                    pid=proc.pid,
                    stdout_bytes=measured[0],
                    stderr_bytes=measured[1],
                    signal_stop=record,
                )
        self._emit_exit(serial, proc, killed, measured, started, reason, deferred)
        if record is not None:
            self._emit("signal_stop", {"serial": serial, "record": record}, deferred)
        (self.root / f"process-{self.serial:03}.json").write_text(json.dumps(result, indent=2))
        if deferred:
            if reason:
                deferred[0].add_note(f"process stop reason: {reason}")
            raise deferred[0]
        if reason in RESOURCE_STOPS:
            raise Refused(reason, gate="resource_guard")
        if reason:
            raise Refused(reason)
        return result
