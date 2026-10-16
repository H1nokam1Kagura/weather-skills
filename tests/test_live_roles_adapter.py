"""CAP-LIVE-ROLE combining transport (adapter_support/live_roles.py) on the real controller.

Frozen cases run verbatim (id, profile, budget, agent_roles, injection_schedule and
adapter_requirements) through WeatherAdapter.run_case: E12-P1-CTL (all live), E01-P2-CTL
(mixed), E06-P1-CTL (reviewer split by occurrence), E13-P2-INJ and E15-P2-INJ (mixed, with
schedules). The live member is a ZERO-COST FAKE relay with the RelayRoles interface
(tools/workflow/relay_roles.py): it answers from the substituted baselines and returns a
receipt with exactly the relay receipt fields. No model, relay or network is touched.

CAP-LIVE-ROLE and CAP-BINDING-PASSTHROUGH have no adapter method yet (live-role sub-wave 3),
so a TEST-LOCAL adapter subclass declares them (extension_methods) over the live_roles
producers, with test-local signed producer records (pattern of
tests/test_operator_ledger_capabilities.py signed()/producers_with). Nothing here is a
committed declaration.

The fake relay also records the exact relay request body size of every live call
(json.dumps({"role", "payload"}), the RelayRoles serializer) with the controller's REAL skill
documentation in the payload, against the 88,000-byte body cap of live-role sub-wave 1. The
table is written to <basetemp>/live-role-payload-sizes.json and printed.
"""

import copy
import hashlib
import json
import shutil
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_evidence_projection_v27 import (
    DATA_GOAL,
    FROZEN_CASES,
    FROZEN_CASES_SHA256,
    MAP_GOAL,
    Lab,
)

from adapter_support import fixture_rereview_projection as frx
from adapter_support import live_roles
from adapter_support import role_substitution as rs
from adapter_support.weather_adapter import AdapterRefusal, WeatherAdapter, load_producers
from tools.workflow import agents, catalog, relay_roles, runner
from tools.workflow.contracts import Refused, digest
from tools.workflow.persistence import read_checkpoint
from tools.workflow.review_evidence import text_hash

PREFIX = "DEV-E2E-V2_7-"
REPO = Path(__file__).resolve().parents[1]
BODY_CAP = 88_000  # live-role sub-wave 1 relay body cap (LIVE-WAVE-BRIEF.md)
MODEL = "claude-sonnet-4-6"
CASES = (
    "E12-P1-CTL",
    "E01-P2-CTL",
    "E01-P2-INJ",
    "E06-P1-CTL",
    "E06-P1-INJ",
    "E13-P2-INJ",
    "E15-P2-INJ",
)
MEASURED_ROLES = ("planner", "reviewer", "evidence_checker", "repairer", "shadow_policy")
SIGN = {
    "d4_gated": True,
    "status": "PRODUCER_MAPPED",
    "reviewer": "test-local",
    "reviewed_at": "2026-10-15T00:00:00Z",
    "reviewed_commit": "0" * 40,
}
FAKE_PROMPT = "FAKE RELAY (test): no system prompt was sent to any model"


def test_producers():
    """The committed producer file plus test-local signed records for the two capabilities
    whose adapter methods land in sub-wave 3."""
    producers = load_producers()
    for cap in (live_roles.CAPABILITY, live_roles.PASSTHROUGH_CAPABILITY):
        producers["capabilities"][cap] = {
            **SIGN,
            "producers": [{"file": "adapter_support/live_roles.py", "line": 1, "symbol": cap}],
        }
    return producers


test_producers.__test__ = False  # a helper, not a test


class LiveLabAdapter(WeatherAdapter):
    """TEST-LOCAL stand-in for the sub-wave 3 adapter methods; delegates to live_roles."""

    extension_methods = {
        **WeatherAdapter.extension_methods,
        live_roles.CAPABILITY: "invoke_live_role",
        live_roles.PASSTHROUGH_CAPABILITY: "confirm_binding_passthrough",
    }

    def invoke_live_role(self, case, goal, run_dir, relay_factory):
        return live_roles.live_roles_factory(case, goal, run_dir, relay_factory)

    def confirm_binding_passthrough(self, run_dir):
        return live_roles.confirm_binding_passthrough(run_dir)


class FakeRelay:
    """Zero-cost RelayRoles stand-in: same ask/receipts/spent/budget/model/dispatch_* interface,
    answers from the substituted baselines, receipt fields exactly relay_roles.RECEIPT_FIELDS."""

    def __init__(self, case, goal, budget=5, model=MODEL):
        every = {
            "id": case["id"],
            "agent_roles": {"substituted": sorted(rs.NATIVE_ROLE)},
        }
        self.baseline = rs.SubstitutedRoles(every, goal)
        self.budget, self.spent, self.model = budget, 0.0, model
        self.receipts, self.sessions, self.unknown_outcomes = [], set(), []
        self.bodies = []
        self.calls = []

    def ask(self, role, payload):
        if self.budget - self.spent < 0.05:
            raise Refused("aggregate model budget exhausted", gate="budget")
        body = json.dumps({"role": role, "payload": payload}, allow_nan=False).encode()
        keys = {k: len(json.dumps(v).encode()) for k, v in payload.items()}
        self.bodies.append({"role": role, "bytes": len(body), "keys": keys})
        self.calls.append(role)
        invocation = str(uuid.uuid4())
        context = getattr(self, "dispatch_context", None)
        if context is not None:
            self.dispatch_observation = {
                "attempt_id": context["attempt_id"],
                "invocation_id": invocation,
                "process_boundary": "NOT_ENTERED",
            }
            self.dispatch_observer(dict(self.dispatch_observation))
            self.dispatch_observation["process_boundary"] = "ENTERED"
        value = self.baseline.baseline(role, payload)
        if context is not None:
            self.dispatch_observation.update(process_boundary="RETURNED", http_status=200)
        response_id = f"msg_fake_{uuid.uuid4().hex}"
        user = json.dumps(payload)
        receipt = {
            "role": role,
            "invocation_id": str(uuid.uuid4()),  # minted host-side, as the relay's KongRoles
            "session_id": response_id,
            "input_hash": digest(payload),
            "output_hash": digest(value),
            "cost_usd": 0.0,
            "cost_basis": "fake relay (test): zero cost, no model called",
            "budget_guard": "on",
            "model_requested": self.model,
            "model_observed": self.model,
            "model_match": True,
            "settings": {
                "max_tokens": 4096,
                "temperature": 0,
                "system_prompt_hash": text_hash(FAKE_PROMPT),
                "thinking": None,
                "retries": 0,
                "sdk": "fake",
            },
            "settings_sent": {"max_tokens": 4096, "temperature": 0},
            "sent_keys": ["max_tokens", "messages", "model", "system", "temperature"],
            "output_normalization": "none",
            "output_form": "json_object",
            "stdin_hash": text_hash(user),
            "usage": {"input_tokens": 0, "output_tokens": 0},
            "stop_reason": "end_turn",
            "response_id": response_id,
            "request_id": f"req_fake_{uuid.uuid4().hex}",
            "via": "fake-relay",
            "latency_ms": 0,
            "gateway_mock": True,
            "transport": relay_roles.TRANSPORT,
            "mode": "live",
            "relay_id": f"relay-fake-{uuid.uuid4().hex}",
        }
        if role == "reviewer" and "review_evidence" in payload:
            receipt["dispatch"] = {
                "stdin_hash": text_hash(user),
                "system_prompt": FAKE_PROMPT,
                "system_prompt_hash": text_hash(FAKE_PROMPT),
            }
        optional = relay_roles.OPTIONAL_RECEIPT_FIELDS.get(role, frozenset())
        assert relay_roles.RECEIPT_FIELDS <= set(receipt) <= relay_roles.RECEIPT_FIELDS | optional
        self.sessions.add(response_id)
        self.receipts.append(dict(receipt))  # RelayRoles appends a copy of the relay receipt
        return value


@pytest.fixture(scope="module")
def frozen():
    raw = FROZEN_CASES.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == FROZEN_CASES_SHA256
    return {c["id"]: c for c in json.loads(raw)["cases"]}


class LiveLab(Lab):
    """Stand-in tools, but the controller's REAL skill documentation (it is in every planner
    and reviewer payload, so the measured body sizes are the real ones), plus the real
    definition registry, pyproject.toml and runtime manifest (FX-RUNTIME-MANIFEST-BUMP)."""

    def __init__(self, root):
        super().__init__(root)
        for skill in catalog.CATALOG:
            shutil.copyfile(
                REPO / "skills" / skill / "SKILL.md", self.repo / "skills" / skill / "SKILL.md"
            )
        target = self.repo / frx.DEFINITIONS_FILE
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(REPO / frx.DEFINITIONS_FILE, target)
        for name in ("pyproject.toml", catalog.RUNTIME_MANIFEST):
            shutil.copyfile(REPO / name, self.repo / name)


@pytest.fixture(scope="module")
def lab(tmp_path_factory):
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(runner, "preflight", lambda *args: ({}, {}))
        mp.setattr(agents.shutil, "which", lambda _: "never-launched-claude")
        yield LiveLab(tmp_path_factory.mktemp("live-roles"))


def goal_of(case):
    return DATA_GOAL if case["profile"] == "cached-onset-data/v1" else MAP_GOAL


def execute(lab, case):
    adapter = LiveLabAdapter(enable_run_case=True, producers=test_producers())
    goal = goal_of(case)
    profile = lab.profile(goal, case, stub=None)
    built, relays = [], []

    def relay_factory(run_dir):
        relays.append(FakeRelay(case, goal, budget=case["budget"]["max_model_usd"]))
        return relays[-1]

    def factory(run_dir):
        built.append(adapter.invoke_live_role(case, goal, run_dir, relay_factory))
        return built[-1]

    profile["roles_factory"] = factory
    evidence = adapter.run_case(case, profile)
    root = Path(profile["case_root"])
    run = root / "run"
    report = json.loads((root / "harness/evidence-projection.json").read_text(encoding="utf-8"))
    return SimpleNamespace(
        case=case,
        evidence=evidence,
        report=report,
        run=run,
        root=root,
        checkpoint=read_checkpoint(run),
        roles=built[0],
        relay=relays[0],
        passthrough=adapter.confirm_binding_passthrough(run),
    )


@pytest.fixture(scope="module")
def runs(lab, frozen):
    return {s: execute(lab, copy.deepcopy(frozen[PREFIX + s])) for s in CASES}


# The routes each frozen agent_roles table implies, written out by hand (independent of
# live_roles.routing_table): role -> member, or role -> {occurrence: member, None: default}.
L, S = live_roles.LIVE, live_roles.SUBSTITUTED
EXPECTED_ROUTES = {
    "E12-P1-CTL": {},
    "E01-P2-CTL": {"evidence_checker": S, "reviewer": S},
    "E01-P2-INJ": {"evidence_checker": S, "reviewer": S},
    "E06-P1-CTL": {"compiler": S, "planner": S, "reviewer": {1: S, None: L}},
    "E06-P1-INJ": {"compiler": S, "planner": S, "reviewer": {1: S, None: L}},
    "E13-P2-INJ": {"compiler": S, "planner": S},
    "E15-P2-INJ": {"compiler": S},
}


def expected_route(short, role, occurrence):
    rule = EXPECTED_ROUTES[short].get(role, L)
    return rule.get(occurrence, rule[None]) if isinstance(rule, dict) else rule


def role_files(run):
    return [
        json.loads(p.read_text(encoding="utf-8"))
        for p in sorted(run.glob("role-[0-9][0-9][0-9]-*.json"))
    ]


def events(run, kind):
    out = []
    for path in sorted(run.glob("dispatch-event-*.json")):
        event = json.loads(path.read_text(encoding="utf-8"))
        if event.get("kind") == kind:
            out.append(event)
    return out


def test_every_invocation_is_routed_by_role_and_controller_occurrence(runs):
    for short, r in runs.items():
        routes = r.roles.routes
        assert routes, short
        attempts = events(r.run, "dispatch_attempt")
        # One route per controller dispatch attempt, keyed by the controller's own occurrence.
        assert [(a["role"], a["occurrence"], a["attempt_id"]) for a in attempts] == [
            (x["role"], x["occurrence"], x["attempt_id"]) for x in routes
        ], short
        for route in routes:
            assert route["route"] == expected_route(short, route["role"], route["occurrence"]), (
                short,
                route,
            )
            assert route["outcome"] == "RETURNED", (short, route)
        # The fake relay saw exactly the live routes, in order; the double the rest.
        assert r.relay.calls == [x["role"] for x in routes if x["route"] == L], short
    # The split reviewer: occurrence 1 to the double, a later occurrence to the live member.
    split = [
        (x["occurrence"], x["route"])
        for x in runs["E06-P1-INJ"].roles.routes
        if x["role"] == "reviewer"
    ]
    assert split == [(1, S), (2, L)]
    assert [x["route"] for x in runs["E06-P1-INJ"].roles.routes if x["role"] == "repairer"] == [L]
    assert {x["route"] for x in runs["E12-P1-CTL"].roles.routes} == {L}


def test_one_shared_receipts_list_holds_the_members_own_receipt_objects(runs):
    for short, r in runs.items():
        receipts = r.roles.receipts
        assert receipts == r.checkpoint["role_receipts"], short
        routed = [x for x in r.roles.routes if x["outcome"] == "RETURNED"]
        assert len(receipts) == len(routed) == len(role_files(r.run)), short
        double = r.roles.double.receipts if r.roles.double is not None else []
        for receipt, route in zip(receipts, routed, strict=True):
            own = r.relay.receipts if route["route"] == L else double
            assert any(receipt is mine for mine in own), (short, route)  # the object itself
            assert receipt["mode"] == route["route"] and receipt["role"] == route["role"]
            assert route["receipts"] == [receipt["invocation_id"]]
        assert len(receipts) == len(r.relay.receipts) + len(double), short
        sessions = [x["session_id"] for x in receipts]
        assert len(set(sessions)) == len(sessions), short
        projected = r.evidence["invocation_receipts"]
        assert [p["session_id"] for p in projected] == sessions
        assert [p["mode"] for p in projected] == [x["mode"] for x in receipts]
        returned = {e["invocation_id"]: e["receipt"] for e in events(r.run, "dispatch_returned")}
        for receipt in receipts:
            assert returned[receipt["invocation_id"]]["output_hash"] == receipt["output_hash"]


def test_rebind_still_applies_to_the_doubles_receipt_on_the_shared_list(runs):
    for short in ("E01-P2-INJ", "E06-P1-CTL", "E06-P1-INJ", "E13-P2-INJ"):
        r = runs[short]
        rebinds = r.report["harness"]["case_schedule"]["rebinds"]
        rebound = [x for x in r.roles.receipts if x.get("substitutions")]
        assert len(rebinds) == len(rebound) == 1, short
        (receipt,) = rebound
        assert receipt["mode"] == S
        assert any(receipt is mine for mine in r.roles.double.receipts)
        assert rebinds[0]["invocation_id"] == receipt["invocation_id"]
        assert rebinds[0]["session_id"] == receipt["session_id"]
        (record,) = [
            f
            for f in role_files(r.run)
            if f["receipt"]["invocation_id"] == receipt["invocation_id"]
        ]
        assert digest(record["output"]) == receipt["output_hash"]
        assert receipt["output_hash"] == receipt["substitutions"][0]["delivered_output_hash"]
        assert (
            receipt["baseline_output_hash"]
            == receipt["substitutions"][0]["pre_substitution_output_hash"]
        )
        assert receipt["baseline_output_hash"] != receipt["output_hash"]
    assert runs["E06-P1-INJ"].report["harness"]["case_schedule"]["coverage"]["applied"] == [0]


def test_dispatch_observations_are_the_chosen_members_own(runs):
    for short, r in runs.items():
        prepared = events(r.run, "invocation_prepared")
        routes = r.roles.routes
        assert [p["attempt_id"] for p in prepared] == [x["attempt_id"] for x in routes], short
        for event, route in zip(prepared, routes, strict=True):
            boundary = "NOT_ENTERED" if route["route"] == L else "NO_PROCESS"
            assert event["process_boundary"] == boundary, (short, route)
            if route["route"] == S:
                assert event["transport"] == rs.SUBSTITUTE_ID
    assert runs["E12-P1-CTL"].roles.dispatch_observation["process_boundary"] == "RETURNED"
    assert runs["E12-P1-CTL"].roles.dispatch_context is None


def test_budget_model_and_retry_recorder_delegate_to_the_live_member(runs):
    for short, r in runs.items():
        assert r.checkpoint["spent"] == r.relay.spent == 0.0, short
        assert r.checkpoint["budget"] == r.relay.budget == 5, short
        assert r.checkpoint["model"] == MODEL, short
        # Controller.bind_recovery set the recorder through the combined roles.
        assert r.relay.transport_retry_recorder.func.__name__ == "record_transport_retry"
        assert r.roles.transport_retry_recorder is r.relay.transport_retry_recorder


def test_binding_passthrough_is_confirmed_or_recorded_as_not_passed_through(runs):
    by_case = {short: r.passthrough for short, r in runs.items()}
    for short in ("E12-P1-CTL", "E01-P2-CTL", "E15-P2-INJ"):
        assert by_case[short] == [], short  # no after_role substitution delivered
    (planner,) = by_case["E13-P2-INJ"]
    assert planner["substituted_role"] == "planner" and planner["confirmed"] is True
    assert (planner["carrier_role"], planner["carrier_occurrence"]) == ("reviewer", 1)
    assert planner["carrier_mode"] == L
    assert planner["reviewed_hash"] == planner["delivered_hash"]
    delivered = [
        f for f in role_files(runs["E13-P2-INJ"].run) if f["receipt"]["role"] == "planner"
    ][0]
    claims = [c["claim_id"] for c in delivered["output"]["plan_claims"]]
    assert "C-ONSET-RULE" in claims  # the injected claim is in the reviewed plan version
    # E06-P1-INJ: the substituted REJECT's finding reaches the live repairer, inside the
    # rejected review the controller serializes into the repairer's failure field.
    (review,) = by_case["E06-P1-INJ"]
    assert review["confirmed"] is True and review["carrier_role"] == "repairer"
    assert review["carrier_paths"] == ["/failure(json)/findings/0"]
    # E01-P2-INJ: the waiver is not in the verifier's recorded input. Recorded, never filled.
    (waiver,) = by_case["E01-P2-INJ"]
    assert waiver["confirmed"] is False and waiver["reviewed_hash"] is None
    assert (waiver["carrier_role"], waiver["carrier_mode"]) == ("verifier", L)
    assert waiver["not_passed"] == ["verification_waiver"]
    assert waiver["delivered_hash"] is not None
    # E06-P1-CTL: APPROVE with no findings carries no proposal: nothing checked, nothing filled.
    (approve,) = by_case["E06-P1-CTL"]
    assert approve["confirmed"] is None and approve["carrier_role"] is None
    assert approve["delivered_hash"] is None and "no proposal" in approve["reason"]


def test_project_live_receipt_carries_live_identity_and_substituted_mode(runs):
    r = runs["E13-P2-INJ"]
    for receipt in r.roles.receipts:
        projected = live_roles.project_live_receipt(receipt)
        if receipt["mode"] == L:
            assert set(projected) == set(live_roles.LIVE_RECEIPT_FIELDS)
            assert projected["model_observed"] == MODEL
            assert projected["relay_id"] == receipt["relay_id"]
            assert projected["settings"]["temperature"] == 0
        else:
            assert projected == {"mode": S, "transport": rs.SUBSTITUTE_ID}
    sparse = {"mode": "live", "transport": relay_roles.TRANSPORT, "usage": None}
    assert live_roles.project_live_receipt(sparse) == sparse  # missing stays missing
    assert live_roles.project_live_receipt({"role": "x"}) == {}


def test_payload_sizes_against_the_relay_body_cap(runs, tmp_path_factory):
    """Measurement, not a gate: every live relay body per case and role, written to
    <basetemp>/live-role-payload-sizes.json and printed (real skill documentation)."""
    table = {
        short: [{"role": b["role"], "bytes": b["bytes"], "keys": b["keys"]} for b in r.relay.bodies]
        for short, r in runs.items()
    }
    maxima = {}
    for bodies in table.values():
        for body in bodies:
            maxima[body["role"]] = max(maxima.get(body["role"], 0), body["bytes"])
    record = {
        "body_cap": BODY_CAP,
        "serializer": "json.dumps({'role': role, 'payload': payload}) (RelayRoles.ask)",
        "max_by_role": maxima,
        "over_cap": sorted(role for role, size in maxima.items() if size > BODY_CAP),
        "by_case": table,
    }
    path = tmp_path_factory.getbasetemp() / "live-role-payload-sizes.json"
    path.write_text(json.dumps(record, indent=1), encoding="utf-8")
    lines = [f"live relay body sizes (cap {BODY_CAP}) -> {path}"]
    for role in sorted(maxima):
        flag = "OVER" if maxima[role] > BODY_CAP else "ok"
        lines.append(f"  {role:17} max {maxima[role]:7d}  {flag}")
    print("\n".join(lines))
    assert set(table) == set(CASES) and all(table.values())
    assert {"planner", "reviewer", "evidence_checker", "repairer"} <= set(maxima)


# --------------------------------------------------------------------------- unit checks


def combined_for(frozen, short, relay=None):
    case = copy.deepcopy(frozen[PREFIX + short])
    goal = goal_of(case)
    made = []

    def relay_factory(run_dir):
        made.append(relay or FakeRelay(case, goal))
        return made[-1]

    return live_roles.live_roles_factory(case, goal, None, relay_factory), made


def test_not_invoked_and_unlisted_roles_are_refused_with_no_call(frozen):
    roles, (relay,) = combined_for(frozen, "E01-P2-CTL")
    for role, why in (
        ("repairer", "not_invoked"),
        ("shadow_policy", "not_invoked"),
        ("router", "not listed"),
    ):
        with pytest.raises(Refused, match=why) as caught:
            roles.ask(role, {})
        assert caught.value.gate == live_roles.ROLE_GATES.get(role, "transition")
    assert relay.calls == [] and roles.receipts == [] and roles.double.receipts == []
    assert {x["outcome"] for x in roles.routes} == {"REFUSED_NO_CALL"}
    # A split role cannot be routed without the controller's attempt occurrence.
    split, (relay,) = combined_for(frozen, "E06-P1-INJ")
    with pytest.raises(Refused, match="split by occurrence"):
        split.ask("reviewer", {})
    assert relay.calls == [] and split.receipts == []


class RaisingRelay(FakeRelay):
    def ask(self, role, payload):
        self.dispatch_observation = {
            "attempt_id": self.dispatch_context["attempt_id"],
            "invocation_id": "inv-x",
            "process_boundary": "ENTERED",
        }
        raise relay_roles.RelayOutcomeUnknown("reset after send", {"reason": "reset"})


def test_dispatch_observation_is_copied_back_on_a_member_exception(frozen):
    case = copy.deepcopy(frozen[PREFIX + "E12-P1-CTL"])
    relay = RaisingRelay(case, DATA_GOAL)
    roles, _ = combined_for(frozen, "E12-P1-CTL", relay=relay)
    seen = []
    roles.dispatch_context = {"attempt_id": "att-1", "occurrence": 1, "role": "compiler"}
    roles.dispatch_observer = seen.append
    with pytest.raises(relay_roles.RelayOutcomeUnknown):
        roles.ask("compiler", {"messages": []})
    assert roles.dispatch_observation == {
        "attempt_id": "att-1",
        "invocation_id": "inv-x",
        "process_boundary": "ENTERED",
    }
    assert relay.dispatch_context is None and relay.dispatch_observer is None
    assert roles.receipts == [] and roles.routes[-1]["outcome"] == "RAISED RelayOutcomeUnknown"


def test_resume_restore_and_meter_writes_reach_the_live_member(frozen):
    roles, (relay,) = combined_for(frozen, "E13-P2-INJ")
    roles.spent, roles.budget, roles.model = 1.25, 4, "m"
    assert (relay.spent, relay.budget, relay.model) == (1.25, 4, "m")
    assert (roles.spent, roles.budget, roles.model) == (1.25, 4, "m")
    restored = [{"role": "planner", "session_id": "s-1"}]
    roles.receipts = restored
    roles.sessions = {"s-1"}
    assert roles.receipts is restored
    assert relay.sessions == {"s-1"} and roles.double.sessions == {"s-1"}
    assert roles.sessions == {"s-1"}
    with pytest.raises(AttributeError):
        roles.processes  # noqa: B018 - the fake relay owns no process manager
    relay.processes = sentinel = object()
    assert roles.processes is sentinel


def test_factory_and_table_refusals_happen_before_any_live_member_exists(frozen, tmp_path):
    made = []

    def relay_factory(run_dir):
        made.append(run_dir)

    with pytest.raises(AdapterRefusal, match="no live roles"):
        live_roles.live_roles_factory(
            copy.deepcopy(frozen[PREFIX + "E02-P1-CTL"]), MAP_GOAL, None, relay_factory
        )
    base = copy.deepcopy(frozen[PREFIX + "E13-P2-INJ"])
    bad = {
        "compilers are live all three": {"live": ["compiler_1", "planner"], "substituted": []},
        "not_invoked and invoked": {"live": ["planner"], "not_invoked": ["planner"]},
        "no after_role entry selects": {"live": ["reviewer"], "substituted": ["reviewer"]},
    }
    for message, roles in bad.items():
        with pytest.raises(AdapterRefusal, match=message):
            live_roles.routing_table({**base, "agent_roles": roles, "injection_schedule": []})
    # A replacement of a live-only role is refused by the substitution plan.
    live_only = copy.deepcopy(base)
    live_only["injection_schedule"][0]["selector"]["role_or_tool_or_stage"] = "reviewer"
    live_only["injection_schedule"][0]["operation"] = "replace_role_output"
    live_only["injection_schedule"][0]["payload"] = {"verdict": "APPROVE"}
    with pytest.raises(AdapterRefusal, match="live role's output is never replaced"):
        live_roles.live_roles_factory(live_only, MAP_GOAL, None, relay_factory)
    assert made == []
    with pytest.raises(AdapterRefusal, match="live member is required"):
        live_roles.CombinedRoles(base, live_roles.routing_table(base))
    # case_hooks accepts the combined roles of this case only.
    plan = rs.substitution_plan(base, MAP_GOAL)
    roles, _ = combined_for(frozen, "E13-P2-INJ")
    assert rs.case_hooks(plan, roles, tmp_path) is not None
    other, _ = combined_for(frozen, "E06-P1-INJ")
    with pytest.raises(AdapterRefusal, match="substituted roles factory"):
        rs.case_hooks(plan, other, tmp_path)
