import importlib.util
import json
import threading
from http.server import ThreadingHTTPServer

import pytest

from tools.workflow.agents import PROMPTS
from tools.workflow.contracts import Refused, digest
from tools.workflow.kong_roles import (
    KIT_PYTHON,
    KIT_ROOT,
    WORKER_SOURCE,
    KongOutcomeUnknown,
    KongRoles,
)
from tools.workflow.processes import Processes
from tools.workflow.review_evidence import text_hash

ANSWER = {"verdict": "PASS", "binding_hash": "h", "scope": "data-replay"}
CITED = {
    "claude-sonnet-4-6": {"input_per_mtok": 3.0, "output_per_mtok": 15.0, "source": "test fixture"}
}


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
    def __init__(self, *outputs, exit_code=0, error=None):
        self.outputs = list(outputs)
        self.exit_code = exit_code
        self.error = error
        self.calls = []

    def run(self, argv, cwd, env, stdin):
        self.calls.append({"argv": argv, "cwd": cwd, "env": env, "stdin": json.loads(stdin)})
        if self.error:
            raise self.error
        out = self.outputs.pop(0)
        stdout = out if isinstance(out, str) else json.dumps(out)
        return {"exit_code": self.exit_code, "stdout": stdout, "stderr": ""}


def roles(tmp_path, process, **kwargs):
    python = tmp_path / "python.exe"
    python.write_text("")
    kwargs.setdefault("prices", CITED)
    return KongRoles(
        process, tmp_path / "ctx", budget=1, data_policy_ack=True, kit_python=python, **kwargs
    )


def test_receipt_records_observed_identity_settings_and_cost(tmp_path):
    process = Process(envelope())
    agent = roles(tmp_path, process, max_tokens=256)
    assert agent.ask("verifier", {"manifest": [1, 2, 3]}) == ANSWER
    (receipt,) = agent.receipts
    sent = process.calls[0]["stdin"]
    assert sent["system"] == PROMPTS["verifier"] and sent["max_tokens"] == 256
    assert receipt["input_hash"] == digest({"manifest": [1, 2, 3]})
    assert receipt["output_hash"] == digest(ANSWER)
    assert receipt["model_requested"] == receipt["model_observed"] == "claude-sonnet-4-6"
    assert receipt["settings"]["system_prompt_hash"] == text_hash(PROMPTS["verifier"])
    assert receipt["settings"]["max_tokens"] == 256 and receipt["settings"]["temperature"] is None
    assert receipt["cost_usd"] == pytest.approx(0.0045) and receipt["cost_basis"] == "test fixture"
    assert agent.spent == pytest.approx(0.0045)
    assert receipt["transport"] == "kong-native-anthropic" and receipt["mode"] == "live"
    assert receipt["request_id"] == "req-fake" and receipt["via"] == "1.1 kong/fake"
    assert len(receipt["session_id"]) == 36 and receipt["session_id"] != receipt["invocation_id"]
    argv = process.calls[0]["argv"]
    assert argv[1:4] == ["-I", "-c", WORKER_SOURCE]
    assert set(sent) == {"model", "max_tokens", "temperature", "system", "user"}  # no key


def test_sessions_are_fresh_per_invocation(tmp_path):
    agent = roles(tmp_path, Process(envelope(), envelope()))
    agent.ask("verifier", {})
    agent.ask("verifier", {})
    assert len({r["session_id"] for r in agent.receipts}) == 2


def test_observed_model_that_differs_is_recorded_not_hidden(tmp_path):
    agent = roles(tmp_path, Process(envelope(model="claude-sonnet-4-6-20260901")))
    agent.ask("verifier", {})
    receipt = agent.receipts[0]
    assert receipt["model_observed"] == "claude-sonnet-4-6-20260901"
    assert receipt["model_requested"] == "claude-sonnet-4-6" and receipt["model_match"] is False
    # Priced on observed identity only: no cited price for the observed id means no cost.
    assert receipt["cost_usd"] is None and "no pinned cited price" in receipt["cost_basis"]


def test_unpriced_success_keeps_reservation_and_fails_closed(tmp_path):
    agent = roles(tmp_path, Process(envelope(), envelope()), prices={})
    agent.ask("verifier", {})
    assert agent.receipts[0]["cost_usd"] is None and agent.spent == agent.budget
    with pytest.raises(Refused, match="budget exhausted"):
        agent.ask("verifier", {})


def test_budget_refusal_happens_before_any_call_and_survives_resume(tmp_path):
    process = Process(envelope())
    agent = roles(tmp_path, process)
    agent.spent = 0.97  # restored by Controller.resume from the checkpoint
    with pytest.raises(Refused, match="budget exhausted"):
        agent.ask("verifier", {})
    assert process.calls == [] and agent.receipts == []


def test_cost_over_ceiling_is_refused_without_receipt(tmp_path):
    usage = {"input_tokens": 1_000_000, "output_tokens": 0}
    agent = roles(tmp_path, Process(envelope(usage=usage)))
    with pytest.raises(Refused, match="budget"):
        agent.ask("verifier", {})
    assert agent.receipts == [] and agent.spent == pytest.approx(3.0)


@pytest.mark.parametrize(
    "process",
    [
        Process(envelope(ok=False, error={"status": 500, "via": "1.1 kong", "request_id": "r"})),
        Process(envelope(), exit_code=1),
        Process("not json"),
        Process("[]"),
        Process(error=TimeoutError("killed")),
    ],
)
def test_transport_errors_after_send_are_unknown_never_success(tmp_path, process):
    agent = roles(tmp_path, process)
    with pytest.raises(KongOutcomeUnknown) as caught:
        agent.ask("verifier", {})
    assert caught.value.outcome == "UNKNOWN" and not isinstance(caught.value, Refused)
    assert agent.receipts == [] and agent.spent == agent.budget  # may have been billed
    assert agent.unknown_outcomes[0]["outcome"] == "UNKNOWN"


def test_unknown_carries_redacted_gateway_evidence(tmp_path):
    error = {"status": 429, "via": "1.1 kong", "request_id": "r-1", "hint": "rate limited"}
    agent = roles(tmp_path, Process(envelope(ok=False, error=error, error_type="RateLimitError")))
    with pytest.raises(KongOutcomeUnknown) as caught:
        agent.ask("verifier", {})
    assert caught.value.evidence["status"] == 429 and caught.value.evidence["request_id"] == "r-1"


def test_failure_before_send_is_refused_and_releases_reservation(tmp_path):
    out = {"sent": False, "error_type": "RuntimeError", "error": {"message": "key not set"}}
    agent = roles(tmp_path, Process(out))
    with pytest.raises(Refused, match="before sending"):
        agent.ask("verifier", {})
    assert agent.spent == 0.0


@pytest.mark.parametrize(
    "changes",
    [
        {"text": '```json\n{"verdict":"PASS"}\n```'},
        {"text": "[]"},
        {"text": ""},
        {"model": None},
        {"model": ""},
    ],
)
def test_invalid_answers_cannot_be_receipts(tmp_path, changes):
    agent = roles(tmp_path, Process(envelope(**changes)))
    with pytest.raises(Refused):
        agent.ask("verifier", {})
    assert agent.receipts == []


def test_policy_ack_prices_and_prompt_variants_are_enforced(tmp_path):
    with pytest.raises(Refused, match="data policy"):
        KongRoles(Process(), tmp_path)
    with pytest.raises(Refused, match="cited source"):
        roles(tmp_path, Process(), prices={"m": {"input_per_mtok": 1, "output_per_mtok": 1}})
    agent = roles(tmp_path, Process(envelope()))
    for payload in ({"review_evidence": {}}, {"review_evidence_request": {}}):
        with pytest.raises(Refused, match="review-evidence"):
            agent.ask("reviewer", payload)
    with pytest.raises(Refused):
        agent.ask("not-a-role", {})


def test_dispatch_observation_mirrors_native_roles(tmp_path):
    agent = roles(tmp_path, Process(envelope()))
    seen = []
    agent.dispatch_context = {"attempt_id": "a-1"}
    agent.dispatch_observer = seen.append
    agent.ask("verifier", {})
    assert seen[0]["process_boundary"] == "NOT_ENTERED"
    assert agent.dispatch_observation["process_boundary"] == "RETURNED"
    assert agent.dispatch_observation["invocation_id"] == agent.receipts[0]["invocation_id"]


# Real worker source under the kit interpreter against the kit's loopback FAKE gateway.
# Mock results exercise the plumbing only; they prove nothing about the real gateway.
needs_kit = pytest.mark.skipif(not KIT_PYTHON.is_file(), reason="Kong kit not installed")


@pytest.fixture
def mock_gateway():
    spec = importlib.util.spec_from_file_location("kit_mock", KIT_ROOT / "scripts/mock_gateway.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    server = ThreadingHTTPServer(("127.0.0.1", 0), module.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def mock_roles(tmp_path, url, model="claude-sonnet-4-6"):
    env = {
        "KONG_GATEWAY_URL": url,
        "KONG_ANTHROPIC_KEY": "test-key",  # the mock's fixed fake key, not a credential
        "KONG_ANTHROPIC_API_KEY": "",
        "KONG_UNIFIED_AI_KEY": "",
    }
    return KongRoles(
        Processes(tmp_path / "proc"),
        tmp_path / "ctx",
        budget=1,
        model=model,
        max_tokens=32,
        data_policy_ack=True,
        worker_env=env,
    )


@needs_kit
def test_real_worker_against_mock_reaches_gateway_and_validates_output(tmp_path, mock_gateway):
    (tmp_path / "proc").mkdir()
    agent = mock_roles(tmp_path, mock_gateway)
    with pytest.raises(Refused, match="invalid verifier Kong response"):
        agent.ask("verifier", {"synthetic": True})  # the mock answers plain "OK", not JSON
    record = json.loads(next((tmp_path / "proc").glob("process-*.json")).read_text())
    worker = json.loads(record["stdout"])
    assert worker["sent"] and worker["ok"] and worker["mock"] is True
    assert worker["model"] == "claude-sonnet-4-6" and worker["via"] == "1.1 kong/mock"
    assert "test-key" not in record["stdout"] + record["stderr"]


@needs_kit
def test_real_worker_rejects_unsupported_sdk_argument_before_sending(tmp_path, mock_gateway):
    (tmp_path / "proc").mkdir()
    agent = mock_roles(tmp_path, mock_gateway)
    agent.temperature = 0.0  # anthropic-python 1.13 Messages.create has no temperature
    with pytest.raises(Refused, match="before sending: TypeError"):
        agent.ask("verifier", {"synthetic": True})
    assert agent.spent == 0.0


@needs_kit
def test_real_worker_gateway_error_is_unknown(tmp_path, mock_gateway):
    (tmp_path / "proc").mkdir()
    agent = mock_roles(tmp_path, mock_gateway, model="claude-not-routable")
    with pytest.raises(KongOutcomeUnknown) as caught:
        agent.ask("verifier", {"synthetic": True})
    assert caught.value.evidence["status"] == 404
    assert caught.value.evidence["via"] == "1.1 kong/mock"
