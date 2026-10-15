"""Wave 7 sub-wave B: interrupt delivery for the E10 / E11 / E14 stop-and-resume family.

The frozen v2.7 DEV-ONLY schedules are read-only inputs driven through the real hook adapter
(tools/workflow/e2e_injections.InjectionHooks). Offline: no model, no network; the mid-process
tests launch one real short-lived child through the real process manager.

Interfaces documented here for the adapter's resume driver (sub-wave C):

* ``InjectionHooks(case, ..., stopper=None)``. ``stopper(packet) -> NoReturn`` is called once,
  by the hook, at the stop boundary of a ``harness_stop_controller`` interrupt. In the real
  route it hands ``packet`` to the parent, requests the kill and blocks (the process is
  killed). If it returns, the hook raises ``HarnessDispatchError``. With no stopper the old
  ``InjectedInterruption`` stays the unit double, and the mid-process form is refused.
* ``packet = {"schema": "e2e-injections.stop/1", "stop": <stop record>, "deferred": [...],
  "state": <export_state()>}``.
* A deferred entry is ``{"index", "id", "hook", "selector", "operation", "occurrence",
  "boundary_state", "stop_index"}``: an entry matched at the SAME boundary after the stop and
  NOT applied (``id`` is None for schedules without ``requires``). Deferred entries stay in
  ``hooks.deferred`` (and in the exported state) until ``apply_deferred`` delivers them.
* ``hooks.apply_deferred(controller, entries=None) -> [record...]`` delivers them in order
  (E10-P1-INJ: FX-ONSET-CELL-SHIFT, then the resume request). ``controller`` only needs what
  the operation reads (``paths`` for a fixture mutation).
* ``hooks.export_state(rebinds=None) -> dict`` (JSON data, schema ``e2e-injections.state/1``)
  and ``fresh_hooks.import_state(state) -> rebinds``: records, done, counts (as
  ``[[key...], n]``), dispositions (as ``[[index, dict]]``), deferred entries, the stop record,
  resume/notification/approval counters and wrapper-owned rebinds.
* ``unsupported_requirements(case, stop_delivery=False)``: only a caller that runs the hooks
  with a stopper passes ``stop_delivery=True``; only then is the frozen CAP-MIDPROCESS stop
  form statically supported. The default keeps today's run_case / admission refusal.
* Mid-process (E11-P1-INJ): armed at ``before_tool``; fires on that tool's own
  ``process_launch``, after the controller's own process-event binding (``controller_owner``)
  has recorded it and after ``process-NNN.start.json`` is durable.
* E14-P2-INJ's after-AUTHORIZED ``deny_write`` naming HARNESS_RUN_DIR calls
  ``write_boundary.inject_run_dir_denial(controller, artifact=, errno=, message=)`` (sub-wave
  A), looked up at call time; refused when absent. E12-P1-INJ's commit-time path is unchanged.
"""

import copy
import functools
import hashlib
import json
import os
import sys
from types import SimpleNamespace

import pytest
from frozen_v27 import frozen_root

from tools.workflow import write_boundary
from tools.workflow.e2e_injections import (
    STATE_SCHEMA,
    STOP_SCHEMA,
    HarnessDispatchError,
    InjectedInterruption,
    InjectionHooks,
    UnsupportedInjection,
    unsupported_requirements,
)
from tools.workflow.processes import Processes

FROZEN = frozen_root()  # verified against FINAL-PINS by tests/frozen_v27.py
DEV_ONLY = FROZEN / "casesets/cached-onset-e2e-v2_7/frozen/agentic-e2e-injections.DEV-ONLY.json"
SYMBOLS = {"HARNESS_RUN_ID": "run-interrupt"}
ONSET_ARGV = ["--input", "source.zarr", "--output", "onset.zarr"]


@functools.cache
def _frozen():
    pins = json.loads((FROZEN / "FINAL-PINS.json").read_text(encoding="utf-8"))["files"]
    actual = hashlib.sha256(DEV_ONLY.read_bytes()).hexdigest()
    assert actual == pins[DEV_ONLY.relative_to(FROZEN).as_posix()], "frozen file drifted"
    data = json.loads(DEV_ONLY.read_text(encoding="utf-8"))
    return {c["id"].rsplit("V2_7-", 1)[1]: c for c in data["cases"]}


def frozen(name):
    return copy.deepcopy(_frozen()[name])


class Killed(BaseException):
    """The test stopper's stand-in for the kill: the real stopper never returns."""


def recording_stopper():
    packets = []

    def stopper(packet):
        packets.append(json.loads(json.dumps(packet)))  # must be JSON data
        raise Killed

    return stopper, packets


def onset_fixture(path):
    import numpy as np
    import zarr

    group = zarr.open_group(str(path), mode="w", zarr_format=2)
    missing = np.iinfo("int64").min
    values = np.array([[missing, 0], [1, 2]], dtype="int64")
    array = group.create_array("onset_precip_date", data=values, fill_value=missing)
    array.attrs["units"] = "days since 2025-09-16 00:00:00"
    return array


def stop_e10(tmp_path):
    stopper, packets = recording_stopper()
    h = InjectionHooks(
        frozen("E10-P1-INJ"), symbols=SYMBOLS, disposable_root=tmp_path, stopper=stopper
    )
    with pytest.raises(Killed):
        h("after_transition", {"state": "ONSET_CHECKED"}, SimpleNamespace(pending=None))
    return h, packets


# ----------------------------------------------------------------------- (1) stopper


def test_stopper_is_called_at_the_stop_boundary_with_a_json_packet(tmp_path):
    h, (packet,) = stop_e10(tmp_path)
    assert packet["schema"] == STOP_SCHEMA
    assert packet["stop"]["index"] == 0
    assert packet["stop"]["status"] == "APPLIED"
    assert packet["stop"]["delivery"] == "STOP_REQUESTED"
    assert packet["state"]["schema"] == STATE_SCHEMA
    assert h.coverage()["applied"] == [0]
    assert not h.resume_requested  # deferred, not delivered in the stopped incarnation


def test_a_stopper_that_returns_is_a_harness_error():
    h = InjectionHooks(frozen("E10-P1-CTL"), symbols=SYMBOLS, stopper=lambda packet: None)
    with pytest.raises(HarnessDispatchError, match="must not return"):
        h("after_transition", {"state": "ONSET_CHECKED"}, SimpleNamespace(pending=None))
    assert h.harness_error


def test_without_a_stopper_the_unit_double_is_unchanged():
    h = InjectionHooks(frozen("E10-P1-CTL"), symbols=SYMBOLS)
    with pytest.raises(InjectedInterruption):
        h("after_transition", {"state": "ONSET_CHECKED"}, SimpleNamespace(pending=None))
    assert h.resume_requested and h.coverage()["applied"] == [0, 1] and h.deferred == []


def test_a_stop_with_pending_work_is_still_refused():
    stopper, packets = recording_stopper()
    h = InjectionHooks(frozen("E10-P1-CTL"), symbols=SYMBOLS, stopper=stopper)
    with pytest.raises(UnsupportedInjection):
        h("after_transition", {"state": "ONSET_CHECKED"}, SimpleNamespace(pending={"x": 1}))
    assert packets == []


# --------------------------------------------------------------- (2) deferred entries


def test_same_boundary_entries_after_the_stop_are_deferred_and_returned(tmp_path):
    h, (packet,) = stop_e10(tmp_path)
    assert [(d["index"], d["operation"]) for d in packet["deferred"]] == [
        (1, "mutate_fixture"),
        (2, "request_transition"),
    ]
    assert all(
        d["hook"] == "after_transition"
        and d["boundary_state"] == "ONSET_CHECKED"
        and d["occurrence"] == 1
        and d["stop_index"] == 0
        for d in packet["deferred"]
    )
    assert h.deferred == packet["deferred"] == packet["state"]["deferred"]
    assert 1 in h.coverage()["unreached"] and 2 in h.coverage()["unreached"]  # not dropped


def test_parent_applies_the_deferred_entries_after_the_kill_in_order(tmp_path):
    _, (packet,) = stop_e10(tmp_path)
    array = onset_fixture(tmp_path / "onset.zarr")
    parent = InjectionHooks(frozen("E10-P1-INJ"), symbols=SYMBOLS, disposable_root=tmp_path)
    parent.import_state(packet["state"])
    records = parent.apply_deferred(SimpleNamespace(paths={"onset": tmp_path / "onset.zarr"}))
    assert [(r["index"], r["status"]) for r in records] == [(1, "APPLIED"), (2, "APPLIED")]
    assert all(r["deferred_after_stop"] == 0 for r in records)
    assert records[0]["before_hash"] != records[0]["after_hash"]
    assert array[0, 1] == 1  # FX-ONSET-CELL-SHIFT delivered
    assert parent.resume_requested and parent.deferred == []
    assert parent.coverage()["applied"] == [0, 1, 2] and parent.coverage()["unreached"] == []
    # Once only: the resumed incarnation never re-applies at the same boundary.
    parent("after_transition", {"state": "ONSET_CHECKED"}, SimpleNamespace(pending=None))
    assert len(parent.records) == 3


def test_apply_deferred_refuses_an_entry_that_was_not_deferred(tmp_path):
    h, _ = stop_e10(tmp_path)
    with pytest.raises(HarnessDispatchError, match="not a deferred entry"):
        h.apply_deferred(SimpleNamespace(paths={}), [{"index": 0}])


# ------------------------------------------------------------- (3) mid-process arming


def bound_controller(tmp_path, log):
    """A controller whose process manager carries its own durable event binding."""
    root = tmp_path / "run"
    root.mkdir()
    controller = SimpleNamespace(root=root, paths={}, pending=None)

    def on_event(kind, details):
        start = root / f"process-{details.get('serial', 0):03}.start.json"
        log.append(("controller_record", kind, details.get("tool"), start.is_file()))

    on_event.controller_owner = controller
    processes = Processes(root, memory_reader=lambda: 64.0, on_event=on_event)
    controller.processes = processes
    return controller, on_event


def child(code="import time; time.sleep(30)"):
    return [sys.executable, "-c", code, *ONSET_ARGV]


def arm_e11(tmp_path, log):
    stopper, packets = recording_stopper()

    def logging_stopper(packet):
        log.append(("stopper", packet["stop"]["start_receipt"]["serial"]))
        stopper(packet)

    h = InjectionHooks(frozen("E11-P1-INJ"), symbols=SYMBOLS, stopper=logging_stopper)
    controller, bound = bound_controller(tmp_path, log)
    h("before_tool", {"skill": "onset-date", "argv": ONSET_ARGV}, controller)
    return h, controller, bound, packets


def test_midprocess_stop_fires_on_its_own_launch_after_the_durable_record(tmp_path):
    log = []
    h, controller, bound, packets = arm_e11(tmp_path, log)
    (armed,) = h.records
    assert armed["delivery"] == "ARMED" and armed["status"] == "APPLIED"
    assert [d["operation"] for d in h.deferred] == ["request_transition"]
    env = dict(os.environ)
    with pytest.raises(Killed):
        controller.processes.run(child(), tmp_path, env, tool="onset-date")
    # The controller's own record (with the start receipt durable) precedes the stop.
    launch = log.index(("controller_record", "process_launch", "onset-date", True))
    assert log[launch + 1] == ("stopper", 1)
    assert (controller.root / "process-001.start.json").is_file()
    (packet,) = packets
    assert packet["stop"]["delivery"] == "STOP_REQUESTED"
    assert packet["stop"]["start_receipt"]["tool"] == "onset-date"
    assert [d["index"] for d in packet["deferred"]] == [1]
    assert controller.processes.on_event is bound  # the trigger is consumed exactly once
    assert not h.resume_requested


def test_midprocess_trigger_ignores_another_tools_launch(tmp_path):
    log = []
    h, controller, bound, packets = arm_e11(tmp_path, log)
    argv = [sys.executable, "-c", "pass"]
    result = controller.processes.run(argv, tmp_path, dict(os.environ), tool="check-artifact")
    assert result["exit_code"] == 0 and packets == []
    assert h.records[0]["delivery"] == "UNDELIVERED"
    assert controller.processes.on_event is bound


def test_midprocess_form_without_a_stopper_or_binding_is_refused(tmp_path):
    h = InjectionHooks(frozen("E11-P1-INJ"), symbols=SYMBOLS)
    controller, _ = bound_controller(tmp_path, [])
    with pytest.raises(UnsupportedInjection, match="process-start adapter"):
        h("before_tool", {"skill": "onset-date", "argv": ONSET_ARGV}, controller)
    stopper, _ = recording_stopper()
    h = InjectionHooks(frozen("E11-P1-INJ"), symbols=SYMBOLS, stopper=stopper)
    controller.processes.on_event = lambda kind, details: None  # not the controller's record
    with pytest.raises(UnsupportedInjection, match="durable process-event record"):
        h("before_tool", {"skill": "onset-date", "argv": ONSET_ARGV}, controller)


def test_static_findings_relax_only_the_midprocess_stop_form():
    for name in ("E10-P1-INJ", "E10-P1-CTL", "E11-P1-INJ", "E11-P1-CTL"):
        assert unsupported_requirements(frozen(name), stop_delivery=True) == [], name
    # Without stop delivery (no stopper: today's run_case and admission) E11-P1-INJ is refused.
    assert unsupported_requirements(frozen("E11-P1-INJ")) == [
        {"index": 0, "reason": "midprocess crash adapter unavailable"}
    ]
    # E12-P2's deferred metric (also "CAP-MIDPROCESS") stays refused.
    for name in ("E12-P2-INJ", "E12-P2-CTL"):
        findings = unsupported_requirements(frozen(name), stop_delivery=True)
        assert {"index": 2, "reason": "resource metric adapter unavailable"} in findings
    # A before_tool stop that is not the frozen mid-process text stays refused.
    case = frozen("E11-P1-INJ")
    case["injection_schedule"][0]["payload"]["when"] = "immediately"
    findings = unsupported_requirements(case, stop_delivery=True)
    assert findings[0]["reason"] == "midprocess crash adapter unavailable"


# ----------------------------------------------------------------- (4) export / import


def test_export_import_round_trips_into_a_fresh_process(tmp_path):
    h, (packet,) = stop_e10(tmp_path)
    state = json.loads(json.dumps(packet["state"]))
    fresh = InjectionHooks(frozen("E10-P1-INJ"), symbols=SYMBOLS)
    assert fresh.import_state(state) == []
    assert fresh.export_state() == state == h.export_state()
    assert fresh.counts == h.counts and fresh.done == {0}
    rebinds = [{"invocation_id": "i-1", "session_id": "s-1"}]
    other = InjectionHooks(frozen("E10-P1-INJ"))
    assert other.import_state(h.export_state(rebinds=rebinds)) == rebinds


def test_dependent_dispositions_round_trip(tmp_path):
    hooks = InjectionHooks(frozen("E14-P1-INJ"), symbols=SYMBOLS)
    hooks.dispositions[0] = {"disposition": "DELIVERED", "occurrence": 1}
    hooks.counts[("after_role", "compiler")] = 3
    hooks.seen_notifications.update({None, 2})
    fresh = InjectionHooks(frozen("E14-P1-INJ"), symbols=SYMBOLS)
    fresh.import_state(hooks.export_state())
    assert fresh.dispositions == hooks.dispositions
    assert fresh.counts == hooks.counts and fresh.seen_notifications == {None, 2}
    assert fresh.dependency_report() == hooks.dependency_report()


def test_import_refuses_another_schedule_or_used_hooks(tmp_path):
    h, (packet,) = stop_e10(tmp_path)
    with pytest.raises(HarnessDispatchError, match="another schedule"):
        InjectionHooks(frozen("E10-P1-CTL")).import_state(packet["state"])
    with pytest.raises(HarnessDispatchError, match="fresh hooks"):
        h.import_state(packet["state"])
    with pytest.raises(HarnessDispatchError, match="not an exported"):
        InjectionHooks(frozen("E10-P1-INJ")).import_state({"schema": "x"})


def test_export_refuses_an_armed_transient_fault():
    h = InjectionHooks(frozen("E10-P1-INJ"))
    h.transport_fault = {"armed": True}
    with pytest.raises(HarnessDispatchError, match="armed"):
        h.export_state()


# ------------------------------------------------------------ (5) run-directory denial


def denial_controller(tmp_path):
    root = tmp_path / "run"
    return SimpleNamespace(
        root=root,
        paths={"source": tmp_path / "source.zarr", "onset": root / "outputs" / "onset.zarr"},
        context={},
    )


def run_dir_denial(tmp_path):
    case = frozen("E14-P2-INJ")
    hooks = InjectionHooks(case, symbols=SYMBOLS)
    controller = denial_controller(tmp_path)
    spec = case["injection_schedule"][2]
    payload = hooks.resolve(spec["payload"], controller)
    record = {"index": 2}
    event = {"state": "AUTHORIZED"}
    return hooks, controller, payload, record, event


def test_run_dir_denial_routes_to_the_pre_launch_api(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(
        write_boundary,
        "inject_run_dir_denial",
        lambda controller, **kw: calls.append((controller, kw)),
        raising=False,
    )
    hooks, controller, payload, record, event = run_dir_denial(tmp_path)
    assert (
        hooks._apply("deny_write", payload, "after_transition", event, controller, record) is False
    )
    message = f"Permission denied: {controller.root}"
    assert calls == [(controller, {"artifact": "onset", "errno": 13, "message": message})]
    assert record["target"] == "run_dir" and record["denial_source"] == "injected_fault"
    assert getattr(controller, "write_boundary", None) is None  # commit-time path untouched


def test_run_dir_denial_is_refused_without_the_controller_api(tmp_path, monkeypatch):
    monkeypatch.delattr(write_boundary, "inject_run_dir_denial", raising=False)
    hooks, controller, payload, record, event = run_dir_denial(tmp_path)
    with pytest.raises(UnsupportedInjection, match="inject_run_dir_denial"):
        hooks._apply("deny_write", payload, "after_transition", event, controller, record)


def test_commit_time_denial_of_e12_p1_is_unchanged(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(
        write_boundary, "inject_run_dir_denial", lambda *a, **kw: calls.append(a), raising=False
    )
    case = frozen("E12-P1-INJ")
    hooks = InjectionHooks(case, symbols=SYMBOLS)
    controller = denial_controller(tmp_path)
    argv = ["--input", str(controller.paths["source"]), "--output", str(controller.paths["onset"])]
    hooks("before_tool", {"skill": "onset-date", "argv": argv}, controller)
    assert calls == []
    assert list(controller.write_boundary.armed) == ["onset"]
    assert hooks.records[0]["effect"] == "armed controller write boundary"
