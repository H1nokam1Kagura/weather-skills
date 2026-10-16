"""Interrupt broker (Wave 7 sub-wave D): broker logic, analyzer accounting, no-stop byte identity.

* Broker logic runs everywhere against a fake process table (``FakeProc``).
* ``linux_only`` tests drive real processes, real /proc and real FIFOs; they skip cleanly where
  /proc, fork or FIFOs are unavailable (Windows hosts), and when run as root (the broker refuses
  uid 0 targets by design).
* The analyzer tests use the synthetic strace captures of test_analyze_trace.
"""

import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

import analyze_trace as A  # noqa: E402
from test_analyze_trace import ENTRY, ROOT, TRACER, Capture, child, start, thread  # noqa: E402

from container_harness import interrupt_broker as B  # noqa: E402

BROKER_PID = 85
REG, FIFO = 0o100644, 0o010600


def linux_only(fn):
    ok = (
        sys.platform.startswith("linux")
        and os.path.isdir("/proc/self/fd")
        and hasattr(os, "fork")
        and hasattr(os, "mkfifo")
        and os.getuid() != 0
    )
    return pytest.mark.skipif(not ok, reason="needs Linux /proc, fork, FIFOs and a non-root uid")(
        fn
    )


# ------------------------------------------------------------------ fake process table


class FakeProc:
    """pid -> {state, ppid, tracer, uid, exe, fds: {fd: (mode, size, dev, ino, target)}}."""

    def __init__(self):
        self.table = {}

    def add(self, pid, ppid, *, tracer=TRACER, uid=1000, exe="/usr/local/bin/python", fds=None):
        self.table[pid] = dict(state="S", ppid=ppid, tracer=tracer, uid=uid, exe=exe, fds=fds or {})
        return self

    def pids(self):
        return list(self.table)

    def stat(self, pid):
        p = self.table.get(pid)
        if p is None:
            return None
        return {"state": p["state"], "ppid": p["ppid"], "pgrp": pid, "starttime": 1}

    def status(self, pid):
        p = self.table.get(pid)
        if p is None:
            return None
        return {"ppid": p["ppid"], "tracer_pid": p["tracer"], "uid": p["uid"]}

    def exe(self, pid):
        return (self.table.get(pid) or {}).get("exe")

    def _fd(self, pid, fd):
        return ((self.table.get(pid) or {}).get("fds") or {}).get(fd)

    def fd_target(self, pid, fd):
        f = self._fd(pid, fd)
        return f[4] if f else None

    def fd_stat(self, pid, fd):
        f = self._fd(pid, fd)
        if not f:
            return None
        return SimpleNamespace(st_mode=f[0], st_size=f[1], st_dev=f[2], st_ino=f[3])

    def fd_pos(self, pid, fd):
        f = self._fd(pid, fd)
        return f[1] if f else None


def fake_tree():
    """entry 80 -> strace 90 -> traced root 100 -> incarnation 101 -> tools 102, 103."""
    shared = {1: (REG, 4000, 1, 7, "/harness-out/root.stdout"), 2: (FIFO, 0, 1, 8, "pipe:[8]")}
    fp = FakeProc()
    fp.add(ENTRY, 1, tracer=0, uid=0, exe="/bin/dash")
    fp.add(TRACER, ENTRY, tracer=0, uid=0, exe="/usr/bin/strace")
    fp.add(ROOT, TRACER, fds=dict(shared))
    fp.add(101, ROOT, fds=dict(shared))
    tool = {1: (REG, 1234, 1, 50, "/tmp/#50 (deleted)"), 2: (REG, 7, 1, 51, "/tmp/#51 (deleted)")}
    fp.add(102, 101, exe="/usr/bin/cdo", fds=tool)
    fp.add(103, 102, exe="/usr/bin/cdo", fds={1: (FIFO, 0, 1, 60, "pipe:[60]")})
    return fp


class FakeTarget:
    """Signals act on the fake table: SIGSTOP -> T, SIGKILL -> Z (unless immortal)."""

    procfs = None
    immortal = set()
    sent = []
    on_stop = {}  # pid -> callable(procfs): e.g. a fork that completed as the stop landed

    def __init__(self, pid, parent):
        self.pid, self.parent, self.pidfd = pid, parent, None

    def send(self, sig):
        import signal

        type(self).sent.append((self.pid, sig))
        p = self.procfs.table.get(self.pid)
        if p is None:
            raise ProcessLookupError(self.pid)
        if sig == signal.SIGSTOP:
            p["state"] = "T"
            hook = type(self).on_stop.pop(self.pid, None)
            if hook:
                hook(self.procfs)
        elif sig == signal.SIGKILL and self.pid not in self.immortal:
            p["state"] = "Z"

    def close(self):
        pass


def fake_target_cls(fp, immortal=(), on_stop=None):
    return type(
        "T",
        (FakeTarget,),
        {"procfs": fp, "immortal": set(immortal), "sent": [], "on_stop": dict(on_stop or {})},
    )


@pytest.fixture(autouse=True)
def posix_signal_numbers(monkeypatch):
    """The fake-table tests only compare signal numbers: give Windows the Linux values."""
    import signal

    for name, number in (("SIGSTOP", 19), ("SIGKILL", 9)):
        if not hasattr(signal, name):
            monkeypatch.setattr(signal, name, number, raising=False)


# ------------------------------------------------------------------ documentation and selection


def test_protocol_names_are_documented_where_sub_wave_c_reads_them():
    doc = B.__doc__
    assert (B.REQUEST_FD, B.ACK_FD) == (5, 6)
    assert "fd 5 = request" in doc and "fd 6 = ack" in doc
    for name in (
        B.ENV_VAR,
        B.PROFILE_KEY,
        B.EVIDENCE_KEY,
        B.REQUEST_FIFO,
        B.ACK_FIFO,
        B.RECORD_NAME,
        B.BROKER_DIRNAME,
        "request_fd=5,ack_fd=6",
        '"op": "stop"',
        '"op": "ping"',
    ):
        assert name in doc, name
    # The analyzer and the broker agree on the record and the evidence key.
    assert A.BROKER_RECORD == B.RECORD_NAME and A.BROKER_SCHEMA == B.SCHEMA
    assert A.BROKER_EVIDENCE_KEY == B.EVIDENCE_KEY


def test_schedules_stop_counts_only_harness_stop_controller_payloads():
    stop = {"operation": "interrupt", "payload": {"mode": "harness_stop_controller"}}
    other = {"operation": "interrupt", "payload": {"mode": "in_process"}}
    assert B.schedules_stop({"injection_schedule": [stop, other, stop]}) == 2
    assert B.schedules_stop({"injection_schedule": [other, {"payload": None}, "x"]}) == 0
    assert B.schedules_stop({}) == 0


def test_dev_dataset_stop_cases_are_the_interrupt_family_only():
    path = (
        HERE.parent / "evals/controller/fixtures/e2e-20261005/agentic-e2e-injections.DEV-ONLY.json"
    )
    if not path.is_file():
        pytest.skip("dev dataset not in this checkout")
    cases = json.loads(path.read_text(encoding="utf-8"))["cases"]
    stops = sorted(c["id"] for c in cases if B.schedules_stop(c))
    assert stops, "the dev dataset schedules harness_stop_controller somewhere"
    assert all(("-E10-" in i) or ("-E11-" in i) or ("-E14-" in i) for i in stops), stops
    assert len(stops) < len(cases)


# ------------------------------------------------------------------ verification


@pytest.mark.parametrize(
    "mutate,pid,needle",
    [
        (None, ROOT, "traced root itself"),
        (None, 102, "is not the traced root"),  # a tool of the incarnation, not a fork
        (None, 9999, "does not exist"),
        (None, 1, "integer > 1"),
        (None, True, "integer > 1"),
        (lambda fp: fp.table[101].update(exe="/usr/bin/sleep"), 101, "not a fork"),
        (lambda fp: fp.table[101].update(tracer=0), 101, "not traced"),
        (lambda fp: fp.table[101].update(state="Z"), 101, "zombie"),
        (lambda fp: fp.table[101].update(uid=0), 101, "uid"),
        (lambda fp: fp.table[TRACER].update(ppid=1), 101, "not a child of the entry shell"),
        (lambda fp: fp.table[ROOT].update(tracer=0), 101, "not traced by"),
    ],
)
def test_verify_refuses_anything_but_a_fork_of_the_traced_root(mutate, pid, needle):
    fp = fake_tree()
    if mutate:
        mutate(fp)
    _facts, errors = B.verify_target(pid, fp, entry_pid=ENTRY, recorded_tracer=TRACER)
    assert any(needle in e for e in errors), errors


def test_verify_accepts_a_direct_fork_and_checks_the_recorded_tracer():
    fp = fake_tree()
    facts, errors = B.verify_target(101, fp, entry_pid=ENTRY, recorded_tracer=TRACER)
    assert errors == []
    assert facts["traced_root_pid"] == ROOT and facts["tracer_pid"] == TRACER
    _f, errors = B.verify_target(101, fp, entry_pid=ENTRY, recorded_tracer=TRACER + 1)
    assert any("recorded tracer_pid" in e for e in errors)


def test_parse_stat_handles_parens_and_spaces_in_comm():
    text = "4242 (a (b) c) T 100 4242 4242 0 -1 0 " + " ".join(["0"] * 12) + " 777 0 0\n"
    st = B.parse_stat(text)
    assert st == {"state": "T", "ppid": 100, "pgrp": 4242, "starttime": 777}
    assert B.parse_stat("garbage") is None


def test_descendants_are_breadth_first_and_exclude_the_pid():
    fp = fake_tree()
    assert B.descendants(fp, 101) == [102, 103]
    assert B.descendants(fp, ROOT) == [101, 102, 103]


def test_output_bytes_counts_regular_files_only_and_never_the_roots_shared_file():
    fp = fake_tree()
    tool = B.output_bytes(fp, 102, ROOT)
    assert (tool["stdout"], tool["stderr"]) == (1234, 7)
    assert tool["fds"]["1"]["kind"] == "regular" and tool["fds"]["1"]["pos"] == 1234
    inc = B.output_bytes(fp, 101, ROOT)  # same file the traced root holds: not its output
    assert inc["stdout"] is None and inc["fds"]["1"]["shared_with_traced_root"] is True
    assert inc["stderr"] is None and inc["fds"]["2"]["kind"] == "fifo"
    gone = B.output_bytes(fp, 103, ROOT)
    assert gone["stdout"] is None and gone["fds"]["2"]["kind"] == "unreadable"


# ------------------------------------------------------------------ stop sequence (fake)


def test_stop_tree_freezes_late_forks_then_kills_everything():
    fp = fake_tree()
    # 101 completes a fork (104) just as SIGSTOP lands: the next scan must freeze it too.
    cls = fake_target_cls(fp, on_stop={101: lambda p: p.add(104, 101)})
    rec = B.stop_tree(101, ROOT, fp, target_cls=cls)
    assert rec["errors"] == []
    assert rec["descendants"] == [102, 104, 103]
    assert rec["stopped"] == [101, 102, 103, 104]
    assert rec["killed"] == [101, 102, 103, 104]
    assert rec["output_bytes"]["102"]["stdout"] == 1234
    import signal

    stops = [p for p, s in cls.sent if s == signal.SIGSTOP]
    kills = [p for p, s in cls.sent if s == signal.SIGKILL]
    assert set(stops) == set(kills) == {101, 102, 103, 104}
    assert max(cls.sent.index((p, signal.SIGSTOP)) for p in stops) < min(
        cls.sent.index((p, signal.SIGKILL)) for p in kills
    ), "every member is frozen before the first SIGKILL"
    assert fp.table[ROOT]["state"] == "S", "the traced root is never signalled"


def test_stop_tree_is_bounded_when_a_process_survives_sigkill():
    fp = fake_tree()
    cls = fake_target_cls(fp, immortal={103})
    t0 = time.monotonic()
    rec = B.stop_tree(101, ROOT, fp, target_cls=cls, stop_timeout=0.2, kill_timeout=0.1)
    assert time.monotonic() - t0 < 2
    assert rec["killed"] == [101, 102]
    assert any("still alive" in e and "103" in e for e in rec["errors"])


def test_broker_requests_ack_every_failure_and_record_refusals(tmp_path):
    fp = fake_tree()
    record = tmp_path / B.RECORD_NAME
    broker = B.Broker(str(record), entry_pid=ENTRY, procfs=fp)
    assert broker.handle_line("{not json")["ok"] is False
    assert broker.handle_line('{"op": "nope", "request_id": "x"}')["errors"] == [
        "unknown op 'nope'"
    ]
    assert broker.handle_line('{"op": "ping", "request_id": "p"}')["ok"] is True
    ack = broker.handle_line(json.dumps({"op": "stop", "request_id": "r", "pid": ROOT}))
    assert ack["ok"] is False and ack["verified"] is False and ack["killed"] == []
    assert any("traced root itself" in e for e in ack["errors"])
    saved = json.loads(record.read_text(encoding="utf-8"))
    assert saved["schema"] == B.SCHEMA and saved["tracer_check"] == "TracerPid"
    assert saved["stops"][0]["pid"] == ROOT and saved["stops"][0]["verified"] is False
    assert all(p["state"] == "S" for p in fp.table.values()), "a refused stop signals nothing"


def test_broker_stop_acks_kill_facts_and_records_them(tmp_path):
    fp = fake_tree()
    record = tmp_path / B.RECORD_NAME
    status = tmp_path / "harness_status.txt"
    status.write_text(f"tracer_pid={TRACER}\n", encoding="utf-8")
    broker = B.Broker(
        str(record),
        entry_pid=ENTRY,
        status_file=str(status),
        procfs=fp,
        target_cls=fake_target_cls(fp),
    )
    ack = broker.handle_line(json.dumps({"op": "stop", "request_id": "r1", "pid": 101}))
    assert ack["ok"] is True and ack["verified"] is True, ack
    assert ack["killed"] == [101, 102, 103] and ack["stop_index"] == 0
    assert ack["output_bytes"]["102"] == {"stdout": 1234, "stderr": 7}
    stop = json.loads(record.read_text(encoding="utf-8"))["stops"][0]
    assert stop["requested_at_ns"] <= stop["kill_sent_at_ns"] <= stop["completed_at_ns"]
    assert stop["verification"]["traced_root_pid"] == ROOT
    assert len(B._encode(ack)) <= B.MAX_LINE
    status.write_text(f"tracer_pid={TRACER + 1}\n", encoding="utf-8")
    fp.add(106, ROOT)
    other = broker.handle_line(json.dumps({"op": "stop", "request_id": "r2", "pid": 106}))
    assert other["verified"] is False and fp.table[106]["state"] == "S"


def test_oversized_ack_is_truncated_to_one_atomic_line():
    big = {"request_id": "r", "op": "stop", "ok": True, "errors": ["e" * 9000], "pid": 5}
    data = B._encode(big)
    assert len(data) <= B.MAX_LINE and data.endswith(b"\n")
    assert json.loads(data)["errors"][-1] == "ack truncated: see the record"


def test_channel_from_env_is_none_unless_the_broker_fifos_are_inherited(tmp_path):
    assert B.channel_from_env({}) is None
    assert B.channel_from_env({B.ENV_VAR: "garbage"}) is None
    with open(tmp_path / "f", "w") as f:  # a regular file is not the channel
        fd = f.fileno()
        assert B.channel_from_env({B.ENV_VAR: f"request_fd={fd},ack_fd={fd}"}) is None


# ------------------------------------------------------------------ real processes (Linux)

TREE = r"""
import os, subprocess, sys, time
out_path = sys.argv[1]
pid = os.fork()
if pid == 0:
    out = open(out_path, "wb")
    tool = subprocess.Popen(
        [sys.executable, "-c",
         "import sys, time; sys.stdout.write('x' * 1234); sys.stdout.flush(); time.sleep(120)"],
        stdout=out, stderr=subprocess.DEVNULL)
    print(f"incarnation={os.getpid()} tool={tool.pid}", flush=True)
    time.sleep(120)
    os._exit(0)
os.waitpid(pid, 0)
print("root-reaped", flush=True)
time.sleep(120)
"""


def spawn_tree(tmp_path):
    out = tmp_path / "tool.out"
    root = subprocess.Popen(
        [sys.executable, "-c", TREE, str(out)], stdout=subprocess.PIPE, text=True
    )
    line = root.stdout.readline()
    pids = dict(kv.split("=") for kv in line.split())
    inc, tool = int(pids["incarnation"]), int(pids["tool"])
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline and (not out.exists() or out.stat().st_size < 1234):
        time.sleep(0.02)
    return root, inc, tool


def _cleanup(*procs):
    for p in procs:
        if p is not None and p.poll() is None:
            p.kill()
            p.wait(timeout=10)


@linux_only
def test_real_tree_stop_kills_the_fork_and_its_tool_and_reads_output_bytes(tmp_path):
    root, inc, tool = spawn_tree(tmp_path)
    try:
        broker = B.Broker(str(tmp_path / B.RECORD_NAME), assume_tracer=os.getpid())
        refused = broker.handle_stop(root.pid, "r0")
        assert refused["verified"] is False and root.poll() is None
        ack = broker.handle_stop(inc, "r1")
        assert ack["ok"] is True, ack
        assert set(ack["killed"]) == {inc, tool} and ack["descendants"] == [tool]
        assert ack["output_bytes"][str(tool)]["stdout"] == 1234
        assert ack["output_bytes"][str(inc)]["stdout"] is None  # a pipe to this test
        assert root.stdout.readline().strip() == "root-reaped"  # incarnation died, root lives
        assert root.poll() is None
    finally:
        _cleanup(root)


@linux_only
def test_real_fifo_channel_ping_stop_refusal_and_clean_shutdown(tmp_path):
    root, inc, tool = spawn_tree(tmp_path)
    bdir = tmp_path / B.BROKER_DIRNAME
    record = tmp_path / B.RECORD_NAME
    broker = subprocess.Popen(
        [
            sys.executable,
            "-I",
            "-B",
            str(HERE / "interrupt_broker.py"),
            "serve",
            "--dir",
            str(bdir),
            "--record",
            str(record),
            "--entry-pid",
            str(os.getppid()),
            "--assume-tracer",
            str(os.getpid()),
        ]
    )
    req = ack = None
    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and not (bdir / B.READY_NAME).exists():
            time.sleep(0.02)
        assert (bdir / B.READY_NAME).exists()
        req = os.open(bdir / B.REQUEST_FIFO, os.O_RDWR)
        ack = os.open(bdir / B.ACK_FIFO, os.O_RDWR)
        client = B.channel_from_env({B.ENV_VAR: f"request_fd={req},ack_fd={ack}"})
        assert isinstance(client, B.BrokerClient)
        assert client.ping()["ok"] is True
        refused = client.request_stop(root.pid, timeout=20)
        assert refused["ok"] is False and refused["verified"] is False
        done = client.request_stop(inc, timeout=20)
        assert done["ok"] is True and set(done["killed"]) == {inc, tool}, done
        assert root.poll() is None
        broker.terminate()
        assert broker.wait(timeout=20) == 0
        saved = json.loads(record.read_text(encoding="utf-8"))
        assert saved["complete"] is True and saved["tracer_check"] == f"assumed:{os.getpid()}"
        assert [s["ok"] for s in saved["stops"]] == [False, True]
    finally:
        for fd in (req, ack):
            if fd is not None:
                os.close(fd)
        _cleanup(broker, root)


@linux_only
def test_client_times_out_without_a_broker(tmp_path):
    r, w = os.pipe()
    r2, w2 = os.pipe()
    try:
        client = B.BrokerClient(w, r2)
        t0 = time.monotonic()
        got = client.request_stop(4242, timeout=0.3)
        assert got["ok"] is False and got["errors"] == ["ack timeout"]
        assert time.monotonic() - t0 < 3
    finally:
        for fd in (r, w, r2, w2):
            os.close(fd)


# ------------------------------------------------------------------ analyzer: kill from outside

STOP_STATUS = {
    "interrupt_broker_needed": 1,
    "interrupt_broker_pid": BROKER_PID,
    "interrupt_broker_pgid": ENTRY,
    "interrupt_broker_ready": 1,
    "interrupt_broker_exit": 0,
}


def ts_now(cap):
    return 1700000000 * 10**9 + cap.t


def broker_record(requested_at, **stop):
    entry = {
        "request_id": "r1",
        "requested_at_ns": requested_at,
        "pid": 101,
        "verified": True,
        "verification": {"traced_root_pid": ROOT, "tracer_pid": TRACER},
        "descendants": [102, 103],
        "stopped": [101, 102, 103],
        "output_bytes": {
            "101": {"stdout": None, "stderr": None},
            "102": {"stdout": 12, "stderr": 0},
            "103": {"stdout": 5, "stderr": 0},
        },
        "kill_sent_at_ns": requested_at + 200,
        "killed": [101, 102, 103],
        "completed_at_ns": requested_at + 300,
        "ok": True,
        "errors": [],
    }
    entry.update(stop)
    return {
        "schema": B.SCHEMA,
        "broker_pid": BROKER_PID,
        "entry_pid": ENTRY,
        "tracer_check": "TracerPid",
        "ready": True,
        "complete": True,
        "output_bytes_basis": B.OUTPUT_BYTES_BASIS,
        "stops": [entry],
        "errors": [],
    }


def stop_capture(tmp_path, *, incarnation_killed=True):
    """Traced root 100 forks incarnation 1 (101), which runs two tools (102, 103) at once; the
    broker (85, outside the tree) freezes and SIGKILLs all three; incarnation 2 runs in the
    root, whose tools (104, then 105) never overlap. The controller signals only 105."""
    cap = start(Capture(tmp_path))
    cap.status.update(STOP_STATUS)
    child(cap, ROOT, 101, vfork=False)  # os.fork: incarnation 1
    for tool in (102, 103):
        child(cap, 101, tool)
        cap(tool, 'execve("/usr/bin/cdo", ["cdo"], 0x7f /* 9 vars */) = 0')
    requested_at = ts_now(cap) + 500
    for pid in (101, 102, 103):
        cap(
            pid,
            f"--- SIGSTOP {{si_signo=SIGSTOP, si_code=SI_USER, si_pid={BROKER_PID}, si_uid=0}} ---",
        )
        cap(pid, "--- stopped by SIGSTOP ---")
    for pid in (102, 103):
        cap(pid, "+++ killed by SIGKILL +++")
    if incarnation_killed:
        cap(101, "+++ killed by SIGKILL +++")
    else:
        cap.exit(101)
    cap(
        ROOT,
        "--- SIGCHLD {si_signo=SIGCHLD, si_code=CLD_KILLED, si_pid=101, si_uid=1000,"
        " si_status=SIGKILL, si_utime=0, si_stime=0} ---",
    )
    child(cap, ROOT, 104)
    cap.exit(104)
    child(cap, ROOT, 105)
    cap(ROOT, "kill(105, SIGTERM) = 0")
    cap(105, "--- SIGTERM {si_signo=SIGTERM, si_code=SI_USER, si_pid=100, si_uid=1000} ---")
    cap.exit(105, 143)
    cap.exit(ROOT)
    return cap, requested_at


def write_record(cap, record):
    (cap.dir / A.BROKER_RECORD).write_text(json.dumps(record), encoding="utf-8")


def test_kill_from_outside_counts_incarnation_tools_and_is_a_harness_action(tmp_path):
    cap, requested_at = stop_capture(tmp_path)
    write_record(cap, broker_record(requested_at))
    ev = cap.write()
    obs = ev["harness_observations"]
    assert obs["tracer_status"]["status"] == "OK"
    assert "parse_anomalies" not in obs["tracer_status"]  # group-stop lines are parsed
    assert ev["missing"] == {}
    # 102 and 103 overlapped under incarnation 1; 101 itself is not a tool child.
    assert obs["max_concurrent_children"] == 2
    # Broker SIGSTOP/SIGKILL are not the controller signalling; its own kill(105) is.
    assert ev["controller_trace"]["signalled_pids"] == [105]
    notes = ev["harness_notes"]
    assert notes["sigkill_deaths_without_attempt"] == []
    assert sorted(k["pid"] for k in notes["sigkill_deaths_by_interrupt_broker"]) == [101, 102, 103]
    assert notes["deliveries_unmatched_to_attempts"] == []
    assert {d["receiver_pid"] for d in notes["interrupt_broker_deliveries"]} == {101, 102, 103}
    assert ev["harness_observations"]["signal_capture_exhaustive"] is True
    section = ev[A.BROKER_EVIDENCE_KEY]
    assert section["status"] == "OK" and section["incarnation_pids"] == [101]
    assert section["harness_killed_pids"] == [101, 102, 103]
    stop = section["stops"][0]
    assert stop["trace_confirmed"] is True and stop["trace_incarnation_key"] == "101#0"
    assert stop["output_bytes"]["102"] == {"stdout": 12, "stderr": 0}
    assert ev["process_tree"]["102"] == 101 and ev["process_tree"]["101"] == ROOT
    assert ev["detail"]["harness_processes"][str(BROKER_PID)]["label"] == A.BROKER


def test_without_the_broker_accounting_the_same_trace_undercounts(tmp_path):
    """Why the incarnation basis exists: root-only counting sees one child (the fork)."""
    cap, _ = stop_capture(tmp_path)
    for key in STOP_STATUS:
        cap.status.pop(key)
    ev = cap.write()
    assert ev["harness_observations"]["max_concurrent_children"] == 1
    assert A.BROKER_EVIDENCE_KEY not in ev
    assert sorted(k["pid"] for k in ev["harness_notes"]["sigkill_deaths_without_attempt"]) == [
        101,
        102,
        103,
    ]


@pytest.mark.parametrize(
    "setup,needle",
    [
        (lambda cap, req: None, "record absent"),
        (
            lambda cap, req: write_record(cap, {**broker_record(req), "complete": False}),
            "incomplete",
        ),
        (
            lambda cap, req: write_record(cap, {**broker_record(req), "tracer_check": "assumed:1"}),
            "TracerPid",
        ),
        (lambda cap, req: write_record(cap, broker_record(req, ok=False)), "failed in the broker"),
        (
            lambda cap, req: write_record(cap, broker_record(req, verified=False, killed=[])),
            "refused",
        ),
        (lambda cap, req: write_record(cap, broker_record(req, pid=104)), "not confirmed"),
        (lambda cap, req: write_record(cap, broker_record(req + 10**12)), "not confirmed"),
        (
            lambda cap, req: (
                cap.status.update(interrupt_broker_exit=1),
                write_record(cap, broker_record(req)),
            ),
            "exited 1",
        ),
        (
            lambda cap, req: (cap.status.update(interrupt_broker_needed="error"),),
            "need check failed",
        ),
        (
            lambda cap, req: (
                cap.status.update(interrupt_broker_ready=0),
                write_record(cap, broker_record(req)),
            ),
            "not ready",
        ),
    ],
)
def test_unconfirmed_or_absent_broker_facts_make_concurrency_missing(tmp_path, setup, needle):
    cap, requested_at = stop_capture(tmp_path)
    setup(cap, requested_at)
    ev = cap.write()
    assert "max_concurrent_children" not in ev["harness_observations"]
    assert needle in ev["missing"][A.CONCURRENCY_FIELD]
    assert ev[A.BROKER_EVIDENCE_KEY]["status"] in ("UNAVAILABLE", "UNCONFIRMED")
    assert ev["controller_trace"]["signalled_pids"] == [105]  # never the broker's kills


def test_a_ready_broker_with_no_request_is_labelled_and_makes_concurrency_missing(tmp_path):
    """D4 w7 B-HO-1: the fork (101) ran two overlapping tools; a root-only count (1) would be a
    false V23-CONCURRENCY PASS, so an engaged broker that was never asked withholds the field."""
    cap, requested_at = stop_capture(tmp_path)
    write_record(cap, {**broker_record(requested_at), "stops": []})
    ev = cap.write()
    section = ev[A.BROKER_EVIDENCE_KEY]
    assert section["status"] == "NO_STOP_REQUESTED" and section["reason"] == A.NO_STOP_REASON
    assert "max_concurrent_children" not in ev["harness_observations"]
    assert ev["missing"][A.CONCURRENCY_FIELD] == A.NO_STOP_REASON
    assert ev["harness_notes"]["sigkill_deaths_by_interrupt_broker"] == []
    assert ev["controller_trace"]["signalled_pids"] == [105]  # nothing else changes


def test_in_process_fallback_kill_in_the_container_makes_concurrency_missing(tmp_path):
    """The broker was needed and ready, but the traced root killed incarnation 1 itself (the
    in-process fallback): the broker record holds no stop and the kills are tree attempts."""
    cap = start(Capture(tmp_path))
    cap.status.update(STOP_STATUS)
    child(cap, ROOT, 101, vfork=False)  # incarnation 1
    for tool in (102, 103):
        child(cap, 101, tool)
    cap(ROOT, "kill(101, SIGSTOP) = 0")
    cap(ROOT, "kill(102, SIGKILL) = 0")
    cap(ROOT, "kill(103, SIGKILL) = 0")
    cap(ROOT, "kill(101, SIGKILL) = 0")
    for pid in (102, 103, 101):
        cap(pid, "+++ killed by SIGKILL +++")
    child(cap, ROOT, 104)
    cap.exit(104)
    cap.exit(ROOT)
    write_record(cap, {**broker_record(ts_now(cap)), "stops": []})
    ev = cap.write()
    assert "max_concurrent_children" not in ev["harness_observations"]
    assert ev["missing"][A.CONCURRENCY_FIELD] == A.NO_STOP_REASON
    assert ev[A.BROKER_EVIDENCE_KEY]["status"] == "NO_STOP_REQUESTED"
    assert ev[A.BROKER_EVIDENCE_KEY]["incarnation_pids"] == []
    # The root's own kills are controller-tree attempts, correctly in signalled_pids.
    assert {101, 102, 103} <= set(ev["controller_trace"]["signalled_pids"])


def test_a_declared_incarnation_the_trace_shows_exiting_normally_is_not_confirmed(tmp_path):
    cap, requested_at = stop_capture(tmp_path, incarnation_killed=False)
    write_record(cap, broker_record(requested_at))
    ev = cap.write()
    stop = ev[A.BROKER_EVIDENCE_KEY]["stops"][0]
    assert stop["trace_confirmed"] is False and stop["trace_unconfirmed_kills"] == [101]
    assert "not confirmed" in ev["missing"][A.CONCURRENCY_FIELD]


# ------------------------------------------------------------------ no stop: byte-identical

# sha256 of json.dumps(evidence, indent=1, default=str) (analyze_trace.main's encoding) for the
# scenarios below, produced by the analyzer BEFORE this change (controller 429f80e). A case that
# schedules no stop must produce exactly these bytes, with or without the
# interrupt_broker_needed=0 line entry.sh now writes for it.
GOLDEN_NO_STOP = {
    "signals_and_tree": "3f04f22ef3690a75d771f0fa3a9bd90ab1f8698b583c275823b40ab4bfc7f626",
    "threads_concurrency_network": "53ed764f95d963c6729c62a31d367de2f28aa696ca6bd8fa916a1ced2cba77bd",
    "confined_case": "c0511546b04df62fb3ed06de5c31514673f5b163c019ca8a1fb4934c5fb01083",
}


def scenario_signals_and_tree(cap):
    start(cap)
    child(cap, ROOT, 101)
    cap(101, "setsid() = 101")
    child(cap, 101, 102)
    child(cap, ROOT, 103)
    cap(ROOT, "kill(-101, SIGKILL) = 0")
    cap(ROOT, "pidfd_open(103, 0) = 5<anon_inode:[pidfd]>")
    cap(ROOT, "pidfd_send_signal(5<anon_inode:[pidfd]>, SIGTERM, NULL, 0) = 0")
    cap(ROOT, "kill(100, 0) = 0")
    cap(ROOT, f"kill({ENTRY}, SIGUSR1) = -1 EPERM (Operation not permitted)")
    for pid in (101, 102):
        cap(pid, "+++ killed by SIGKILL +++")
    cap(103, "--- SIGTERM {si_signo=SIGTERM, si_code=SI_USER, si_pid=100, si_uid=1000} ---")
    cap.exit(103, 143)
    cap(ROOT, "prctl(PR_SET_PDEATHSIG, SIGKILL) = 0")
    cap.exit(ROOT)


def scenario_threads_concurrency_network(cap):
    start(cap)
    thread(cap, ROOT, 150)
    child(cap, ROOT, 101, vfork=False)
    child(cap, ROOT, 102, vfork=False)
    cap(101, 'openat(AT_FDCWD</work/run>, "a", O_WRONLY|O_CREAT, 0666) = 3</work/run/a>')
    cap(102, 'execve("/bin/sh", ["/bin/sh", "-c", "x"], 0x7f /* 9 vars */) = 0')
    cap(
        150,
        'connect(4<socket:[9]>, {sa_family=AF_INET, sin_port=htons(443), sin_addr=inet_addr("1.2.3.4")}, 16) = -1 ENETUNREACH (Network is unreachable)',
    )
    cap(ROOT, 'mkdir("/tmp/out", 0777) = 0')
    cap.exit(101)
    cap.exit(102)
    cap.exit(150)
    cap.exit(ROOT)


def scenario_confined_case(cap):
    cap.status.update(run_dir="/work/case/run", root_cwd="/opt/ctl")
    cap.baseline += ["/opt", "/opt/ctl", "/work/case", "/work/case/run"]
    run = "/work/case/run"
    cap(
        ROOT,
        'execve("/usr/local/bin/python", ["python", "-m", "case_driver"], 0x7f /* 9 vars */) = 0',
    )
    cap(ROOT, f'mkdir("{run}/artifacts", 0777) = 0')
    cap(
        ROOT,
        f'openat(AT_FDCWD</opt/ctl>, "{run}/c.tmp", O_WRONLY|O_CREAT|O_TRUNC, 0666) = 3<{run}/c.tmp>',
    )
    cap(ROOT, f'rename("{run}/c.tmp", "{run}/checkpoint.json") = 0')
    child(cap, ROOT, 101)
    cap(101, 'execve("/usr/bin/cdo", ["cdo"], 0x7f /* 9 vars */) = 0')
    cap.exit(101)
    cap.exit(ROOT)


SCENARIOS = {
    "signals_and_tree": scenario_signals_and_tree,
    "threads_concurrency_network": scenario_threads_concurrency_network,
    "confined_case": scenario_confined_case,
}


def evidence_bytes(tmp_path, name, extra_status=None):
    cap = Capture(tmp_path)
    SCENARIOS[name](cap)
    cap.status.update(extra_status or {})
    ev = cap.write()
    return json.dumps(ev, indent=1, sort_keys=False, default=str).encode("utf-8")


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_no_stop_case_output_is_byte_identical_to_the_pre_broker_analyzer(tmp_path, name):
    plain = evidence_bytes(tmp_path / "a", name)
    marked = evidence_bytes(tmp_path / "b", name, {"interrupt_broker_needed": 0})
    assert plain == marked
    assert hashlib.sha256(plain).hexdigest() == GOLDEN_NO_STOP[name]
