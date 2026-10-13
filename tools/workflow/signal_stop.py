"""Per-PID controller stop with parent-at-signal capture (Windows prototype).

Prototype for the frozen CAP-SIGNAL-CAPTURE text: "Record every PID the controller
signals (terminated or not) and its parent at signal time." Instead of an opaque
``taskkill /T``, the controller enumerates its own tree and signals each PID itself,
writing the target's identity into the record BEFORE the signal is issued.

How ownership is proven (no kernel callbacks):

* Every proven member is held open by a process handle for the life of the tracker.
  Windows never reuses a PID while a handle to that process object is open, so a
  member's PID is pinned from its creation time until ``close()``.
* A snapshot entry ``(pid, ppid)`` becomes a member only if ``ppid`` is a pinned
  member, a handle on ``pid`` reports the same pid and parent, and its creation time
  is strictly later than the parent's. A stale ``ppid`` left by an exited parent whose
  PID was later reused fails the ordering check; a snapshot/open race where ``pid``
  is recycled fails the handle-parent check.
* A process whose parent exited before it was observed has an unprovable ancestry.
  Its parent PID is no longer pinned, so nothing links it to the root.

Completeness ("exhaustive") needs a census that does not depend on observation
timing. A Job object assigned while the root is still suspended provides one: every
non-breakaway descendant is a job member whatever its ancestry. The job is a CENSUS
and a final SAFETY NET only. It is never the per-PID signal evidence. Without a job,
an owned orphan is invisible, so ``exhaustive`` is always False. The routine never
claims exhaustive by default: it is True only when the reason list is empty.

``AncestryJournal`` polls continuously while the controller runs. It pins
intermediates while they are alive, so their orphaned descendants stay provable. The
residual race window is one poll gap plus the snapshot latency. A process that spawns
a child and exits inside that window leaves an unprovable orphan. The census detects
it, but the run cannot be exhaustive.

Signalled means "a running process the controller signalled". A target found exited
right before its signal (handle already signalled: the WaitForSingleObject form of
GetExitCodeProcess != STILL_ACTIVE, immune to a real exit code of 259) is recorded as
``already_exited``. A target whose TerminateProcess call fails while its handle then
reports exited (Windows fails TerminateProcess on an exited process with
ERROR_ACCESS_DENIED) exited on its own between that check and the call, and is recorded
as ``exited_before_signal``. Neither is in ``signalled_pids``/``signalled``; both stay
in ``signal_attempts`` and ``exited_before_signal``. A TerminateProcess refused on a
process that is still running is an attempt, not a signal: it is recorded in
``signal_attempts`` and ``signal_denied`` (once per PID; dropped if a later round signals
it) and adds an exhaustive reason. ``signalled_pids`` lists each PID once across stop
rounds, in first-signal order. Residual race, not closable from
user mode: a process already inside ExitProcess when TerminateProcess succeeds is
listed as signalled although it was ending, so its own end time may precede the
signal by a few microseconds. An empty signal list is never exhaustive evidence (D4).
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import threading
import time
from ctypes import wintypes
from dataclasses import dataclass, field
from datetime import UTC, datetime

SCHEMA = "signal-stop/1"
TERMINATE_EXIT_CODE = 0xC000013A & 0xFFFFFFFF  # STATUS_CONTROL_C_EXIT, distinguishable
JOB_ROLE = (
    "completeness census and final safety net only; per-PID signal evidence comes from "
    "toolhelp snapshots plus pinned handles, never from job membership"
)

# Win32 constants
TH32CS_SNAPPROCESS = 0x00000002
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
PROCESS_TERMINATE = 0x0001
PROCESS_SET_QUOTA = 0x0100
PROCESS_SUSPEND_RESUME = 0x0800
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
SYNCHRONIZE = 0x00100000
WAIT_OBJECT_0 = 0
WAIT_TIMEOUT = 258
ERROR_ACCESS_DENIED = 5
ERROR_INVALID_PARAMETER = 87
CREATE_SUSPENDED = 0x00000004
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
JOB_BASIC_ACCOUNTING = 1
JOB_BASIC_PROCESS_ID_LIST = 3
JOB_EXTENDED_LIMIT = 9


class SignalStopError(RuntimeError):
    pass


class SnapshotError(SignalStopError):
    pass


class OpenError(SignalStopError):
    def __init__(self, pid, winerror):
        super().__init__(f"OpenProcess({pid}) failed: winerror {winerror}")
        self.pid = pid
        self.winerror = winerror


def _utc_now():
    return datetime.now(UTC).isoformat(timespec="microseconds")


# --------------------------------------------------------------------------- Win32 backend


class _ProcessEntry(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", wintypes.LONG),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", wintypes.WCHAR * 260),
    ]


class _BasicInfo(ctypes.Structure):
    _fields_ = [
        ("exit_status", wintypes.LONG),
        ("peb", ctypes.c_void_p),
        ("affinity", ctypes.c_size_t),
        ("priority", wintypes.LONG),
        ("pid", ctypes.c_size_t),
        ("parent", ctypes.c_size_t),
    ]


class _BasicLimits(ctypes.Structure):
    _fields_ = [
        ("process_time", ctypes.c_longlong),
        ("job_time", ctypes.c_longlong),
        ("flags", wintypes.DWORD),
        ("min_ws", ctypes.c_size_t),
        ("max_ws", ctypes.c_size_t),
        ("active_limit", wintypes.DWORD),
        ("affinity", ctypes.c_size_t),
        ("priority", wintypes.DWORD),
        ("scheduling", wintypes.DWORD),
    ]


class _IoCounters(ctypes.Structure):
    _fields_ = [(n, ctypes.c_ulonglong) for n in ("ro", "wo", "oo", "rb", "wb", "ob")]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [
        ("basic", _BasicLimits),
        ("io", _IoCounters),
        ("process_memory", ctypes.c_size_t),
        ("job_memory", ctypes.c_size_t),
        ("peak_process", ctypes.c_size_t),
        ("peak_job", ctypes.c_size_t),
    ]


class _Accounting(ctypes.Structure):
    _fields_ = [
        (n, ctypes.c_longlong) for n in ("user", "kernel", "period_user", "period_kernel")
    ] + [
        ("faults", wintypes.DWORD),
        ("total", wintypes.DWORD),
        ("active", wintypes.DWORD),
        ("terminated", wintypes.DWORD),
    ]


_JOB_LIST_CAPACITY = 4096


class _JobPidList(ctypes.Structure):
    _fields_ = [
        ("assigned", wintypes.DWORD),
        ("in_list", wintypes.DWORD),
        ("pids", ctypes.c_size_t * _JOB_LIST_CAPACITY),
    ]


class Win32Backend:
    """Thin ctypes wrapper. Every method maps to one documented Win32/NT call."""

    def __init__(self):
        if os.name != "nt":
            raise SignalStopError("Win32Backend requires Windows")
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        n = ctypes.WinDLL("ntdll")
        H, D, B = wintypes.HANDLE, wintypes.DWORD, wintypes.BOOL
        k.CreateToolhelp32Snapshot.argtypes = [D, D]
        k.CreateToolhelp32Snapshot.restype = H
        for name in ("Process32FirstW", "Process32NextW"):
            getattr(k, name).argtypes = [H, ctypes.POINTER(_ProcessEntry)]
            getattr(k, name).restype = B
        k.OpenProcess.argtypes = [D, B, D]
        k.OpenProcess.restype = H
        k.CloseHandle.argtypes = [H]
        k.CloseHandle.restype = B
        k.GetProcessTimes.argtypes = [H] + [ctypes.POINTER(wintypes.FILETIME)] * 4
        k.GetProcessTimes.restype = B
        k.WaitForSingleObject.argtypes = [H, D]
        k.WaitForSingleObject.restype = D
        k.TerminateProcess.argtypes = [H, wintypes.UINT]
        k.TerminateProcess.restype = B
        k.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        k.CreateJobObjectW.restype = H
        k.SetInformationJobObject.argtypes = [H, ctypes.c_int, ctypes.c_void_p, D]
        k.SetInformationJobObject.restype = B
        k.QueryInformationJobObject.argtypes = [
            H,
            ctypes.c_int,
            ctypes.c_void_p,
            D,
            ctypes.POINTER(D),
        ]
        k.QueryInformationJobObject.restype = B
        k.AssignProcessToJobObject.argtypes = [H, H]
        k.AssignProcessToJobObject.restype = B
        k.TerminateJobObject.argtypes = [H, wintypes.UINT]
        k.TerminateJobObject.restype = B
        k.IsProcessInJob.argtypes = [H, H, ctypes.POINTER(B)]
        k.IsProcessInJob.restype = B
        n.NtQueryInformationProcess.argtypes = [
            H,
            wintypes.ULONG,
            ctypes.c_void_p,
            wintypes.ULONG,
            ctypes.POINTER(wintypes.ULONG),
        ]
        n.NtQueryInformationProcess.restype = wintypes.LONG
        n.NtResumeProcess.argtypes = [H]
        n.NtResumeProcess.restype = wintypes.LONG
        self.k, self.n = k, n

    # -- process table
    def snapshot(self):
        snap = self.k.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if not snap or snap == INVALID_HANDLE_VALUE:
            raise SnapshotError(f"CreateToolhelp32Snapshot failed: {ctypes.get_last_error()}")
        try:
            entry = _ProcessEntry()
            entry.dwSize = ctypes.sizeof(_ProcessEntry)
            rows = []
            ok = self.k.Process32FirstW(snap, ctypes.byref(entry))
            while ok:
                rows.append((int(entry.th32ProcessID), int(entry.th32ParentProcessID)))
                ok = self.k.Process32NextW(snap, ctypes.byref(entry))
            if not rows:
                raise SnapshotError("Process snapshot returned no rows")
            return rows
        finally:
            self.k.CloseHandle(snap)

    def open(self, pid):
        access = PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE
        handle = self.k.OpenProcess(access | PROCESS_TERMINATE, False, pid)
        if not handle:
            err = ctypes.get_last_error()
            if err != ERROR_ACCESS_DENIED:
                raise OpenError(pid, err)
            # Observable but not signalable: the signal attempt will record 'denied'.
            handle = self.k.OpenProcess(access, False, pid)
            if not handle:
                raise OpenError(pid, ctypes.get_last_error())
        return handle

    def identity(self, handle):
        """(pid, creation-time parent pid, created_100ns), all read from the same handle."""
        basic = _BasicInfo()
        got = wintypes.ULONG()
        status = self.n.NtQueryInformationProcess(
            handle, 0, ctypes.byref(basic), ctypes.sizeof(basic), ctypes.byref(got)
        )
        if status < 0:
            raise SignalStopError(f"NtQueryInformationProcess failed: {status & 0xFFFFFFFF:#x}")
        times = [wintypes.FILETIME() for _ in range(4)]
        if not self.k.GetProcessTimes(handle, *(ctypes.byref(t) for t in times)):
            raise SignalStopError(f"GetProcessTimes failed: {ctypes.get_last_error()}")
        created = times[0].dwLowDateTime | times[0].dwHighDateTime << 32
        return int(basic.pid), int(basic.parent), int(created)

    def is_exited(self, handle):
        return self.k.WaitForSingleObject(handle, 0) == WAIT_OBJECT_0

    def wait(self, handle, timeout_ms):
        return self.k.WaitForSingleObject(handle, int(timeout_ms)) == WAIT_OBJECT_0

    def terminate(self, handle, code):
        if self.k.TerminateProcess(handle, code):
            return True, 0
        return False, ctypes.get_last_error()

    def close(self, handle):
        self.k.CloseHandle(handle)

    # -- job census
    def create_job(self):
        job = self.k.CreateJobObjectW(None, None)
        if not job:
            raise SignalStopError(f"CreateJobObjectW failed: {ctypes.get_last_error()}")
        limits = _ExtendedLimits()
        limits.basic.flags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE  # no BREAKAWAY_OK
        if not self.k.SetInformationJobObject(
            job, JOB_EXTENDED_LIMIT, ctypes.byref(limits), ctypes.sizeof(limits)
        ):
            err = ctypes.get_last_error()
            self.k.CloseHandle(job)
            raise SignalStopError(f"SetInformationJobObject failed: {err}")
        return job

    def assign_suspended_and_resume(self, job, pid, before_resume=None):
        access = PROCESS_SET_QUOTA | PROCESS_TERMINATE | PROCESS_SUSPEND_RESUME
        handle = self.k.OpenProcess(access, False, pid)
        if not handle:
            raise OpenError(pid, ctypes.get_last_error())
        try:
            if not self.k.AssignProcessToJobObject(job, handle):
                raise SignalStopError(f"AssignProcessToJobObject failed: {ctypes.get_last_error()}")
            if before_resume is not None:
                before_resume()  # the root is a job member and has not run an instruction
            status = self.n.NtResumeProcess(handle)
            if status < 0:
                raise SignalStopError(f"NtResumeProcess failed: {status & 0xFFFFFFFF:#x}")
        finally:
            self.k.CloseHandle(handle)

    def job_pids(self, job):
        data = _JobPidList()
        if not self.k.QueryInformationJobObject(
            job, JOB_BASIC_PROCESS_ID_LIST, ctypes.byref(data), ctypes.sizeof(data), None
        ):
            raise SnapshotError(f"Job PID list query failed: {ctypes.get_last_error()}")
        if data.assigned > data.in_list:
            raise SnapshotError("Job PID list truncated")
        return [int(data.pids[i]) for i in range(data.in_list)]

    def job_accounting(self, job):
        acct = _Accounting()
        if not self.k.QueryInformationJobObject(
            job, JOB_BASIC_ACCOUNTING, ctypes.byref(acct), ctypes.sizeof(acct), None
        ):
            raise SnapshotError(f"Job accounting query failed: {ctypes.get_last_error()}")
        return {"total": acct.total, "active": acct.active, "terminated": acct.terminated}

    def in_job(self, handle, job):
        result = wintypes.BOOL()
        if not self.k.IsProcessInJob(handle, job, ctypes.byref(result)):
            raise SignalStopError(f"IsProcessInJob failed: {ctypes.get_last_error()}")
        return bool(result.value)

    def terminate_job(self, job, code):
        return bool(self.k.TerminateJobObject(job, code))

    def close_job(self, job):
        self.k.CloseHandle(job)


# --------------------------------------------------------------------------- census job


@dataclass
class CensusJob:
    """A job object the root was assigned to BEFORE its first instruction ran."""

    backend: object
    handle: object
    assigned_before_resume: bool

    def close(self):
        if self.handle is not None:
            self.backend.close_job(self.handle)  # KILL_ON_JOB_CLOSE: last-resort cleanup
            self.handle = None


def launch_in_census_job(argv, backend=None, on_suspended=None, **popen_kwargs):
    """Start ``argv`` suspended, assign it to a fresh job, then resume it.

    Returns ``(popen, CensusJob)``. Because assignment happens before the root runs,
    every descendant that does not break away is a job member. ``on_suspended(popen)``,
    if given, runs after assignment and before resume (e.g. to pin the root in an
    ``AncestryJournal`` with no gap); if it raises, the root is killed unresumed.
    """
    backend = backend or Win32Backend()
    flags = popen_kwargs.pop("creationflags", 0) | CREATE_SUSPENDED
    job = backend.create_job()
    try:
        proc = subprocess.Popen(argv, creationflags=flags, **popen_kwargs)
    except BaseException:
        backend.close_job(job)
        raise
    try:
        if on_suspended is None:
            backend.assign_suspended_and_resume(job, proc.pid)
        else:
            backend.assign_suspended_and_resume(
                job, proc.pid, before_resume=lambda: on_suspended(proc)
            )
    except BaseException as exc:
        try:  # close_job runs even if kill/wait raise; the original failure propagates
            for step in (proc.kill, lambda: proc.wait(timeout=30)):
                try:
                    step()
                except BaseException as cleanup_exc:
                    try:
                        exc.add_note(f"launch cleanup also failed: {type(cleanup_exc).__name__}")
                    except BaseException:
                        pass
        finally:
            backend.close_job(job)
        raise
    return proc, CensusJob(backend, job, assigned_before_resume=True)


# --------------------------------------------------------------------------- tracker / journal


@dataclass
class Member:
    pid: int
    parent_pid: int
    created_100ns: int
    handle: object
    first_observed_ns: int
    first_observed_utc: str
    ancestry_proven: bool = True
    seen_during_stop: bool = False  # first observed after the first signal
    exited_observed: bool = field(default=False)


class AncestryJournal:
    """Pinned-handle ancestry journal for one controller-owned root.

    ``refresh()`` is one poll. ``start()`` runs polls continuously on a thread, so an
    intermediate that is alive for at least one poll gap is pinned before it exits.
    """

    def __init__(self, root_process, backend=None, controller_pid=None):
        self.backend = backend or Win32Backend()
        self.controller_pid = os.getpid() if controller_pid is None else controller_pid
        if root_process.poll() is not None:
            raise SignalStopError("Root already exited; ownership cannot be established")
        self.lock = threading.RLock()
        self.members = {}
        self.unproven = {}
        self.rejections = []
        self.exited_unobserved = []
        self.errors = []
        self.polls = 0
        self.poll_times_ns = []
        self.snapshot_ns = []
        self.stopping = False
        self._thread = None
        self._halt = threading.Event()
        self.poll_interval_s = None
        handle = self.backend.open(root_process.pid)
        pid, ppid, created = self.backend.identity(handle)
        if pid != root_process.pid or ppid != self.controller_pid:
            self.backend.close(handle)
            raise SignalStopError("Root is not this controller's direct child")
        self.root = self._add(pid, ppid, created, handle)
        self.controller_parent_pid = self._controller_parent()

    def _controller_parent(self):
        try:
            for pid, ppid in self.backend.snapshot():
                if pid == self.controller_pid:
                    return ppid
        except SnapshotError as exc:
            self.errors.append(f"controller parent lookup: {exc}")
        return 0

    def _add(self, pid, ppid, created, handle, proven=True):
        now = time.monotonic_ns()
        member = Member(pid, ppid, created, handle, now, _utc_now(), ancestry_proven=proven)
        member.seen_during_stop = self.stopping
        (self.members if proven else self.unproven)[pid] = member
        return member

    def refresh(self):
        """One snapshot pass. Returns newly proven members; never raises on races."""
        with self.lock:
            self.polls += 1
            self.poll_times_ns.append(time.monotonic_ns())
            started = time.monotonic_ns()
            try:
                rows = self.backend.snapshot()
            except SnapshotError as exc:
                self.errors.append(f"snapshot: {exc}")
                return []
            self.snapshot_ns.append(time.monotonic_ns() - started)
            added = []
            pending = [
                (pid, ppid)
                for pid, ppid in rows
                if pid not in self.members and pid not in self.unproven
            ]
            grew = True
            while grew:  # fixpoint: a new member may be the parent of a later row
                grew = False
                rest = []
                for pid, ppid in pending:
                    parent = self.members.get(ppid)
                    if parent is None or pid == ppid:
                        rest.append((pid, ppid))
                        continue
                    member = self._prove(pid, ppid, parent)
                    if member is not None:
                        added.append(member)
                        grew = True
                pending = rest
            return added

    def _prove(self, pid, ppid, parent):
        try:
            handle = self.backend.open(pid)
        except OpenError as exc:
            if exc.winerror == ERROR_INVALID_PARAMETER:
                # Exited between snapshot and open: never pinned, its children are orphans.
                self.exited_unobserved.append({"pid": pid, "parent_pid": ppid})
            else:
                self.errors.append(f"open {pid} (child of member {ppid}): {exc}")
            return None
        try:
            hpid, hppid, created = self.backend.identity(handle)
        except SignalStopError as exc:
            self.backend.close(handle)
            self.errors.append(f"identity {pid}: {exc}")
            return None
        reason = None
        if hpid != pid or hppid != ppid:
            reason = (
                "handle identity differs from snapshot row (PID reused between snapshot and open)"
            )
        elif created <= parent.created_100ns:
            reason = "created no later than the member holding its parent PID (stale parent PID)"
        if reason:
            self.backend.close(handle)
            self.rejections.append(
                {
                    "pid": pid,
                    "snapshot_parent_pid": ppid,
                    "handle_parent_pid": hppid,
                    "created_100ns": created,
                    "parent_created_100ns": parent.created_100ns,
                    "reason": reason,
                }
            )
            return None
        return self._add(pid, ppid, created, handle)

    def depth(self, member):
        d, cur, seen = 0, member, set()
        while cur.pid != self.root.pid and cur.pid not in seen:
            seen.add(cur.pid)
            parent = self.members.get(cur.parent_pid)
            if parent is None:
                return 10**6  # unproven: treat as a leaf
            cur, d = parent, d + 1
        return d

    def ancestry(self, member):
        if not member.ancestry_proven:
            return None
        chain, cur = [member.pid], member
        while cur.pid != self.root.pid:
            cur = self.members[cur.parent_pid]
            chain.append(cur.pid)
        return chain + [self.controller_pid]

    # -- continuous polling
    def start(self, poll_interval_s=0.02):
        self.poll_interval_s = poll_interval_s

        def loop():
            while not self._halt.wait(poll_interval_s):
                self.refresh()

        self._thread = threading.Thread(target=loop, name="ancestry-journal", daemon=True)
        self._thread.start()
        return self

    def halt(self):
        self._halt.set()
        if self._thread is not None:
            self._thread.join()

    def poll_stats(self):
        gaps = [b - a for a, b in zip(self.poll_times_ns, self.poll_times_ns[1:], strict=False)]
        return {
            "polls": self.polls,
            "poll_interval_s": self.poll_interval_s,
            "max_poll_gap_ms": round(max(gaps) / 1e6, 3) if gaps else None,
            "max_snapshot_ms": round(max(self.snapshot_ns) / 1e6, 3) if self.snapshot_ns else None,
        }

    def close(self):
        self.halt()
        with self.lock:
            for member in list(self.members.values()) + list(self.unproven.values()):
                if member.handle is not None:
                    self.backend.close(member.handle)
                    member.handle = None


# --------------------------------------------------------------------------- stop routine


def _census(journal, job, reasons, attempts_unproven):
    """Adopt job members that tree tracking could not prove. They are owned by census."""
    backend = journal.backend
    try:
        pids = backend.job_pids(job.handle)
    except SnapshotError as exc:
        reasons.append(f"job census failed: {exc}")
        return
    try:
        rows = dict(backend.snapshot())
    except SnapshotError as exc:
        rows = {}
        reasons.append(f"snapshot for census parents failed: {exc}")
    for pid in pids:
        if pid in journal.members or pid in journal.unproven:
            continue
        try:
            handle = backend.open(pid)
        except OpenError as exc:
            if exc.winerror != ERROR_INVALID_PARAMETER:
                reasons.append(f"job member {pid} could not be opened: {exc}")
            continue
        try:
            hpid, hppid, created = backend.identity(handle)
            owned = hpid == pid and backend.in_job(handle, job.handle)
        except SignalStopError as exc:
            backend.close(handle)
            reasons.append(f"job member {pid} identity failed: {exc}")
            continue
        if not owned:
            backend.close(handle)
            continue
        if rows.get(pid, hppid) != hppid:
            reasons.append(f"job member {pid} parent differs between snapshot and handle")
        parent = journal.members.get(hppid)
        if parent is not None and created > parent.created_100ns:
            # Created after the last snapshot: same proof as refresh(), from the handle.
            journal._add(pid, hppid, created, handle)
            continue
        journal._add(pid, hppid, created, handle, proven=False)
        attempts_unproven.append(pid)


def stop_tree(journal, job=None, timeout_s=10.0, settle_ms=200, attempts=None):
    """Stop the journal's tree leaf-first, one PID at a time, recording before signalling.

    Returns the evidence record. ``exhaustive`` is True only if ``exhaustive_reasons``
    is empty after every check below; any unresolved race adds a reason. ``attempts``
    may be a caller-owned list: each entry is appended before its signal is issued, so
    a supervisor can persist it even if the stop is interrupted.
    """
    backend = journal.backend
    journal.halt()
    reasons = []
    attempts = [] if attempts is None else attempts
    signalled = {}  # pid -> first successful entry: each PID listed once across rounds
    denied = {}  # pid -> first denied entry, dropped if a later round signals it
    exited_before = []
    unproven_found = []
    rounds = 0
    deadline = time.monotonic() + timeout_s
    with journal.lock:
        pre_stop_errors = len(journal.errors)
        while True:
            rounds += 1
            journal.refresh()
            if job is not None and job.handle is not None:
                _census(journal, job, reasons, unproven_found)
            everyone = list(journal.members.values()) + list(journal.unproven.values())
            live = [m for m in everyone if m.handle is not None and not backend.is_exited(m.handle)]
            if not live:
                break
            if time.monotonic() > deadline:
                reasons.append(
                    f"deadline hit with {len(live)} live member(s): "
                    + ",".join(str(m.pid) for m in live)
                )
                break
            live.sort(key=lambda m: (-journal.depth(m), -m.created_100ns))
            for member in live:
                entry = {
                    "sequence": len(attempts) + 1,
                    "round": rounds,
                    "pid": member.pid,
                    "parent_pid": member.parent_pid,
                    "created_100ns": member.created_100ns,
                    "observed_at_ns": time.monotonic_ns(),
                    "observed_at_utc": _utc_now(),
                    "first_observed_utc": member.first_observed_utc,
                    "ancestry_proven": member.ancestry_proven,
                    "ancestry": journal.ancestry(member),
                    "seen_during_stop": member.seen_during_stop,
                    "operation": "TerminateProcess",
                    "result": "RECORDED_BEFORE_SIGNAL",
                }
                attempts.append(entry)  # appended before the signal is issued
                if backend.is_exited(member.handle):
                    entry["result"] = "already_exited"  # not signalled
                    exited_before.append(entry)
                    continue
                journal.stopping = True  # from here on, a newly seen process is churn
                ok, err = backend.terminate(member.handle, TERMINATE_EXIT_CODE)
                entry["returned_at_ns"] = time.monotonic_ns()
                entry["winerror"] = err
                if not ok and backend.is_exited(member.handle):
                    # TerminateProcess fails on an exited process: it ended on its own
                    # between the check above and the call, so it was not signalled.
                    entry["result"] = "exited_before_signal"
                    exited_before.append(entry)
                    continue
                if ok:
                    entry["result"] = "ok"
                    signalled.setdefault(member.pid, entry)
                    denied.pop(member.pid, None)
                else:
                    # A refused TerminateProcess on a live process is an attempt, not a
                    # signal (W2 review signal_stop.py:713): never in signalled_pids.
                    entry["result"] = "denied"
                    if member.pid not in signalled:
                        denied.setdefault(member.pid, entry)
            for member in live:
                if member.handle is not None:
                    backend.wait(member.handle, settle_ms)
        if len(journal.errors) > pre_stop_errors:
            reasons.extend(f"tracking error: {e}" for e in journal.errors[pre_stop_errors:])
        if journal.errors[:pre_stop_errors]:
            reasons.append(f"{pre_stop_errors} tracking error(s) before stop (journal gaps)")

    job_info = {"used": job is not None, "role": JOB_ROLE}
    if job is None:
        reasons.append(
            "no completeness census: without a job (or kernel callbacks) an owned process "
            "whose parent exited unobserved is invisible, so its absence cannot be shown"
        )
    else:
        job_info["assigned_before_resume"] = job.assigned_before_resume
        if not job.assigned_before_resume:
            reasons.append("job assigned after the root ran: early descendants may be outside it")
        _finish_job(journal, job, reasons, job_info)
    for pid in unproven_found:
        reasons.append(f"owned process {pid} has unprovable ancestry (parent exited unobserved)")
    if denied:
        reasons.append(
            "TerminateProcess denied for live process(es): " + ",".join(str(pid) for pid in denied)
        )
    if not signalled:
        reasons.append("empty signal list: never exhaustive evidence (D4)")
    if journal.rejections:
        job_info["note_rejections"] = len(journal.rejections)

    everyone = list(journal.members.values()) + list(journal.unproven.values())
    tree = {str(m.pid): m.parent_pid for m in everyone}
    tree[str(journal.controller_pid)] = journal.controller_parent_pid
    churn = sorted(m.pid for m in everyone if m.seen_during_stop)
    record = {
        "schema": SCHEMA,
        "controller_pid": journal.controller_pid,
        "root_pid": journal.root.pid,
        "signalled_pids": list(signalled),
        "signalled": [
            {k: e[k] for k in ("pid", "parent_pid", "created_100ns", "result")}
            for e in signalled.values()
        ],
        "signal_denied": [
            {k: e[k] for k in ("pid", "parent_pid", "created_100ns", "result", "winerror")}
            for e in denied.values()
        ],
        "exited_before_signal": [
            {k: e[k] for k in ("pid", "parent_pid", "created_100ns", "result")}
            for e in exited_before
        ],
        "signal_attempts": attempts,
        "process_tree": tree,
        "exhaustive": not reasons,
        "exhaustive_reasons": reasons,
        "reasons": reasons,
        "rounds": rounds,
        "churn_pids": churn,
        "unproven_pids": sorted(journal.unproven),
        "rejections": journal.rejections,
        "exited_before_observation": journal.exited_unobserved,
        "journal": {
            "mode": "continuous" if journal.poll_interval_s else "stop_time_only",
            **journal.poll_stats(),
        },
        "job": job_info,
    }
    return record


def _finish_job(journal, job, reasons, info):
    backend = journal.backend
    if job.handle is None:
        reasons.append("job handle already closed")
        return
    for member in journal.members.values():
        if member.handle is None:
            continue
        try:
            if not backend.in_job(member.handle, job.handle):
                reasons.append(
                    f"member {member.pid} is outside the census job (breakaway): its "
                    "subtree is not census-covered"
                )
        except SignalStopError as exc:
            reasons.append(f"job membership of {member.pid} unknown: {exc}")
    try:
        acct = backend.job_accounting(job.handle)
    except SnapshotError as exc:
        reasons.append(str(exc))
        return
    info["accounting_before_safety_net"] = acct
    seen_in_job = len(journal.members) + len(journal.unproven)
    info["observed_members"] = seen_in_job
    # Informational: processes that lived and died without ever being observed. They
    # were never signalled, so they are not missing signal records; any live descendant
    # they left would have surfaced as an unproven census member above.
    info["never_observed_job_processes"] = max(0, acct["total"] - seen_in_job)
    if acct["active"]:
        try:
            left = backend.job_pids(job.handle)
        except SnapshotError:
            left = []
        backend.terminate_job(job.handle, TERMINATE_EXIT_CODE)
        info["safety_net_terminated_pids"] = left
        reasons.append(
            f"job safety net terminated {acct['active']} process(es) with no per-PID record"
        )
    else:
        info["safety_net_terminated_pids"] = []
