"""Runner-side Kong client (relay/runner_kit) and the relay's ``--client runner`` mode. Offline.

The SDK-backed tests run the real KongRoles worker (``python -I -c WORKER_SOURCE``) against a
loopback fake of the native route, under any interpreter that has the pinned SDK: this one if
it has it, else the Kong kit's. Without one they skip (the GitHub relay-smoke job installs the
pin and runs them).
"""

import importlib.util
import json
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from tools.workflow.contracts import Refused
from tools.workflow.kong_roles import KIT_PYTHON, WORKER_SOURCE, KongOutcomeUnknown, KongRoles
from tools.workflow.processes import Processes

ROOT = Path(__file__).resolve().parents[3]
RUNNER_KIT = ROOT / "relay" / "runner_kit"
FAKE_KEY = "test-key"  # the only key a loopback gateway accepts; not a credential
ANSWER = {"verdict": "PASS", "sub_reason": None, "binding_hash": "h", "scope": "data-replay"}
PAYLOAD = {"artifact_manifest": ["synthetic gate PASS"], "binding_hash": "synthetic"}


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


kong_client = load("runner_kong_client", RUNNER_KIT / "scripts" / "kong_client.py")
host = load("kong_relay_host_runner", ROOT / "relay" / "kong_relay_host.py")


def sdk_python():
    pinned = host.RUNNER_SDK["anthropic"]
    probe = "from importlib.metadata import version; print(version('anthropic'))"
    for candidate in (Path(sys.executable), KIT_PYTHON):
        if not candidate.is_file():
            continue
        out = subprocess.run([str(candidate), "-I", "-c", probe], capture_output=True, text=True)
        if out.returncode == 0 and out.stdout.strip() == pinned:
            return candidate
    return None


SDK_PYTHON = sdk_python()
needs_sdk = pytest.mark.skipif(SDK_PYTHON is None, reason="no interpreter with the pinned SDK")


# ------------------------------------------------------------------ pure (no SDK)


def test_runner_client_provides_every_name_the_worker_uses():
    used = ("KongConfig", "anthropic_client", "check_body_size", "create_message")
    used += ("describe_error", "text_of", "is_mock_url")
    for name in used:
        assert f"kong_client.{name}" in WORKER_SOURCE
        assert callable(getattr(kong_client, name))


def test_route_is_fixed_to_the_production_native_route(monkeypatch):
    for name in ("KONG_GATEWAY_URL", "KONG_ANTHROPIC_PATH", "KONG_KEY_HEADER"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("KONG_ANTHROPIC_KEY", "real-looking-secret-value-123")
    cfg = kong_client.KongConfig.from_env()
    assert cfg.anthropic_base_url == "https://ai-kong-gateway.bmgf.io/v2/anthropic"
    assert cfg.key_header == "x-kong-key" and cfg.placeholder_key == "kong-managed"
    assert cfg.max_retries == 0
    assert "real-looking-secret-value-123" not in repr(cfg)
    monkeypatch.setenv("KONG_ANTHROPIC_PATH", "/elsewhere")  # ignored: the path is fixed
    assert kong_client.KongConfig.from_env().anthropic_path == "/v2/anthropic"


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example.com",
        "http://ai-kong-gateway.bmgf.io",
        "https://ai-kong-gateway-nonprod.bmgf.io",
        "https://ai-kong-gateway.bmgf.io/v2/anthropic",
        "https://user:pw@ai-kong-gateway.bmgf.io",
    ],
)
def test_a_key_cannot_be_sent_anywhere_but_the_gateway(monkeypatch, url):
    monkeypatch.setenv("KONG_GATEWAY_URL", url)
    monkeypatch.setenv("KONG_ANTHROPIC_KEY", FAKE_KEY)
    with pytest.raises(ValueError):
        kong_client.KongConfig.from_env()


def test_loopback_mock_refuses_a_real_key(monkeypatch):
    monkeypatch.setenv("KONG_GATEWAY_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("KONG_ANTHROPIC_KEY", "a-real-key-must-never-go-to-a-mock")
    with pytest.raises(ValueError, match="test keys"):
        kong_client.KongConfig.from_env()
    monkeypatch.setenv("KONG_ANTHROPIC_KEY", FAKE_KEY)
    assert kong_client.KongConfig.from_env().gateway_url == "http://127.0.0.1:9"


def test_describe_error_redacts_env_secrets_and_passed_values(monkeypatch):
    monkeypatch.setenv("KONG_ANTHROPIC_KEY", "sekrit-AAAA-1111")

    class Response:
        status_code = 401
        headers = {"via": "kong/3.9", "x-kong-request-id": "rid-1"}
        text = '{"message": "bad key sekrit-AAAA-1111 and other-BBBB"}'

    class Err(Exception):
        status_code = 401
        response = Response()

    out = kong_client.describe_error(Err("rejected sekrit-AAAA-1111 / other-BBBB"), ("other-BBBB",))
    text = json.dumps(out)
    assert "sekrit-AAAA-1111" not in text and "other-BBBB" not in text
    assert out["status"] == 401 and out["via"] == "kong/3.9" and out["request_id"] == "rid-1"


def test_runner_client_never_reads_a_dotenv():
    source = (RUNNER_KIT / "scripts" / "kong_client.py").read_text(encoding="utf-8")
    assert "dotenv" not in source.replace("no ``.env`` is read", "")


def test_requirements_pin_matches_the_relay_check():
    pins = dict(
        line.split("==")
        for line in (RUNNER_KIT / "requirements.txt").read_text().splitlines()
        if "==" in line and not line.startswith("#")
    )
    assert pins["anthropic"] == host.RUNNER_SDK["anthropic"]


def test_client_paths_runner_mode_checks_presence_and_pin(monkeypatch):
    with pytest.raises(ValueError, match="KONG_ANTHROPIC_KEY"):
        host.client_paths("runner", environ={})
    with pytest.raises(ValueError, match="unknown client"):
        host.client_paths("nope", environ={})
    monkeypatch.setattr(host, "RUNNER_SDK", {"anthropic": "0.0.0-not-installed"})
    with pytest.raises(ValueError, match="anthropic==0.0.0-not-installed"):
        host.client_paths("runner", environ={"KONG_ANTHROPIC_KEY": "x"})


def test_client_paths_kit_mode_still_requires_the_kit_interpreter(tmp_path):
    if Path(sys.executable).resolve() != KIT_PYTHON.resolve():
        with pytest.raises(ValueError, match="Kong kit interpreter"):
            host.client_paths("kit", environ={}, executable=sys.executable)
    assert host.client_paths("kit", environ={}, executable=KIT_PYTHON) == (None, None)


# ------------------------------------------------------------------ real worker, fake route


class FakeNativeRoute:
    """Loopback stand-in for /v2/anthropic/v1/messages; records what reached it."""

    def __init__(self, status=200, body=None):
        self.seen = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length", "0"))
                outer.seen.append(
                    {
                        "path": self.path,
                        "headers": {k.lower(): v for k, v in self.headers.items()},
                        "body": json.loads(self.rfile.read(length)),
                    }
                )
                data = json.dumps(body if body is not None else outer.message()).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("via", "1.1 kong/3.9-fake")
                self.send_header("x-kong-request-id", "fake-request-1")
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    @staticmethod
    def message():
        return {
            "id": "msg_fake_1",
            "type": "message",
            "role": "assistant",
            "model": "claude-sonnet-4-6",
            "content": [{"type": "text", "text": json.dumps(ANSWER)}],
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "usage": {"input_tokens": 11, "output_tokens": 7},
        }

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def runner_roles(tmp_path, gateway_url):
    (tmp_path / "proc").mkdir()
    return KongRoles(
        Processes(tmp_path / "proc", memory_reader=lambda: 64.0),
        tmp_path / "ctx",
        budget=1,
        max_tokens=64,
        prices={},
        data_policy_ack=True,
        kit_root=RUNNER_KIT,
        kit_python=SDK_PYTHON,
        worker_env={
            "KONG_GATEWAY_URL": gateway_url,
            "KONG_ANTHROPIC_KEY": FAKE_KEY,
            "KONG_SDK_MAX_RETRIES": "0",
            "ANTHROPIC_LOG": "debug",  # must be neutralised by the runner client
        },
    )


@needs_sdk
def test_runner_client_success_produces_the_kong_receipt(tmp_path):
    route = FakeNativeRoute()
    try:
        roles = runner_roles(tmp_path, route.url)
        assert roles.ask("verifier", PAYLOAD) == ANSWER
    finally:
        route.close()
    (seen,) = route.seen
    assert seen["path"] == "/v2/anthropic/v1/messages"
    assert seen["headers"]["x-kong-key"] == FAKE_KEY
    assert seen["headers"]["x-api-key"] == "kong-managed"
    assert seen["body"]["model"] == "claude-sonnet-4-6" and seen["body"]["max_tokens"] == 64
    receipt = roles.receipts[-1]
    assert receipt["model_observed"] == "claude-sonnet-4-6" and receipt["model_match"] is True
    assert receipt["via"] == "1.1 kong/3.9-fake" and receipt["request_id"] == "fake-request-1"
    assert receipt["gateway_mock"] is True and receipt["transport"] == "kong-native-anthropic"
    assert receipt["settings"]["sdk"] == "anthropic-python " + host.RUNNER_SDK["anthropic"]
    assert receipt["cost_usd"] is None and roles.spent == roles.budget  # unpriced: kept
    assert FAKE_KEY not in json.dumps(receipt)


@needs_sdk
def test_runner_client_http_error_is_unknown_with_redacted_evidence(tmp_path):
    route = FakeNativeRoute(status=401, body={"error": f"bad x-kong-key {FAKE_KEY}"})
    try:
        roles = runner_roles(tmp_path, route.url)
        with pytest.raises(KongOutcomeUnknown) as info:
            roles.ask("verifier", PAYLOAD)
    finally:
        route.close()
    evidence = info.value.evidence
    assert evidence["status"] == 401 and evidence["request_id"] == "fake-request-1"
    assert FAKE_KEY not in json.dumps(evidence) and roles.spent == roles.budget


@needs_sdk
def test_runner_client_invalid_answer_is_refused_after_send(tmp_path):
    message = FakeNativeRoute.message()
    message["content"] = [{"type": "text", "text": "OK"}]
    route = FakeNativeRoute(body=message)
    try:
        roles = runner_roles(tmp_path, route.url)
        with pytest.raises(Refused, match="invalid verifier Kong response"):
            roles.ask("verifier", PAYLOAD)
    finally:
        route.close()
    assert roles.spent == roles.budget  # sent: reservation kept


@needs_sdk
def test_relay_runner_mode_end_to_end_without_the_kit(tmp_path):
    from tools.workflow.relay_roles import RelayRoles

    route = FakeNativeRoute()
    roles = runner_roles(tmp_path, route.url)
    audit = host.AuditLog(tmp_path / "audit.jsonl")
    token = "r" * 43
    relay = host.Relay(roles, run_id="run-x", token=token, allowed_roles=["verifier"], audit=audit)
    (server,) = host.serve(relay, ["127.0.0.1"], 0)
    try:
        agent = RelayRoles(f"http://127.0.0.1:{server.server_address[1]}", token, "run-x", budget=1)
        assert agent.ask("verifier", PAYLOAD) == ANSWER
    finally:
        server.shutdown()
        server.server_close()
        route.close()
    assert agent.receipts[-1]["model_observed"] == "claude-sonnet-4-6"
    text = (tmp_path / "audit.jsonl").read_text()
    assert FAKE_KEY not in text and token not in text
    (ask,) = [json.loads(x) for x in text.splitlines() if json.loads(x)["event"] == "ask"]
    assert ask["outcome"] == "SUCCESS" and ask["model_observed"] == "claude-sonnet-4-6"
