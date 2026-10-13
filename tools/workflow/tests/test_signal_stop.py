import importlib.util
import itertools
import os
import tempfile
from pathlib import Path

import pytest

from tools.workflow import signal_stop as ss

CONTROLLER = 1000


class Proc:
    def __init__(self, pid, ppid, created, *, in_job=True, deny=False, immortal=False):
        self.pid, self.ppid, self.created = pid, ppid, created
        self.alive, self.in_job, self.deny, self.immortal = True, in_job, deny, immortal
        self.handles = 0


class FakeBackend:
    """Process table where a handle refers to a process OBJECT, so PID reuse is modelable."""

    def __init__(self):
        self.procs = []
        self.clock = itertools.count(100)
        self.log = []
        self.before_snapshot = []
        self.on_terminate = []
        self.snapshot_fails = False
        self.record = None  # attempts list, to prove record-before-signal
        self.add(CONTROLLER, 1, in_job=False)

    def add(self, pid, ppid, created=None, **kw):
        assert not any(p.pid == pid and self._present(p) for p in self.procs), "PID in use"
        proc = Proc(pid, ppid, next(self.clock) if created is None else created, **kw)
        self.procs.append(proc)
        return proc

    def exit(self, pid):
        self.live(pid).alive = False

    @staticmethod
    def _present(p):
        return p.alive or p.handles > 0  # a handle pins the process object and its PID

    def live(self, pid):
        return next(p for p in self.procs if p.pid == pid and self._present(p))

    def snapshot(self):
        for hook in list(self.before_snapshot):
            hook(self)
        if self.snapshot_fails:
            raise ss.SnapshotError("injected")
        return [(p.pid, p.ppid) for p in self.procs if p.alive]

    def open(self, pid):
        for p in self.procs:
            if p.pid == pid and self._present(p):
                p.handles += 1
                return p
        raise ss.OpenError(pid, ss.ERROR_INVALID_PARAMETER)

    def identity(self, h):
        return h.pid, h.ppid, h.created

    def is_exited(self, h):
        return not h.alive

    def wait(self, h, ms):
        return not h.alive

    def terminate(self, h, code):
        if self.record is not None:
            assert self.record and self.record[-1]["pid"] == h.pid, "signal before record"
        self.log.append(("terminate", h.pid))
        for hook in list(self.on_terminate):
            hook(self, h)
        if not h.alive:
            return False, ss.ERROR_ACCESS_DENIED
        if h.deny:
            return False, ss.ERROR_ACCESS_DENIED
        if not h.immortal:
            h.alive = False
        return True, 0

    def close(self, h):
        h.handles -= 1

    # job census
    def job_pids(self, job):
        return [p.pid for p in self.procs if p.alive and p.in_job]

    def job_accounting(self, job):
        members = [p for p in self.procs if p.in_job]
        return {"total": len(members), "active": sum(p.alive for p in members), "terminated": 0}

    def in_job(self, h, job):
        return h.in_job

    def terminate_job(self, job, code):
        for p in self.procs:
            if p.in_job:
                p.alive = False
        return True

    def close_job(self, job):
        pass


class Popenish:
    def __init__(self, pid):
        self.pid = pid

    def poll(self):
        return None


def make(backend, root=2000):
    backend.add(root, CONTROLLER)
    journal = ss.AncestryJournal(Popenish(root), backend=backend, controller_pid=CONTROLLER)
    return journal


def job(backend, assigned=True):
    return ss.CensusJob(backend, object(), assigned_before_resume=assigned)


def pids(record, result=None):
    return [a["pid"] for a in record["signal_attempts"] if result in (None, a["result"])]


def test_normal_tree_with_census_is_exhaustive_and_leaf_first():
    b = FakeBackend()
    j = make(b)
    b.add(2001, 2000)
    b.add(2002, 2001)
    b.add(2003, 2000)
    rec = ss.stop_tree(j, job=job(b), timeout_s=2)
    assert rec["exhaustive"] is True, rec["exhaustive_reasons"]
    assert set(rec["signalled_pids"]) == {2000, 2001, 2002, 2003}
    order = [pid for _, pid in b.log]
    assert order.index(2002) < order.index(2001) < order.index(2000)
    assert rec["process_tree"]["2002"] == 2001 and rec["process_tree"]["2000"] == CONTROLLER
    att = {a["pid"]: a for a in rec["signal_attempts"]}
    assert att[2002]["ancestry"] == [2002, 2001, 2000, CONTROLLER]
    assert all(a["result"] == "ok" and a["ancestry_proven"] for a in att.values())


def test_every_record_precedes_its_signal():
    b = FakeBackend()
    j = make(b)
    b.add(2001, 2000)
    b.add(2002, 2001)
    b.record = []  # FakeBackend.terminate asserts the target is already the last entry
    rec = ss.stop_tree(j, job=job(b), timeout_s=2, attempts=b.record)
    assert rec["signal_attempts"] is b.record and len(b.log) == 3
    assert all(a["observed_at_ns"] <= a["returned_at_ns"] for a in rec["signal_attempts"])


def test_without_census_never_exhaustive():
    b = FakeBackend()
    j = make(b)
    b.add(2001, 2000)
    rec = ss.stop_tree(j, timeout_s=2)
    assert rec["exhaustive"] is False
    assert any("no completeness census" in r for r in rec["exhaustive_reasons"])
    assert set(rec["signalled_pids"]) == {2000, 2001}


def test_job_assigned_after_start_is_not_exhaustive():
    b = FakeBackend()
    j = make(b)
    rec = ss.stop_tree(j, job=job(b, assigned=False), timeout_s=2)
    assert rec["exhaustive"] is False


def test_orphan_proven_by_pinned_observation_while_parent_alive():
    b = FakeBackend()
    j = make(b)
    b.add(2001, 2000)
    b.add(2002, 2001)
    j.refresh()  # journal pins 2001 and 2002 while 2001 is alive
    b.exit(2001)
    rec = ss.stop_tree(j, job=job(b), timeout_s=2)
    assert rec["exhaustive"] is True, rec["exhaustive_reasons"]
    att = {a["pid"]: a for a in rec["signal_attempts"]}
    assert att[2002]["parent_pid"] == 2001 and att[2002]["ancestry_proven"]
    assert 2001 not in rec["signalled_pids"]  # exited on its own: never signalled
    assert rec["process_tree"]["2001"] == 2000  # still in the tree for the ancestry walk


def test_orphan_with_unobserved_parent_is_signalled_but_not_exhaustive():
    b = FakeBackend()
    j = make(b)
    b.add(2001, 2000)
    b.add(2002, 2001)
    b.exit(2001)  # exits before any observation: PID 2001 is not pinned
    rec = ss.stop_tree(j, job=job(b), timeout_s=2)
    assert rec["exhaustive"] is False
    att = {a["pid"]: a for a in rec["signal_attempts"]}
    assert att[2002]["ancestry_proven"] is False and att[2002]["ancestry"] is None
    assert att[2002]["parent_pid"] == 2001 and 2002 in rec["signalled_pids"]
    assert "2001" not in rec["process_tree"]


def test_orphan_with_unobserved_parent_is_invisible_without_census():
    b = FakeBackend()
    j = make(b)
    b.add(2001, 2000)
    orphan = b.add(2002, 2001)
    b.exit(2001)
    rec = ss.stop_tree(j, timeout_s=2)
    assert 2002 not in pids(rec) and orphan.alive  # the silent miss a census exists for
    assert rec["exhaustive"] is False


def test_stale_parent_pid_reused_by_member_is_rejected():
    """PID reuse: an old process whose dead parent's PID now belongs to our member."""
    b = FakeBackend()
    b.add(3000, 1, created=10)
    foreign = b.add(2001, 3000, created=20)  # foreign process, ppid -> 3000
    b.exit(3000)
    j = make(b)  # root 2000 created later
    b.add(3000, 2000)  # our child happens to receive the recycled PID 3000
    j.refresh()
    assert 3000 in j.members and 2001 not in j.members
    assert any(r["pid"] == 2001 and "stale" in r["reason"] for r in j.rejections)
    rec = ss.stop_tree(j, timeout_s=2)
    assert 2001 not in pids(rec) and foreign.alive


def test_pid_reused_between_snapshot_and_open_is_rejected():
    b = FakeBackend()
    j = make(b)
    b.add(2001, 2000)
    real_snapshot = b.snapshot

    def racing_snapshot():
        rows = real_snapshot()
        b.exit(2001)  # 2001 dies after the snapshot row was taken, never pinned...
        b.add(2001, 4000)  # ...and a foreign process receives the PID before open()
        return rows

    b.snapshot = racing_snapshot
    j.refresh()
    b.snapshot = real_snapshot
    assert 2001 not in j.members
    assert any("reused between snapshot and open" in r["reason"] for r in j.rejections)
    rec = ss.stop_tree(j, timeout_s=2)
    assert 2001 not in pids(rec)


def test_decoy_outside_tree_never_opened_for_signal():
    b = FakeBackend()
    decoy = b.add(5000, CONTROLLER, in_job=False)
    j = make(b)
    b.add(2001, 2000)
    rec = ss.stop_tree(j, job=job(b), timeout_s=2)
    assert ("terminate", 5000) not in b.log and decoy.alive
    assert "5000" not in rec["process_tree"]
    assert rec["exhaustive"] is True


def test_churn_during_stop_is_caught_and_signalled():
    b = FakeBackend()
    j = make(b)
    b.add(2001, 2000)
    spawned = []

    def spawn_on_first_kill(backend, handle):
        if handle.pid == 2001 and not spawned:
            spawned.append(backend.add(2002, 2000))  # root forks while its child dies

    b.on_terminate.append(spawn_on_first_kill)
    rec = ss.stop_tree(j, job=job(b), timeout_s=2)
    assert 2002 in rec["signalled_pids"] and 2002 in rec["churn_pids"]
    assert rec["exhaustive"] is True, rec["exhaustive_reasons"]


def test_child_created_after_snapshot_proven_by_census_handle():
    b = FakeBackend()
    j = make(b)
    real = b.job_pids

    def job_pids_with_late_child(jobh):
        if not any(p.pid == 2009 for p in b.procs):
            b.add(2009, 2000)  # appears after refresh's snapshot, before the census
        return real(jobh)

    b.job_pids = job_pids_with_late_child
    rec = ss.stop_tree(j, job=job(b), timeout_s=2)
    att = {a["pid"]: a for a in rec["signal_attempts"]}
    assert att[2009]["ancestry_proven"] is True
    assert rec["exhaustive"] is True, rec["exhaustive_reasons"]


def test_denied_signal_is_recorded_and_not_exhaustive():
    b = FakeBackend()
    j = make(b)
    b.add(2001, 2000, deny=True)
    rec = ss.stop_tree(j, job=job(b), timeout_s=0.05, settle_ms=0)
    assert 2001 in rec["signalled_pids"]  # terminated or not, it was signalled
    assert "denied" in {a["result"] for a in rec["signal_attempts"] if a["pid"] == 2001}
    assert rec["exhaustive"] is False
    assert any("deadline" in r for r in rec["exhaustive_reasons"])


def test_already_exited_target_is_recorded_but_not_signalled():
    b = FakeBackend()
    j = make(b)
    b.add(2001, 2000)
    b.add(2002, 2000)  # created later, so signalled first among equal-depth siblings

    def sibling_exits(backend, handle):
        if handle.pid == 2002:
            backend.exit(2001)

    b.on_terminate.append(sibling_exits)
    rec = ss.stop_tree(j, job=job(b), timeout_s=2)
    results = {a["pid"]: a["result"] for a in rec["signal_attempts"]}
    assert results[2001] == "already_exited" and 2001 not in rec["signalled_pids"]
    assert rec["exhaustive"] is True, rec["exhaustive_reasons"]


def test_snapshot_error_is_not_exhaustive():
    b = FakeBackend()
    j = make(b)
    b.add(2001, 2000)
    j.refresh()
    b.snapshot_fails = True
    rec = ss.stop_tree(j, job=job(b), timeout_s=0.2, settle_ms=0)
    assert rec["exhaustive"] is False
    assert any("snapshot" in r for r in rec["exhaustive_reasons"])


def test_pre_stop_journal_error_is_not_exhaustive():
    b = FakeBackend()
    j = make(b)
    b.snapshot_fails = True
    j.refresh()
    b.snapshot_fails = False
    rec = ss.stop_tree(j, job=job(b), timeout_s=2)
    assert rec["exhaustive"] is False


def test_member_outside_job_breakaway_is_not_exhaustive():
    b = FakeBackend()
    j = make(b)
    b.add(2001, 2000, in_job=False)  # broke away from the census job
    rec = ss.stop_tree(j, job=job(b), timeout_s=2)
    assert 2001 in rec["signalled_pids"]  # still tracked by ancestry and signalled
    assert rec["exhaustive"] is False
    assert any("breakaway" in r for r in rec["exhaustive_reasons"])


def test_job_safety_net_firing_is_not_exhaustive():
    b = FakeBackend()
    j = make(b)
    b.add(2001, 2000, immortal=True)
    rec = ss.stop_tree(j, job=job(b), timeout_s=0.05, settle_ms=0)
    assert rec["exhaustive"] is False
    assert rec["job"]["safety_net_terminated_pids"] == [2001]
    assert rec["job"]["role"] == ss.JOB_ROLE and "never from job membership" in ss.JOB_ROLE


def test_root_must_be_controller_child():
    b = FakeBackend()
    b.add(2000, 4242)
    with pytest.raises(ss.SignalStopError):
        ss.AncestryJournal(Popenish(2000), backend=b, controller_pid=CONTROLLER)


def test_record_shape_matches_frozen_evidence_fields():
    b = FakeBackend()
    j = make(b)
    b.add(2001, 2000)
    rec = ss.stop_tree(j, job=job(b), timeout_s=2)
    assert isinstance(rec["signalled_pids"], list)
    tree = rec["process_tree"]
    assert all(isinstance(k, str) and isinstance(v, int) for k, v in tree.items())
    assert str(CONTROLLER) in tree
    j.close()
    assert all(p.handles == 0 for p in b.procs)


# ----------------------------------------------------------------- real Windows processes


def _canary():
    path = Path(__file__).resolve().parents[3] / "completion-evidence" / "signal_stop_canary.py"
    spec = importlib.util.spec_from_file_location("signal_stop_canary", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


windows = pytest.mark.skipif(os.name != "nt", reason="Win32 process table")


@pytest.fixture(scope="module")
def real():
    canary = _canary()
    with tempfile.TemporaryDirectory(prefix="signal-stop-test-") as work:
        decoy = canary.start_decoy(work)
        try:
            yield canary, work, decoy
        finally:
            decoy.kill()
            decoy.wait()


def _run(real, name):
    canary, work, decoy = real
    mode, kw = canary.SCENARIOS[name]
    result = canary.run_scenario(name, mode, work, decoy=decoy, **kw)
    assert result["decoy_attempted"] is False and result["decoy_alive_after"] is True
    return result


@windows
def test_real_normal_tree_with_census_and_journal(real):
    r = _run(real, "normal_job_journal")
    assert r["exhaustive"] is True, r["exhaustive_reasons"]
    assert r["every_signalled_chains_to_controller"] and not r["leaves_survived_stop"]


@windows
def test_real_normal_tree_without_census(real):
    r = _run(real, "normal_no_job_journal")
    assert r["exhaustive"] is False and r["every_signalled_chains_to_controller"]


@windows
def test_real_orphan_proven_by_journal(real):
    r = _run(real, "orphan_job_journal")
    assert r["exhaustive"] is True, r["exhaustive_reasons"]
    assert r["every_signalled_chains_to_controller"]


@windows
def test_real_orphan_stop_time_only_is_unprovable(real):
    r = _run(real, "orphan_job_stoptime")
    assert r["exhaustive"] is False and r["unproven_pids"]
    assert not r["every_signalled_chains_to_controller"] and not r["leaves_survived_stop"]


@windows
def test_real_orphan_without_census_is_a_silent_miss(real):
    r = _run(real, "orphan_no_job_stoptime")
    assert r["exhaustive"] is False
    assert r["leaves_survived_stop"] == r["leaf_pids_from_markers"]  # never signalled


@windows
def test_real_churn_converges_without_leaving_survivors(real):
    r = _run(real, "churn_job_journal")
    assert r["record"]["job"]["safety_net_terminated_pids"] in ([], None) or not r["exhaustive"]
    if r["exhaustive"]:
        assert r["every_signalled_chains_to_controller"]
