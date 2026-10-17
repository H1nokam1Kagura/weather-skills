"""Wave 8 B1: the CAP-METRIC resource half and FX-DECOY-SPAWN for E12-P2 (frozen v2.7).

The frozen DEV-ONLY schedules are read-only inputs driven through the real hook adapter and
the real process manager (tools/workflow/processes.Processes). Offline: no model, no network.
The deferred tests launch one real short-lived child; the host RAM counter is replaced by a
constant (processes.free_gib), so only the injected reading can cross the RAM floor.
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

from tools.workflow import processes as processes_module
from tools.workflow.contracts import Refused
from tools.workflow.e2e_injections import (
    DECOY_ARGS,
    HarnessDispatchError,
    InjectionHooks,
    UnsupportedInjection,
    resource_reading_form,
    unsupported_requirements,
)
from tools.workflow.processes import Processes

FROZEN = frozen_root()  # verified against FINAL-PINS by tests/frozen_v27.py
DEV_ONLY = FROZEN / "casesets/cached-onset-e2e-v2_7/frozen/agentic-e2e-injections.DEV-ONLY.json"
PLOT_ARGV = ["--input", "onset.zarr", "--output", "plot.png"]
HOST_FREE_GIB = 64.0


@functools.cache
def _frozen():
    pins = json.loads((FROZEN / "FINAL-PINS.json").read_text(encoding="utf-8"))["files"]
    actual = hashlib.sha256(DEV_ONLY.read_bytes()).hexdigest()
    assert actual == pins[DEV_ONLY.relative_to(FROZEN).as_posix()], "frozen file drifted"
    data = json.loads(DEV_ONLY.read_text(encoding="utf-8"))
    return {c["id"].rsplit("V2_7-", 1)[1]: c for c in data["cases"]}


def frozen(name):
    return copy.deepcopy(_frozen()[name])


def only(name, *indices):
    """The frozen case with its schedule narrowed to ``indices`` (entries verbatim)."""
    case = frozen(name)
    case["injection_schedule"] = [case["injection_schedule"][i] for i in indices]
    return case


@pytest.fixture(autouse=True)
def host_ram(monkeypatch):
    monkeypatch.setattr(processes_module, "free_gib", lambda: HOST_FREE_GIB)


def bound_controller(tmp_path):
    """A controller whose process manager carries its own durable event binding."""
    root = tmp_path / "run"
    root.mkdir()
    controller = SimpleNamespace(root=root, paths={}, pending=None)
    log = []

    def on_event(kind, details):
        log.append((kind, json.loads(json.dumps(details))))

    on_event.controller_owner = controller
    controller.processes = Processes(root, on_event=on_event)
    return controller, on_event, log


def child(seconds):
    code = f"import time; time.sleep({seconds})" if seconds else "pass"
    return [sys.executable, "-c", code, *PLOT_ARGV]


def launch_checks(log):
    return [d for k, d in log if k == "launch_check"]


PLOT_EVENT = {"state": "ONSET_CHECKED", "skill": "plot-onset", "argv": PLOT_ARGV}


def before_plot(h, controller):
    h("before_tool", dict(PLOT_EVENT), controller)


def after_plot(h, controller):
    h("after_tool", dict(PLOT_EVENT), controller)


# ------------------------------------------------------------------ launch-check form


def test_launch_check_label_is_carried_by_that_launch_check_only(tmp_path):
    h = InjectionHooks(only("E12-P2-CTL", 1))
    controller, _, log = bound_controller(tmp_path)
    before_plot(h, controller)
    (record,) = h.records
    assert record["delivery"] == "ARMED" and record["reading_form"] == "launch_check"
    result = controller.processes.run(child(0), tmp_path, dict(os.environ), tool="plot-onset")
    assert result["exit_code"] == 0
    (first,) = launch_checks(log)
    assert first["tool"] == "plot-onset" and first["free_ram_gib"] == 4.0
    assert first["reading_source"] == "injected" and first["launched"] is True
    assert record["delivery"] == "DELIVERED" and record["injected_reading"] is True
    after_plot(h, controller)  # the next boundary settles the spent one-shot reader
    assert controller.processes.memory_reader is None and h.armed_readings == []
    controller.processes.run(child(0), tmp_path, dict(os.environ), tool="verify-run")
    second = launch_checks(log)[1]
    assert second["tool"] == "verify-run" and second["free_ram_gib"] == HOST_FREE_GIB
    assert second["reading_source"] == "observed"
    assert "inject" not in json.dumps(result)  # the flag never reaches what the system reads


def test_one_shot_reader_uninstalls_itself_on_the_next_read(tmp_path):
    h = InjectionHooks(only("E12-P2-CTL", 3))  # verify-run: "before every later launch"
    controller, _, log = bound_controller(tmp_path)
    h("before_tool", {"state": "X", "skill": "verify-run", "argv": []}, controller)
    (record,) = h.records
    assert record["reading_form"] == "launch_check_every_later"
    assert "selected launch" in record["adapter_mapping"]  # D6: the selector governs
    result = controller.processes.run(child(1.5), tmp_path, dict(os.environ), tool="verify-run")
    assert launch_checks(log)[0]["free_ram_gib"] == 4.0
    assert result["min_free_gib"] == HOST_FREE_GIB  # in-run samples are real readings
    assert controller.processes.memory_reader is None and h.armed_readings == []


def test_a_reading_with_no_launch_check_is_undelivered_at_the_next_boundary(tmp_path):
    h = InjectionHooks(only("E12-P2-CTL", 1))
    controller, _, _ = bound_controller(tmp_path)
    before_plot(h, controller)
    after_plot(h, controller)
    assert h.records[0]["delivery"] == "UNDELIVERED"
    assert controller.processes.memory_reader is None


# ------------------------------------------------------------------- deferred form


def test_deferred_1_8_trips_the_ram_guard_two_seconds_after_the_start_receipt(tmp_path):
    h = InjectionHooks(frozen("E12-P2-INJ"))
    controller, bound, log = bound_controller(tmp_path)
    before_plot(h, controller)
    launch, deferred = h.records
    assert deferred["delivery"] == "ARMED" and deferred["offset_seconds"] == 2.0
    with pytest.raises(Refused) as caught:
        controller.processes.run(child(5), tmp_path, dict(os.environ), tool="plot-onset")
    assert caught.value.gate == "resource_guard" and str(caught.value) == "RAM guard"
    assert launch_checks(log)[0]["reading_source"] == "injected"
    assert launch["delivery"] == "DELIVERED"
    assert deferred["delivery"] == "DELIVERED" and deferred["delivered_value"] == 1.8
    assert 2.0 <= deferred["delivered_offset_seconds"] < 4.5
    receipt = json.loads((controller.root / "process-001.start.json").read_text("utf-8"))
    assert deferred["start_receipt"]["serial"] == 1
    assert deferred["start_receipt"]["started_monotonic_ns"] == receipt["started_monotonic_ns"]
    assert deferred["delivered_monotonic_ns"] - receipt["started_monotonic_ns"] >= 2_000_000_000
    final = json.loads((controller.root / "process-001.json").read_text("utf-8"))
    assert final["stop_reason"] == "RAM guard" and final["min_free_gib"] == 1.8
    (exit_event,) = [d for k, d in log if k == "process_exit"]
    assert exit_event["stop_reason"] == "RAM guard" and exit_event["exit_code"] is None
    assert controller.processes.on_event is bound and controller.processes.memory_reader is None
    assert h.armed_readings == []
    json.dumps(h.export_state())  # nothing armed any more


def test_deferred_2_4_does_not_trip_the_guard(tmp_path):
    h = InjectionHooks(frozen("E12-P2-CTL"))
    controller, bound, _ = bound_controller(tmp_path)
    before_plot(h, controller)
    result = controller.processes.run(child(3.5), tmp_path, dict(os.environ), tool="plot-onset")
    assert result["exit_code"] == 0 and result["stop_reason"] is None
    assert result["min_free_gib"] == 2.4
    deferred = h.records[1]
    assert deferred["delivery"] == "DELIVERED" and deferred["delivered_value"] == 2.4
    assert controller.processes.on_event is bound and controller.processes.memory_reader is None


def test_an_early_exit_leaves_the_deferred_reading_undelivered(tmp_path):
    h = InjectionHooks(frozen("E12-P2-INJ"))
    controller, bound, _ = bound_controller(tmp_path)
    before_plot(h, controller)
    result = controller.processes.run(child(0), tmp_path, dict(os.environ), tool="plot-onset")
    assert result["exit_code"] == 0
    deferred = h.records[1]
    assert deferred["delivery"] == "UNDELIVERED"
    assert deferred["reason"] == "tool exited before the deferred reading was due"
    assert "delivered_value" not in deferred
    assert controller.processes.on_event is bound
    assert all(s["kind"] != "deferred" for s in h.armed_readings)
    after_plot(h, controller)
    assert controller.processes.memory_reader is None and h.armed_readings == []


def test_deferred_reading_ignores_another_tools_launch(tmp_path):
    h = InjectionHooks(only("E12-P2-INJ", 2))
    controller, bound, _ = bound_controller(tmp_path)
    before_plot(h, controller)
    controller.processes.run([sys.executable, "-c", "pass"], tmp_path, dict(os.environ), tool="x")
    assert h.records[0]["delivery"] == "UNDELIVERED"
    assert controller.processes.on_event is bound and controller.processes.memory_reader is None


def test_deferred_reading_needs_the_controllers_process_event_binding(tmp_path):
    h = InjectionHooks(only("E12-P2-INJ", 2))
    controller, _, _ = bound_controller(tmp_path)
    controller.processes.on_event = lambda kind, details: None  # not the controller's record
    with pytest.raises(UnsupportedInjection, match="durable process-event record"):
        before_plot(h, controller)


# --------------------------------------------------------------- only the frozen forms


def test_only_the_three_frozen_forms_are_accepted():
    inj = frozen("E12-P2-INJ")["injection_schedule"]
    ctl = frozen("E12-P2-CTL")["injection_schedule"]
    assert [resource_reading_form(s["payload"]) for s in inj[1:]] == ["launch_check", "deferred"]
    assert [resource_reading_form(s["payload"]) for s in ctl[1:]] == [
        "launch_check",
        "deferred",
        "launch_check_every_later",
    ]
    base = inj[1]["payload"]
    variants = [
        {"metric": "free_ram_gib", "value": 1.8},  # test_e2e_injections / rereview form
        {**base, "timing": "immediate"},
        {**base, "timing": "Launch check"},
        {**base, "timing": "deferred"},
        {**base, "metric": "consumed_model_usd"},
        {**base, "value": True},
        {**base, "value": float("inf")},
        {**base, "value": 0},
        {**base, "value": "4.0"},
        {**base, "also": {"elapsed_seconds": 1}},
        {**base, "source": "injected"},
    ]
    for payload in variants:
        assert resource_reading_form(payload) is None, payload
        spec = {"hook": "before_tool", "operation": "inject_metric", "payload": payload}
        spec["selector"] = {"role_or_tool_or_stage": "plot-onset", "occurrence": 1}
        findings = unsupported_requirements({"injection_schedule": [spec]}, stop_delivery=True)
        assert findings == [{"index": 0, "reason": "resource metric adapter unavailable"}]
        with pytest.raises(UnsupportedInjection):
            InjectionHooks({})._apply("inject_metric", payload, "before_tool", {}, None, {})
    # A frozen form outside before_tool is refused, statically and at delivery.
    moved = copy.deepcopy(inj[1])
    moved["hook"] = "after_tool"
    assert unsupported_requirements({"injection_schedule": [moved]}) != []
    with pytest.raises(UnsupportedInjection, match="before_tool"):
        InjectionHooks({})._apply("inject_metric", base, "after_tool", {}, None, {})


def test_export_state_is_refused_while_a_reading_is_armed(tmp_path):
    h = InjectionHooks(frozen("E12-P2-INJ"))
    controller, _, _ = bound_controller(tmp_path)
    before_plot(h, controller)
    with pytest.raises(HarnessDispatchError, match="resource reading is armed"):
        h.export_state()
    controller.processes.run(child(0), tmp_path, dict(os.environ), tool="plot-onset")
    after_plot(h, controller)
    state = h.export_state()
    assert [r["delivery"] for r in state["records"]] == ["DELIVERED", "UNDELIVERED"]


# ---------------------------------------------------------------------- FX-DECOY-SPAWN


def test_decoy_spawn_records_the_harness_spawned_pid_and_pgid(tmp_path):
    calls = []

    def spawner(**kwargs):
        calls.append(kwargs)
        return {"pid": 424242, "pgid": 424242}

    h = InjectionHooks(only("E12-P2-INJ", 0), decoy_spawner=spawner)
    h.prepare({}, tmp_path)
    (record,) = h.records
    assert record["status"] == "APPLIED"
    assert calls == [
        {"argv": DECOY_ARGS["argv"], "label": "plot-onset-decoy", "lifetime_seconds": 1800}
    ]
    assert record["decoy"] == {**DECOY_ARGS, "pid": 424242, "pgid": 424242, "spawned_by": "harness"}
    assert record["target"] == "harness process space"


def test_decoy_spawn_is_refused_without_a_harness_spawner(tmp_path):
    h = InjectionHooks(only("E12-P2-INJ", 0))
    with pytest.raises(UnsupportedInjection, match="harness spawner"):
        h.prepare({}, tmp_path)
    assert h.records[0]["status"] == "UNSUPPORTED"


def test_decoy_spawn_accepts_only_the_frozen_literal_argv(tmp_path):
    for change in (
        {"argv": ["python", "-c", "import time; time.sleep(1800)"]},
        {"argv": ["sh", "-c", "sleep 1800", "plot-onset-decoy"]},
        {"lifetime_seconds": 60},
        {"parent": "controller"},
        {"label": "other"},
    ):
        case = only("E12-P2-INJ", 0)
        case["injection_schedule"][0]["payload"]["args"].update(change)
        h = InjectionHooks(case, decoy_spawner=lambda **kw: {"pid": 9, "pgid": 9})
        with pytest.raises(UnsupportedInjection, match="literal argv"):
            h.prepare({}, tmp_path)


def test_decoy_spawner_must_return_a_harness_owned_pid(tmp_path):
    for returned in (None, {"pid": 0, "pgid": 1}, {"pid": os.getpid(), "pgid": 5}, {"pid": 7}):
        h = InjectionHooks(only("E12-P2-INJ", 0), decoy_spawner=lambda r=returned, **kw: r)
        with pytest.raises(HarnessDispatchError):
            h.prepare({}, tmp_path)
        assert h.records[0]["status"] == "ERROR"
