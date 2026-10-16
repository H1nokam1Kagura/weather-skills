"""Interrupt broker: kills a controller incarnation from OUTSIDE the traced tree (Wave 7, sub-wave D).

Only for frozen v2.7 cases whose ``injection_schedule`` carries a ``harness_stop_controller``
payload (E10/E11/E14 interrupt family). Every other case runs exactly as before: the broker is not
started, no fd or env var below exists, and the analyzer output is byte-identical.

Process model (WAVE7-BRIEF.md, "Mechanism")
-------------------------------------------
``entry.sh`` (root) starts this broker as a root, UNTRACED process before the trace: it is a child
of the entry shell, never of strace, so nothing it does is a traced syscall and nothing it creates
is a created path. The traced root (``case_driver run``, uid 1000) runs ``run_case``, which forks
incarnation 1 (a direct child of the traced root running the controller). At the stop boundary a
tree process asks the broker to stop incarnation 1; the broker freezes it and all its
descendants, reads their output byte counts, SIGKILLs them, records the facts in ``$OUT`` and
acknowledges. The traced root then builds incarnation 2 (in-process). ``strace -ff`` follows the
fork, so the trace stays one continuous trace.

Channel (names sub-wave C calls)
--------------------------------
* FIFOs ``$OUT/interrupt-broker/request.fifo`` and ``$OUT/interrupt-broker/ack.fifo``
  (``BROKER_DIRNAME``/``REQUEST_FIFO``/``ACK_FIFO``), created by this broker (root, mode 0600, in a
  0700 directory) BEFORE the trace, inside ``$OUT`` (``/harness-out``, pruned from the baseline and
  never a traced path). RC10 holds: the traced tree creates nothing for the channel.
* The entry shell opens both read-write (``exec 5<>request.fifo 6<>ack.fifo``; O_RDWR never blocks
  on a FIFO) after the RAM sampler started and before strace, so only strace and the traced tree
  inherit them: **fd 5 = request (tree writes), fd 6 = ack (tree reads)** (``REQUEST_FD``,
  ``ACK_FD``). uid 1000 cannot open the FIFOs by path; it only holds the inherited fds.
* Env var ``SIMKIT_INTERRUPT_BROKER=request_fd=5,ack_fd=6`` (``ENV_VAR``), exported only when the
  broker is ready. Python's ``subprocess`` closes fds >= 3 in tools (close_fds=True), so tools see
  the variable but hold no channel; ``os.fork`` children (incarnations) inherit the fds.
* ``channel_from_env()`` returns a ``BrokerClient`` when the env var names fds that are FIFOs at
  the expected paths, else ``None`` (no broker: the in-process route, where the parent kills the
  child itself). ``case_driver.case_profile`` puts it under the run_case profile key
  **``interrupt_broker``** (``PROFILE_KEY``): a ``BrokerClient`` or ``None``.

Protocol (one JSON object per line, each line <= ``MAX_LINE`` = PIPE_BUF bytes, so a write is
atomic even with several writers)
-------------------------------------------------------------------------------------------------
request  ``{"op": "stop", "request_id": "<str>", "pid": <int>}``
         ``{"op": "ping", "request_id": "<str>"}``
ack      ``{"request_id": ..., "op": ..., "ok": <bool>, "errors": [<str>...], ...}``; a stop ack also
         carries ``pid``, ``verified``, ``descendants``, ``killed``, ``output_bytes`` and
         ``stop_index`` (the index of its record in ``stops``); a ping ack ``broker_pid``.
Recommended use: the traced root (parent) receives the incarnation's state packet, then calls
``BrokerClient.request_stop(child_pid)``, which blocks for the ack (bounded, default
``ACK_TIMEOUT_S``) and never raises: a timeout or a broken channel returns an ack with
``ok: False`` and the reason in ``errors``. If the incarnation requests its own stop instead, it is
frozen before it can read the ack; the parent then reads it with ``await_ack(request_id)``.

Verification (a stop is refused, ``verified: false``, nothing signalled, unless ALL hold)
-----------------------------------------------------------------------------------------
* ``pid`` exists, is not a zombie, and has a non-zero ``TracerPid`` T (it is in the traced tree);
* T is a child of the entry shell (``--entry-pid``) and equals ``tracer_pid`` in
  ``harness_status.txt`` when that is recorded;
* the parent R of ``pid`` is the traced root: R's parent is T and R's ``TracerPid`` is T. So
  ``pid`` is a DIRECT child of the traced root, and never the traced root itself;
* ``pid`` runs the same executable as R (a fork, not an exec'd tool) under the same non-zero uid.

Stop sequence (bounded everywhere; any failure acks ``ok: false`` with errors, never hangs)
-------------------------------------------------------------------------------------------
SIGSTOP ``pid``; repeatedly scan /proc for descendants and SIGSTOP each new one until a scan finds
none (``STOP_TIMEOUT_S``); read output byte counts; SIGKILL every member; wait until each is dead
(gone or zombie, ``KILL_TIMEOUT_S``). Signals go through pidfds where available (no pid-reuse
race after discovery). ``ok`` is true only if every member was confirmed stopped and killed.

Output bytes (harness-observed, OI-04): ``st_size`` of the regular file open on fd 1 / fd 2
(``stat`` of ``/proc/<pid>/fd/N``) taken while the process is frozen, before SIGKILL. That is the
controller's own basis (``Processes`` measures ``os.fstat(...).st_size`` of the tool's temporary
output files). ``null`` when the fd is not a regular file (pipe, socket, tty: bytes written are not
observable from outside) or unreadable, and when the file is the one the traced root holds on the
same fd (``shared_with_traced_root``: its size is not this process's output). The fd details
(target, kind, size, fdinfo ``pos``) are kept beside each count.

Record: ``$OUT/interrupt_broker.json`` (``RECORD_NAME``), rewritten atomically after start-up and
after every request: ``{schema, broker_pid, entry_pid, tracer_check, started_at_ns, ready,
complete, stopped_at_ns, output_bytes_basis, stops: [...], errors: [...]}``; each stop is
``{request_id, requested_at_ns, pid, verified, verification, descendants, stopped, output_bytes,
kill_sent_at_ns, killed, completed_at_ns, ok, errors}``. Times are ``time.time_ns()`` (the clock
strace stamps ``--timestamps=unix,ns`` with).

Analyzer (``analyze_trace.py``): the broker's facts land in ``evidence.json`` under the key
**``harness_interrupt_broker``** (``EVIDENCE_KEY``), each stop marked ``trace_confirmed`` only when
the trace shows that pid as a direct child of the traced root alive at the request and killed by
SIGKILL. Broker signals are harness actions, never controller signals (``signalled_pids``).

usage:
    python -m container_harness.interrupt_broker needs CASE_ID        (uid 1000, untraced)
        exit 0 when the case schedules harness_stop_controller, 1 when not, 2 on error
    python -I -B interrupt_broker.py serve --dir DIR --record PATH --entry-pid N
        [--status-file harness_status.txt]                            (root, untraced)
Stdlib only; importable on any platform (the broker itself needs Linux /proc).
"""

import json
import os
import select
import signal
import stat
import sys
import time

SCHEMA = "simkit.interrupt-broker/1"
ENV_VAR = "SIMKIT_INTERRUPT_BROKER"
REQUEST_FD, ACK_FD = 5, 6
BROKER_DIRNAME = "interrupt-broker"
REQUEST_FIFO, ACK_FIFO = "request.fifo", "ack.fifo"
READY_NAME = "ready"
RECORD_NAME = "interrupt_broker.json"
PROFILE_KEY = "interrupt_broker"
EVIDENCE_KEY = "harness_interrupt_broker"
STOP_MODE = "harness_stop_controller"
MAX_LINE = 4096  # PIPE_BUF on Linux: a write of at most this many bytes to a FIFO is atomic
ACK_TIMEOUT_S = 30.0
STOP_TIMEOUT_S = 3.0
KILL_TIMEOUT_S = 5.0
POLL_S = 0.01
OUTPUT_BYTES_BASIS = (
    "harness-observed: st_size of the regular file open on fd 1/2 (stat of /proc/<pid>/fd/N) "
    "while the process was frozen by SIGSTOP, before SIGKILL; the controller's own basis "
    "(os.fstat st_size of the tool output files). null when the fd is not a regular file "
    "(pipe/socket/tty), unreadable, or the file the traced root holds on the same fd"
)


# ------------------------------------------------------------------ case selection


def schedules_stop(case):
    """Number of injection_schedule entries whose payload mode is harness_stop_controller."""
    n = 0
    for entry in case.get("injection_schedule") or []:
        payload = entry.get("payload") if isinstance(entry, dict) else None
        if isinstance(payload, dict) and payload.get("mode") == STOP_MODE:
            n += 1
    return n


def needs(case_id):
    from container_harness import case_driver

    root, _ = case_driver.frozen_root()
    cases, _ratified = case_driver.load_cases(root)
    case = next(c for c in cases if c["id"] == case_id)
    n = schedules_stop(case)
    print(json.dumps({"case_id": case_id, "harness_stop_controller_entries": n}))
    return 0 if n else 1


# ------------------------------------------------------------------ /proc access


class ProcFS:
    """Reads of /proc (one object, so tests can substitute a fake process table)."""

    def __init__(self, root="/proc"):
        self.root = root

    def _path(self, pid, *parts):
        return os.path.join(self.root, str(pid), *parts)

    def pids(self):
        try:
            return [int(n) for n in os.listdir(self.root) if n.isdigit()]
        except OSError:
            return []

    def stat(self, pid):
        """-> {state, ppid, pgrp, starttime} or None (gone/unreadable)."""
        try:
            with open(self._path(pid, "stat"), encoding="utf-8", errors="replace") as f:
                text = f.read()
        except OSError:
            return None
        return parse_stat(text)

    def status(self, pid):
        """-> {ppid, tracer_pid, uid} or None."""
        try:
            with open(self._path(pid, "status"), encoding="utf-8", errors="replace") as f:
                text = f.read()
        except OSError:
            return None
        return parse_status(text)

    def exe(self, pid):
        try:
            return os.readlink(self._path(pid, "exe"))
        except OSError:
            return None

    def fd_target(self, pid, fd):
        try:
            return os.readlink(self._path(pid, "fd", str(fd)))
        except OSError:
            return None

    def fd_stat(self, pid, fd):
        try:
            return os.stat(self._path(pid, "fd", str(fd)))
        except OSError:
            return None

    def fd_pos(self, pid, fd):
        try:
            with open(self._path(pid, "fdinfo", str(fd)), encoding="utf-8") as f:
                for line in f:
                    if line.startswith("pos:"):
                        return int(line.split()[1])
        except (OSError, ValueError, IndexError):
            return None
        return None


def parse_stat(text):
    try:
        rest = text[text.rindex(")") + 2 :].split()
        return {
            "state": rest[0],
            "ppid": int(rest[1]),
            "pgrp": int(rest[2]),
            "starttime": int(rest[19]) if len(rest) > 19 else None,
        }
    except (ValueError, IndexError):
        return None


def parse_status(text):
    fields = {}
    for line in text.splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            fields[k.strip()] = v.split()
    try:
        return {
            "ppid": int(fields["PPid"][0]),
            "tracer_pid": int(fields["TracerPid"][0]),
            "uid": int(fields["Uid"][0]),
        }
    except (KeyError, ValueError, IndexError):
        return None


def children_map(procfs):
    kids = {}
    for pid in procfs.pids():
        st = procfs.stat(pid)
        if st is not None:
            kids.setdefault(st["ppid"], []).append(pid)
    return kids


def descendants(procfs, pid):
    """Every live descendant of pid (by current parent), breadth-first, pid excluded."""
    kids, out, queue = children_map(procfs), [], [pid]
    while queue:
        for c in sorted(kids.get(queue.pop(0), [])):
            if c not in out and c != pid:
                out.append(c)
                queue.append(c)
    return out


def _read_status_file(path):
    out = {}
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                if "=" in line:
                    k, v = line.rstrip("\n").split("=", 1)
                    out[k.strip()] = v.strip()
    except OSError:
        pass
    return out


def verify_target(pid, procfs, *, entry_pid=None, recorded_tracer=None, assume_tracer=None):
    """-> (facts, errors). errors empty means the stop may proceed (see the module docstring).

    ``assume_tracer`` is for tests on an untraced tree only (the broker's ``--assume-tracer``,
    which entry.sh never passes): that pid stands in for the tracer and TracerPid is not read.
    """
    facts, errors = {"pid": pid}, []
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 1:
        return facts, ["pid must be an integer > 1"]
    st, ps = procfs.stat(pid), procfs.status(pid)
    if st is None or ps is None:
        return facts, ["pid does not exist or is unreadable"]
    if st["state"] in ("Z", "X"):
        return facts, ["pid is a zombie"]
    tracer = assume_tracer if assume_tracer is not None else ps["tracer_pid"]
    facts["tracer_pid"] = tracer
    if not tracer:
        errors.append("pid is not traced (TracerPid 0): not in the traced tree")
        return facts, errors
    tst = procfs.status(tracer)
    if tst is None:
        errors.append(f"tracer {tracer} is unreadable")
        return facts, errors
    if entry_pid is not None and tst["ppid"] != entry_pid:
        errors.append(f"tracer {tracer} is not a child of the entry shell {entry_pid}")
    if recorded_tracer is not None and tracer != recorded_tracer:
        errors.append(f"tracer {tracer} is not the recorded tracer_pid {recorded_tracer}")
    root = ps["ppid"]
    facts["traced_root_pid"] = root
    if pid == root or ps["ppid"] == tracer:
        errors.append("pid is the traced root itself")
        return facts, errors
    rst = procfs.status(root)
    if rst is None:
        errors.append(f"parent {root} is unreadable")
        return facts, errors
    if rst["ppid"] != tracer:
        errors.append(f"parent {root} is not the traced root (its parent is not the tracer)")
    if assume_tracer is None and rst["tracer_pid"] != tracer:
        errors.append(f"parent {root} is not traced by {tracer}")
    facts["uid"] = ps["uid"]
    if ps["uid"] == 0 or ps["uid"] != rst["uid"]:
        errors.append("pid uid is root or differs from the traced root's")
    exe, rexe = procfs.exe(pid), procfs.exe(root)
    facts["exe"], facts["traced_root_exe"] = exe, rexe
    if exe is None or exe != rexe:
        errors.append("pid does not run the traced root's executable (not a fork)")
    facts["starttime"] = st["starttime"]
    return facts, errors


# ------------------------------------------------------------------ signalling


class Target:
    """One process to freeze and kill, held through a pidfd when the platform offers one."""

    def __init__(self, pid, parent):
        self.pid, self.parent, self.pidfd = pid, parent, None
        try:
            self.pidfd = os.pidfd_open(pid)
        except (AttributeError, OSError):
            self.pidfd = None

    def send(self, sig):
        if self.pidfd is not None:
            signal.pidfd_send_signal(self.pidfd, sig)
        else:
            os.kill(self.pid, sig)

    def close(self):
        if self.pidfd is not None:
            try:
                os.close(self.pidfd)
            except OSError:
                pass
            self.pidfd = None


def _kind(mode):
    for test, name in (
        (stat.S_ISREG, "regular"),
        (stat.S_ISFIFO, "fifo"),
        (stat.S_ISSOCK, "socket"),
        (stat.S_ISCHR, "char"),
    ):
        if test(mode):
            return name
    return "other"


def output_bytes(procfs, pid, root_pid):
    """-> {"stdout": n|None, "stderr": n|None, "fds": {...}} (see OUTPUT_BYTES_BASIS)."""
    out = {"fds": {}}
    for fd, name in ((1, "stdout"), (2, "stderr")):
        info = {"target": procfs.fd_target(pid, fd)}
        st = procfs.fd_stat(pid, fd)
        value = None
        if st is None:
            info["kind"] = "unreadable"
        else:
            info["kind"] = _kind(st.st_mode)
            if info["kind"] == "regular":
                rst = procfs.fd_stat(root_pid, fd) if root_pid is not None else None
                shared = rst is not None and (rst.st_dev, rst.st_ino) == (st.st_dev, st.st_ino)
                info.update(
                    size_bytes=st.st_size,
                    pos=procfs.fd_pos(pid, fd),
                    shared_with_traced_root=shared,
                )
                value = None if shared else st.st_size
        out[name] = value
        out["fds"][str(fd)] = info
    return out


def stop_tree(
    pid,
    root_pid,
    procfs,
    *,
    stop_timeout=STOP_TIMEOUT_S,
    kill_timeout=KILL_TIMEOUT_S,
    target_cls=Target,
):
    """Freeze pid and its descendants, read output bytes, SIGKILL all. Never raises."""
    sigstop = getattr(signal, "SIGSTOP", None)
    sigkill = getattr(signal, "SIGKILL", None)
    rec = {"descendants": [], "stopped": [], "output_bytes": {}, "killed": [], "errors": []}
    if sigstop is None or sigkill is None:
        rec["errors"].append("SIGSTOP/SIGKILL unavailable on this platform")
        return rec
    members = {}

    def track(p, parent):
        t = target_cls(p, parent)
        st = procfs.stat(p)
        if st is None or st["ppid"] != parent:  # gone, or the pid is no longer that child
            t.close()
            return None
        try:
            t.send(sigstop)
        except OSError as exc:
            rec["errors"].append(f"SIGSTOP {p}: {exc}")
        members[p] = t
        return t

    try:
        if track(pid, root_pid) is None:
            rec["errors"].append(f"{pid} vanished or changed parent before SIGSTOP")
            return rec
        deadline = time.monotonic() + stop_timeout
        settled = False
        while time.monotonic() < deadline:
            pending = [p for p in members if not _stopped_or_dead(procfs, p)]
            if pending:
                time.sleep(POLL_S)
                continue
            kids = children_map(procfs)
            new = [(c, p) for p in list(members) for c in kids.get(p, []) if c not in members]
            if not new:
                settled = True
                break
            for c, parent in new:
                if track(c, parent) is not None:
                    rec["descendants"].append(c)
        if not settled:
            rec["errors"].append(f"tree did not settle stopped within {stop_timeout}s")
        rec["stopped"] = sorted(p for p in members if _state(procfs, p) in ("T", "t"))
        for p in members:
            rec["output_bytes"][str(p)] = output_bytes(procfs, p, root_pid)
        rec["kill_sent_at_ns"] = time.time_ns()
        for p, t in members.items():
            try:
                t.send(sigkill)
            except ProcessLookupError:
                pass  # already dead: confirmed below
            except OSError as exc:
                rec["errors"].append(f"SIGKILL {p}: {exc}")
        deadline = time.monotonic() + kill_timeout
        while time.monotonic() < deadline:
            if all(_dead(procfs, p) for p in members):
                break
            time.sleep(POLL_S)
        rec["killed"] = sorted(p for p in members if _dead(procfs, p))
        alive = sorted(set(members) - set(rec["killed"]))
        if alive:
            rec["errors"].append(f"still alive after SIGKILL ({kill_timeout}s): {alive}")
        unstopped = sorted(set(members) - set(rec["stopped"]) - set(rec["killed"]))
        if unstopped:
            rec["errors"].append(f"never confirmed stopped: {unstopped}")
    except Exception as exc:  # noqa: BLE001 - recorded, acked, never a hang
        rec["errors"].append(f"stop_tree: {type(exc).__name__}: {exc}")
    finally:
        for t in members.values():
            t.close()
    return rec


def _state(procfs, pid):
    st = procfs.stat(pid)
    return None if st is None else st["state"]


def _dead(procfs, pid):
    return _state(procfs, pid) in (None, "Z", "X")


def _stopped_or_dead(procfs, pid):
    return _state(procfs, pid) in (None, "Z", "X", "T", "t")


# ------------------------------------------------------------------ broker


class Broker:
    def __init__(
        self,
        record_path,
        *,
        entry_pid=None,
        status_file=None,
        assume_tracer=None,
        procfs=None,
        stop_timeout=STOP_TIMEOUT_S,
        kill_timeout=KILL_TIMEOUT_S,
        target_cls=Target,
    ):
        self.record_path = record_path
        self.target_cls = target_cls
        self.entry_pid, self.status_file = entry_pid, status_file
        self.assume_tracer = assume_tracer
        self.procfs = procfs or ProcFS()
        self.stop_timeout, self.kill_timeout = stop_timeout, kill_timeout
        self.record = {
            "schema": SCHEMA,
            "broker_pid": os.getpid(),
            "entry_pid": entry_pid,
            "tracer_check": "TracerPid" if assume_tracer is None else f"assumed:{assume_tracer}",
            "started_at_ns": time.time_ns(),
            "ready": False,
            "complete": False,
            "stopped_at_ns": None,
            "output_bytes_basis": OUTPUT_BYTES_BASIS,
            "stops": [],
            "errors": [],
        }

    def save(self):
        tmp = self.record_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.record, f, indent=1)
            f.write("\n")
        os.replace(tmp, self.record_path)

    def handle_line(self, line):
        """One request line -> the ack dict (always; never raises)."""
        try:
            msg = json.loads(line)
            if not isinstance(msg, dict):
                raise ValueError("request is not a JSON object")
        except ValueError as exc:
            self.record["errors"].append(f"unparseable request: {exc}"[:200])
            return {"request_id": None, "op": None, "ok": False, "errors": ["unparseable request"]}
        rid, op = msg.get("request_id"), msg.get("op")
        if op == "ping":
            return {
                "request_id": rid,
                "op": op,
                "ok": True,
                "errors": [],
                "broker_pid": os.getpid(),
            }
        if op != "stop":
            return {"request_id": rid, "op": op, "ok": False, "errors": [f"unknown op {op!r}"]}
        return self.handle_stop(msg.get("pid"), rid)

    def handle_stop(self, pid, request_id=None):
        entry = {
            "request_id": request_id,
            "requested_at_ns": time.time_ns(),
            "pid": pid,
            "verified": False,
            "verification": {},
            "descendants": [],
            "stopped": [],
            "output_bytes": {},
            "kill_sent_at_ns": None,
            "killed": [],
            "completed_at_ns": None,
            "ok": False,
            "errors": [],
        }
        try:
            status = _read_status_file(self.status_file) if self.status_file else {}
            recorded = status.get("tracer_pid")
            facts, errors = verify_target(
                pid,
                self.procfs,
                entry_pid=self.entry_pid,
                recorded_tracer=int(recorded) if recorded and recorded.isdigit() else None,
                assume_tracer=self.assume_tracer,
            )
            entry["verification"], entry["errors"] = facts, list(errors)
            if not errors:
                entry["verified"] = True
                rec = stop_tree(
                    pid,
                    facts["traced_root_pid"],
                    self.procfs,
                    stop_timeout=self.stop_timeout,
                    kill_timeout=self.kill_timeout,
                    target_cls=self.target_cls,
                )
                entry["errors"] += rec.pop("errors")
                entry.update(rec)
                entry["ok"] = not entry["errors"] and pid in entry["killed"]
        except Exception as exc:  # noqa: BLE001 - recorded and acked
            entry["errors"].append(f"broker: {type(exc).__name__}: {exc}")
        entry["completed_at_ns"] = time.time_ns()
        self.record["stops"].append(entry)
        index = len(self.record["stops"]) - 1
        try:
            self.save()
        except OSError as exc:
            entry["errors"].append(f"record write failed: {exc}")
            entry["ok"] = False
        return {
            "request_id": request_id,
            "op": "stop",
            "ok": entry["ok"],
            "pid": pid,
            "verified": entry["verified"],
            "descendants": entry["descendants"],
            "killed": entry["killed"],
            "output_bytes": {
                k: {n: v.get(n) for n in ("stdout", "stderr")}
                for k, v in entry["output_bytes"].items()
            },
            "errors": entry["errors"],
            "stop_index": index,
        }


def _encode(msg):
    data = (json.dumps(msg, separators=(",", ":"), default=str) + "\n").encode("utf-8")
    if len(data) > MAX_LINE:  # keep the write atomic; the full facts are in the record
        slim = {k: msg.get(k) for k in ("request_id", "op", "ok", "pid", "verified", "stop_index")}
        errors = [str(e)[:200] for e in list(msg.get("errors") or [])[:3]]
        slim["errors"] = errors + ["ack truncated: see the record"]
        data = (json.dumps(slim, separators=(",", ":"), default=str) + "\n").encode("utf-8")
    return data


def serve(directory, record_path, *, entry_pid, status_file=None, assume_tracer=None):
    """Create the FIFOs, mark ready, answer requests until SIGTERM. Root, untraced."""
    os.makedirs(directory, mode=0o700, exist_ok=True)
    req_path = os.path.join(directory, REQUEST_FIFO)
    ack_path = os.path.join(directory, ACK_FIFO)
    for path in (req_path, ack_path):
        os.mkfifo(path, 0o600)
    # O_RDWR: opening never blocks waiting for the other end, and neither end ever sees EOF.
    req = os.open(req_path, os.O_RDWR | os.O_NONBLOCK)
    ack = os.open(ack_path, os.O_RDWR | os.O_NONBLOCK)
    broker = Broker(
        record_path, entry_pid=entry_pid, status_file=status_file, assume_tracer=assume_tracer
    )
    stopping = []
    signal.signal(signal.SIGTERM, lambda *_: stopping.append(1))
    broker.record["ready"] = True
    broker.save()
    with open(os.path.join(directory, READY_NAME), "w", encoding="utf-8") as f:
        f.write(str(os.getpid()) + "\n")
    buf = b""
    while not stopping:
        try:
            ready, _, _ = select.select([req], [], [], 0.2)
        except InterruptedError:
            continue
        if not ready:
            continue
        try:
            chunk = os.read(req, 65536)
        except BlockingIOError:
            continue
        buf += chunk
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            if not line.strip():
                continue
            ackmsg = broker.handle_line(line.decode("utf-8", errors="replace"))
            try:
                os.write(ack, _encode(ackmsg))
            except OSError as exc:  # full or broken ack channel: recorded, never a hang
                broker.record["errors"].append(f"ack write failed: {exc}")
                broker.save()
        if len(buf) > MAX_LINE:
            broker.record["errors"].append("request line longer than MAX_LINE dropped")
            buf = b""
    broker.record["complete"] = True
    broker.record["stopped_at_ns"] = time.time_ns()
    broker.save()
    return 0


# ------------------------------------------------------------------ client (traced side)


class BrokerClient:
    """The traced side of the channel: profile[PROFILE_KEY] when the broker runs."""

    def __init__(self, request_fd=REQUEST_FD, ack_fd=ACK_FD):
        self.request_fd, self.ack_fd = request_fd, ack_fd
        self._buf = b""

    def describe(self):
        return {"env": ENV_VAR, "request_fd": self.request_fd, "ack_fd": self.ack_fd}

    def send(self, msg):
        data = _encode(msg)
        os.write(self.request_fd, data)

    def await_ack(self, request_id, timeout=ACK_TIMEOUT_S):
        deadline = time.monotonic() + timeout
        while True:
            while b"\n" in self._buf:
                line, self._buf = self._buf.split(b"\n", 1)
                try:
                    msg = json.loads(line.decode("utf-8", errors="replace"))
                except ValueError:
                    continue
                if isinstance(msg, dict) and msg.get("request_id") == request_id:
                    return msg
            left = deadline - time.monotonic()
            if left <= 0:
                return {"request_id": request_id, "ok": False, "errors": ["ack timeout"]}
            try:
                ready, _, _ = select.select([self.ack_fd], [], [], min(left, 0.5))
                if ready:
                    chunk = os.read(self.ack_fd, 65536)
                    if not chunk:
                        return {"request_id": request_id, "ok": False, "errors": ["ack EOF"]}
                    self._buf += chunk
            except InterruptedError:
                continue
            except OSError as exc:
                return {"request_id": request_id, "ok": False, "errors": [f"ack read: {exc}"]}

    def _call(self, msg, timeout, wait):
        try:
            self.send(msg)
        except OSError as exc:
            return {"request_id": msg["request_id"], "ok": False, "errors": [f"send: {exc}"]}
        if not wait:
            return {"request_id": msg["request_id"], "ok": None, "errors": [], "sent": True}
        return self.await_ack(msg["request_id"], timeout)

    def request_stop(self, pid, *, request_id=None, timeout=ACK_TIMEOUT_S, wait=True):
        rid = request_id or f"{os.getpid()}-{time.time_ns()}"
        return self._call({"op": "stop", "request_id": rid, "pid": int(pid)}, timeout, wait)

    def ping(self, timeout=5.0):
        rid = f"ping-{os.getpid()}-{time.time_ns()}"
        return self._call({"op": "ping", "request_id": rid}, timeout, True)


def channel_from_env(environ=None, fd_dir="/proc/self/fd"):
    """A BrokerClient when ENV_VAR names inherited FIFOs at the broker's paths, else None."""
    environ = os.environ if environ is None else environ
    value = environ.get(ENV_VAR)
    if not value:
        return None
    try:
        parts = dict(p.split("=", 1) for p in value.split(","))
        req, ack = int(parts["request_fd"]), int(parts["ack_fd"])
    except (ValueError, KeyError):
        return None
    for fd, name in ((req, REQUEST_FIFO), (ack, ACK_FIFO)):
        try:
            if not stat.S_ISFIFO(os.fstat(fd).st_mode):
                return None
            target = os.readlink(os.path.join(fd_dir, str(fd)))
        except OSError:
            return None
        if not target.endswith(f"/{BROKER_DIRNAME}/{name}"):
            return None
    return BrokerClient(req, ack)


# ------------------------------------------------------------------ CLI


def main(argv):
    import argparse

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    n = sub.add_parser("needs")
    n.add_argument("case_id")
    s = sub.add_parser("serve")
    s.add_argument("--dir", required=True)
    s.add_argument("--record", required=True)
    s.add_argument("--entry-pid", type=int, required=True)
    s.add_argument("--status-file")
    # Tests on an untraced tree only; entry.sh never passes it, and the record says so
    # (tracer_check "assumed:<pid>"), which the analyzer refuses to confirm.
    s.add_argument("--assume-tracer", type=int, help=argparse.SUPPRESS)
    args = ap.parse_args(argv[1:])
    if args.cmd == "needs":
        try:
            return needs(args.case_id)
        except Exception as exc:  # noqa: BLE001
            print(json.dumps({"case_id": args.case_id, "error": f"{type(exc).__name__}: {exc}"}))
            return 2
    return serve(
        args.dir,
        args.record,
        entry_pid=args.entry_pid,
        status_file=args.status_file,
        assume_tracer=args.assume_tracer,
    )


if __name__ == "__main__":
    sys.exit(main(sys.argv))
