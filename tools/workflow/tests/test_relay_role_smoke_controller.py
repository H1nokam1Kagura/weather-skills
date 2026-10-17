"""Relay ROLE smoke with the real controller, offline (no docker, no network beyond 127.0.0.1,
no secret, no Kong).

Pins: (1) the calls cover every role the frozen live cases run live, in the payload variants
they send, and every payload is the one the REAL controller builds (captured from
``runner.Controller``); (2) the relay caps admit every call at its worst case; (3) a full
offline pass (real RelayRoles -> real host Relay -> real KongRoles with a fake worker) records
evidence the report passes, every answer judged by the controller's own acceptance on the
byte-identical payload; (4) an answer the controller refuses fails the smoke even though the
relay reported SUCCESS, starting with run 38906458002's mixed reviewer shape
{verdict, binding_hash, findings, reason}; (5) each judgement is the controller's (refusal
text and gate as raised by it).

Needs the controller stack (preflight loads the skills), so it lives in tools/workflow/tests
(ci.yml) and the relay-role-smoke job runs it under ``uv sync --group dev``; the slim report /
wiring tests are container_harness/tests/test_relay_role_smoke.py.
"""

import ast
import copy
import hashlib
import itertools
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
HARNESS = REPO / "container_harness"
for _path in (str(HARNESS), str(REPO)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import relay_role_smoke as smoke  # noqa: E402
import relay_role_smoke_report as report  # noqa: E402
import relay_smoke_report  # noqa: E402

from relay import kong_relay_host as host  # noqa: E402
from tools.workflow import agents, role_schemas, runner  # noqa: E402
from tools.workflow.contracts import expected_plan  # noqa: E402
from tools.workflow.kong_roles import (  # noqa: E402
    LIVE_ROLE_MAX_TOKENS,
    PINNED_PRICES,
    KongRoles,
)
from tools.workflow.relay_roles import DEFAULT_MAX_BODY, SHARED_KEY, RelayRoles  # noqa: E402

TOKEN = "f" * 43
RUN = "relay-run-role-smoke"
MIXED_REVIEWER_KEYS = {"verdict", "binding_hash", "findings", "reason"}  # run 38906458002


def fast_manifest(base):
    """The build identity over SKILL.md only (as test_claim_gates_v27 does): the real one
    hashes the whole tree at every integrity check, which only makes these tests slow."""
    return {
        str(p.relative_to(base)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(Path(base).rglob("SKILL.md"))
    }


@pytest.fixture(scope="module")
def patched():
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(runner, "build_manifest", fast_manifest)
        yield


@pytest.fixture(scope="module")
def inputs():
    return smoke.frozen_inputs()


@pytest.fixture(scope="module")
def captured(patched, inputs, tmp_path_factory):
    work = tmp_path_factory.mktemp("capture")
    payloads, contexts = smoke.capture(work, inputs)
    return work, payloads, contexts


# ------------------------------------------------------------------ which calls, which payloads


def test_calls_cover_every_live_role_of_the_frozen_live_cases():
    from case_driver import frozen_root

    from adapter_support import role_substitution as rs

    root, _ = frozen_root()
    doc = json.loads((root / smoke.FROZEN_CASES).read_text(encoding="utf-8"))
    live_cases = [c for c in doc["cases"] if c["agent_roles"]["live"]]
    assert len(live_cases) == 34  # LIVE-WAVE-BRIEF "Case groups"
    live = {rs.NATIVE_ROLE.get(r, r) for c in live_cases for r in c["agent_roles"]["live"]}
    assert live == set(smoke.ROLES)
    assert "router" not in live and "rubric_grader" not in smoke.ROLES
    # every live case runs review evidence + claim gates (weather_adapter.run_case), so the
    # schema variant of each call is the one the live cases send
    assert {c["call"] for c in smoke.CALLS if c["role"] == "reviewer"} == {
        "reviewer",
        "reviewer_rationale",
    }
    assert [(c["call"], c["role"]) for c in smoke.CALLS] == list(report.EXPECTED_CALLS)


def test_payloads_are_the_controllers_in_the_live_variants(captured, inputs):
    _, payloads, _ = captured
    assert set(payloads) == {c["call"] for c in smoke.CALLS}
    variant = {c: [k for k in role_schemas.VARIANT_KEYS if k in p] for c, p in payloads.items()}
    assert variant == {
        "compiler": [],
        "planner": ["review_evidence_request"],
        "evidence_checker": [],
        "reviewer": ["review_evidence"],
        "repairer": ["review_evidence_request"],
        "reviewer_rationale": ["review_evidence"],
        "verifier": [],
        "shadow_policy": [],
    }
    for name in ("reviewer", "reviewer_rationale"):
        assert payloads[name]["claim_check"]["decision"] == "PRESENT_FOR_REVIEW"
        assert payloads[name]["review_evidence"]["capability"] == "PRE_REVIEW_CLAIM_GATES"
    assert "rationale" not in payloads["reviewer"]["review_evidence"]
    assert payloads["reviewer_rationale"]["review_evidence"]["rationale"] == smoke.RATIONALE
    prompt = agents.role_prompt("reviewer", payloads["reviewer_rationale"])
    assert agents.REVIEW_RATIONALE_SUFFIX in prompt
    assert agents.REVIEW_RATIONALE_SUFFIX not in agents.role_prompt(
        "reviewer", payloads["reviewer"]
    )
    # the repairer repairs the frozen E06-P1-INJ rejection, as the controller hands it over
    failure = json.loads(payloads["repairer"]["failure"])
    assert failure["verdict"] == "REJECT"
    assert failure["findings"] == inputs["reviewer_reject"]["findings"]
    assert failure["reviewer_output"] == inputs["reviewer_reject"]
    assert payloads["repairer"]["prior_plan"] == expected_plan(inputs["goal"])
    # the E09-P2-INJ note reaches the first shadow question, outside its fingerprint
    note = inputs["shadow_patch"][0]["value"]
    assert payloads["shadow_policy"]["context"] == {"notes": [note]}
    assert payloads["shadow_policy"]["state"] == "GOAL_CHECKED"
    # the checker judges the plan version the reviewer is then shown
    assert (
        payloads["evidence_checker"]["plan_hash"]
        == payloads["reviewer"]["claim_check"]["plan_hash"]
    )
    assert (
        payloads["reviewer"]["binding_hash"]
        == payloads["reviewer"]["review_evidence"]["binding_hash"]
    )


def test_payload_sizes_fit_the_transport(captured):
    _, payloads, _ = captured
    for call in smoke.CALLS:
        p = smoke.predict(call["role"], payloads[call["call"]])
        assert p["request_body_bytes"] < DEFAULT_MAX_BODY, call["call"]
        assert p["gateway_body_bytes_bound"] < p["gateway_body_known_good"], call["call"]
        dedup = call["call"] in ("planner", "reviewer", "repairer", "reviewer_rationale")
        assert (p["transport_encoding"] is not None) is dedup, call["call"]


def _verifier_literal():
    tree = ast.parse((REPO / "tools" / "workflow" / "runner.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and getattr(node.func, "attr", None) == "ask"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and node.args[0].value == "verifier"
        ):
            return [k.value for k in node.args[1].keys]
    raise AssertionError("no verifier ask in runner.py")


def test_verifier_payload_follows_the_controllers_literal(captured):
    """The one payload assembled outside the controller keeps runner._run's keys and order."""
    _, payloads, contexts = captured
    assert list(payloads["verifier"]) == _verifier_literal()
    c = contexts["reject_then_repair"]
    assert payloads["verifier"]["execution_context"] == c.execution_context()
    assert set(payloads["verifier"]["gates"]) == set(expected_plan(c.goal)["final"])


def test_relay_caps_admit_every_call_at_its_worst_case(captured, tmp_path):
    _, payloads, _ = captured
    python = tmp_path / "python.exe"
    python.write_text("")
    roles = KongRoles(
        Worker({}),
        tmp_path / "ctx",
        budget=smoke.BUDGET_USD,
        max_tokens=int(smoke.RELAY_CAPS["RELAY_MAX_TOKENS"]),
        temperature=0.0,
        prices=PINNED_PRICES,
        role_max_tokens=dict(LIVE_ROLE_MAX_TOKENS),
        session_source="response_id",
        data_policy_ack=True,
        kit_python=python,
    )
    worst = []
    for call in smoke.CALLS:
        fields = smoke.predict(call["role"], payloads[call["call"]])
        worst.append(host.worst_case(roles, call["role"], payloads[call["call"]], fields["_user"]))
    total_tokens = sum(w["tokens"] for w in worst)
    total_usd = sum(w["usd"] for w in worst)
    assert total_tokens <= int(smoke.RELAY_CAPS["RELAY_MAX_TOTAL_TOKENS"])
    assert total_usd <= smoke.BUDGET_USD  # every call admitted whatever the order
    assert 1.0 < total_usd < 1.8  # the job comment's "~USD 1.4" worst case
    assert int(smoke.RELAY_CAPS["RELAY_MAX_REQUESTS"]) == len(smoke.CALLS)


# ------------------------------------------------------------------ fake worker


def payload_of(user):
    """The role payload the model sees: the plain JSON, or the dedup rendering resolved."""
    if not user.startswith(host.MODEL_INPUT_PREAMBLE):
        return json.loads(user)
    lines = user.split("\n")
    role_input = json.loads(lines[lines.index("=== ROLE INPUT (JSON) ===") + 1])
    shared = {}
    for k, line in enumerate(lines):
        if line.startswith(f"--- {SHARED_KEY} "):
            shared[line.split()[2]] = json.loads(lines[k + 1])

    def resolve(node):
        if isinstance(node, dict):
            if set(node) == {SHARED_KEY}:
                return resolve(shared[node[SHARED_KEY]])
            return {k: resolve(v) for k, v in node.items()}
        if isinstance(node, list):
            return [resolve(v) for v in node]
        return node

    return resolve(role_input)


def good_answer(role, payload, goal):
    """What a model following the controller's contract answers (frozen baselines)."""
    from adapter_support import role_substitution as rs

    if role == "compiler":
        return copy.deepcopy(goal)
    if role in ("planner", "repairer"):
        return rs.static_baseline(role, goal, "E13-P1")
    if role == "evidence_checker":
        verdicts = [
            {"claim_id": c["claim_id"], "verdict": "SUPPORTED", "reasoning": "stated verbatim"}
            for c in payload["claims"]
        ]
        return {"plan_hash": payload["plan_hash"], "verdicts": verdicts}
    if role == "reviewer":
        return {"verdict": "APPROVE", "binding_hash": payload["binding_hash"], "findings": []}
    if role == "verifier":
        return {
            "verdict": "PASS",
            "sub_reason": None,
            "binding_hash": payload["binding_hash"],
            "scope": "data-replay",
        }
    if role == "shadow_policy":
        return {
            "schema": payload["schema"],
            "state": payload["state"],
            "question_fingerprint": payload["question_hash"],
            "action": payload["admissible_actions"][0],
            "score": 0.8,
        }
    raise KeyError(role)


IDS = itertools.count(1)


class Worker:
    """Fake kit worker: forces the answer tool KongRoles asked for and answers through it
    (kong_roles.WORKER_SOURCE), reading the payload only from the model-facing message.
    ``override``: {(role, occurrence): fn(payload, good) -> answer}."""

    def __init__(self, goal, override=None):
        self.goal = goal
        self.override = override or {}
        self.calls = []

    def run(self, argv, cwd, env, stdin):
        request = json.loads(stdin)
        self.calls.append(request)
        tool = request["answer_tool"]
        role = tool["name"][len(role_schemas.TOOL_PREFIX) : -len(role_schemas.TOOL_SUFFIX)]
        payload = payload_of(request["user"])
        occurrence = sum(1 for c in self.calls if c["answer_tool"]["name"] == tool["name"])
        answer = good_answer(role, payload, self.goal)
        change = self.override.get((role, occurrence))
        if change is not None:
            answer = change(payload, answer)
        envelope = {
            "sent": True,
            "ok": True,
            "model": "claude-sonnet-4-6",
            "response_id": f"msg_role_smoke_{next(IDS)}",
            "stop_reason": "tool_use",
            "usage": {"input_tokens": len(request["user"]) // 4, "output_tokens": 300},
            "text": role_schemas.answer_text(answer),
            "answer_source": "tool_use.input",
            "tool_use_count": 1,
            "via": "1.1 kong/fake",
            "request_id": "req-fake",
            "latency_ms": 5,
            "sdk": "anthropic-python 0.0",
            "mock": False,  # recorded as a live receipt so the report's live checks apply
            "sent_settings": {
                "model": "claude-sonnet-4-6",
                "max_tokens": 2048,
                "extra_body": {"temperature": 0.0},
                "tools": [tool],
                "tool_choice": {"type": "tool", "name": tool["name"]},
            },
            "sent_keys": [
                "extra_body",
                "max_tokens",
                "messages",
                "model",
                "system",
                "tool_choice",
                "tools",
            ],
            "content_types": ["tool_use"],
        }
        return {"exit_code": 0, "stdout": json.dumps(envelope), "stderr": ""}


def probe_rows():
    return [
        {
            "probe": p,
            "observed": "connected" if p == "relay_port_tcp" else "timeout",
            "expected": "reachable" if p == "relay_port_tcp" else "blocked",
            "passed": True,
            "target": "t",
            "port": 1,
        }
        for p in relay_smoke_report.REQUIRED_PROBES
    ]


def record_offline_run(tmp_path, inputs, override=None):
    """Drive run_smoke through the real client -> relay -> KongRoles path offline and write
    the evidence files the workflow would (launch.json, container JSON, key scan, net, audit)."""
    python = tmp_path / "python.exe"
    python.write_text("")
    worker = Worker(inputs["goal"], override)
    caps = smoke.RELAY_CAPS
    roles = KongRoles(
        worker,
        tmp_path / "ctx",
        budget=smoke.BUDGET_USD,
        max_tokens=int(caps["RELAY_MAX_TOKENS"]),
        temperature=0.0,
        prices=PINNED_PRICES,
        role_max_tokens=dict(LIVE_ROLE_MAX_TOKENS),
        session_source="response_id",
        data_policy_ack=True,
        kit_python=python,
    )
    relay_dir, out, work = tmp_path / "relay", tmp_path / "out", tmp_path / "work"
    for d in (relay_dir, out, work):
        d.mkdir()
    audit = host.AuditLog(relay_dir / "relay-audit.jsonl")
    guard = host.SpendGuard(
        budget_usd=float(caps["RELAY_BUDGET"]),
        max_requests=int(caps["RELAY_MAX_REQUESTS"]),
        max_total_tokens=int(caps["RELAY_MAX_TOTAL_TOKENS"]),
    )
    relay = host.Relay(
        roles,
        run_id=RUN,
        token=TOKEN,
        allowed_roles=caps["RELAY_ROLES"].split(),
        audit=audit,
        guard=guard,
    )
    (server,) = host.serve(relay, ["127.0.0.1"], 0)
    try:
        client = RelayRoles(
            f"http://127.0.0.1:{server.server_address[1]}",
            TOKEN,
            RUN,
            budget=smoke.BUDGET_USD,
            timeout=smoke.CALL_TIMEOUT,
        )
        container = smoke.run_smoke(client, work, probes=probe_rows, inputs=inputs)
    finally:
        server.shutdown()
        server.server_close()
    if relay.asks_done.is_set():  # kong_relay_host.main writes the stop line only once EXHAUSTED
        audit.write("relay_stop", run_id=RUN, reason=f"spend cap reached: {guard.exhausted_by}")
    container = {
        "schema": smoke.SCHEMA,
        "uid": 1000,
        "payload_classification": "synthetic",
        "credential_like_env_names": [],
        "credential_like_files": [],
        **container,
    }
    launch_rec = {
        "client": "relay_role_smoke",
        "run_conditions": {"never_privileged": True, "relay_only_network": True},
        "env_names_passed": sorted(relay_smoke_report.EXPECTED_ENV_NAMES),
        "smoke_parsed": True,
    }
    (out / "launch.json").write_text(json.dumps(launch_rec))
    (out / report.CONTAINER_JSON).write_text(json.dumps(container))
    (out / "key-scan.json").write_text(json.dumps({"key_absent": True}))
    (relay_dir / "net.json").write_text(json.dumps({"internal": True}))
    return out, relay_dir, worker


def run_report(out, relay_dir):
    code = report.main(["x", str(out), str(relay_dir)])
    return code, json.loads((out / "RELAY-ROLE-SMOKE.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def recorded(patched, inputs, tmp_path_factory):
    mp = pytest.MonkeyPatch()
    mp.delenv("GITHUB_STEP_SUMMARY", raising=False)
    try:
        yield record_offline_run(tmp_path_factory.mktemp("good"), inputs)
    finally:
        mp.undo()


def test_report_passes_on_recorded_evidence(recorded):
    out, relay_dir, worker = recorded
    code, result = run_report(out, relay_dir)
    failed = [n for n, c in result["checks"].items() if not c["passed"]]
    assert failed == [] and code == 0 and result["passed"] is True
    assert len(worker.calls) == len(smoke.CALLS)  # the cap probe never reached the worker
    by_call = {s["call"]: s for s in result["calls"]}
    assert list(by_call) == [c["call"] for c in smoke.CALLS]
    for s in by_call.values():
        assert s["outcome"] == "SUCCESS" and s["controller_accepts"] is True, s["call"]
        assert s["controller_outcome"] == "ACCEPTED" and s["schema_errors"] == [], s["call"]
        assert s["structured_output"] == "forced_tool" and s["answer_source"] == "tool_use.input"
        assert s["answer_tool_hash_match"] is True and s["temperature_sent"] == 0
        assert s["model_observed"] == "claude-sonnet-4-6" and s["estimated_usd"] > 0
    # planner and repairer get their larger live output ceiling
    assert by_call["planner"]["max_tokens"] == by_call["repairer"]["max_tokens"] == 4096
    assert by_call["reviewer"]["max_tokens"] == 2048
    assert result["estimated_usd_total"] == pytest.approx(
        sum(s["estimated_usd"] for s in by_call.values())
    )
    assert result["warnings"] == []
    container = json.loads((out / report.CONTAINER_JSON).read_text())
    for record in container["calls"]:
        assert record["controller"]["payload_identical"] is True, record["call"]
    assert result["checks"]["relay_stopped_itself"]["observed"] == [
        "spend cap reached: max_requests"
    ]


def test_report_fails_when_the_controller_refuses_the_mixed_reviewer_shape(
    patched, inputs, tmp_path, monkeypatch
):
    """Run 38906458002: the forced tool offered legacy + frozen keys as one union, the model
    mixed them, the relay said SUCCESS. Only the controller refuses it, and now the smoke."""
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)

    def mixed(payload, good):
        return {**good, "reason": "The plan matches the registered profile."}

    out, relay_dir, _ = record_offline_run(tmp_path, inputs, {("reviewer", 1): mixed})
    code, result = run_report(out, relay_dir)
    failed = [n for n, c in result["checks"].items() if not c["passed"]]
    assert code == 1 and failed == ["reviewer:controller_accepts"]
    (s,) = [s for s in result["calls"] if s["call"] == "reviewer"]
    assert set(s["answer"]) == MIXED_REVIEWER_KEYS
    assert s["outcome"] == "SUCCESS"  # what the relay reported
    assert s["controller_outcome"] == "REFUSED"
    assert s["controller_refusal"]["text"] == (
        "review: expected exactly ['binding_hash', 'findings', 'verdict']"
    )
    assert s["schema_errors"] == []  # the union schema admits it: only the controller can judge
    md = (out / "RELAY-ROLE-SMOKE.md").read_text()
    assert "review: expected exactly" in md and "Relay role smoke: FAIL" in md


# ------------------------------------------------------------------ judgements, one by one


def _good(call, payloads, inputs):
    return good_answer(call["role"], payloads[call["call"]], inputs["goal"])


CALL = {c["call"]: c for c in smoke.CALLS}


@pytest.mark.parametrize(
    "name, change, gate, text",
    [
        (
            "reviewer",
            lambda a: {**a, "reason": "r"},
            None,
            "review: expected exactly ['binding_hash', 'findings', 'verdict']",
        ),
        (
            "reviewer",
            lambda a: {**a, "binding_hash": "0" * 64},
            "review_binding",
            "review binding or findings invalid",
        ),
        (
            "planner",
            lambda a: {**a, "plan": {**a["plan"], "final": ["onset"]}},
            "plan_validation",
            "plan changes constraints, tool arguments, order or declared final artifacts",
        ),
        (
            "planner",
            lambda a: {**a, "claims": a["plan_claims"]},
            "plan_validation",
            "review plan envelope: unexpected or missing fields",
        ),
        (
            "repairer",
            lambda a: {**a, "rationale": "r"},
            "plan_validation",
            "repair: expected exactly ['plan', 'plan_claims', 'response', 'schema']",
        ),
        (
            "compiler",
            lambda a: {**a, "title": "Eastern Kenya"},
            "goal_check",
            "compiled goal changes source constraints: ['title']",
        ),
        (
            "evidence_checker",
            lambda a: {**a, "verdicts": a["verdicts"][:1]},
            "claim_evidence",
            "evidence_checker did not rule every judged claim",
        ),
        (
            "evidence_checker",
            lambda a: {**a, "plan_hash": "sha256:" + a["plan_hash"]},
            "claim_evidence",
            "evidence_checker output is bound to a stale or different plan_hash",
        ),
        (
            "verifier",
            lambda a: {k: v for k, v in a.items() if k != "scope"},
            "verifier",
            "verifier: expected verdict, binding_hash, scope and optional sub_reason",
        ),
        ("shadow_policy", lambda a: {**a, "score": 1.5}, "shadow", "score outside [0, 1]"),
    ],
)
def test_the_controller_refuses_and_says_why(captured, inputs, name, change, gate, text):
    work, payloads, contexts = captured
    call = CALL[name]
    answer = change(_good(call, payloads, inputs))
    record = smoke.judge(call, answer, payloads[name], work, inputs, contexts["reject_then_repair"])
    assert record["controller_accepts"] is False and record["controller_outcome"] in (
        "REFUSED",
        "REJECTED",
    )
    assert record["controller_refusal"]["text"] == text
    assert record["controller_refusal"]["gate"] == gate
    assert record["payload_identical"] is True


def test_verifier_copy_failure_is_refused_on_the_controllers_own_record(captured, inputs):
    work, payloads, contexts = captured
    call = CALL["verifier"]
    answer = {**_good(call, payloads, inputs), "binding_hash": "0" * 64}
    record = smoke.judge(
        call, answer, payloads["verifier"], work, inputs, contexts["reject_then_repair"]
    )
    assert record["controller_accepts"] is False
    assert record["verifier_record"]["binding_valid"] is False
    assert record["verifier_record"]["scope_valid"] is True


@pytest.mark.parametrize(
    "name, answer, outcome",
    [
        ("planner", {"needs_info": "Which season?"}, "NEEDS_INFO"),
        ("repairer", {"needs_info": "Which end date?", "response": "r"}, "NEEDS_INFO"),
    ],
)
def test_needs_info_is_the_controllers_stop_not_a_refusal(captured, inputs, name, answer, outcome):
    work, payloads, _ = captured
    record = smoke.judge(CALL[name], answer, payloads[name], work, inputs)
    assert record["controller_accepts"] is True and record["controller_outcome"] == outcome
    assert record["needs_info_question"] == answer["needs_info"]


@pytest.mark.parametrize(
    "name, change, judgment",
    [
        # judgements are the model's; the controller accepts the shape and moves on
        (
            "reviewer",
            lambda a: {
                **a,
                "verdict": "REJECT",
                "findings": [{"id": "R1", "severity": "minor", "text": "t"}],
            },
            {"verdict": "REJECT", "findings": 1},
        ),
        (
            "reviewer",
            lambda a: {
                "verdict": "APPROVE",
                "approved_binding": {"plan_hash": "x", "source_hash": "y"},
            },
            None,
        ),
        ("planner", lambda a: {**a, "rationale": "kept unchanged"}, None),
        ("shadow_policy", lambda a: {**a, "action": "STOP", "score": 0.0}, None),
    ],
)
def test_accepted_shapes_and_judgements(captured, inputs, name, change, judgment):
    work, payloads, _ = captured
    call = CALL[name]
    answer = change(_good(call, payloads, inputs))
    record = smoke.judge(call, answer, payloads[name], work, inputs)
    if "approved_binding" in answer:  # a frozen-shape approval of a different binding is void
        assert record["controller_accepts"] is False
        assert record["controller_refusal"]["gate"] == "review_binding"
        return
    assert record["controller_accepts"] is True, record
    if judgment is not None:
        assert record["judgment"] == judgment
