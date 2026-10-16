"""Offline tests for the container-side relay client and the host-side Kong relay.

Two kinds of relay stand in for the live one: a scripted fake HTTP relay (client edge cases)
and the real host relay serving a KongRoles whose worker process is faked (end to end, no
network beyond loopback). Nothing here reaches Kong.
"""

import ast
import importlib.util
import json
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from tools.workflow import relay_roles, runner
from tools.workflow.contracts import Refused, digest
from tools.workflow.kong_roles import KIT_PYTHON, KIT_ROOT, KongRoles
from tools.workflow.processes import Processes
from tools.workflow.relay_roles import (
    KONG_RECEIPT_FIELDS,
    RECEIPT_FIELDS,
    RelayOutcomeUnknown,
    RelayRoles,
)

ROOT = Path(__file__).resolve().parents[3]
TOKEN = "t" * 43
RUN = "relay-run-test"
ANSWER = {"verdict": "PASS", "binding_hash": "h", "scope": "data-replay"}
PAYLOAD = {"artifact_manifest": ["synthetic gate PASS"], "binding_hash": "synthetic"}
CITED = {
    "claude-sonnet-4-6": {"input_per_mtok": 3.0, "output_per_mtok": 15.0, "source": "test fixture"}
}


def load_host():
    spec = importlib.util.spec_from_file_location(
        "kong_relay_host", ROOT / "relay" / "kong_relay_host.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


host = load_host()


# ------------------------------------------------------------------ fakes


def envelope(**changes):
    value = {
        "sent": True,
        "ok": True,
        "model": "claude-sonnet-4-6",
        "response_id": "msg_fake",
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 1000, "output_tokens": 100},
        "text": json.dumps(ANSWER),
        "via": "1.1 kong/fake",
        "request_id": "req-fake",
        "latency_ms": 12,
        "sdk": "anthropic-python 0.0",
        "mock": True,
    }
    value.update(changes)
    return value


class Process:
    """KongRoles worker stand-in: returns scripted worker envelopes."""

    def __init__(self, *outputs, exit_code=0, delay=0.0):
        self.outputs = list(outputs)
        self.exit_code = exit_code
        self.delay = delay
        self.calls = []

    def run(self, argv, cwd, env, stdin):
        self.calls.append(json.loads(stdin))
        time.sleep(self.delay)
        out = self.outputs.pop(0)
        stdout = out if isinstance(out, str) else json.dumps(out)
        return {"exit_code": self.exit_code, "stdout": stdout, "stderr": ""}


def kong_roles(tmp_path, process, **kwargs):
    python = tmp_path / "python.exe"
    python.write_text("")
    kwargs.setdefault("prices", CITED)
    kwargs.setdefault("budget", 1)
    return KongRoles(process, tmp_path / "ctx", data_policy_ack=True, kit_python=python, **kwargs)


@pytest.fixture
def relay_host(tmp_path):
    """Real host relay on 127.0.0.1 around a KongRoles with a faked worker."""
    started = []

    def start(*outputs, allowed=("verifier",), max_body=relay_roles.DEFAULT_MAX_BODY, **kw):
        process = Process(*outputs, delay=kw.pop("delay", 0.0))
        roles = kong_roles(tmp_path, process, **kw)
        audit = host.AuditLog(tmp_path / "audit.jsonl")
        relay = host.Relay(
            roles, run_id=RUN, token=TOKEN, allowed_roles=allowed, audit=audit, max_body=max_body
        )
        servers = host.serve(relay, ["127.0.0.1"], 0)
        started.extend(servers)
        url = f"http://127.0.0.1:{servers[0].server_address[1]}"
        return url, relay, process

    yield start
    for server in started:
        server.shutdown()
        server.server_close()


def audit_lines(tmp_path):
    return [json.loads(x) for x in (tmp_path / "audit.jsonl").read_text().splitlines()]


def client(url, **kwargs):
    kwargs.setdefault("budget", 1)
    return RelayRoles(url, kwargs.pop("token", TOKEN), kwargs.pop("run_id", RUN), **kwargs)


class FakeRelay:
    """Scripted HTTP relay. Each script item is (status, body) or a behaviour string."""

    def __init__(self, *script):
        self.script = list(script)
        self.requests = []
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):  # noqa: N802
                length = int(self.headers["Content-Length"])
                fake.requests.append(
                    {"headers": dict(self.headers), "body": json.loads(self.rfile.read(length))}
                )
                item = fake.script.pop(0)
                if item == "drop":
                    self.close_connection = True
                    self.connection.shutdown(socket.SHUT_RDWR)
                    return
                if item == "stall":
                    time.sleep(1.5)
                    return
                status, body = item
                data = body if isinstance(body, bytes) else json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def fake_relay():
    made = []

    def make(*script):
        made.append(FakeRelay(*script))
        return made[-1]

    yield make
    for relay in made:
        relay.close()


def success_envelope(payload=PAYLOAD, value=ANSWER, **receipt_changes):
    receipt = {key: None for key in KONG_RECEIPT_FIELDS}
    receipt.update(
        role="verifier",
        invocation_id="inv",
        session_id="sess-1",
        input_hash=digest(payload),
        output_hash=digest(value),
        cost_usd=None,
        model_requested="claude-sonnet-4-6",
        model_observed="claude-sonnet-4-6",
        transport="kong-native-anthropic",
        mode="live",
        relay_id="r-1",
    )
    receipt.update(receipt_changes)
    return 200, {"outcome": "SUCCESS", "relay_id": "r-1", "value": value, "receipt": receipt}


def closed_port():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


# ------------------------------------------------------------------ parity pins


def test_receipt_fields_match_a_real_kongroles_receipt(tmp_path):
    roles = kong_roles(tmp_path, Process(envelope()))
    roles.ask("verifier", PAYLOAD)
    assert set(roles.receipts[0]) == KONG_RECEIPT_FIELDS
    assert RECEIPT_FIELDS == KONG_RECEIPT_FIELDS | {"relay_id"}


def test_role_gates_match_the_runner_call_site_mapping():
    assert relay_roles.ROLE_GATES == runner.ROLE_GATES


def test_client_module_is_stdlib_plus_contracts_only():
    tree = ast.parse(Path(relay_roles.__file__).read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            imported.add("." * node.level + module if node.level else module.split(".")[0])
    local = {name for name in imported if name.startswith(".")}
    assert local == {".contracts"}
    assert all(name in sys.stdlib_module_names for name in imported - local)


# ------------------------------------------------------------------ end to end (real host)


def test_success_end_to_end_returns_kong_receipt_plus_relay_id(tmp_path, relay_host):
    url, relay, process = relay_host(envelope())
    agent = client(url, model="claude-sonnet-4-6")
    assert agent.health()["allowed_roles"] == ["verifier"]
    assert agent.ask("verifier", PAYLOAD) == ANSWER
    (receipt,) = agent.receipts
    host_receipt = relay.roles.receipts[0]
    assert {k: v for k, v in receipt.items() if k != "relay_id"} == host_receipt
    assert receipt["input_hash"] == digest(PAYLOAD) and receipt["output_hash"] == digest(ANSWER)
    assert agent.sessions == {receipt["session_id"]}
    assert agent.spent == pytest.approx(receipt["cost_usd"]) and receipt["cost_usd"] > 0
    assert len(process.calls) == 1
    lines = audit_lines(tmp_path)
    (ask,) = [x for x in lines if x["event"] == "ask"]
    assert ask["outcome"] == "SUCCESS" and ask["relay_id"] == receipt["relay_id"]
    assert ask["via"] == "1.1 kong/fake" and ask["request_id"] == "req-fake"
    raw = (tmp_path / "audit.jsonl").read_text()
    for secret_or_content in (TOKEN, "synthetic gate PASS", "data-replay", "Authorization"):
        assert secret_or_content not in raw


def test_unpriced_success_keeps_the_reservation(tmp_path, relay_host):
    url, _, _ = relay_host(envelope(), prices={})
    agent = client(url)
    assert agent.ask("verifier", PAYLOAD) == ANSWER
    assert agent.receipts[0]["cost_usd"] is None and agent.spent == agent.budget


def test_bad_token_or_run_is_refused_without_forwarding(tmp_path, relay_host):
    url, _, process = relay_host(envelope())
    for agent in (client(url, token="x" * 43), client(url, run_id="other-run")):
        with pytest.raises(Refused, match="unauthorized") as caught:
            agent.ask("verifier", PAYLOAD)
        assert caught.value.gate == "verifier" and agent.spent == 0
    assert process.calls == []
    events = [x["event"] for x in audit_lines(tmp_path)]
    assert events == ["auth_rejected", "auth_rejected"]


def test_role_outside_the_allow_list_is_refused_without_forwarding(tmp_path, relay_host):
    url, _, process = relay_host(envelope())
    agent = client(url)
    with pytest.raises(Refused, match="role not allowed") as caught:
        agent.ask("planner", PAYLOAD)
    assert caught.value.gate == "plan_validation" and agent.spent == 0
    with pytest.raises(Refused, match="role not allowed"):
        agent.ask("not-a-role", PAYLOAD)
    assert process.calls == []
    assert [x.get("role") for x in audit_lines(tmp_path)] == ["planner", None]


def test_body_caps_on_both_sides(tmp_path, relay_host):
    url, _, process = relay_host(envelope(), max_body=200)
    big = {"artifact_manifest": ["x" * 300]}
    agent = client(url)
    with pytest.raises(Refused, match="cap"):
        agent.ask("verifier", big)  # host refuses with 413 without reading the body
    assert agent.spent == 0 and process.calls == []
    small_client = client(url, max_body=100)
    with pytest.raises(Refused, match="over the 100 cap"):
        small_client.ask("verifier", big)  # client refuses before connecting
    with pytest.raises(ValueError):
        host.Relay(
            object(),
            run_id=RUN,
            token=TOKEN,
            allowed_roles=["verifier"],
            audit=None,
            max_body=host.HARD_MAX_BODY + 1,
        )


def test_kong_unknown_is_unknown_with_redacted_evidence(tmp_path, relay_host):
    error = {"status": 500, "via": "1.1 kong/fake", "request_id": "req-err", "hint": "edge"}
    url, _, _ = relay_host(envelope(ok=None, error=error, error_type="InternalServerError"))
    agent = client(url)
    with pytest.raises(RelayOutcomeUnknown) as caught:
        agent.ask("verifier", PAYLOAD)
    evidence = caught.value.evidence
    assert caught.value.outcome == "UNKNOWN" and evidence["relay_status"] == 502
    assert evidence["via"] == "1.1 kong/fake" and evidence["request_id"] == "req-err"
    assert agent.spent == agent.budget and agent.unknown_outcomes == [evidence]
    (ask,) = [x for x in audit_lines(tmp_path) if x["event"] == "ask"]
    assert ask["outcome"] == "UNKNOWN" and ask["status"] == 500


def test_kong_refusal_before_send_releases_the_reservation(tmp_path, relay_host):
    url, _, _ = relay_host({"sent": False, "error_type": "TypeError", "error": {}})
    agent = client(url)
    with pytest.raises(Refused, match="before sending"):
        agent.ask("verifier", PAYLOAD)
    assert agent.spent == 0
    (ask,) = [x for x in audit_lines(tmp_path) if x["event"] == "ask"]
    assert ask["sent_to_gateway"] is False


def test_invalid_answer_after_send_is_refused_and_keeps_the_reservation(tmp_path, relay_host):
    # One ```json fence is a valid answer since agents.parse_role_object (integration branch);
    # prose around an object is not.
    url, _, _ = relay_host(envelope(text='Here you go: {"verdict": "PASS"}'), prices={})
    agent = client(url)
    with pytest.raises(Refused, match="invalid verifier Kong response"):
        agent.ask("verifier", PAYLOAD)
    assert agent.spent == agent.budget and agent.receipts == []
    (ask,) = [x for x in audit_lines(tmp_path) if x["event"] == "ask"]
    assert ask["sent_to_gateway"] is True and ask["billed_usd"] is None


def test_priced_invalid_answer_reports_the_billed_amount(tmp_path, relay_host):
    url, _, _ = relay_host(envelope(text="not json"))
    agent = client(url)
    with pytest.raises(Refused):
        agent.ask("verifier", PAYLOAD)
    assert agent.spent == pytest.approx((1000 * 3 + 100 * 15) / 1e6)


def test_one_request_in_flight_per_run(tmp_path, relay_host):
    url, relay, process = relay_host(envelope(), delay=0.5)
    first, second = client(url), client(url)
    results = {}

    def run_first():
        results["first"] = first.ask("verifier", PAYLOAD)

    thread = threading.Thread(target=run_first)
    thread.start()
    deadline = time.monotonic() + 5
    while not relay.in_flight.locked() and time.monotonic() < deadline:
        time.sleep(0.01)
    with pytest.raises(Refused, match="already in flight"):
        second.ask("verifier", PAYLOAD)
    thread.join()
    assert results["first"] == ANSWER and len(process.calls) == 1 and second.spent == 0


def test_max_requests_stops_after_the_response_is_written(tmp_path, relay_host):
    url, relay, _ = relay_host(envelope())
    relay.max_asks = 1
    assert client(url).ask("verifier", PAYLOAD) == ANSWER
    assert relay.asks_done.wait(2)


def test_wildcard_binds_are_refused():
    for bind in ("0.0.0.0", "::", "", "localhost"):
        with pytest.raises(ValueError):
            host.validate_binds([bind])
    assert host.validate_binds(["127.0.0.1"]) == ["127.0.0.1"]


def test_host_cli_requires_ack_and_the_kit_interpreter(tmp_path, capsys):
    with pytest.raises(SystemExit):
        host.main(["--allow-role", "verifier"])
    argv = [
        "--ack-not-sensitive",
        "--allow-role",
        "verifier",
        "--audit-log",
        "a",
        "--token-file",
        "t",
        "--ready-file",
        "r",
    ]
    if Path(sys.executable).resolve() != KIT_PYTHON.resolve():
        with pytest.raises(SystemExit):
            host.main(argv)
        assert "Kong kit interpreter" in capsys.readouterr().err


# ------------------------------------------------------------------ client edge cases (fake)


def test_drop_or_stall_after_sending_is_unknown_and_never_retried(fake_relay):
    for behaviour in ("drop", "stall"):
        relay = fake_relay(behaviour)
        agent = client(relay.url, timeout=0.5)
        with pytest.raises(RelayOutcomeUnknown, match="relay exchange failed"):
            agent.ask("verifier", PAYLOAD)
        assert agent.spent == agent.budget and len(relay.requests) == 1


@pytest.mark.parametrize(
    "item",
    [
        (200, b"not json"),
        (200, {"outcome": "SUCCESS"}),  # no relay_id
        (200, {"outcome": "MAYBE", "relay_id": "r"}),
        (422, {"outcome": "REFUSED", "relay_id": "r"}),  # sent_to_gateway missing
        (500, b""),
        (200, b"x" * (relay_roles.MAX_RESPONSE + 10)),
    ],
)
def test_ambiguous_relay_responses_are_unknown(fake_relay, item):
    relay = fake_relay(item)
    agent = client(relay.url)
    with pytest.raises(RelayOutcomeUnknown):
        agent.ask("verifier", PAYLOAD)
    assert agent.spent == agent.budget and agent.receipts == []


@pytest.mark.parametrize(
    "item",
    [
        success_envelope(output_hash="0" * 64),
        success_envelope(input_hash="0" * 64),
        success_envelope(role="reviewer"),
        success_envelope(relay_id="r-other"),
        success_envelope(model_observed=""),
        success_envelope(transport="claude-cli"),
        success_envelope(cost_usd=float("nan")),
        success_envelope(cost_usd=5),
        success_envelope(value=["not", "an", "object"]),
    ],
)
def test_inconsistent_success_is_refused_never_a_receipt(fake_relay, item):
    relay = fake_relay(item)
    agent = client(relay.url)
    with pytest.raises(Refused, match="invalid verifier relay response"):
        agent.ask("verifier", PAYLOAD)
    assert agent.receipts == []


def test_receipt_with_extra_or_missing_fields_is_refused(fake_relay):
    status, body = success_envelope()
    extra = json.loads(json.dumps(body))
    extra["receipt"]["text"] = "leaked answer text"
    missing = json.loads(json.dumps(body))
    del missing["receipt"]["via"]
    relay = fake_relay((status, extra), (status, missing))
    agent = client(relay.url)  # refused answers keep the reservation: one client per ask
    agents = [agent, client(relay.url)]
    for each in agents:
        with pytest.raises(Refused, match="receipt fields differ"):
            each.ask("verifier", PAYLOAD)


def test_repeated_session_and_wrong_model_are_refused(fake_relay):
    priced = success_envelope(cost_usd=0.001)
    relay = fake_relay(priced, priced, priced)
    agent = client(relay.url, model="claude-sonnet-4-6", budget=5)
    assert agent.ask("verifier", PAYLOAD) == ANSWER
    with pytest.raises(Refused, match="repeated independent session"):
        agent.ask("verifier", PAYLOAD)
    other = client(relay.url, model="claude-opus-x", budget=5)
    with pytest.raises(Refused, match="different model"):
        other.ask("verifier", PAYLOAD)


def test_unreachable_relay_is_refused_and_releases_the_reservation():
    agent = client(f"http://127.0.0.1:{closed_port()}", timeout=2)
    with pytest.raises(Refused, match="unreachable before sending") as caught:
        agent.ask("verifier", PAYLOAD)
    assert caught.value.gate == "verifier" and agent.spent == 0 and agent.unknown_outcomes == []


def test_budget_payload_and_constructor_refusals_send_nothing(fake_relay):
    relay = fake_relay()
    agent = client(relay.url, budget=0.01)
    with pytest.raises(Refused, match="budget") as caught:
        agent.ask("verifier", PAYLOAD)
    assert caught.value.gate == "budget"
    with pytest.raises(Refused, match="strict JSON"):
        client(relay.url).ask("verifier", {"x": float("nan")})
    assert relay.requests == []
    for url, token, run in (
        ("https://h:1", TOKEN, RUN),
        ("http://h:1/path", TOKEN, RUN),
        ("http://h:1", "short", RUN),
        ("http://h:1", TOKEN, ""),
    ):
        with pytest.raises(Refused) as caught:
            RelayRoles(url, token, run)
        assert caught.value.gate is not None
    assert TOKEN not in repr(client(relay.url))


def test_from_env_reads_url_token_run_and_model():
    env = {
        "SIMKIT_RELAY_URL": "http://host.docker.internal:8765",
        "SIMKIT_RELAY_TOKEN": TOKEN,
        "SIMKIT_RELAY_RUN_ID": RUN,
        "SIMKIT_RELAY_MODEL": "claude-sonnet-4-6",
    }
    agent = RelayRoles.from_env(env)
    assert (agent.host, agent.port, agent.run_id, agent.model) == (
        "host.docker.internal",
        8765,
        RUN,
        "claude-sonnet-4-6",
    )


def test_request_carries_auth_headers_and_exact_body(fake_relay):
    relay = fake_relay(success_envelope())
    client(relay.url).ask("verifier", PAYLOAD)
    (request,) = relay.requests
    assert request["headers"]["Authorization"] == "Bearer " + TOKEN
    assert request["headers"]["X-Simkit-Run"] == RUN
    assert request["body"] == {"role": "verifier", "payload": PAYLOAD}


def test_dispatch_observation_mirrors_kong_roles(fake_relay):
    relay = fake_relay(success_envelope())
    agent = client(relay.url)
    seen = []
    agent.dispatch_context = {"attempt_id": "a-1"}
    agent.dispatch_observer = seen.append
    agent.ask("verifier", PAYLOAD)
    assert seen[0]["process_boundary"] == "NOT_ENTERED" and seen[0]["attempt_id"] == "a-1"
    assert agent.dispatch_observation["process_boundary"] == "RETURNED"
    assert agent.dispatch_observation["http_status"] == 200


# ------------------------------------------------------------------ real worker, kit mock

needs_kit = pytest.mark.skipif(not KIT_PYTHON.is_file(), reason="Kong kit not installed")


@needs_kit
def test_relay_through_real_worker_to_the_kit_mock_gateway(tmp_path):
    spec = importlib.util.spec_from_file_location("kit_mock", KIT_ROOT / "scripts/mock_gateway.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    gateway = ThreadingHTTPServer(("127.0.0.1", 0), module.Handler)
    threading.Thread(target=gateway.serve_forever, daemon=True).start()
    (tmp_path / "proc").mkdir()
    roles = KongRoles(
        Processes(tmp_path / "proc"),
        tmp_path / "ctx",
        budget=1,
        max_tokens=32,
        data_policy_ack=True,
        worker_env={
            "KONG_GATEWAY_URL": f"http://127.0.0.1:{gateway.server_address[1]}",
            "KONG_ANTHROPIC_KEY": "test-key",  # the mock's fixed fake key, not a credential
            "KONG_ANTHROPIC_API_KEY": "",
            "KONG_UNIFIED_AI_KEY": "",
            "KONG_SDK_MAX_RETRIES": "0",
        },
    )
    audit = host.AuditLog(tmp_path / "audit.jsonl")
    relay = host.Relay(roles, run_id=RUN, token=TOKEN, allowed_roles=["verifier"], audit=audit)
    (server,) = host.serve(relay, ["127.0.0.1"], 0)
    try:
        agent = client(f"http://127.0.0.1:{server.server_address[1]}")
        with pytest.raises(Refused, match="invalid verifier Kong response"):
            agent.ask("verifier", {"synthetic": True})  # the mock answers "OK", not JSON
    finally:
        server.shutdown()
        server.server_close()
        gateway.shutdown()
    (ask,) = [x for x in audit_lines(tmp_path) if x["event"] == "ask"]
    assert ask["sent_to_gateway"] is True and agent.spent == agent.budget
    assert "test-key" not in (tmp_path / "audit.jsonl").read_text()


def test_dispatch_block_is_accepted_only_on_a_reviewer_receipt(fake_relay):
    # KongRoles adds `dispatch` to reviewer receipts that carry review_evidence (kong_roles.py).
    block = {"stdin_hash": "s", "system_prompt": "p", "system_prompt_hash": "h"}
    _, reviewer = success_envelope(role="reviewer", dispatch=block)
    _, verifier = success_envelope(dispatch=block)
    relay = fake_relay((200, reviewer), (200, verifier))
    assert client(relay.url).ask("reviewer", PAYLOAD) == ANSWER
    with pytest.raises(Refused, match="receipt fields differ"):
        client(relay.url).ask("verifier", PAYLOAD)
