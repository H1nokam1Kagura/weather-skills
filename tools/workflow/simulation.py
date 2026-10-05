"""Reproducible synthetic weather journeys; labelled separately from live model evidence."""

import json
from pathlib import Path

from .admission import register_synthetic
from .agents import Roles
from .catalog import tree_hash
from .contracts import Refused, digest, expected_plan
from .evaluation import GOAL, FixtureRoles, fixture_request
from .persistence import atomic_json, run_lock
from .processes import Processes
from .runner import Controller


def weather_fixture(root):
    import numpy as np
    import xarray as xr

    root = Path(root)
    root.mkdir(parents=True, exist_ok=False)
    lat = np.arange(4.975, -5, -0.05)
    lon = np.arange(36.525, 42, 0.05)
    times = np.arange(np.datetime64("2025-09-01"), np.datetime64("2026-01-01"))
    grid = np.arange(len(lat) * len(lon)).reshape(len(lat), len(lon))
    starts = 15 + grid % 16
    day = np.arange(len(times))[:, None, None]
    rain = np.where(
        (day >= starts) & (day < starts + 10),
        3.0,
        np.where((day >= starts + 10) & (day < starts + 30), 1.5, 0.0),
    ).astype("float32")
    missing = grid % 10 == 0
    rain[:, missing] = np.nan
    ds = xr.Dataset(
        {"precip": (("time", "latitude", "longitude"), rain)},
        coords={"time": times, "latitude": lat, "longitude": lon},
        attrs={
            "synthetic": "true",
            "generator": "workflow.synthetic-rainfall/1",
            "seed": 0,
            "source_lineage": "synthetic:workflow.synthetic-rainfall/1",
        },
    )
    ds.precip.attrs.update(units="mm day-1", standard_name="precipitation_flux")
    ds.latitude.attrs.update(units="degrees_north", standard_name="latitude")
    ds.longitude.attrs.update(units="degrees_east", standard_name="longitude")
    ds.time.attrs["standard_name"] = "time"
    source = root / "raw.zarr"
    ds.to_zarr(source, mode="w", consolidated=True, zarr_format=2)
    # Independent analytic oracle: 8 wet days total 24 mm (<25), 9 total 27 mm.
    # Earliest eligible start is one day before rain begins; confirmation exceeds 20 mm.
    expected = times[0] + (starts - 1).astype("timedelta64[D]")
    expected[missing] = np.datetime64("NaT")
    np.save(root / "expected-onset.npy", expected)
    atomic_json(
        root / "fixture.json",
        {
            "schema": "synthetic-rainfall/1",
            "synthetic": True,
            "seed": 0,
            "source_lineage": ["synthetic:workflow.synthetic-rainfall/1"],
            "is_observational_data": False,
            "source_hash": tree_hash(source),
            "dimensions": dict(ds.sizes),
            "oracle": "onset = first 3 mm day minus one day; NaN cells remain undated",
            "rain_pattern": "10 days at 3 mm/day then 20 at 1.5 mm/day; otherwise zero",
        },
    )
    return source


class SyntheticRoles(FixtureRoles):
    def __init__(self, goal):
        super().__init__({"id": "SIMULATION", "stimulus": {"payload": []}})
        self.goal = goal

    def ask(self, role, payload):
        if role == "repairer":
            import uuid

            self.receipts.append({"role": role, "session_id": str(uuid.uuid4()), "mode": "fixture"})
            return {
                "plan": expected_plan(self.goal),
                "response": "The input end and eligible onset end have distinct meanings; the latter is t+29 bounded.",
            }
        result = super().ask(role, payload)
        return (
            self.goal
            if role == "compiler"
            else expected_plan(self.goal)
            if role == "planner"
            else result
        )


def expected_injected_stop(scenario, state, failure, injected):
    expected = {
        "tamper": ("ONSET_CHECKED", "artifact changed since its producing step: onset"),
        "denied": ("AUTHORIZED", "injected write denial; no fallback authorized"),
    }
    return bool(injected and expected.get(scenario) == (state, failure))


def simulate(repo, root, scenario="clean", task="onset_map", model_budget=0, registration=None):
    if scenario not in {"clean", "interrupt", "tamper", "denied", "review-repair"}:
        raise Refused("unknown simulation scenario")
    if not 0 <= model_budget <= 5:
        raise Refused("invalid simulation model budget")
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=False)
    external_fixture = registration is not None
    if registration is None:
        source = weather_fixture(root / "inputs")
        registration = register_synthetic(source, root / "inputs/fixture.json", Path(__file__))
    else:
        source = Path(registration.record["source"])
        registration.verify(source)
    request = fixture_request()
    goal = {**GOAL, "task": task, "title": "SYNTHETIC TEST: rainfall onset"}
    request["constraints"]["task"]["value"] = task
    request["constraints"]["title"]["value"] = goal["title"]
    request["messages"][0]["text"] = json.dumps(goal)
    for c in request["constraints"].values():
        c["quote"] = json.dumps(goal)
    request["authorization"]["goal_hash"] = digest(goal)
    request["authorization"]["actions"] = [
        "cached-onset-data" if task == "onset_data" else "cached-onset"
    ]
    run = root / "run"
    for name in ("processes", "role-context", "scratch"):
        (run / name).mkdir(parents=True)
    processes = Processes(run / "processes")
    roles = (
        Roles(processes, run / "role-context", budget=model_budget)
        if model_budget
        else SyntheticRoles(goal)
    )
    injected = False

    def hook(point, event, controller):
        nonlocal injected
        if injected:
            return
        if scenario == "denied" and point == "before_tool" and event["skill"] == "onset-date":
            injected = True
            raise PermissionError("injected write denial; no fallback authorized")
        if scenario == "review-repair" and point == "after_role" and event["role"] == "reviewer":
            event["answer"].update(
                verdict="REJECT", findings=["Clarify input end versus derived onset end"]
            )
            injected = True
        if point == "after_transition" and event["state"] == "ONSET_CHECKED":
            if scenario == "interrupt":
                injected = True
                raise RuntimeError("injected interruption at a durable checkpoint")
            if scenario == "tamper":
                import numpy as np
                import zarr

                # Change a payload chunk, not provenance or controller receipts.
                group = zarr.open_group(str(controller.paths["onset"]), mode="a")
                array = group["onset_precip_date"]
                values = array[:]
                array[:] = values + np.array(7, dtype=values.dtype)
                injected = True

    with run_lock(run):
        c = Controller(
            repo,
            request,
            source,
            run,
            processes,
            roles,
            hooks=hook if scenario != "clean" else None,
            admission=registration,
        )
        failure = None
        try:
            c.run()
        except Exception as exc:
            failure = str(exc)
            if scenario == "interrupt" and injected:
                c = Controller.resume(run, processes, roles, hooks=hook, admission=registration)
                c.run()
                failure = None
        source_ok = tree_hash(source) == c.source_hash
        expected_stop = scenario in {"tamper", "denied"}
        oracle_ok = None
        if c.state == "RELEASED" and not external_fixture:
            import numpy as np
            import xarray as xr

            expected = np.load(root / "inputs/expected-onset.npy")
            with xr.open_zarr(c.paths["onset"], consolidated=None) as ds:
                actual = ds.onset_precip_date.values.astype("datetime64[D]")
                oracle_ok = bool(
                    np.all((actual == expected) | (np.isnat(actual) & np.isnat(expected)))
                )
        passed = source_ok and (
            expected_injected_stop(scenario, c.state, failure, injected)
            if expected_stop
            else c.state == "RELEASED" and (external_fixture or oracle_ok)
        )
        summary = {
            "schema": "workflow.simulation/1",
            "scenario": scenario,
            "task": task,
            "status": "PASS" if passed else "FAIL",
            "state": c.state,
            "error": failure,
            "source_unchanged": source_ok,
            "analytic_onset_oracle_pass": oracle_ok,
            "injection_applied": injected,
            "repairs_used": c.repairs_used,
            "resumes": c.resumes,
            "model_cost_usd": roles.spent,
            "interpretation_evidence": "live" if model_budget else "fixture",
            "scientific_tools": [
                json.loads(p.read_text(encoding="utf-8"))["argv"][1]
                for p in sorted((run / "processes").glob("process-*.json"))
            ],
            "strict_replay_completed": bool(c.context.get("gates")) and c.state == "RELEASED",
            "is_observational_data": False,
        }
        atomic_json(root / "simulation.json", summary)
        return summary
