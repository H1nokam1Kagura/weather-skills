"""Wave 8 B2: CAP-DECOY and the CAP-METRIC resource half on the adapter (E12-P2, frozen v2.7).

E12-P2-CTL and E12-P2-INJ run verbatim through WeatherAdapter.run_case with the zero-cost fake
relay of live-role sub-wave 2 (tests/test_live_roles_adapter.py: a REAL RelayRoles client over a
fake host transport). No model, relay, network or Kong call is made. The plot-onset stand-in
sleeps a few seconds, so the deferred free_ram_gib reading (2 s after its durable start
receipt) reaches the controller's in-run RAM sampler while it runs; the host RAM counter is the
lab's constant reader, so only the injected reading can cross the floor.

The decoy is a STUB spawner returning a pid no process holds: nothing here starts a sleeper.
That the decoy stays alive and unsignalled is checked as what the run did to it: no os.kill /
os.killpg call named its pid or process group. Only a container run (entry.sh, strace) can show
harness_observations.decoy; in process it is MISSING by design.

CAP-DECOY and the live-wave capabilities are declared here only by test-local signed records
over resigned(); the committed producer file is never written.
"""

import copy
import io
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_evidence_projection_v27 import TOOL_SCRIPT
from test_live_roles_adapter import (
    PREFIX,
    SIGN,
    FakeRelay,
    LiveLab,
    frozen,  # noqa: F401 - module fixture (hash-checked frozen cases)
    goal_of,
)
from test_live_wave_adapter import signed_producers

from adapter_support import weather_adapter as wa
from adapter_support.weather_adapter import AdapterRefusal, WeatherAdapter
from container_harness import decoy_harness
from tools.workflow import agents, e2e_injections, runner
from tools.workflow.catalog import CATALOG
from tools.workflow.e2e_injections import DECOY_ARGS, UnsupportedInjection
from tools.workflow.persistence import read_checkpoint

DECOY_PID = 4_194_297  # a pid the stub hands out; no process is started for it
PLOT_SECONDS = 5  # long enough for the 2 s deferred reading on a 1 s sampler


def decoy_producers(signed=True):
    """signed_producers() plus a test-local CAP-DECOY record (d4_gated, as the coordinator's
    record will be); ``signed=False`` leaves its signature fields null."""
    producers = signed_producers()
    record = {
        **SIGN,
        "method": "spawn_decoy",
        "producers": [
            {"file": "adapter_support/weather_adapter.py", "line": 1, "symbol": "spawn_decoy"}
        ],
    }
    if not signed:
        record.update(reviewer=None, reviewed_at=None, reviewed_commit=None)
    producers["capabilities"]["CAP-DECOY"] = record
    return producers


class StubSpawner:
    """The harness spawner contract, decoy_spawner(argv=, label=, lifetime_seconds=) ->
    {pid, pgid}, without spawning anything."""

    def __init__(self):
        self.calls = []

    def __call__(self, argv, label, lifetime_seconds):
        self.calls.append({"argv": argv, "label": label, "lifetime_seconds": lifetime_seconds})
        return {"pid": DECOY_PID, "pgid": DECOY_PID}


def never_called(**_):
    raise AssertionError("the traced run must never spawn the decoy")


def fake_process(**changes):
    """read_process stand-in: the entry script's decoy as /proc would show it."""
    seen = {
        "argv": list(DECOY_ARGS["argv"]),
        "pgid": DECOY_PID,
        "sid": DECOY_PID,
        "uid": os.getuid() if hasattr(os, "getuid") else 1000,
        **changes,
    }
    return lambda pid: seen if pid == DECOY_PID else None


@pytest.fixture(scope="module")
def make_lab(tmp_path_factory):
    """-> a factory of LiveLabs whose plot-onset stand-in runs PLOT_SECONDS before writing its
    map (a fresh lab per run: Lab.profile stages each case's source once per lab)."""
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(runner, "preflight", lambda *args: ({}, {}))
        mp.setattr(agents.shutil, "which", lambda _: "never-launched-claude")

        def make(name):
            lab = LiveLab(tmp_path_factory.mktemp(f"decoy-{name}"))
            script = lab.repo / "skills" / "plot-onset" / "scripts" / CATALOG["plot-onset"][0]
            script.write_text(
                "import pathlib, sys, time\n"
                f"if pathlib.Path(sys.argv[0]).stem == 'plot_onset': time.sleep({PLOT_SECONDS})\n"
                + TOOL_SCRIPT,
                encoding="utf-8",
            )
            return lab

        yield make


def record_kills(mp):
    """Record every os.kill / os.killpg (delegated to the real call), to show none of the
    run's signals reached the decoy."""
    seen = []
    for name in ("kill", "killpg"):
        real = getattr(os, name, None)
        if real is None:
            continue

        def wrapper(target, sig, _real=real, _name=name):
            seen.append((_name, target, sig))
            return _real(target, sig)

        mp.setattr(os, name, wrapper)
    return seen


@pytest.fixture
def kills(monkeypatch):
    return record_kills(monkeypatch)


def run_e12(lab, case, *, spawner=None, prestaged=None):
    """run_case on the lab route with live roles over the fake relay. ``prestaged``: the
    container route's prepared fixture (with prepared["before_run_decoy"]), JSON round trip."""
    adapter = WeatherAdapter(enable_run_case=True, producers=decoy_producers())
    goal = goal_of(case)
    profile = lab.profile(goal, case, stub=None)
    if spawner is not None:
        profile["decoy_spawner"] = spawner
    channel = None
    if prestaged is not None:
        profile["prepared_fixture"] = json.loads(json.dumps(prestaged(adapter, profile)))
        channel = profile["projection_inputs_channel"] = io.StringIO()
    relays = []

    def relay_factory(run_dir):
        relays.append(
            FakeRelay(case, goal, budget=case["budget"]["max_model_usd"], allow_gateway_mock=True)
        )
        return relays[-1]

    profile["roles_factory"] = lambda run_dir: adapter.invoke_live_role(
        case, goal, run_dir, relay_factory
    )
    evidence = adapter.run_case(case, profile)
    root = Path(profile["case_root"])
    if channel is None:
        report = json.loads((root / "harness/evidence-projection.json").read_text("utf-8"))
        inputs = json.loads((root / "harness/evidence-projection-inputs.json").read_text("utf-8"))
    else:
        inputs = json.loads(channel.getvalue())
        report = inputs["harness_files"]["evidence-projection.json"]
    return SimpleNamespace(
        adapter=adapter,
        case=case,
        evidence=evidence,
        report=report,
        inputs=inputs,
        schedule=report["harness"]["case_schedule"],
        run=root / "run",
        checkpoint=read_checkpoint(root / "run"),
        profile=profile,
    )


def prepare_with_decoy(case, **kwargs):
    """The untraced ``case_driver prepare`` step: stage the fixture, then the before_run
    fixture preparation that adopts the entry script's decoy."""

    def prepare(adapter, profile):
        prepared = adapter.prepare_fixture(
            case,
            profile["case_root"],
            source=profile["source"],
            generator_provenance=profile["generator_provenance"],
            presented_fetch_version=profile["presented_fetch_version"],
        )
        prepared["before_run_fixture"] = adapter.prepare_before_run_fixture(
            case, prepared, profile["profile_document"], profile["repo"], **kwargs
        )
        return prepared

    return prepare


def decoy_targets(kills):
    return [k for k in kills if abs(k[1]) == DECOY_PID]


def record_of(schedule, index):
    (record,) = [r for r in schedule["coverage"]["records"] if r["index"] == index]
    return record


# --------------------------------------------------------------------------- run_case


@pytest.fixture(scope="module")
def e12_runs(make_lab, frozen):  # noqa: F811
    out = {}
    for kind in ("INJ", "CTL"):
        with pytest.MonkeyPatch.context() as mp:
            seen = record_kills(mp)
            spawner = StubSpawner()
            case = copy.deepcopy(frozen[PREFIX + f"E12-P2-{kind}"])
            out[kind] = run_e12(make_lab(kind), case, spawner=spawner)
            out[kind].spawner, out[kind].kills = spawner, seen
    return out


def test_inj_stops_at_onset_checked_on_the_resource_guard_and_leaves_the_decoy(e12_runs):
    r = e12_runs["INJ"]
    trace = r.evidence["controller_trace"]
    assert trace["final_state"] == "ONSET_CHECKED"
    assert trace["terminal"]["outcome"] == "INCOMPLETE"
    assert trace["terminal"]["failed_gate"] == "resource_guard"
    assert "MAP_CREATED" not in trace["states"] and "RELEASED" not in trace["states"]
    # The deferred 1.8 reached the in-run sampler 2 s after plot-onset's durable start receipt.
    deferred = record_of(r.schedule, 2)
    assert deferred["delivery"] == "DELIVERED" and deferred["delivered_value"] == 1.8
    assert deferred["start_receipt"]["tool"] == "plot-onset"
    assert 2.0 <= deferred["delivered_offset_seconds"] < PLOT_SECONDS
    # The decoy: spawned once by the harness spawner, before the run; never signalled.
    assert r.spawner.calls == [
        {"argv": DECOY_ARGS["argv"], "label": "plot-onset-decoy", "lifetime_seconds": 1800}
    ]
    spawn = record_of(r.schedule, 0)
    assert spawn["status"] == "APPLIED" and spawn["decoy"]["pid"] == DECOY_PID
    assert spawn["decoy"]["spawned_by"] == "harness" and "prestaged" not in spawn
    assert decoy_targets(r.kills) == []
    assert DECOY_PID not in (trace.get("signalled_pids") or [])


def test_ctl_releases_with_every_reading_delivered(e12_runs):
    r = e12_runs["CTL"]
    assert r.evidence["controller_trace"]["final_state"] == "RELEASED"
    assert record_of(r.schedule, 2)["delivered_value"] == 2.4
    verify = record_of(r.schedule, 3)
    assert verify["delivery"] == "DELIVERED" and verify["tool"] == "verify-run"
    assert "selected launch" in verify["adapter_mapping"]  # D6, OI-12
    assert decoy_targets(r.kills) == [] and len(r.spawner.calls) == 1


def test_injected_ram_readings_are_the_delivered_deferred_readings(e12_runs):
    for kind, value in (("INJ", 1.8), ("CTL", 2.4)):
        r = e12_runs[kind]
        (reading,) = r.inputs["injected_ram_readings"]
        assert reading["schedule_index"] == 2 and reading["value"] == value
        assert reading["metric"] == "free_ram_gib" and reading["tool"] == "plot-onset"
        assert 2.0 <= reading["offset_seconds"] < PLOT_SECONDS
        assert reading["start_receipt"]["path"].endswith(".start.json")
        # Launch-check readings are excluded (D5).
        assert all(x["schedule_index"] == 2 for x in r.inputs["injected_ram_readings"])


def test_inject_metric_reports_the_delivered_resource_readings(e12_runs):
    r = e12_runs["CTL"]
    report = r.adapter.inject_metric(r.case, run_dir=r.run, schedule=r.schedule)
    assert report["scope"] == wa.METRIC_SCOPE and "status" not in report
    rows = {x["index"]: x for x in report["resource_readings"]}
    assert rows[1]["delivery"] == "DELIVERED" and rows[1]["reading_form"] == "launch_check"
    assert rows[1]["controller_launch_check"]["free_ram_gib"] == 4.0
    assert rows[1]["controller_launch_check"]["tool"] == "plot-onset"
    assert rows[2]["delivered_value"] == 2.4 and "controller_launch_check" not in rows[2]
    assert rows[3]["controller_launch_check"]["tool"] == "verify-run"
    assert rows[3]["controller_launch_check"]["free_ram_gib"] == 4.0
    assert report["injected_ram_readings"] == r.inputs["injected_ram_readings"]
    assert "budget_readings" not in report  # E12-P2 schedules no budget reading
    bare = r.adapter.inject_metric(r.case, run_dir=r.run)
    assert bare["injected_ram_readings"] is None and all(
        x["delivery"] is None for x in bare["resource_readings"]
    )


def test_spawn_decoy_reports_the_delivery_and_missing_observation_in_process(e12_runs):
    r = e12_runs["INJ"]
    report = r.adapter.spawn_decoy(r.case, run_dir=r.run, harness=r.inputs)
    (entry,) = report["entries"]
    assert entry["delivery"]["status"] == "APPLIED"
    assert entry["delivery"]["delivered_target"] == "harness process space"
    assert report["status"] == "MISSING" and "decoy tracer" in report["reason"]
    assert "decoy" not in r.evidence["harness_observations"]


def test_container_route_records_the_entry_script_decoy_and_never_spawns_in_the_run(
    make_lab,
    frozen,  # noqa: F811
    kills,
):
    case = copy.deepcopy(frozen[PREFIX + "E12-P2-INJ"])
    r = run_e12(
        make_lab("container"),
        case,
        spawner=never_called,  # profile spawner present but unused on this route
        prestaged=prepare_with_decoy(case, decoy_pid=DECOY_PID, read_process=fake_process()),
    )
    state = r.profile["prepared_fixture"]["before_run_decoy"]
    assert state["indices"] == [0] and state["raised"] is None
    spawn = record_of(r.schedule, 0)
    assert spawn["prestaged"] is True and spawn["decoy"]["pid"] == DECOY_PID
    trace = r.evidence["controller_trace"]
    assert (trace["final_state"], trace["terminal"]["outcome"]) == ("ONSET_CHECKED", "INCOMPLETE")
    assert trace["terminal"]["failed_gate"] == "resource_guard"
    assert r.inputs["injected_ram_readings"][0]["value"] == 1.8
    assert decoy_targets(kills) == []
    plan = r.adapter.spawn_decoy(case, run_dir=r.run, harness=r.inputs)
    assert plan["entries"][0]["delivery"]["prestaged"] is True


# --------------------------------------------------------------------------- refusals


def test_run_case_refuses_a_decoy_case_without_a_harness_decoy(make_lab, frozen):  # noqa: F811
    case = copy.deepcopy(frozen[PREFIX + "E12-P2-CTL"])
    adapter = WeatherAdapter(enable_run_case=True, producers=decoy_producers())
    profile = make_lab("refuse").profile(goal_of(case), case, stub=None)
    with pytest.raises(AdapterRefusal, match="harness's own spawner"):
        adapter.run_case(case, profile)
    assert not Path(profile["case_root"]).exists()  # refused before any side effect
    profile["prepared_fixture"] = {"schema": "anything"}  # pre-staged, no before_run_decoy
    with pytest.raises(AdapterRefusal, match="fixture preparation"):
        adapter.run_case(case, profile)
    assert not Path(profile["case_root"]).exists()


def test_prepare_without_a_decoy_pid_carries_the_refusal_to_run_case(
    make_lab,
    frozen,  # noqa: F811
    monkeypatch,
):
    monkeypatch.delenv(wa.DECOY_PID_ENV, raising=False)
    case = copy.deepcopy(frozen[PREFIX + "E12-P2-INJ"])
    adapter = WeatherAdapter(enable_run_case=True, producers=decoy_producers())
    profile = make_lab("nopid").profile(goal_of(case), case, stub=None)
    prepared = prepare_with_decoy(case)(adapter, profile)
    state = prepared["before_run_decoy"]
    assert state["raised"]["type"] == "UnsupportedInjection"
    assert wa.DECOY_PID_ENV in state["raised"]["message"]
    profile["prepared_fixture"] = json.loads(json.dumps(prepared))
    with pytest.raises(UnsupportedInjection, match=wa.DECOY_PID_ENV):
        adapter.run_case(case, profile)
    assert not (Path(prepared["run"]) / "checkpoint.json").exists()  # no controller ran


def test_prepare_reads_the_decoy_pid_from_the_entry_script_environment(
    make_lab,
    frozen,  # noqa: F811
    monkeypatch,
):
    assert wa.DECOY_PID_ENV == decoy_harness.ENV_VAR
    monkeypatch.setenv(wa.DECOY_PID_ENV, str(DECOY_PID))
    case = copy.deepcopy(frozen[PREFIX + "E12-P2-CTL"])
    adapter = WeatherAdapter(enable_run_case=True, producers=decoy_producers())
    profile = make_lab("env").profile(goal_of(case), case, stub=None)
    prepared = prepare_with_decoy(case, read_process=fake_process())(adapter, profile)
    assert prepared["before_run_fixture"] is None  # no source operation in E12-P2
    (record,) = prepared["before_run_decoy"]["records"]
    assert record["decoy"] == {
        **DECOY_ARGS,
        "pid": DECOY_PID,
        "pgid": DECOY_PID,
        "spawned_by": "harness",
    }
    assert record["prestaged"] is True


def test_adopting_the_entry_script_decoy_checks_the_process():
    adopt = wa.adopt_harness_decoy
    args = {"argv": list(DECOY_ARGS["argv"]), "label": "plot-onset-decoy", "lifetime_seconds": 1800}
    assert adopt(str(DECOY_PID), fake_process())(**args) == {"pid": DECOY_PID, "pgid": DECOY_PID}
    bad = {
        "absent": (None, fake_process()),
        "not a pid": ("12x", fake_process()),
        "gone": (str(DECOY_PID + 1), fake_process()),
        "argv": (str(DECOY_PID), fake_process(argv=["python", "-c", "pass"])),
        "session": (str(DECOY_PID), fake_process(sid=1)),
        "group": (str(DECOY_PID), fake_process(pgid=1)),
    }
    if hasattr(os, "getuid"):
        bad["uid"] = (str(DECOY_PID), fake_process(uid=os.getuid() + 1))
    for name, (pid, reader) in bad.items():
        with pytest.raises(UnsupportedInjection):
            adopt(pid, reader)(**args)
        assert name


def test_verify_and_merge_prestage_states():
    plan = {"entries": [{"index": 0}]}
    record = {
        "index": 0,
        "status": "APPLIED",
        "decoy": {**DECOY_ARGS, "pid": DECOY_PID, "pgid": DECOY_PID, "spawned_by": "harness"},
    }
    state = {
        "schema": e2e_injections.PRESTAGE_SCHEMA,
        "schedule_digest": "d",
        "indices": [0],
        "records": [record],
        "dispositions": [],
        "stopped": False,
        "harness_error": None,
        "raised": None,
    }
    assert wa.verify_decoy_prestage(state, plan) == state
    for change in (
        {"indices": [1]},
        {"schema": "other"},
        {"records": [{**record, "decoy": {**record["decoy"], "argv": ["sleep", "9"]}}]},
        {"records": [{**record, "status": "UNSUPPORTED"}]},
        {"records": [{**record, "decoy": {**record["decoy"], "pid": 0}}]},
        {"records": []},
    ):
        with pytest.raises(AdapterRefusal):
            wa.verify_decoy_prestage({**state, **change}, plan)
    raised = {**state, "records": [], "raised": {"type": "UnsupportedInjection", "message": "m"}}
    assert wa.verify_decoy_prestage(raised, plan) == raised  # adopt_before_run raises it
    source = {**state, "indices": [3], "records": [{"index": 3, "status": "APPLIED"}]}
    merged = wa.merge_prestage_states([source, state])
    assert merged["indices"] == [0, 3] and [r["index"] for r in merged["records"]] == [0, 3]
    assert wa.merge_prestage_states([None, state]) is state
    assert wa.merge_prestage_states([]) is None
    with pytest.raises(AdapterRefusal, match="different schedules"):
        wa.merge_prestage_states([source, {**state, "schedule_digest": "e"}])
    with pytest.raises(AdapterRefusal, match="twice"):
        wa.merge_prestage_states([state, state])


# --------------------------------------------------------------------------- plans


def test_spawn_decoy_plan_and_refusals(frozen):  # noqa: F811
    adapter = WeatherAdapter()
    for kind in ("INJ", "CTL"):
        plan = adapter.spawn_decoy(frozen[PREFIX + f"E12-P2-{kind}"])
        (entry,) = plan["entries"]
        assert plan["capability"] == "CAP-DECOY" and entry["index"] == 0
        assert entry["argv"] == DECOY_ARGS["argv"] and entry["lifetime_seconds"] == 1800
        assert entry["spawned_by"] == "harness" and set(entry["routes"]) == {
            "container",
            "in_process",
        }
    assert adapter.spawn_decoy(frozen[PREFIX + "E12-P1-CTL"])["entries"] == []
    base = frozen[PREFIX + "E12-P2-INJ"]

    def variant(change):
        case = copy.deepcopy(base)
        change(case, case["injection_schedule"][0])
        return case

    bad = {
        "hook": lambda c, e: e.update(hook="before_tool"),
        "argv": lambda c, e: e["payload"]["args"].update(argv=["sleep", "1800"]),
        "lifetime": lambda c, e: e["payload"]["args"].update(lifetime_seconds=60),
        "selector": lambda c, e: e["selector"].update(role_or_tool_or_stage="controller"),
        "occurrence": lambda c, e: e["selector"].update(occurrence=2),
        "args": lambda c, e: e["payload"].update(args=None),
    }
    for change in bad.values():
        with pytest.raises(AdapterRefusal, match="literal argv"):
            adapter.spawn_decoy(variant(change))
    twice = variant(lambda c, e: c["injection_schedule"].append(copy.deepcopy(e)))
    with pytest.raises(AdapterRefusal, match="one FX-DECOY-SPAWN"):
        adapter.spawn_decoy(twice)


def test_inject_metric_plans_the_three_frozen_forms_and_refuses_the_rest(frozen):  # noqa: F811
    adapter = WeatherAdapter()
    plan = adapter.inject_metric(frozen[PREFIX + "E12-P2-CTL"])
    assert plan["scope"] == wa.METRIC_SCOPE
    forms = {e["index"]: e for e in plan["entries"]}
    assert {i: e["reading_form"] for i, e in forms.items()} == {
        1: "launch_check",
        2: "deferred",
        3: "launch_check_every_later",
    }
    assert all(e["half"] == "resource" and e["metric"] == "free_ram_gib" for e in forms.values())
    assert [forms[i]["value"] for i in (1, 2, 3)] == [4.0, 2.4, 4.0]
    assert forms[2]["offset_seconds"] == 2.0 and "start.json" in forms[2]["anchor"]
    assert "D6" in forms[3]["adapter_mapping"] and "adapter_mapping" not in forms[1]
    inj = adapter.inject_metric(frozen[PREFIX + "E12-P2-INJ"])
    assert [(e["index"], e["value"]) for e in inj["entries"]] == [(1, 4.0), (2, 1.8)]
    # The budget half is unchanged.
    (budget,) = adapter.inject_metric(frozen[PREFIX + "E11-P2-INJ"])["entries"]
    assert budget["half"] == "budget"
    base = frozen[PREFIX + "E12-P2-INJ"]

    def variant(change):
        case = copy.deepcopy(base)
        change(case["injection_schedule"][2])
        return case

    bad = {
        "hook": lambda e: e.update(hook="after_tool"),
        "timing": lambda e: e["payload"].update(timing="deferred: 3 seconds after start"),
        "immediate": lambda e: e["payload"].update(timing="immediate"),
        "also": lambda e: e["payload"].update(also={"consumed_model_usd": 1.0}),
        "metric": lambda e: e["payload"].update(metric="free_cpu_cores"),
        "value": lambda e: e["payload"].update(value=-1.0),
        "nan": lambda e: e["payload"].update(value=float("nan")),
        "extra": lambda e: e["payload"].update(simulated=True),
    }
    for change in bad.values():
        with pytest.raises(AdapterRefusal, match="neither"):
            adapter.inject_metric(variant(change))


def test_injected_ram_readings_keeps_only_delivered_deferred_readings():
    records = [
        {"index": 1, "reading_form": "launch_check", "delivery": "DELIVERED", "value": 4.0},
        {"index": 2, "reading_form": "deferred", "delivery": "UNDELIVERED", "value": 1.8},
        {
            "index": 5,
            "reading_form": "deferred",
            "delivery": "DELIVERED",
            "metric": "free_ram_gib",
            "value": 1.8,
            "delivered_value": 1.8,
            "delivered_offset_seconds": 2.01,
            "delivered_monotonic_ns": 7,
            "tool": "plot-onset",
            "start_receipt": {"path": "process-004.start.json"},
        },
    ]
    assert wa.injected_ram_readings(records) == [
        {
            "schedule_index": 5,
            "metric": "free_ram_gib",
            "value": 1.8,
            "offset_seconds": 2.01,
            "delivered_monotonic_ns": 7,
            "tool": "plot-onset",
            "start_receipt": {"path": "process-004.start.json"},
        }
    ]
    assert wa.injected_ram_readings(None) == []


def test_cap_decoy_is_declared_only_by_its_record():
    unsigned = WeatherAdapter(producers=decoy_producers(signed=False))
    assert "CAP-DECOY" not in unsigned.implemented_capabilities()
    assert "CAP-DECOY" in WeatherAdapter(producers=decoy_producers()).implemented_capabilities()
