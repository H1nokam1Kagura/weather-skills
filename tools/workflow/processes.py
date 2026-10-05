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


def stop(proc):
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
            capture_output=True,
            creationflags=subprocess.CREATE_NO_WINDOW,
            check=False,
        )
    else:
        os.killpg(proc.pid, signal.SIGKILL)
    proc.wait(timeout=30)


class Processes:
    def __init__(self, root, timeout=900, ram_floor=2, total_seconds=1800):
        self.root = Path(root)
        self.timeout = timeout
        self.ram_floor = ram_floor
        self.total_seconds = total_seconds
        self.deadline = time.monotonic() + total_seconds
        self.serial = max(
            (int(p.stem.split("-")[1]) for p in self.root.glob("process-*.json")), default=0
        )

    def run(self, argv, cwd, env, stdin=""):
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
                        stop(proc)
                        break
                    time.sleep(1)
            except BaseException:
                if proc.poll() is None:
                    stop(proc)
                raise
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
        (self.root / f"process-{self.serial:03}.json").write_text(json.dumps(result, indent=2))
        if reason:
            raise Refused(reason)
        return result
