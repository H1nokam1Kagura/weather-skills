"""Targeted Windows observation for a harness-owned process tree.

This helper is NOT exhaustive controller signal capture. It neither intercepts
taskkill nor discovers descendants. The harness must register each known child.
Only an observed descendant of the supplied live Popen root can be terminated.
"""

import ctypes
import os
import subprocess
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from ctypes import wintypes


class ObservationError(RuntimeError):
    pass


@dataclass(frozen=True)
class Identity:
    pid: int
    created_100ns: int
    parent_pid: int


def _api():
    if os.name != "nt":
        raise ObservationError("Only Windows is supported")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    native = ctypes.WinDLL("ntdll")
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    kernel.GetProcessTimes.restype = wintypes.BOOL
    kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel.GetExitCodeProcess.restype = wintypes.BOOL
    kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel.TerminateProcess.restype = wintypes.BOOL
    native.NtQueryInformationProcess.argtypes = [
        wintypes.HANDLE, wintypes.ULONG, ctypes.c_void_p, wintypes.ULONG,
        ctypes.POINTER(wintypes.ULONG),
    ]
    native.NtQueryInformationProcess.restype = wintypes.LONG
    return kernel, native


@contextmanager
def _handle(pid, terminate=False):
    kernel, native = _api()
    # Query only one explicitly supplied PID; no system process enumeration.
    handle = kernel.OpenProcess(0x0400 | (0x0001 if terminate else 0), False, pid)
    if not handle:
        raise ObservationError(f"OpenProcess({pid}) failed: {ctypes.get_last_error()}")
    try:
        yield kernel, native, handle
    finally:
        kernel.CloseHandle(handle)


def _identity(kernel, native, handle, pid):
    class Basic(ctypes.Structure):
        _fields_ = [
            ("exit_status", wintypes.LONG), ("peb", ctypes.c_void_p),
            ("affinity", ctypes.c_size_t), ("priority", wintypes.LONG),
            ("pid", ctypes.c_size_t), ("parent", ctypes.c_size_t),
        ]

    basic = Basic()
    returned = wintypes.ULONG()
    status = native.NtQueryInformationProcess(
        handle, 0, ctypes.byref(basic), ctypes.sizeof(basic), ctypes.byref(returned)
    )
    if status < 0 or basic.pid != pid:
        raise ObservationError("Parent identity query failed")
    created, exited, kernel_time, user_time = (wintypes.FILETIME() for _ in range(4))
    if not kernel.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited),
                                  ctypes.byref(kernel_time), ctypes.byref(user_time)):
        raise ObservationError("Creation identity query failed")
    exit_code = wintypes.DWORD()
    if not kernel.GetExitCodeProcess(handle, ctypes.byref(exit_code)) or exit_code.value != 259:
        raise ObservationError("Process is not observed live")
    return Identity(pid, created.dwLowDateTime | created.dwHighDateTime << 32, basic.parent)


def observe(pid):
    with _handle(pid) as (kernel, native, handle):
        return _identity(kernel, native, handle, pid)


class OwnedTreeCapture:
    exhaustive_controller_capture = False

    def __init__(self, root_process):
        if not isinstance(root_process, subprocess.Popen):
            raise ObservationError("Root must be an owned Popen object")
        if root_process.poll() is not None:
            raise ObservationError("Owned Popen root already exited")
        self.root_process = root_process
        self.root = observe(root_process.pid)
        if self.root.parent_pid != os.getpid():
            raise ObservationError("Root is not this harness's direct child")
        self.known = {self.root.pid: self.root}
        self.events = []

    def _ancestry(self, target):
        chain = []
        current = target
        while True:
            if current.pid in {item.pid for item in chain}:
                raise ObservationError("Ancestry cycle")
            if observe(current.pid) != current:
                raise ObservationError("Process identity changed")
            chain.append(current)
            if current == self.root:
                return chain
            parent = self.known.get(current.parent_pid)
            if parent is None or parent.created_100ns > current.created_100ns:
                raise ObservationError("Target is outside verified owned ancestry")
            current = parent

    def register_descendant(self, pid):
        identity = observe(pid)
        if identity == self.root:
            raise ObservationError("Expected descendant, not root")
        self._ancestry(identity)
        self.known[pid] = identity
        return identity

    def terminate(self, identity):
        if identity == self.root or self.known.get(identity.pid) != identity:
            raise ObservationError("Target is not an authorized registered descendant")
        with _handle(identity.pid, terminate=True) as (kernel, native, handle):
            if _identity(kernel, native, handle, identity.pid) != identity:
                raise ObservationError("Target identity changed")
            ancestry = self._ancestry(identity)
            record = {
                "sequence": len(self.events) + 1,
                "observed_ns": time.monotonic_ns(),
                "target": asdict(identity),
                "ancestry": [asdict(item) for item in ancestry],
                "operation": "TerminateProcess",
                "requested_ns": time.monotonic_ns(),
                "status": "ATTEMPTED",
            }
            self.events.append(record)
            # Same handle as identity observation: PID reuse cannot retarget this call.
            success = bool(kernel.TerminateProcess(handle, 97))
            record["returned_ns"] = time.monotonic_ns()
            record["status"] = "REQUEST_ACCEPTED" if success else "REQUEST_FAILED"
            record["winerror"] = 0 if success else ctypes.get_last_error()
            if not success:
                raise ObservationError("Termination request failed; see recorded attempt")
            return record
