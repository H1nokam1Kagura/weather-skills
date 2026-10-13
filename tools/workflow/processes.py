"""Sequential subprocesses with bounded output, wall time and available-memory guard."""

import ctypes
import json
import os
import signal
import subprocess
import tempfile
import time
from pathlib import Path

from .contracts import Refused


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
    raise Refused("available RAM cannot be measured")


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
    emit("stop_request", reason=reason, mechanism=mechanism,
         requested_root_pid=proc.pid, actual_target_set=None)
    try:
        if os.name == "nt":
            result = subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                capture_output=True,
                creationflags=subprocess.CREATE_NO_WINDOW,
                check=False,
            )
            backend_exit = result.returncode if observe is not None else None
            backend_output = {
                "stdout": result.stdout[:4096].decode("utf-8", errors="replace"),
                "stderr": result.stderr[:4096].decode("utf-8", errors="replace"),
                "truncated": len(result.stdout) > 4096 or len(result.stderr) > 4096,
            } if observe is not None else None
        else:
            os.killpg(proc.pid, signal.SIGKILL)
            backend_exit = None
            backend_output = None
        emit("stop_backend_return", mechanism=mechanism,
             backend_exit_code=backend_exit, backend_output=backend_output,
             actual_target_set=None)
    except BaseException as exc:
        emit("stop_backend_error", mechanism=mechanism,
             error_type=type(exc).__name__, actual_target_set=None)
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
        return {"observer_errors": observer_errors, "backend_exit_code": backend_exit,
                "exhaustive_signal_capture": False}


class Processes:
    def __init__(self, root, timeout=900, ram_floor=2, total_seconds=1800, observer=None):
        self.root = Path(root)
        self.timeout = timeout
        self.ram_floor = ram_floor
        self.total_seconds = total_seconds
        self.deadline = time.monotonic() + total_seconds
        self.observer = observer
        self.observation_events = []
        self.serial = max(
            (int(p.stem.split("-")[1]) for p in self.root.glob("process-*.json")), default=0
        )

    def _observe(self, event, proc, **details):
        if self.observer is None:
            return
        record = {"sequence": len(self.observation_events) + 1,
                  "monotonic_ns": time.monotonic_ns(), "event": event,
                  "requested_root_pid": proc.pid, "details": details,
                  "exhaustive_signal_capture": False}
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

    def _stop(self, proc, reason):
        if self.observer is None:
            stop(proc)  # Preserve the legacy call contract and cleanup behavior.
        else:
            stop(proc, observe=self._observe, reason=reason)

    def run(self, argv, cwd, env, stdin=""):
        observation_start = len(self.observation_events)
        if free_gib() < self.ram_floor + 1:
            raise Refused("insufficient free RAM to start a child")
        if time.monotonic() >= self.deadline:
            raise Refused("aggregate time budget exhausted")
        self.serial += 1
        samples = []
        started = time.monotonic()
        reason = None
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
            proc = subprocess.Popen(
                argv,
                cwd=cwd,
                env=env,
                stdin=input_stream,
                stdout=output,
                stderr=error,
                shell=False,
                **kwargs,
            )
            try:
                self._observe("launch", proc)
                while proc.poll() is None:
                    samples.append(free_gib())
                    if samples[-1] < self.ram_floor:
                        reason = "RAM guard"
                    if (
                        time.monotonic() - started > self.timeout
                        or time.monotonic() > self.deadline
                    ):
                        reason = "time budget exhausted"
                    if (
                        os.fstat(output.fileno()).st_size + os.fstat(error.fileno()).st_size
                        > 4_000_000
                    ):
                        reason = "output budget exhausted"
                    if reason:
                        self._stop(proc, reason)
                        break
                    time.sleep(1)
            except BaseException as exc:
                if proc.poll() is None:
                    self._stop(proc, "exception:" + type(exc).__name__)
                raise
            self._observe("process_exit", proc, exit_code=proc.returncode)
            if os.fstat(output.fileno()).st_size + os.fstat(error.fileno()).st_size > 4_000_000:
                reason = "output budget exhausted"
            output.seek(0)
            error.seek(0)
            result = {
                "argv": argv,
                "exit_code": proc.returncode,
                "stdout": output.read(4_000_001).decode("utf-8", errors="replace"),
                "stderr": error.read(4_000_001).decode("utf-8", errors="replace"),
                "stop_reason": reason,
                "elapsed_s": time.monotonic() - started,
                "min_free_gib": min(samples) if samples else free_gib(),
            }
            if self.observer is not None:
                result["process_observations"] = self.observation_events[observation_start:]
                result["exhaustive_signal_capture"] = False
        (self.root / f"process-{self.serial:03}.json").write_text(json.dumps(result, indent=2))
        if reason:
            raise Refused(reason)
        return result
