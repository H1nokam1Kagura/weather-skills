"""Relay spend guard, pricing, session identity, key-env hygiene and the RelayTransport seam.

LIVE-WAVE-BRIEF.md sub-wave 1 (spend guard and pricing). Offline: the KongRoles worker is a
scripted fake and every relay is on 127.0.0.1. Nothing here reaches Kong.
"""

import importlib.util
import itertools
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from tools.workflow import recovery, relay_roles
from tools.workflow.catalog import child_environment
from tools.workflow.contracts import Refused
from tools.workflow.kong_roles import (
    COST_BASIS,
    LIVE_ROLE_MAX_TOKENS,
    PINNED_PRICES,
    KongRoles,
)
from tools.workflow.relay_roles import (
    DETERMINISTIC,
    TRANSIENT,
    RelayOutcomeUnknown,
    RelayRoles,
    classify_fault,
)

ROOT = Path(__file__).resolve().parents[3]
TOKEN = "s" * 43
RUN = "relay-run-guard"
ANSWER = {"verdict": "PASS", "binding_hash": "h", "scope": "data-replay"}
PAYLOAD = {"artifact_manifest": ["synthetic gate PASS"], "binding_hash": "synthetic"}
IDS = itertools.count(1)
CALL_USD = (1000 * 3 + 100 * 15) / 1e6


def load_host():
    spec = importlib.util.spec_from_file_location(
        "kong_relay_host_guard", ROOT / "relay" / "kong_relay_host.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


host = load_host()


def envelope(**changes):
    value = {
        "sent": True,
        "ok": True,
        "model": "claude-sonnet-4-6",
        "response_id": f"msg_guard_{next(IDS)}",
        "stop_reason": "tool_use",  # the forced answer tool (role_schemas)
        "usage": {"input_tokens": 1000, "output_tokens": 100},
        "text": json.dumps(ANSWER, sort_keys=True),  # role_schemas.answer_text
        "answer_source": "tool_use.input",
        "tool_use_count": 1,
        "content_types": ["tool_use"],
        "via": "1.1 kong/fake",
        "request_id": "req-fake",
        "latency_ms": 5,
        "sdk": "anthropic-python 0.0",
        "mock": True,
    }
    value.update(changes)
    return value


class Process:
    """KongRoles worker stand-in; every run() is a request that reached the 'gateway'."""

    def __init__(self, *outputs):
        self.outputs = list(outputs)
        self.calls = []

    def run(self, argv, cwd, env, stdin):
        self.calls.append(json.loads(stdin))
        out = self.outputs.pop(0)
        if isinstance(out, BaseException):
            raise out
        return {"exit_code": 0, "stdout": json.dumps(out), "stderr": ""}


def live_roles(tmp_path, process, budget=5):
    """KongRoles exactly as relay/kong_relay_host.build_roles configures it."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    python = tmp_path / "python.exe"
    python.write_text("")
    return KongRoles(
        process,
        tmp_path / "ctx",
        budget=budget,
        max_tokens=2048,
        temperature=0,
        prices=PINNED_PRICES,
        role_max_tokens=LIVE_ROLE_MAX_TOKENS,
        session_source="response_id",
        data_policy_ack=True,
        kit_python=python,
    )


def make_relay(tmp_path, process, *, budget=5, allowed=("verifier",), **caps):
    roles = live_roles(tmp_path, process, budget=budget)
    guard = host.SpendGuard(budget_usd=budget, **caps)
    audit = host.AuditLog(tmp_path / "audit.jsonl")
    relay = host.Relay(
        roles, run_id=RUN, token=TOKEN, allowed_roles=allowed, audit=audit, guard=guard
    )
    return relay


def ask(relay, role="verifier", payload=PAYLOAD):
    status, body = relay.ask(json.dumps({"role": role, "payload": payload}).encode(), "127.0.0.1")
    relay.responded()
    return status, body


def audit_lines(tmp_path):
    return [json.loads(x) for x in (tmp_path / "audit.jsonl").read_text().splitlines()]


@pytest.fixture
def served(tmp_path):
    started = []

    def start(relay):
        servers = host.serve(relay, ["127.0.0.1"], 0)
        started.extend(servers)
        return f"http://127.0.0.1:{servers[0].server_address[1]}"

    yield start
    for server in started:
        server.shutdown()
        server.server_close()


def client(url, **kwargs):
    kwargs.setdefault("budget", 5)
    return RelayRoles(url, TOKEN, RUN, **kwargs)


# ------------------------------------------------------------------ caps refuse before send


def test_token_cap_refuses_before_forwarding_and_maps_to_budget(tmp_path, served, monkeypatch):
    process = Process(envelope())
    relay = make_relay(tmp_path, process, max_total_tokens=500)  # below any verifier call
    order = []
    send = host.Handler._send
    monkeypatch.setattr(host.Handler, "_send", lambda h, *a: (send(h, *a), order.append("sent")))
    responded = relay.responded
    monkeypatch.setattr(relay, "responded", lambda: (order.append("responded"), responded()))
    agent = client(served(relay))
    with pytest.raises(Refused, match="relay spend cap") as caught:
        agent.ask("verifier", PAYLOAD)
    assert caught.value.gate == "budget" and agent.spent == 0  # nothing forwarded: released
    assert process.calls == [] and relay.guard.state == "EXHAUSTED"
    # The stop is signalled on the handler thread only AFTER the refusal was written, so the
    # client can hold the full refusal before the signal exists: wait for it, never poll once.
    assert relay.asks_done.wait(5)
    assert order == ["sent", "responded"]  # the refusal is on the wire before the stop
    lines = audit_lines(tmp_path)
    (cap,) = [x for x in lines if x["event"] == "spend_cap_reached"]
    assert cap["kind"] == "spend_cap_reached" and cap["cap"] == "max_total_tokens"
    assert cap["spend"]["requests"] == 0 and cap["spend"]["state"] == "EXHAUSTED"
    (refused,) = [x for x in lines if x["event"] == "ask"]
    assert refused["outcome"] == "REFUSED" and refused["sent_to_gateway"] is False
    assert refused["reason_code"] == "relay_spend_cap"
    assert refused["fault_class"] == DETERMINISTIC


def test_request_and_budget_caps_refuse_before_forwarding(tmp_path):
    process = Process(envelope(), envelope(), envelope())
    relay = make_relay(tmp_path / "a", process, max_requests=2)
    assert ask(relay)[0] == 200 and ask(relay)[0] == 200
    status, body = ask(relay)
    assert status == 402 and body["reason_code"] == "relay_spend_cap"
    assert body["sent_to_gateway"] is False and len(process.calls) == 2
    assert relay.guard.exhausted_by == "max_requests"

    poor = Process(envelope())
    relay = make_relay(tmp_path / "b", poor, budget=0.1)
    big = {"artifact_manifest": ["x" * 60_000]}  # its worst case alone is over $0.10
    worst = host.worst_case(relay.roles, "verifier", big)
    assert worst["usd"] > 0.1
    status, body = ask(relay, payload=big)
    assert body["reason_code"] == "relay_spend_cap" and poor.calls == []
    assert relay.guard.exhausted_by == "budget"


def test_a_call_after_the_request_cap_is_a_spend_cap_refusal_and_a_guard_abort(tmp_path, served):
    process = Process(envelope(), envelope(), envelope())
    relay = make_relay(tmp_path, process, max_requests=2)
    agent = client(served(relay))
    assert agent.ask("verifier", PAYLOAD) == ANSWER
    assert agent.ask("verifier", PAYLOAD) == ANSWER
    # The cap is full but not exceeded: the relay still serves, so the next call is answered.
    assert relay.guard.state == "OPEN" and not relay.asks_done.is_set()
    spent = agent.spent
    with pytest.raises(Refused, match="relay spend cap") as caught:
        agent.ask("verifier", PAYLOAD)  # never "unreachable before sending" (gate=verifier)
    assert caught.value.gate == "budget" and agent.spent == spent  # nothing forwarded
    assert len(process.calls) == 2 and relay.guard.exhausted_by == "max_requests"
    assert relay.asks_done.wait(5)  # EXHAUSTED: the relay stops after writing that refusal
    lines = audit_lines(tmp_path)
    (cap,) = [x for x in lines if x["event"] == "spend_cap_reached"]
    assert cap["cap"] == "max_requests" and cap["spend"]["requests"] == 2
    refused = [x for x in lines if x["event"] == "ask"][-1]
    assert refused["reason_code"] == "relay_spend_cap" and refused["sent_to_gateway"] is False
    usage = load_harness("relay_usage").usage_from_events(lines)  # what cases_report reads
    assert usage["label"] == "RELAY_GUARD_ABORT" and usage["cap_state"] == "SPEND_CAP_REACHED"
    assert usage["requests"] == 2 and usage["refused_before_send"] == 1


def load_harness(name):
    spec = importlib.util.spec_from_file_location(
        f"{name}_guard", ROOT / "container_harness" / f"{name}.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_smoke_cap_probe_exhausts_the_cli_relay_which_stops_itself(tmp_path, monkeypatch):
    """The relay-smoke shape end to end, minus docker: the real CLI relay (--max-requests 1)
    on 127.0.0.1, the container client, relay_smoke.cap_probe and the smoke report's checks."""
    process = Process(envelope())
    started = threading.Event()
    holder = {}

    def build_roles(args, kit_root=None, kit_python=None):
        return live_roles(tmp_path / "roles", process, budget=args.budget)

    def serve(relay, binds, port):
        servers = real_serve(relay, binds, port)
        holder.update(relay=relay, port=servers[0].server_address[1])
        started.set()
        return servers

    real_serve = host.serve
    for name, _ in host.CAP_ENV.values():
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(host, "build_roles", build_roles)
    monkeypatch.setattr(host, "serve", serve)
    monkeypatch.setattr(host, "client_paths", lambda client: (None, None))
    relay_dir = tmp_path / "relay"
    argv = ["--ack-not-sensitive", "--allow-role", "verifier", "--max-requests", "1"]
    argv += ["--run-seconds", "60", "--audit-log", str(relay_dir / "relay-audit.jsonl")]
    argv += ["--token-file", str(tmp_path / "t"), "--ready-file", str(tmp_path / "r.json")]
    main = threading.Thread(target=lambda: holder.update(rc=host.main(argv)), daemon=True)
    main.start()
    assert started.wait(10)
    relay = holder["relay"]
    agent = RelayRoles(f"http://127.0.0.1:{holder['port']}", relay._token, relay.run_id, budget=1)
    agent.ask("verifier", PAYLOAD)
    assert not relay.asks_done.is_set()  # the cap is full, not exceeded: the relay still serves
    probe = load_harness("relay_smoke").cap_probe(agent, Refused)
    assert probe["outcome"] == "REFUSED" and probe["gate"] == "budget"
    assert (probe["requests"], probe["max_requests"]) == (1, 1) and len(process.calls) == 1
    main.join(10)  # the relay stopped ITSELF: EXHAUSTED by the probe, well before run_seconds
    assert not main.is_alive() and holder["rc"] == 0
    lines = [json.loads(x) for x in (relay_dir / "relay-audit.jsonl").read_text().splitlines()]
    (stop,) = [x for x in lines if x["event"] == "relay_stop"]
    assert stop["reason"] == "spend cap reached: max_requests" and stop["requests"] == 1
    out = tmp_path / "out"
    out.mkdir()
    smoke = {"outcome": "SUCCESS", "cap_probe": probe}
    (out / "relay-smoke.container.json").write_text(json.dumps(smoke))
    load_harness("relay_smoke_report").main(["x", str(out), str(relay_dir)])
    checks = json.loads((out / "RELAY-SMOKE.json").read_text())["checks"]
    for name in (
        "audit_one_successful_ask",
        "request_cap_refused_before_forwarding",
        "relay_stopped_itself",
        "audit_redacted_fields_only",
    ):
        assert checks[name]["passed"], (name, checks[name])
    usage = load_harness("relay_usage").usage_from_events(lines)
    assert usage["label"] == "RELAY_GUARD_ABORT" and usage["cap_state"] == "SPEND_CAP_REACHED"


def test_exhausted_is_sticky_and_reported_once(tmp_path):
    process = Process(envelope(), envelope())
    relay = make_relay(tmp_path, process, max_total_tokens=50_000)
    big = {"artifact_manifest": ["x" * 60_000]}  # worst case > 50K tokens
    assert ask(relay, payload=big)[1]["reason_code"] == "relay_spend_cap"
    for _ in range(2):  # a small call that would fit is still refused: EXHAUSTED is sticky
        assert ask(relay)[1]["reason_code"] == "relay_spend_cap"
    assert process.calls == []
    events = [x["event"] for x in audit_lines(tmp_path)]
    assert events.count("spend_cap_reached") == 1 and events.count("ask") == 3
    assert relay.guard.snapshot()["refused_by_cap"] == 3


# ------------------------------------------------------------------ charging


def test_unknown_outcome_is_charged_at_its_worst_case(tmp_path):
    error = {"status": 503, "via": "1.1 kong/fake", "request_id": "req-503", "hint": "upstream"}
    process = Process(envelope(ok=None, error=error, error_type="InternalServerError"))
    relay = make_relay(tmp_path, process)
    worst = host.worst_case(relay.roles, "verifier", PAYLOAD)
    status, body = ask(relay)
    assert status == 502 and body["outcome"] == "UNKNOWN"
    spend = relay.guard.snapshot()
    assert spend["requests"] == 1 and spend["worst_case_charges"] == 1
    assert spend["charged_tokens"] == worst["tokens"]
    assert spend["estimated_usd"] == pytest.approx(worst["usd"], abs=1e-6)
    assert spend["input_tokens"] == spend["output_tokens"] == 0  # nothing observed
    (line,) = [x for x in audit_lines(tmp_path) if x["event"] == "ask"]
    assert line["gateway_status"] == 503 and line["fault_class"] == TRANSIENT
    assert line["fault_kind"] == "http_5xx" and line["charge"]["basis"] == "worst_case"


def test_worker_crash_with_no_gateway_response_is_transient_and_worst_case(tmp_path):
    process = Process(TimeoutError("killed"))
    relay = make_relay(tmp_path, process)
    status, body = ask(relay)
    assert status == 502 and body["evidence"]["status"] is None
    assert body["evidence"]["fault_class"] == TRANSIENT
    assert relay.guard.snapshot()["worst_case_charges"] == 1


def test_pre_send_refusal_is_not_charged_or_counted(tmp_path):
    process = Process({"sent": False, "error_type": "ValueError", "error": {}})
    relay = make_relay(tmp_path, process)
    status, body = ask(relay)
    assert status == 422 and body["sent_to_gateway"] is False
    assert body["reason_code"] == "kong_pre_send"
    assert relay.guard.snapshot()["requests"] == 0
    assert relay.guard.snapshot()["estimated_usd"] == 0


def test_a_twelve_call_run_passes_within_caps(tmp_path, served):
    roles_order = ["compiler", "planner", "reviewer", "repairer", "reviewer", "verifier"] * 2
    process = Process(*[envelope() for _ in roles_order])
    relay = make_relay(
        tmp_path,
        process,
        allowed=tuple(sorted(set(roles_order))),
        max_requests=24,
        max_total_tokens=1_000_000,
    )
    agent = client(served(relay), model="claude-sonnet-4-6")
    for role in roles_order:
        assert agent.ask(role, PAYLOAD) == ANSWER
    assert len(agent.receipts) == 12 and agent.spent == pytest.approx(12 * CALL_USD)
    spend = relay.guard.snapshot()
    assert spend["state"] == "OPEN" and spend["requests"] == 12
    assert spend["input_tokens"] == 12_000 and spend["output_tokens"] == 1_200
    assert spend["estimated_usd"] == pytest.approx(12 * CALL_USD)
    sent = {(c["max_tokens"], c["temperature"]) for c in process.calls}
    assert [c["max_tokens"] for c in process.calls[:4]] == [2048, 8192, 2048, 8192]
    assert sent == {(2048, 0), (8192, 0)}
    assert {r["cost_basis"] for r in agent.receipts} == {COST_BASIS}


def test_cap_defaults_come_from_the_relay_env_names():
    assert host.cap_defaults({}) == {
        "max_requests": 24,
        "max_total_tokens": 1_000_000,
        "budget": 5.0,
    }
    env = {"RELAY_MAX_REQUESTS": "8", "RELAY_MAX_TOTAL_TOKENS": "1234", "RELAY_BUDGET": "0.5"}
    assert host.cap_defaults(env) == {"max_requests": 8, "max_total_tokens": 1234, "budget": 0.5}
    with pytest.raises(ValueError, match="RELAY_BUDGET"):
        host.cap_defaults({"RELAY_BUDGET": "lots"})
    for bad in ({"budget_usd": 0}, {"budget_usd": 1, "max_requests": 0}):
        with pytest.raises(ValueError):
            host.SpendGuard(**bad)


def test_relay_ci_passes_every_cap_from_env():
    script = (ROOT / "container_harness" / "relay_ci.sh").read_text(encoding="utf-8")
    for flag, name in (
        ("--max-requests", "RELAY_MAX_REQUESTS"),
        ("--max-total-tokens", "RELAY_MAX_TOTAL_TOKENS"),
        ("--budget", "RELAY_BUDGET"),
    ):
        assert f'{flag} "${{{name}:-' in script
    assert "--budget 1 " not in script


def test_body_cap_is_88000_under_the_hard_limit_and_the_kit_known_good():
    assert relay_roles.DEFAULT_MAX_BODY == 88_000 < host.HARD_MAX_BODY == 90_000
    kit = (ROOT / "relay" / "runner_kit" / "scripts" / "kong_client.py").read_text()
    assert "KNOWN_GOOD_BODY_BYTES = 97_600" in kit


# ------------------------------------------------------------------ session identity


def test_session_id_is_the_provider_response_id(tmp_path, served):
    process = Process(envelope(response_id="msg_observed_1"), envelope(response_id=None))
    relay = make_relay(tmp_path, process)
    agent = client(served(relay))
    agent.ask("verifier", PAYLOAD)
    (receipt,) = agent.receipts
    assert receipt["session_id"] == receipt["response_id"] == "msg_observed_1"
    with pytest.raises(Refused, match="no response id"):
        agent.ask("verifier", PAYLOAD)  # refused at the host; billed estimate charged
    assert len(agent.receipts) == 1 and agent.spent == pytest.approx(2 * CALL_USD)


def test_client_refuses_a_minted_session_id(fake_success_relay):
    url = fake_success_relay(session_id="uuid-minted-before-the-call", response_id="msg_1")
    with pytest.raises(Refused, match="not the observed provider response_id"):
        client(url).ask("verifier", PAYLOAD)
    url = fake_success_relay(session_id=None, response_id=None)
    with pytest.raises(Refused, match="response_id"):
        client(url).ask("verifier", PAYLOAD)


@pytest.fixture
def fake_success_relay():
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from threading import Thread

    from tools.workflow.contracts import digest

    servers = []

    def make(**changes):
        receipt = {key: None for key in relay_roles.KONG_RECEIPT_FIELDS}
        receipt.update(
            role="verifier",
            input_hash=digest(PAYLOAD),
            output_hash=digest(ANSWER),
            model_requested="claude-sonnet-4-6",
            model_observed="claude-sonnet-4-6",
            transport="kong-native-anthropic",
            mode="live",
            relay_id="r-1",
            transport_encoding=None,  # PAYLOAD repeats nothing: sent plain
            transport_bytes={},
            **changes,
        )
        data = json.dumps(
            {"outcome": "SUCCESS", "relay_id": "r-1", "value": ANSWER, "receipt": receipt}
        ).encode()

        class H(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):  # noqa: N802
                self.rfile.read(int(self.headers["Content-Length"]))
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        return f"http://127.0.0.1:{server.server_address[1]}"

    yield make
    for server in servers:
        server.shutdown()
        server.server_close()


# ------------------------------------------------------------------ key env hygiene


def test_from_env_removes_the_relay_token_from_the_child_environment(monkeypatch, tmp_path):
    values = {
        "SIMKIT_RELAY_URL": "http://127.0.0.1:9",
        "SIMKIT_RELAY_TOKEN": TOKEN,
        "SIMKIT_RELAY_RUN_ID": RUN,
        "SIMKIT_RELAY_MODEL": "claude-sonnet-4-6",
        "SIMKIT_RELAY_EXTRA": "also-dropped",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(relay_roles, "_CONSUMED", {})
    agent = RelayRoles.from_env()
    assert (agent.run_id, agent.model) == (RUN, "claude-sonnet-4-6")
    assert not [name for name in os.environ if name.startswith("SIMKIT_RELAY_")]
    env = child_environment(tmp_path / "scratch")
    assert TOKEN not in json.dumps(env) and not any(k.startswith("SIMKIT_RELAY_") for k in env)
    probe = "import os, json; print(json.dumps(dict(os.environ)))"
    child = subprocess.run(
        [sys.executable, "-I", "-c", probe], capture_output=True, text=True, env=env, check=True
    )
    assert TOKEN not in child.stdout
    again = RelayRoles.from_env()  # a later factory call in the same process still works
    assert again.run_id == RUN and TOKEN not in repr(again)


def test_from_env_consumes_an_explicit_mapping_too():
    env = {
        "SIMKIT_RELAY_URL": "http://h:1",
        "SIMKIT_RELAY_TOKEN": TOKEN,
        "SIMKIT_RELAY_RUN_ID": RUN,
    }
    RelayRoles.from_env(env)
    assert env == {}


# ------------------------------------------------------------------ transport retry parity


class Seam:
    """A processes stand-in with the serial contract: each script item is 'ok', 'reset',
    'reset_after_write', 'abort' or 'refused'."""

    def __init__(self, *script):
        self.script = list(script)
        self.serial = 0
        self.calls = 0

    def run(self):
        self.calls += 1
        item = self.script.pop(0)
        if item == "ok":
            self.serial += 1
            return "answer"
        if item == "reset":
            raise ConnectionResetError("reset before write")
        if item == "abort":
            raise ConnectionAbortedError("aborted before write")
        if item == "reset_after_write":
            self.serial += 1
            raise ConnectionResetError("reset after write")
        raise ConnectionRefusedError("refused")


class Owner:
    def __init__(self, seam, recorder):
        self.processes = seam
        self.recorded = []
        if recorder is not None:

            def record(role, exc, invocation_id):
                self.recorded.append((role, type(exc).__name__, invocation_id))
                return recorder

            self.transport_retry_recorder = record


def outcome(call_fn, script, recorder):
    seam = Seam(*script)
    owner = Owner(seam, recorder)
    try:
        result = call_fn(owner, "planner", seam.run, "inv-1")
    except Exception as exc:  # noqa: BLE001 - compared across implementations
        result = ("raised", type(exc).__name__)
    return result, seam.calls, seam.serial, owner.recorded


@pytest.mark.parametrize(
    ("script", "recorder"),
    [
        (["ok"], True),
        (["reset", "ok"], True),
        (["abort", "ok"], True),
        (["reset", "ok"], False),
        (["reset", "ok"], None),
        (["reset", "reset"], True),
        (["reset_after_write", "ok"], True),
        (["refused", "ok"], True),
    ],
)
def test_transport_retry_matches_recovery_transport_call(script, recorder):
    assert outcome(relay_roles._transport_call, script, recorder) == outcome(
        recovery.transport_call, script, recorder
    )


class FlakyTransport(relay_roles.RelayTransport):
    """The real seam, with one pre-write reset injected before the first connect."""

    def __init__(self, inner, faults):
        super().__init__(inner.host, inner.port, inner.timeout)
        self.faults = faults

    def run(self, *args):
        if self.faults:
            self.faults -= 1
            raise ConnectionResetError("injected before any byte was written")
        return super().run(*args)


def test_relay_roles_retries_one_pre_write_reset_through_the_recorder(tmp_path, served):
    relay = make_relay(tmp_path, Process(envelope()))
    agent = client(served(relay))
    agent.processes = FlakyTransport(agent.processes, faults=1)
    seen = []
    agent.transport_retry_recorder = lambda role, exc, inv: seen.append((role, inv)) or True
    assert agent.ask("verifier", PAYLOAD) == ANSWER
    assert len(seen) == 1 and agent.processes.serial == 1  # one exchange wrote bytes

    other = client(agent_url := f"http://127.0.0.1:{agent.port}")
    other.processes = FlakyTransport(other.processes, faults=1)  # no recorder: not retried
    with pytest.raises(Refused, match="unreachable before sending") as caught:
        other.ask("verifier", PAYLOAD)
    assert caught.value.gate == "verifier" and other.spent == 0 and agent_url


# ------------------------------------------------------------------ fault labels


@pytest.mark.parametrize(
    ("status", "error_type", "kind", "expected"),
    [
        (429, "RateLimitError", None, (TRANSIENT, "http_429")),
        (500, None, None, (TRANSIENT, "http_5xx")),
        (504, None, None, (TRANSIENT, "http_5xx")),
        (None, "APITimeoutError", None, (TRANSIENT, "timeout")),
        (None, "ConnectionResetError", None, (TRANSIENT, "connection_reset")),
        (None, None, None, (TRANSIENT, "no_gateway_response")),
        (400, "BadRequestError", None, (DETERMINISTIC, "http_4xx")),
        (401, None, None, (DETERMINISTIC, "http_4xx")),
        (None, None, "relay_spend_cap", (DETERMINISTIC, "relay_spend_cap")),
        (None, None, "body_cap", (DETERMINISTIC, "body_cap")),
    ],
)
def test_fault_classification_follows_the_rerun_ruling(status, error_type, kind, expected):
    fault = classify_fault(status, error_type, kind)
    assert (fault["fault_class"], fault["fault_kind"]) == expected


def test_client_unknown_records_carry_fault_labels(tmp_path, served):
    error = {"status": 429, "via": "1.1 kong/fake", "request_id": "r-429", "hint": "busy"}
    process = Process(envelope(ok=None, error=error, error_type="RateLimitError", retry_after="9"))
    relay = make_relay(tmp_path, process)
    agent = client(served(relay))
    with pytest.raises(RelayOutcomeUnknown) as caught:
        agent.ask("verifier", PAYLOAD)
    evidence = caught.value.evidence
    assert evidence["status"] == 429 and evidence["retry_after"] == 9.0
    assert (evidence["fault_class"], evidence["fault_kind"]) == (TRANSIENT, "http_429")
    (line,) = [x for x in audit_lines(tmp_path) if x["event"] == "ask"]
    assert line["gateway_status"] == 429 and line["retry_after"] == 9.0


def test_relay_stop_audit_fields_are_in_the_snapshot(tmp_path):
    relay = make_relay(tmp_path, Process(envelope()))
    ask(relay)
    spend = relay.guard.snapshot()
    for key in ("requests", "input_tokens", "output_tokens", "estimated_usd", "state", "caps"):
        assert key in spend
    assert spend["cost_basis"] == COST_BASIS
    forbidden = {"payload", "value", "text", "token", "answer", "key", "system", "user"}
    assert not forbidden & {k for line in audit_lines(tmp_path) for k in line}


def test_relay_worker_sdk_timeout_outlasts_a_long_planner_answer():
    """The kit's default 60 s SDK timeout ended planner answers near 4k tokens (~55-69 tok/s
    in the live audit logs: truncations at 59.4/59.8 s, APITimeoutError at 61/62 s). The relay
    sets 170 s (8192 tokens down to ~48 tok/s): below the controller's relay client timeout
    (RelayRoles, 180 s; D4-REVIEW-LW6-BEH B-LW6-1 / F-LW6-1) and below the 240 s per-call
    timeout that relay_ci.sh passes as --call-seconds and that is the relay CLI default."""
    from types import SimpleNamespace

    from tools.workflow.kong_roles import LIVE_ROLE_MAX_TOKENS

    args = SimpleNamespace(
        call_seconds=240,
        run_seconds=1500,
        budget=1.0,
        model="claude-sonnet-4-6",
        max_tokens=2048,
        temperature=0.0,
        role_max_tokens=dict(LIVE_ROLE_MAX_TOKENS),
    )
    roles = host.build_roles(args)
    assert roles.worker_env["KONG_SDK_MAX_RETRIES"] == "0"
    timeout = float(roles.worker_env["KONG_TIMEOUT_SECONDS"])
    assert timeout == 170 and timeout < args.call_seconds
    import inspect

    client_timeout = inspect.signature(relay_roles.RelayRoles).parameters["timeout"].default
    assert timeout < client_timeout < args.call_seconds
    ci = (ROOT / "container_harness" / "relay_ci.sh").read_text(encoding="utf-8")
    assert "--call-seconds 240" in ci
    parser_default = host.main.__code__.co_consts  # the CLI default is a literal in main
    assert 240 in parser_default
