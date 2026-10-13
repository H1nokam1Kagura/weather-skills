"""Evidence path of Processes: on_event order and fields, start receipts, memory seam,
per-PID signal capture. Real synthetic python children where cheap, fakes elsewhere."""

import json
import os
import subprocess
import sys

import pytest

from tools.workflow import processes as processes_module
from tools.workflow import signal_stop as ss
from tools.workflow.contracts import Refused
from tools.workflow.processes import Processes
from tools.workflow.tests.test_signal_stop import FakeBackend, job, make

windows = pytest.mark.skipif(os.name != "nt", reason="Win32 census job and per-PID stop")
PY = sys.executable
SLEEP_60 = "import time; time.sleep(60)"


class Events:
    def __init__(self, fail_on=None):
        self.items = []
        self.fail_on = fail_on

    def __call__(self, name, payload):
        self.items.append((name, json.loads(json.dumps(payload))))
        if name == self.fail_on:
            raise RuntimeError(f"handler failed on {name}")

    def names(self):
        return [n for n, _ in self.items]

    def one(self, name):
        (payload,) = [p for n, p in self.items if n == name]
        return payload


def readings(*values, then=4.0):
    it = iter(values)
    return lambda: next(it, then)


def alive(pid):
    if os.name != "nt":
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True
    backend = ss.Win32Backend()
    try:
        handle = backend.open(pid)
    except ss.OpenError:
        return False
    try:
        return not backend.is_exited(handle)
    finally:
        backend.close(handle)


def spawn_child(code):
    """A child that starts a 60 s grandchild, prints nothing, then runs ``code``."""
    return f"import subprocess, sys; subprocess.Popen([sys.executable, '-c', {SLEEP_60!r}]); {code}"


@pytest.fixture
def decoy():
    flags = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    proc = subprocess.Popen([PY, "-c", SLEEP_60], **flags)
    yield proc
    proc.kill()
    proc.wait()


# ----------------------------------------------------------------- event order and fields


def test_event_order_and_fields_on_normal_exit(tmp_path):
    events = Events()
    p = Processes(tmp_path, on_event=events, memory_reader=readings(4.0))
    result = p.run([PY, "-c", "print('ok')"], tmp_path, None, tool="probe")
    assert events.names() == ["launch_check", "process_launch", "process_exit"]
    check = events.one("launch_check")
    assert check["tool"] == "probe" and check["free_ram_gib"] == 4.0
    assert check["budget_ok"] is True and check["launched"] is True
    assert check["reading_source"] == "injected"
    launch = events.one("process_launch")
    assert set(launch) == {"serial", "tool", "argv", "pid", "parent_pid", "started_monotonic_ns"}
    assert launch["serial"] == 1 and launch["tool"] == "probe"
    assert launch["argv"] == [PY, "-c", "print('ok')"] and launch["parent_pid"] == os.getpid()
    assert isinstance(launch["started_monotonic_ns"], int)
    exit_ = events.one("process_exit")
    assert set(exit_) == {
        "serial",
        "pid",
        "exit_code",
        "stdout_bytes",
        "stderr_bytes",
        "wall_seconds",
        "stop_reason",
    }
    assert exit_["serial"] == 1 and exit_["pid"] == launch["pid"] == result["pid"]
    assert exit_["exit_code"] == 0 and exit_["stop_reason"] is None
    assert exit_["stdout_bytes"] == len(os.linesep.encode()) + 2
    assert exit_["wall_seconds"] >= 0


def test_tool_defaults_to_argv0_basename(tmp_path):
    events = Events()
    Processes(tmp_path, on_event=events, memory_reader=readings(4.0)).run(
        [PY, "-c", "pass"], tmp_path, None
    )
    assert events.one("launch_check")["tool"] == os.path.basename(PY)
    assert events.one("process_launch")["tool"] == os.path.basename(PY)


def test_exact_byte_counts_with_non_utf8_output(tmp_path):
    events = Events()
    code = (
        "import sys; sys.stdout.buffer.write(b'\\xff\\xfe\\x80abc'); "
        "sys.stderr.buffer.write(b'\\x80' * 7)"
    )
    result = Processes(tmp_path, on_event=events, memory_reader=readings(4.0)).run(
        [PY, "-c", code], tmp_path, None
    )
    exit_ = events.one("process_exit")
    assert exit_["stdout_bytes"] == 6 and exit_["stderr_bytes"] == 7
    assert result["stdout_bytes"] == 6 and result["stderr_bytes"] == 7
    assert "�" in result["stdout"]  # decoded text lost bytes; the count did not


def test_refused_launch_emits_launch_check_and_raises(tmp_path):
    events = Events()
    p = Processes(tmp_path, ram_floor=2, on_event=events, memory_reader=readings(2.5))
    with pytest.raises(Refused, match="insufficient free RAM"):
        p.run([PY, "-c", "pass"], tmp_path, None, tool="probe")
    assert events.names() == ["launch_check"]
    check = events.one("launch_check")
    assert check["launched"] is False and check["budget_ok"] is False
    assert check["free_ram_gib"] == 2.5
    assert not list(tmp_path.glob("process-*"))


def test_time_budget_refusal_is_a_launch_check(tmp_path):
    events = Events()
    p = Processes(tmp_path, on_event=events, memory_reader=readings(4.0))
    p.deadline = 0
    with pytest.raises(Refused, match="aggregate time budget"):
        p.run([PY, "-c", "pass"], tmp_path, None)
    check = events.one("launch_check")
    assert check["launched"] is False and check["budget_ok"] is True
    assert check["time_budget_ok"] is False


def test_default_reader_is_reported_observed(tmp_path, monkeypatch):
    monkeypatch.setattr(processes_module, "free_gib", lambda: 5.5)
    events = Events()
    Processes(tmp_path, on_event=events).run([PY, "-c", "pass"], tmp_path, None)
    check = events.one("launch_check")
    assert check["reading_source"] == "observed" and check["free_ram_gib"] == 5.5


# ----------------------------------------------------------------- start receipt


def test_start_receipt_is_durable_before_the_wait(tmp_path):
    seen = {}

    def on_event(name, payload):
        if name == "process_launch":
            path = tmp_path / "process-001.start.json"
            seen["receipt"] = json.loads(path.read_text(encoding="utf-8"))
            seen["payload"] = payload
            seen["running"] = alive(payload["pid"])
            seen["tmp"] = list(tmp_path.glob("*.tmp"))

    Processes(tmp_path, on_event=on_event, memory_reader=readings(4.0)).run(
        [PY, "-c", "import time; time.sleep(1.5)"], tmp_path, None
    )
    assert seen["receipt"] == seen["payload"] and seen["running"] is True
    assert seen["tmp"] == []
    assert json.loads((tmp_path / "process-001.start.json").read_text()) == seen["payload"]


def test_orphan_start_receipt_reserves_its_serial(tmp_path):
    p = Processes(tmp_path, on_event=Events(), memory_reader=readings(4.0))
    p.run([PY, "-c", "pass"], tmp_path, None)
    (tmp_path / "process-001.json").unlink()  # crash after start, before the final record
    events = Events()
    Processes(tmp_path, on_event=events, memory_reader=readings(4.0)).run(
        [PY, "-c", "pass"], tmp_path, None
    )
    assert events.one("process_launch")["serial"] == 2
    assert (tmp_path / "process-001.start.json").exists()


def test_default_path_writes_no_start_receipt_and_keeps_result_shape(tmp_path):
    result = Processes(tmp_path).run([PY, "-c", "pass"], tmp_path, None)
    assert set(result) == {
        "argv",
        "exit_code",
        "stdout",
        "stderr",
        "stop_reason",
        "elapsed_s",
        "min_free_gib",
    }
    assert [p.name for p in tmp_path.glob("process-*")] == ["process-001.json"]


# ----------------------------------------------------------------- memory seam


def test_injected_reader_triggers_ram_guard_at_1_8(tmp_path):
    events = Events()
    p = Processes(tmp_path, ram_floor=2, on_event=events, memory_reader=readings(4.0, then=1.8))
    with pytest.raises(Refused, match="RAM guard"):
        p.run([PY, "-c", SLEEP_60], tmp_path, None)
    exit_ = events.one("process_exit")
    assert exit_["stop_reason"] == "RAM guard" and exit_["exit_code"] is None
    assert not alive(exit_["pid"])


def test_injected_reader_at_2_4_does_not_trigger_ram_guard(tmp_path):
    events = Events()
    p = Processes(tmp_path, ram_floor=2, on_event=events, memory_reader=readings(4.0, then=2.4))
    result = p.run([PY, "-c", "import time; time.sleep(1.2)"], tmp_path, None)
    assert result["stop_reason"] is None and result["min_free_gib"] == 2.4
    assert events.one("process_exit")["exit_code"] == 0


# ----------------------------------------------------------------- on_event failures


def test_on_event_failure_still_cleans_up_child_default_path(tmp_path):
    events = Events(fail_on="process_launch")
    p = Processes(tmp_path, on_event=events, memory_reader=readings(4.0))
    with pytest.raises(RuntimeError, match="process_launch"):
        p.run([PY, "-c", SLEEP_60], tmp_path, None)
    pid = events.one("process_launch")["pid"]
    assert not alive(pid)
    exit_ = events.one("process_exit")
    assert exit_["exit_code"] is None and exit_["stop_reason"] == "exception:RuntimeError"


def test_on_event_failure_after_exit_is_raised_after_the_record(tmp_path):
    events = Events(fail_on="process_exit")
    p = Processes(tmp_path, on_event=events, memory_reader=readings(4.0))
    with pytest.raises(RuntimeError, match="process_exit"):
        p.run([PY, "-c", "pass"], tmp_path, None)
    assert (tmp_path / "process-001.json").exists()


@windows
def test_on_event_failure_still_cleans_up_tree_signal_path(tmp_path):
    events = Events(fail_on="process_launch")
    p = Processes(tmp_path, on_event=events, memory_reader=readings(4.0), signal_capture=True)
    with pytest.raises(RuntimeError, match="process_launch"):
        p.run([PY, "-c", spawn_child("import time; time.sleep(60)")], tmp_path, None)
    record = events.one("signal_stop")["record"]
    assert record["trigger"] == "exception:RuntimeError"
    assert events.names() == ["launch_check", "process_launch", "process_exit", "signal_stop"]
    assert not any(alive(int(pid)) for pid in record["process_tree"] if int(pid) != os.getpid())


# ----------------------------------------------------------------- per-PID signal capture


def _assert_owned(record, root_pid):
    tree = record["process_tree"]
    assert tree[str(root_pid)] == os.getpid()
    for entry in record["signalled"]:
        assert set(entry) == {"pid", "parent_pid", "created_100ns", "result"}
        assert tree[str(entry["pid"])] == entry["parent_pid"]
        cur, hops = entry["pid"], 0
        while cur != root_pid:  # every signalled PID chains to the controller's root
            cur, hops = tree[str(cur)], hops + 1
            assert hops < 50
    assert record["exhaustive"] is (not record["reasons"])
    assert record["signalled_pids"] == [e["pid"] for e in record["signalled"]]


@windows
def test_timeout_signal_path_records_per_pid_parents_and_spares_decoy(tmp_path, decoy):
    events = Events()
    p = Processes(
        tmp_path, timeout=1.5, on_event=events, memory_reader=readings(4.0), signal_capture=True
    )
    with pytest.raises(Refused, match="time budget exhausted"):
        p.run([PY, "-c", spawn_child("import time; time.sleep(60)")], tmp_path, None)
    assert events.names() == ["launch_check", "process_launch", "process_exit", "signal_stop"]
    root = events.one("process_launch")["pid"]
    payload = events.one("signal_stop")
    record = payload["record"]
    assert payload["serial"] == 1 and record["trigger"] == "time budget exhausted"
    signalled = {e["pid"]: e for e in record["signalled"]}
    assert signalled[root]["parent_pid"] == os.getpid()
    assert any(e["parent_pid"] != os.getpid() for e in record["signalled"])  # descendants
    _assert_owned(record, root)
    assert str(decoy.pid) not in record["process_tree"] and decoy.poll() is None
    assert not any(alive(pid) for pid in signalled)
    exit_ = events.one("process_exit")
    assert exit_["exit_code"] is None and exit_["stop_reason"] == "time budget exhausted"
    saved = json.loads((tmp_path / "process-001.json").read_text())
    assert saved["signal_stop"]["signalled_pids"] == record["signalled_pids"]


@windows
def test_normal_exit_with_surviving_descendants_is_a_signal_stop(tmp_path, decoy):
    events = Events()
    p = Processes(tmp_path, on_event=events, memory_reader=readings(4.0), signal_capture=True)
    result = p.run([PY, "-c", spawn_child("pass")], tmp_path, None)
    assert result["exit_code"] == 0 and result["stop_reason"] is None
    assert events.names() == ["launch_check", "process_launch", "process_exit", "signal_stop"]
    root = events.one("process_launch")["pid"]
    record = events.one("signal_stop")["record"]
    assert record["trigger"] == "surviving descendants after root exit"
    assert root not in record["signalled_pids"]  # it had exited: never signalled
    assert record["signalled"], "the surviving grandchild must be signalled"
    _assert_owned(record, root)
    assert events.one("process_exit")["exit_code"] == 0
    assert decoy.poll() is None
    assert not any(alive(e["pid"]) for e in record["signalled"])


@windows
def test_normal_exit_without_descendants_emits_no_signal_stop(tmp_path):
    events = Events()
    p = Processes(tmp_path, on_event=events, memory_reader=readings(4.0), signal_capture=True)
    p.run([PY, "-c", "pass"], tmp_path, None)
    assert "signal_stop" not in events.names()


# ----------------------------------------------------------------- signal_stop semantics


def test_target_exiting_between_check_and_signal_is_not_signalled():
    b = FakeBackend()
    j = make(b)
    b.add(2001, 2000)

    def exits_on_its_own(backend, handle):
        if handle.pid == 2001:
            backend.exit(2001)  # gone before TerminateProcess acts: ACCESS_DENIED

    b.on_terminate.append(exits_on_its_own)
    rec = ss.stop_tree(j, job=job(b), timeout_s=2)
    att = {a["pid"]: a for a in rec["signal_attempts"]}
    assert att[2001]["result"] == "exited_before_signal"
    assert 2001 not in rec["signalled_pids"]
    assert [e["pid"] for e in rec["exited_before_signal"]] == [2001]
    assert [e["pid"] for e in rec["signalled"]] == [2000]
    assert rec["exhaustive"] is True, rec["exhaustive_reasons"]


def test_empty_signal_list_is_never_exhaustive():
    b = FakeBackend()
    j = make(b)
    b.exit(2000)  # nothing alive at stop time: nothing signalled
    rec = ss.stop_tree(j, job=job(b), timeout_s=2)
    assert rec["signalled_pids"] == [] and rec["exhaustive"] is False
    assert any("D4" in r for r in rec["reasons"])
