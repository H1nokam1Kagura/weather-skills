"""Live-role sub-waves 3 and 4b: the WeatherAdapter live-role methods and the case driver.

Zero-cost fake relay (the sub-wave 2 pattern, tests/test_live_roles_adapter.py): every live call
runs a REAL relay_roles.RelayRoles client over a fake host transport that answers from the
substituted baselines. No model, relay, network or Kong call is made.

Frozen cases run verbatim through WeatherAdapter.run_case (E12-P1-CTL all live and E01-P2-CTL
mixed, both through case_driver.case_profile, the container route; E08-P2-INJ, the before_role
verifier patch). Two LOCAL cases (never frozen ones, labelled LOCAL-*) add one frozen schedule
entry each to E12-P1-CTL: E11-P2-INJ's budget-meter inject_metric and E14-P3-INJ's
FX-TRANSPORT-RETRY planner fault, which their own frozen cases cannot reach yet (both also
need the wave-7 interrupt/resume family, live sub-wave 5).

The seven live-wave capabilities are D4-gated: declared here only by test-local signed records
over resigned() (the committed producer file with the wave-7 rereview flags cleared
test-locally). The committed file declares none of them, and admission refuses every live
case while they are unsigned (asserted below).
"""

import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_evidence_projection_v27 import (
    GENERATOR,
    LIVE_SIGNED_ADMITTED,
    MAP_GOAL,
    SIGNED_ADMITTED,
    TEST_FREE_RAM_GIB,
    ledger,
    profile_document,
    resigned,
    simkit_run,
    write_source,
)
from test_live_roles_adapter import (
    MODEL,
    PREFIX,
    SIGN,
    FakeHostTransport,
    FakeRelay,
    frozen,  # noqa: F401 - module fixture (hash-checked frozen cases)
    goal_of,
    lab,  # noqa: F401 - module fixture (LiveLab with the real skill documentation)
)

from adapter_support import live_roles
from adapter_support import role_substitution as rs
from adapter_support.weather_adapter import AdapterRefusal, WeatherAdapter
from container_harness import case_driver as cd
from container_harness import relay_usage
from tools.workflow import e2e_injections, relay_roles
from tools.workflow.agents import role_prompt
from tools.workflow.contracts import Refused, digest
from tools.workflow.persistence import read_checkpoint
from tools.workflow.processes import Processes

LIVE_WAVE = sorted(WeatherAdapter.LIVE_WAVE_D4_GATED)
METHODS = {
    "CAP-LIVE-ROLE": "invoke_live_role",
    "CAP-BINDING-PASSTHROUGH": "confirm_binding_passthrough",
    "CAP-ROLE-INPUT-PATCH": "patch_role_input",
    "CAP-METRIC": "inject_metric",
    "CAP-TRANSPORT-RETRY": "record_transport_retry",
    "CAP-TRANSPORT-RETRY-SETUP": "setup_planner_transport_retry",
    "CAP-OPERATOR-NOTIFY-HOOK": "dispatch_after_operator_notification",
}


def signed_producers(unsigned=()):
    """resigned() plus a test-local signed record for every live-wave capability (those named
    in ``unsigned`` get a record with the signature fields null instead)."""
    producers = resigned()
    for cap in LIVE_WAVE:
        record = {
            **SIGN,
            "method": METHODS[cap],
            "producers": [
                {"file": "adapter_support/weather_adapter.py", "line": 1, "symbol": METHODS[cap]}
            ],
        }
        if cap in unsigned:
            record.update(reviewer=None, reviewed_at=None, reviewed_commit=None)
        producers["capabilities"][cap] = record
    return producers


def local_case(frozen, short, *, suffix, entries=(), requirements=()):  # noqa: F811
    """A LOCAL copy of a frozen case: its profile, budget and agent_roles, plus ``entries``
    appended to its schedule and ``requirements`` to its adapter_requirements. Never a frozen
    case: the id carries LOCAL and the suffix; pair_id keeps the frozen pair's baselines."""
    case = copy.deepcopy(frozen[PREFIX + short])
    case["id"] = f"LOCAL-{short}-{suffix}"
    case["pair_id"] = short.rsplit("-", 1)[0]
    case["injection_schedule"] = case["injection_schedule"] + [copy.deepcopy(e) for e in entries]
    case["adapter_requirements"] = sorted(set(case["adapter_requirements"]) | set(requirements))
    return case


class PersistentFakeRelay(FakeRelay):
    """FakeRelay whose transport object persists across asks, as one RelayRoles keeps one
    RelayTransport (``processes``) for the run: FX-TRANSPORT-RETRY wraps its run() exactly as it
    wraps the real one. ``serial`` moves only when the host is reached."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.processes = FakeHostTransport(self, None)

    def ask(self, role, payload):
        encoded = relay_roles.dedup_encode(payload)
        request = (
            {"role": role, "payload": payload} if encoded is None else {"role": role, **encoded}
        )
        body = json.dumps(request, allow_nan=False).encode()
        self.bodies.append({"role": role, "body_bytes": len(body)})
        self.calls.append(role)
        self.processes.expected_body = body
        client = relay_roles.RelayRoles(
            "http://127.0.0.1:9", "t" * 43, "fake-run", budget=self.budget, model=self.model
        )
        client.processes = self.processes
        client.spent = self.spent
        client.receipts = self.receipts
        client.sessions = self.sessions
        client.unknown_outcomes = self.unknown_outcomes
        client.transport_retry_recorder = getattr(self, "transport_retry_recorder", None)
        context = getattr(self, "dispatch_context", None)
        if context is not None:
            client.dispatch_context = context
            client.dispatch_observer = self.dispatch_observer
        try:
            return client.ask(role, payload)
        finally:
            self.spent = client.spent
            if context is not None:
                self.dispatch_observation = client.dispatch_observation


def run_lab(lab, case, relay_cls=FakeRelay, producers=None):  # noqa: F811
    """run_case on the lab route with the adapter's own invoke_live_role over a fake relay."""
    adapter = WeatherAdapter(enable_run_case=True, producers=producers or signed_producers())
    goal = goal_of(case)
    profile = lab.profile(goal, case, stub=None)
    relays, built = [], []

    def relay_factory(run_dir):
        relays.append(
            relay_cls(case, goal, budget=case["budget"]["max_model_usd"], allow_gateway_mock=True)
        )
        return relays[-1]

    def factory(run_dir):
        built.append(adapter.invoke_live_role(case, goal, run_dir, relay_factory))
        return built[-1]

    profile["roles_factory"] = factory
    evidence = adapter.run_case(case, profile)
    root = Path(profile["case_root"])
    report = json.loads((root / "harness/evidence-projection.json").read_text(encoding="utf-8"))
    return SimpleNamespace(
        adapter=adapter,
        case=case,
        evidence=evidence,
        report=report,
        schedule=report["harness"].get("case_schedule"),
        run=root / "run",
        checkpoint=read_checkpoint(root / "run"),
        roles=built[0],
        relay=relays[0],
    )


def driver_profile(case, adapter, work, live=True, relays=None):
    """case_driver.case_profile exactly as the traced ``run`` builds it, on a pre-staged
    fixture; only the source grid and the memory reader are stand-ins."""
    goal = goal_of(case)
    document = {**profile_document(goal), "reference_fixture": {"source_ledger": ledger(goal)}}
    work.mkdir(parents=True)
    prepared = adapter.prepare_fixture(
        case,
        work / "case",
        source=write_source(work.parent / f"gen-{work.name}", goal),
        generator_provenance=GENERATOR,
        presented_fetch_version="0.0.2",
    )
    return cd.case_profile(
        case,
        adapter,
        document,
        work,
        spec={"supplement_id": "test-only"},
        pins={"fixture_generator": {"sha256": GENERATOR["generator_sha256"]}},
        version="0.0.2",
        live=live,
        prepared=json.loads(json.dumps(prepared)),
        inputs_channel=None,
        processes_factory=lambda run_dir: Processes(
            Path(run_dir) / "processes", memory_reader=lambda: TEST_FREE_RAM_GIB
        ),
    )


def run_driver(lab, frozen, short, tmp_path_factory, monkeypatch):  # noqa: F811
    """A frozen live case through case_driver.case_profile (the container route), with the
    RelayRoles client replaced by the zero-cost fake (cd._relay_roles is the one seam)."""
    case = copy.deepcopy(frozen[PREFIX + short])
    adapter = WeatherAdapter(enable_run_case=True, producers=signed_producers())
    work = tmp_path_factory.mktemp(f"driver-{short}") / "work"
    relays, budgets = [], []

    def fake_relay_roles(**kwargs):
        budgets.append(kwargs.get("budget"))
        relays.append(
            FakeRelay(case, goal_of(case), budget=kwargs["budget"], allow_gateway_mock=True)
        )
        return relays[-1]

    monkeypatch.setattr(cd, "_relay_roles", fake_relay_roles)
    profile = driver_profile(case, adapter, work)
    profile["repo"] = lab.repo
    built = []
    roles_factory = profile["roles_factory"]
    profile["roles_factory"] = lambda run_dir: built.append(roles_factory(run_dir)) or built[-1]
    evidence = adapter.run_case(case, profile)
    run = work / "case" / "run"
    return SimpleNamespace(
        case=case,
        evidence=evidence,
        run=run,
        checkpoint=read_checkpoint(run),
        roles=built[0],
        relay=relays[0],
        budgets=budgets,
        adapter=adapter,
    )


@pytest.fixture(scope="module")
def driven(lab, frozen, tmp_path_factory):  # noqa: F811
    with pytest.MonkeyPatch.context() as mp:
        return {
            short: run_driver(lab, frozen, short, tmp_path_factory, mp)
            for short in ("E12-P1-CTL", "E01-P2-CTL")
        }


METRIC_ENTRY_FROM = ("E11-P2-INJ", 0)  # after_transition ONSET_CHECKED budget meters
RETRY_ENTRY_FROM = ("E14-P3-INJ", 1)  # planner-retry: FX-TRANSPORT-RETRY before_role planner


@pytest.fixture(scope="module")
def local_runs(lab, frozen):  # noqa: F811
    metric_entry = frozen[PREFIX + METRIC_ENTRY_FROM[0]]["injection_schedule"][METRIC_ENTRY_FROM[1]]
    retry_entry = {
        **frozen[PREFIX + RETRY_ENTRY_FROM[0]]["injection_schedule"][RETRY_ENTRY_FROM[1]],
        "requires": [],  # its frozen dependency (interrupt-1) is the wave-7 family's
    }
    assert retry_entry["payload"]["fixture_operation"] == "FX-TRANSPORT-RETRY"
    metric = local_case(
        frozen, "E12-P1-CTL", suffix="METRIC", entries=[metric_entry], requirements=["CAP-METRIC"]
    )
    retry = local_case(
        frozen,
        "E12-P1-CTL",
        suffix="RETRY",
        entries=[retry_entry],
        requirements=["CAP-TRANSPORT-RETRY", "CAP-TRANSPORT-RETRY-SETUP"],
    )
    return {
        "metric": run_lab(lab, metric),
        "retry": run_lab(lab, retry, relay_cls=PersistentFakeRelay),
    }


@pytest.fixture(scope="module")
def e08(lab, frozen):  # noqa: F811
    return {k: run_lab(lab, copy.deepcopy(frozen[PREFIX + f"E08-P2-{k}"])) for k in ("INJ", "CTL")}


# --------------------------------------------------------------------------- D4 gate, admission


def test_live_wave_capabilities_are_methods_but_stay_undeclared_until_signed():
    from simkit.adapter import CAPABILITY_METHODS, Adapter

    assert set(METHODS) == set(LIVE_WAVE)
    for cap, method in METHODS.items():
        assert callable(getattr(WeatherAdapter, method)), cap
        if cap in CAPABILITY_METHODS:  # the frozen table names it: an override declares it
            assert CAPABILITY_METHODS[cap] == method
            assert getattr(WeatherAdapter, method) is not getattr(Adapter, method)
            assert cap not in WeatherAdapter.extension_methods
        else:
            assert WeatherAdapter.extension_methods[cap] == method
    # Six live records are signed (D4-CONFIRM-LW-A); CAP-OPERATOR-NOTIFY-HOOK stays unsigned until
    # sub-wave 5 opens the notification boundary in run_case.
    held = {"CAP-OPERATOR-NOTIFY-HOOK"}
    committed = WeatherAdapter().implemented_capabilities()
    assert set(LIVE_WAVE) & committed == set(LIVE_WAVE) - held
    assert set(LIVE_WAVE) & WeatherAdapter(producers=resigned()).implemented_capabilities() == (
        set(LIVE_WAVE) - held
    )
    assert set(LIVE_WAVE) <= WeatherAdapter(producers=signed_producers()).implemented_capabilities()
    unsigned = WeatherAdapter(producers=signed_producers(unsigned=LIVE_WAVE))
    assert not set(LIVE_WAVE) & unsigned.implemented_capabilities()
    flagged = signed_producers()
    flagged["capabilities"]["CAP-LIVE-ROLE"]["rereview_required"] = "producer changed"
    assert "CAP-LIVE-ROLE" not in WeatherAdapter(producers=flagged).implemented_capabilities()


def test_admission_refuses_every_live_case_while_the_live_records_are_unsigned(frozen):  # noqa: F811
    cases = list(frozen.values())
    live = {c["id"] for c in cases if "CAP-LIVE-ROLE" in c["adapter_requirements"]}
    assert len(live) == 34
    # Admission also depends on the shared gate records in SIGNED_ADMITTED. E14-P3
    # needs the held CAP-OPERATOR-NOTIFY-HOOK and E12-P2 needs CAP-DECOY.
    records = simkit_run(cases, WeatherAdapter(enable_run_case=True), profile={})
    by_id = {r["case_id"]: r for r in records}
    assert {c for c in live if by_id[c]["status"] != "NOT_IMPLEMENTED"} == live & SIGNED_ADMITTED
    for short in ("E14-P3-CTL", "E14-P3-INJ"):
        missing = by_id[PREFIX + short]["missing_capabilities"]
        assert by_id[PREFIX + short]["status"] == "NOT_IMPLEMENTED"
        assert "CAP-OPERATOR-NOTIFY-HOOK" in missing, short
    # Without the live signatures (stripped test-locally) every live case is refused.
    for producers in (
        signed_producers(unsigned=LIVE_WAVE),  # live records present, unsigned
        signed_producers(unsigned=["CAP-LIVE-ROLE"]),  # only CAP-LIVE-ROLE unsigned
    ):
        adapter = WeatherAdapter(enable_run_case=True, producers=producers)
        records = simkit_run(cases, adapter, profile={})
        by_id = {r["case_id"]: r for r in records}
        for case_id in live:
            assert by_id[case_id]["status"] == "NOT_IMPLEMENTED", case_id
            assert "CAP-LIVE-ROLE" in by_id[case_id]["missing_capabilities"], case_id
            assert by_id[case_id]["evidence"] is None


def admission_with(monkeypatch, producers, selector="live"):
    from adapter_support import weather_adapter

    monkeypatch.setattr(weather_adapter, "load_producers", lambda path=None: producers)
    return cd.admission(selector)


def test_case_driver_admission_runs_mixed_cases_and_matrix_rows_carry_relay_caps(monkeypatch):
    committed = cd.admission("live")
    committed_live = {
        r["case_id"] for r in committed["cases"] if r["role_mode"] == "live" and r["admitted"]
    }
    assert committed_live == LIVE_SIGNED_ADMITTED & SIGNED_ADMITTED
    assert set(committed["selected"]) == LIVE_SIGNED_ADMITTED & SIGNED_ADMITTED
    signed = admission_with(monkeypatch, signed_producers())
    rows = {r["case_id"][len(PREFIX) :]: r for r in signed["cases"]}
    # Group A (all live) and group B/C (mixed, through the combining hook) are runnable.
    for short in ("E12-P1-CTL", "E02-P2-CTL", "E01-P2-CTL", "E13-P2-INJ", "E06-P1-INJ"):
        assert rows[short]["disposition"] == "RUNNABLE", (short, rows[short])
        assert rows[short]["selected"] is True
    # Wave 7 is signed (D4 w7), so with every live record signed test-locally admission by
    # requirements reaches 32 of the 34 live cases; E12-P2 x2 stay out (CAP-DECOY is not built).
    live = {s for s, r in rows.items() if r["role_mode"] == "live"}
    runnable = {s for s in live if rows[s]["disposition"] == "RUNNABLE"}
    assert live - runnable == {"E12-P2-CTL", "E12-P2-INJ"}
    # E12-P1-INJ needs only CAP-DENY-WRITE beyond the live records: legitimately runnable.
    assert rows["E12-P1-INJ"]["disposition"] == "RUNNABLE"
    # CAP-OPERATOR-NOTIFY-HOOK is signed only after sub-wave 5 opens the notification boundary
    # in run_case; until then E14-P3 stays refused.
    pre_sw5 = admission_with(monkeypatch, signed_producers(unsigned=["CAP-OPERATOR-NOTIFY-HOOK"]))
    pre = {r["case_id"][len(PREFIX) :]: r for r in pre_sw5["cases"]}
    for short in ("E14-P3-CTL", "E14-P3-INJ"):
        assert pre[short]["disposition"] == "NOT_IMPLEMENTED", short
    # Admitted by requirements although their LIVE-WAVE-BRIEF sub-wave 5 mechanics (live roles
    # across incarnations; the E11-P2-INJ override channel) are not built [UNVERIFIED whether
    # they refuse at run time]: pinned here so any change is deliberate. They are dispatched
    # only by explicit case list, last in the spend sequence, after sub-wave 5.
    for short in ("E10-P2-CTL", "E10-P2-INJ", "E11-P2-INJ"):
        assert pre[short]["disposition"] == "RUNNABLE", short
    matrix = {m["case_id"][len(PREFIX) :]: m for m in signed["matrix"]}
    assert set(matrix) == {s for s, r in rows.items() if r["selected"]}
    row = matrix["E12-P1-CTL"]
    assert row["role_mode"] == "live" and row["expected_calls"] == 7
    assert row["max_requests"] == 14 and row["max_model_usd"] == 5
    assert row["live_roles"] == ["compiler", "evidence_checker", "planner", "reviewer", "verifier"]
    caps = relay_usage.case_caps(row)
    assert caps["source"] == {
        "max_requests": "matrix",
        "max_total_tokens": "matrix",
        "budget_usd": "matrix",
        "roles": "matrix",
    }
    assert caps["max_total_tokens"] == row["max_total_tokens"]
    mixed = matrix["E01-P2-CTL"]  # substituted reviewer and evidence checker: not relay roles
    assert mixed["live_roles"] == ["compiler", "planner", "verifier"]
    assert mixed["expected_calls"] == 5 and mixed["max_requests"] == 10
    # Every non-live (offline) matrix row stays as it was: id and role mode only.
    offline = admission_with(monkeypatch, signed_producers(), selector="substituted")
    assert all(set(m) == {"case_id", "role_mode"} for m in offline["matrix"])


def test_relay_caps_bound_the_observed_live_calls(driven):
    for short, result in driven.items():
        plan = cd.live_relay_plan(result.case)
        observed = result.relay.calls
        assert observed and len(observed) <= plan["expected_calls"], short
        assert set(observed) <= set(plan["live_roles"]), short
        for body in result.relay.bodies:
            role, sent = body["role"], body["body_bytes"]
            bound = plan["relay_caps_basis"]["token_bound_by_role"][role]
            # The per-call bound covers the relay's own worst case for what was actually sent.
            prompt = len(role_prompt(role, cd._relay_host()._ALL_SUFFIXES).encode("utf-8"))
            assert bound > sent + prompt, (short, role)
        assert plan["max_total_tokens"] >= 1.5 * sum(
            plan["relay_caps_basis"]["token_bound_by_role"][r] for r in observed
        )


def test_expected_calls_follow_repairs_rereviews_splits_and_interrupts(frozen):  # noqa: F811
    def calls(short):
        return cd.expected_live_calls(frozen[PREFIX + short])

    assert calls("E06-P1-INJ") == (
        {"repairer": 1, "evidence_checker": 2, "reviewer": 1, "verifier": 1},
        0,
    )  # the split reviewer's occurrence 1 goes to the double
    assert calls("E15-P2-CTL")[0]["reviewer"] == 5  # four manifest bumps after approval
    assert calls("E14-P3-INJ")[1] == 3  # three interrupts, each may re-ask a role
    assert calls("E09-P2-INJ")[0] == {"shadow_policy": 4}


# --------------------------------------------------------------------------- 4b: container route


def test_case_profile_runs_all_live_and_mixed_cases_through_invoke_live_role(driven):
    e12, e01 = driven["E12-P1-CTL"], driven["E01-P2-CTL"]
    for result in (e12, e01):
        assert isinstance(result.roles, live_roles.CombinedRoles)
        assert result.roles.live_member is result.relay
        assert result.budgets == [result.case["budget"]["max_model_usd"]]
        assert result.evidence["controller_trace"]["final_state"] == "RELEASED"
    assert e12.roles.double is None and {x["route"] for x in e12.roles.routes} == {"live"}
    assert e12.relay.calls == ["compiler"] * 3 + ["planner", "evidence_checker", "reviewer"] + [
        "verifier"
    ]
    assert isinstance(e01.roles.double, rs.SubstitutedRoles)
    assert e01.relay.calls == ["compiler"] * 3 + ["planner", "verifier"]
    assert {x["role"] for x in e01.roles.routes if x["route"] == rs.MODE} == {
        "evidence_checker",
        "reviewer",
    }


def test_live_identity_is_projected_per_live_receipt_and_missing_stays_missing(driven):
    for result in driven.values():
        projected = result.evidence["invocation_receipts"]
        receipts = result.checkpoint["role_receipts"]
        assert len(projected) == len(receipts)
        for record, receipt in zip(projected, receipts, strict=True):
            if receipt["mode"] == live_roles.LIVE:
                live = record["live"]
                assert live == live_roles.project_live_receipt(receipt)
                assert live["model_requested"] == live["model_observed"] == MODEL
                assert live["settings"]["temperature"] == 0 and live["settings"]["sdk"]
                assert live["relay_id"] and live["response_id"] == record["session_id"]
                # B-LR-1: the zero-cost fake relay is a mock gateway and the projection says
                # so; it is never indistinguishable from a real model call.
                assert live["gateway_mock"] is True
                assert live["cost_basis"] == "fake relay (test): zero cost, no model called"
            else:
                assert "live" not in record  # a substituted receipt carries no live identity


def test_live_case_without_a_relay_refuses_and_never_substitutes(frozen, tmp_path, monkeypatch):  # noqa: F811
    for name in [n for n in list(__import__("os").environ) if n.startswith("SIMKIT_RELAY_")]:
        monkeypatch.delenv(name)
    monkeypatch.setattr(relay_roles, "_CONSUMED", {})
    case = copy.deepcopy(frozen[PREFIX + "E01-P2-CTL"])  # mixed: a double exists for two roles
    adapter = WeatherAdapter(enable_run_case=True, producers=signed_producers())
    profile = driver_profile(case, adapter, tmp_path / "work")
    with pytest.raises(Refused, match="relay url") as caught:
        profile["roles_factory"](tmp_path / "work" / "case" / "run")
    assert caught.value.gate == "transition"
    # The adapter hook itself refuses a missing relay before any member exists.
    with pytest.raises(AdapterRefusal, match="never fall back"):
        adapter.invoke_live_role(case, goal_of(case), None, None)
    # An adapter without its own invoke_live_role cannot build a live profile.
    from simkit.adapter import Adapter

    class NoLive(WeatherAdapter):
        invoke_live_role = Adapter.invoke_live_role

    assert cd.live_factory(NoLive()) is None and cd.live_factory(adapter) is not None
    with pytest.raises(RuntimeError, match=cd.BLOCKED_LIVE):
        driver_profile(case, NoLive(enable_run_case=True), tmp_path / "work-2")


# --------------------------------------------------------------------------- CAP-ROLE-INPUT-PATCH


def test_e08_verifier_input_patch_reaches_the_live_verifier(e08):
    inj, ctl = e08["INJ"], e08["CTL"]
    report = inj.adapter.patch_role_input(inj.case, run_dir=inj.run, schedule=inj.schedule)
    (entry,) = report["entries"]
    assert entry["native_role"] == "verifier" and entry["occurrence"] == 1
    assert entry["adapter_mapping"] == "/evidence/verify-run(<artifact>) -> /gates/<artifact>"
    assert entry["delivery"]["status"] == "APPLIED"
    assert entry["delivery"]["before_hash"] != entry["delivery"]["after_hash"]
    assert entry["receipt_mode"] == live_roles.LIVE
    assert entry["received_patched_input"] is True
    assert entry["receipt_input_hash"] == entry["delivery"]["after_hash"]
    # The live verifier's recorded input carries the injected values.
    (verifier,) = [
        json.loads(p.read_text(encoding="utf-8"))
        for p in sorted(inj.run.glob("role-[0-9][0-9][0-9]-verifier.json"))
    ]
    check = verifier["input"]["gates"]["map"]["checks"][0]
    assert check["status"] == "FAIL" and check["differing_values"] == 37
    assert digest(verifier["input"]) == entry["receipt_input_hash"]
    # The control's empty patch is delivered and changes nothing.
    (control,) = ctl.adapter.patch_role_input(ctl.case, run_dir=ctl.run, schedule=ctl.schedule)[
        "entries"
    ]
    assert control["delivery"]["before_hash"] == control["delivery"]["after_hash"]
    assert control["received_patched_input"] is True
    # Without the schedule report nothing is filled.
    (unfilled,) = inj.adapter.patch_role_input(inj.case, run_dir=inj.run)["entries"]
    assert unfilled["delivery"] is None and unfilled["received_patched_input"] is None


def test_patch_role_input_refuses_what_cannot_reach_a_live_role(frozen):  # noqa: F811
    adapter = WeatherAdapter()
    e08 = frozen[PREFIX + "E08-P2-INJ"]
    assert len(adapter.patch_role_input(e08)["entries"]) == 1
    assert adapter.patch_role_input(frozen[PREFIX + "E12-P1-CTL"])["entries"] == []
    substituted = copy.deepcopy(e08)
    substituted["agent_roles"]["live"].remove("verifier")
    substituted["agent_roles"]["substituted"] = ["verifier"]
    with pytest.raises(AdapterRefusal, match="not a live role"):
        adapter.patch_role_input(substituted)
    split = copy.deepcopy(frozen[PREFIX + "E06-P1-INJ"])
    split["injection_schedule"].append(
        {
            "hook": "before_role",
            "selector": {"role_or_tool_or_stage": "reviewer", "occurrence": 2},
            "operation": "json_patch",
            "payload": [],
        }
    )
    with pytest.raises(AdapterRefusal, match="split"):
        adapter.patch_role_input(split)
    for payload in (
        {"op": "replace"},
        [{"op": "move", "path": "/x", "from": "/y"}],
        [{"op": "add"}],
    ):
        bad = copy.deepcopy(e08)
        bad["injection_schedule"][0]["payload"] = payload
        with pytest.raises(AdapterRefusal):
            adapter.patch_role_input(bad)


# --------------------------------------------------------------------------- CAP-METRIC


def test_budget_meter_readings_reach_the_controller_and_the_live_meter(local_runs):
    r = local_runs["metric"]
    report = r.adapter.inject_metric(r.case, run_dir=r.run, schedule=r.schedule)
    assert report["scope"] == "budget meters only"
    (entry,) = report["entries"]
    assert entry["readings"] == {
        "consumed_model_usd": 1.0,
        "elapsed_seconds": 600,
        "repair_attempts_used": 0,
    }
    readings = {x["metric"]: x for x in report["budget_readings"]}
    assert set(readings) == set(entry["readings"])
    assert all(x["schedule_index"] == entry["index"] for x in readings.values())
    usd = readings["consumed_model_usd"]
    assert usd["applied"] is True and usd["effective"] == 1.0 and usd["native_before"] == 0.0
    assert usd["state"] == "ONSET_CHECKED"
    # The live member owns the meter: the reading landed on the relay client's spent.
    assert r.relay.spent == 1.0 and r.checkpoint["spent"] == 1.0
    assert r.checkpoint["context"]["budget_readings"] == [
        {k: v for k, v in x.items() if k != "schedule_index"} for x in report["budget_readings"]
    ]


def test_inject_metric_refuses_resource_readings_and_reports_missing(frozen, driven):  # noqa: F811
    adapter = WeatherAdapter()
    for short in ("E12-P2-INJ", "E12-P2-CTL"):  # free_ram_gib: no controller meter
        with pytest.raises(AdapterRefusal, match="budget meters only"):
            adapter.inject_metric(frozen[PREFIX + short])
    plan = adapter.inject_metric(frozen[PREFIX + "E11-P2-INJ"])
    assert [e["index"] for e in plan["entries"]] == [0]
    timed = copy.deepcopy(frozen[PREFIX + "E11-P2-INJ"])
    timed["injection_schedule"][0]["payload"]["timing"] = "deferred"
    with pytest.raises(AdapterRefusal):
        adapter.inject_metric(timed)
    e12 = driven["E12-P1-CTL"]
    missing = adapter.inject_metric(e12.case, run_dir=e12.run)
    assert missing["status"] == "MISSING" and "budget_readings" not in missing


# --------------------------------------------------------------------------- CAP-TRANSPORT-RETRY(-SETUP)


def test_planner_transport_fault_is_retried_once_inside_the_attempt(local_runs):
    r = local_runs["retry"]
    setup = r.adapter.setup_planner_transport_retry(r.case, run_dir=r.run, schedule=r.schedule)
    (entry,) = setup["entries"]
    assert entry["delivery"] == "DELIVERED" and entry["confirmed"] is True
    assert entry["answered_by_live_transport"] is True
    (retry,) = entry["transport_retries"]
    assert retry["step"] == "planner" and retry["before_write"] is True
    assert retry["failure_class"] == "connection_reset"
    # One planner relay request reached the host: the fault raised before any byte was sent.
    assert r.relay.calls.count("planner") == 1 and r.relay.processes.serial == len(r.relay.calls)
    report = r.adapter.record_transport_retry(r.run)
    (check,) = report["checks"]
    assert check["inside_attempt"] is True and check["resume_between"] is False
    assert check["outcome"] == "dispatch_returned" and check["before_write"] is True
    assert report["transport_retries"][0]["attempt_id"] == check["attempt_id"]
    assert r.evidence["controller_trace"]["transport_retries"] == report["transport_retries"]
    assert r.evidence["controller_trace"]["final_state"] == "RELEASED"
    # Nothing was a resume.
    assert r.checkpoint["context"]["recovery"]["resume"] == []


def test_transport_retry_reports_without_a_retry_and_setup_refusals(frozen, driven):  # noqa: F811
    adapter = WeatherAdapter()
    e12 = driven["E12-P1-CTL"]
    report = adapter.record_transport_retry(e12.run)
    assert report["transport_retries"] == [] and report["checks"] == []
    plan = adapter.setup_planner_transport_retry(frozen[PREFIX + "E14-P3-INJ"])
    assert [(e["index"], e["occurrence"]) for e in plan["entries"]] == [(1, 1)]
    base = frozen[PREFIX + "E14-P3-INJ"]

    def variant(change):
        case = copy.deepcopy(base)
        change(case, case["injection_schedule"][1])
        return case

    bad = {
        "twice": lambda c, e: e["payload"]["args"].update(failures=2),
        "after write": lambda c, e: e["payload"]["args"].update(before_write=False),
        "timeout": lambda c, e: e["payload"]["args"].update(failure_class="timeout"),
        "verifier": lambda c, e: e["selector"].update(role_or_tool_or_stage="verifier"),
        "after_role": lambda c, e: e.update(hook="after_role"),
        "occurrence": lambda c, e: e["selector"].update(occurrence=2),
        "extra arg": lambda c, e: e["payload"]["args"].update(retries=1),
    }
    for change in bad.values():
        with pytest.raises(AdapterRefusal, match="FX-TRANSPORT-RETRY"):
            adapter.setup_planner_transport_retry(variant(change))
    substituted = variant(
        lambda c, e: (
            c["agent_roles"]["live"].remove("planner"),
            c["agent_roles"]["substituted"].append("planner"),
        )
    )
    with pytest.raises(AdapterRefusal, match="not a live role"):
        adapter.setup_planner_transport_retry(substituted)
    # invoke_live_role runs the same check before any live member exists.
    made = []
    with pytest.raises(AdapterRefusal):
        adapter.invoke_live_role(variant(bad["twice"]), MAP_GOAL, None, made.append)
    assert made == []


# --------------------------------------------------------------------------- CAP-OPERATOR-NOTIFY-HOOK


def notify_case():
    return {
        "id": "LOCAL-NOTIFY-HOOK",
        "injection_schedule": [
            {
                "id": "forged-resume",
                "requires": [],
                "hook": e2e_injections.NOTIFICATION_HOOK,
                "selector": {"role_or_tool_or_stage": "operator_notification", "occurrence": 1},
                "operation": "request_transition",
                "payload": {"action": "resume", "requested_by_role_channel": "planner"},
            }
        ],
    }


def test_notification_hook_fires_only_on_a_recorded_and_emitted_notification():
    hooks = e2e_injections.InjectionHooks(notify_case(), notification_boundary=True)
    delivered = []
    controller = SimpleNamespace(
        paths={}, request_transition=lambda p: delivered.append(p) or {"sequence": 9}
    )
    adapter = WeatherAdapter()
    event = {"target": "operator_notification", "notification": {"index": 0}}
    # Recorded but not emitted (an intention to notify): no occurrence, nothing delivered.
    hooks(e2e_injections.NOTIFICATION_HOOK, {**event, "recorded": True, "emitted": False}, None)
    report = adapter.dispatch_after_operator_notification(hooks)
    assert report["notifications_seen"] == 0
    assert report["entries"][0]["disposition"] == "INACTIVE" and delivered == []
    hooks(
        e2e_injections.NOTIFICATION_HOOK,
        {**event, "recorded": True, "emitted": True, "occurrence": 1},
        controller,
    )
    report = adapter.dispatch_after_operator_notification(hooks)
    assert report["notifications_seen"] == 1
    (entry,) = report["entries"]
    assert (entry["disposition"], entry["occurrence"]) == ("DELIVERED", 1)
    assert len(delivered) == 1
    # A repeated emission of the same native notification is not a second occurrence.
    hooks(
        e2e_injections.NOTIFICATION_HOOK,
        {**event, "recorded": True, "emitted": True, "occurrence": 1},
        controller,
    )
    assert adapter.dispatch_after_operator_notification(hooks)["notifications_seen"] == 1


def test_notification_hook_refuses_a_closed_boundary_or_another_dispatcher(frozen):  # noqa: F811
    adapter = WeatherAdapter()
    closed = e2e_injections.InjectionHooks(notify_case())  # run_case's hooks today
    with pytest.raises(AdapterRefusal, match="not opened"):
        adapter.dispatch_after_operator_notification(closed)
    e14 = frozen[PREFIX + "E14-P3-INJ"]
    plan = rs.substitution_plan(e14)
    roles = SimpleNamespace(double=None)
    case_hooks = rs.CaseScheduleHooks(plan, roles, None)  # the hooks run_case builds
    with pytest.raises(AdapterRefusal, match="live sub-wave 5"):
        adapter.dispatch_after_operator_notification(case_hooks)
    flat = notify_case()
    for entry in flat["injection_schedule"]:
        del entry["id"], entry["requires"]
    with pytest.raises(AdapterRefusal, match="dependent dispatcher"):
        adapter.dispatch_after_operator_notification(
            e2e_injections.InjectionHooks(flat, notification_boundary=True)
        )
    with pytest.raises(AdapterRefusal, match="InjectionHooks"):
        adapter.dispatch_after_operator_notification(object())


# --------------------------------------------------------------------------- passthrough


def test_binding_passthrough_method_reads_the_run_records(driven):
    for result in driven.values():
        # No after_role substitution was delivered in either run: nothing to confirm.
        assert result.adapter.confirm_binding_passthrough(result.run) == []
