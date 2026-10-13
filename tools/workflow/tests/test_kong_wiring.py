"""D16 / CP-F1 wiring: Kong live roles usable by the controller (shared prompt + output rule,
per-role models, observed identity and sent settings, untracked-spend budget semantics, CLI)."""

import argparse
import importlib.util
import json
import threading
from http.server import ThreadingHTTPServer

import pytest

from tools.workflow import __main__ as cli
from tools.workflow.agents import (
    CLAIM_CHECK_SUFFIX,
    OUTPUT_NORMALIZATION,
    PROMPTS,
    REVIEW_EVIDENCE_REQUEST_SUFFIX,
    REVIEW_EVIDENCE_SUFFIX,
    REVIEW_RATIONALE_SUFFIX,
    Roles,
    parse_role_object,
    role_prompt,
)
from tools.workflow.contracts import Refused, digest
from tools.workflow.kong_roles import (
    DEFAULT_MODEL,
    KIT_PYTHON,
    KIT_ROOT,
    NATIVE_ROSTER,
    KongOutcomeUnknown,
    KongRoles,
    role_models,
)
from tools.workflow.processes import Processes
from tools.workflow.review_evidence import text_hash

ANSWER = {"verdict": "PASS", "binding_hash": "h", "scope": "data-replay"}
FENCED = "```json\n" + json.dumps(ANSWER) + "\n```"


def envelope(**changes):
    value = {
        "sent": True,
        "ok": True,
        "model": DEFAULT_MODEL,
        "response_id": "msg_fake",
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 10, "output_tokens": 5},
        "text": json.dumps(ANSWER),
        "via": "1.1 kong/fake",
        "request_id": "req-fake",
        "latency_ms": 3,
        "sdk": "anthropic-python 0.0",
        "mock": True,
        "sent_settings": {"model": DEFAULT_MODEL, "max_tokens": 64},
        "sent_keys": ["max_tokens", "messages", "model", "system"],
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
        self.calls.append({"argv": argv, "stdin": json.loads(stdin)})
        if self.error:
            raise self.error
        out = self.outputs.pop(0)
        stdout = out if isinstance(out, str) else json.dumps(out)
        return {"exit_code": self.exit_code, "stdout": stdout, "stderr": ""}


def kong(tmp_path, process, **kwargs):
    python = tmp_path / "python.exe"
    python.write_text("")
    kwargs.setdefault("budget_guard", "off")
    if kwargs["budget_guard"] == "off":
        kwargs.setdefault("prices", {})
    return KongRoles(
        process, tmp_path / "ctx", budget=1, data_policy_ack=True, kit_python=python, **kwargs
    )


class CliProcess:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def run(self, argv, cwd, env, stdin):
        self.calls.append({"argv": argv, "stdin": stdin})
        out = {"total_cost_usd": 0.01, "session_id": f"s-{len(self.calls)}", "result": self.result}
        return {"exit_code": 0, "stdout": json.dumps(out), "stderr": ""}


def cli_roles(monkeypatch, tmp_path, result):
    monkeypatch.setattr("tools.workflow.agents.shutil.which", lambda _: "claude.exe")
    process = CliProcess(result)
    return Roles(process, tmp_path, budget=1), process


# ------------------------------------------------------------ (1) one shared output rule


@pytest.mark.parametrize(
    ("text", "form"),
    [
        ('{"a": 1}', "bare"),
        ('  {"a": 1}\n', "bare"),
        ('```json\n{"a": 1}\n```', "json_fence"),
        ('\n```json \r\n{"a": 1}\r\n```\n', "json_fence"),
    ],
)
def test_parser_accepts_one_object_bare_or_in_one_json_fence(text, form):
    assert parse_role_object(text) == ({"a": 1}, form)


@pytest.mark.parametrize(
    "text",
    [
        'Here is the answer: {"a": 1}',
        'Sure.\n```json\n{"a": 1}\n```',
        '```json\n{"a": 1}\n```\nThat is my verdict.',
        '```\n{"a": 1}\n```',
        '```JSON\n{"a": 1}\n```',
        '```python\n{"a": 1}\n```',
        '```json\n{"a": 1}\n```\n```json\n{"b": 2}\n```',
        '```json {"a": 1}```',
        '{"a": 1}{"b": 2}',
        '{"a": 1}\n{"b": 2}',
        '{"a": 1, "a": 2}',
        '{"a": NaN}',
        '{"a": Infinity}',
        "[]",
        '"a"',
        "",
        None,
    ],
)
def test_parser_rejects_prose_extra_content_and_non_strict_json(text):
    with pytest.raises(ValueError):
        parse_role_object(text)


def test_both_transports_apply_the_same_rule_and_record_it(monkeypatch, tmp_path):
    agent = kong(tmp_path, Process(envelope(text=FENCED)))
    assert agent.ask("verifier", {}) == ANSWER
    roles, _ = cli_roles(monkeypatch, tmp_path, FENCED)
    assert roles.ask("verifier", {}) == ANSWER
    for receipt in (agent.receipts[0], roles.receipts[0]):
        assert receipt["output_normalization"] == OUTPUT_NORMALIZATION
        assert receipt["output_form"] == "json_fence"
        assert receipt["output_hash"] == digest(ANSWER)


@pytest.mark.parametrize("text", ['Answer: {"verdict": "PASS"}', '```json\n{"a": 1}\n``` ok'])
def test_both_transports_refuse_prose_without_receipt(monkeypatch, tmp_path, text):
    agent = kong(tmp_path, Process(envelope(text=text)))
    with pytest.raises(Refused, match="invalid verifier Kong response"):
        agent.ask("verifier", {})
    roles, _ = cli_roles(monkeypatch, tmp_path, text)
    with pytest.raises(Refused, match="invalid verifier envelope"):
        roles.ask("verifier", {})
    assert agent.receipts == [] and roles.receipts == []


# ------------------------------------------------------------ shared review-evidence prompts


REVIEW_PAYLOAD = {
    "review_evidence": {"rationale": "r", "plan_claims": []},
    "claim_check": {"citations": []},
    "binding_hash": "b",
}


def test_role_prompt_composes_the_shared_suffixes():
    assert role_prompt("verifier", REVIEW_PAYLOAD) == PROMPTS["verifier"]
    assert role_prompt("reviewer", REVIEW_PAYLOAD) == (
        PROMPTS["reviewer"] + REVIEW_EVIDENCE_SUFFIX + REVIEW_RATIONALE_SUFFIX + CLAIM_CHECK_SUFFIX
    )
    for role in ("planner", "repairer"):
        assert role_prompt(role, {"review_evidence_request": {}}) == (
            PROMPTS[role] + REVIEW_EVIDENCE_REQUEST_SUFFIX
        )
    assert role_prompt("reviewer", {"review_evidence_request": {}}) == PROMPTS["reviewer"]


def test_kong_reuses_review_evidence_prompts_and_matches_cli_dispatch(monkeypatch, tmp_path):
    process = Process(envelope(), envelope())
    agent = kong(tmp_path, process)
    agent.ask("reviewer", REVIEW_PAYLOAD)
    agent.ask("planner", {"review_evidence_request": {"v": 1}})
    assert process.calls[0]["stdin"]["system"] == role_prompt("reviewer", REVIEW_PAYLOAD)
    assert process.calls[1]["stdin"]["system"].endswith(REVIEW_EVIDENCE_REQUEST_SUFFIX)
    roles, cli_process = cli_roles(monkeypatch, tmp_path, json.dumps(ANSWER))
    roles.ask("reviewer", REVIEW_PAYLOAD)
    kong_dispatch, cli_dispatch = agent.receipts[0]["dispatch"], roles.receipts[0]["dispatch"]
    # The block Controller.ask binds to the review capture is identical across transports.
    assert kong_dispatch == cli_dispatch
    assert kong_dispatch["stdin_hash"] == text_hash(json.dumps(REVIEW_PAYLOAD))
    assert "dispatch" not in agent.receipts[1]


# ------------------------------------------------------------ (2) per-role model map


def test_default_map_uses_the_probed_native_model_for_every_role():
    assert role_models() == {role: DEFAULT_MODEL for role in PROMPTS}
    assert NATIVE_ROSTER[DEFAULT_MODEL].startswith("PROBED")


def test_role_model_is_sent_and_recorded_per_role(tmp_path):
    cheap = "claude-haiku-4-5"
    process = Process(envelope(model=cheap), envelope())
    agent = kong(tmp_path, process, models={"verifier": cheap})
    agent.ask("verifier", {})
    agent.ask("reviewer", {})
    assert [c["stdin"]["model"] for c in process.calls] == [cheap, DEFAULT_MODEL]
    first, second = agent.receipts
    assert first["model_requested"] == first["model_observed"] == cheap
    assert second["model_requested"] == DEFAULT_MODEL and second["model_match"] is True


@pytest.mark.parametrize(
    "models",
    [{"verifier": "claude-opus-5-5"}, {"verifier": "gpt-5.6-sol"}, {"not-a-role": DEFAULT_MODEL}],
)
def test_unevidenced_model_or_unknown_role_is_rejected(tmp_path, models):
    with pytest.raises(ValueError):
        kong(tmp_path, Process(), models=models)


def test_observed_model_mismatch_is_recorded(tmp_path):
    agent = kong(tmp_path, Process(envelope(model="claude-sonnet-4-6-20991231")))
    agent.ask("compiler", {})
    receipt = agent.receipts[0]
    assert receipt["model_observed"] == "claude-sonnet-4-6-20991231"
    assert receipt["model_match"] is False


# ------------------------------------------------------------ (3) exact settings sent


def test_receipt_records_settings_the_worker_sent(tmp_path):
    sent = {"model": DEFAULT_MODEL, "max_tokens": 64}
    agent = kong(tmp_path, Process(envelope(sent_settings=sent)), max_tokens=64)
    agent.ask("verifier", {})
    receipt = agent.receipts[0]
    assert receipt["settings_sent"] == sent
    assert receipt["sent_keys"] == ["max_tokens", "messages", "model", "system"]
    assert receipt["settings"]["retries"] == 0 and receipt["settings"]["thinking"] is None


# ------------------------------------------------------------ (4) budget semantics


def test_guard_off_records_untracked_cost_and_allows_many_calls(tmp_path):
    agent = kong(tmp_path, Process(envelope(), envelope(), envelope()))
    for _ in range(3):
        agent.ask("verifier", {})
    assert agent.spent == 0.0 and len(agent.receipts) == 3
    for receipt in agent.receipts:
        assert receipt["cost_usd"] is None and receipt["cost_basis"] == "untracked"
        assert receipt["budget_guard"] == "off"


def test_guard_off_unknown_outcome_reserves_nothing_and_stays_unknown(tmp_path):
    agent = kong(tmp_path, Process(envelope(ok=False, error={"status": 500})))
    with pytest.raises(KongOutcomeUnknown):
        agent.ask("verifier", {})
    assert agent.spent == 0.0 and agent.receipts == []
    assert agent.unknown_outcomes[0]["budget_guard"] == "off"


def test_guard_on_keeps_fail_closed_reservation(tmp_path):
    agent = kong(tmp_path, Process(envelope(), envelope()), budget_guard="on", prices={})
    agent.ask("verifier", {})
    assert agent.spent == agent.budget and agent.receipts[0]["cost_usd"] is None
    with pytest.raises(Refused, match="budget exhausted"):
        agent.ask("verifier", {})


def test_guard_configuration_is_explicit_and_unmixed(tmp_path):
    price = {DEFAULT_MODEL: {"input_per_mtok": 1, "output_per_mtok": 1, "source": "fixture"}}
    with pytest.raises(ValueError, match="untracked"):
        kong(tmp_path, Process(), budget_guard="off", prices=price)
    with pytest.raises(ValueError, match="budget_guard"):
        kong(tmp_path, Process(), budget_guard="maybe")


def test_provenance_states_guard_off_and_spent_semantics(tmp_path):
    agent = kong(tmp_path, Process(), models={"router": "claude-haiku-4-5"})
    value = agent.provenance()
    assert value["budget_guard"] == "off" and value["cost_tracking"] == "untracked"
    assert "not a measure of Kong spend" in value["spent_field_semantics"]
    assert value["models"]["router"] == "claude-haiku-4-5"
    assert set(value["model_evidence"]) == {DEFAULT_MODEL, "claude-haiku-4-5"}
    assert value["output_normalization"] == OUTPUT_NORMALIZATION and value["retries"] == 0


# ------------------------------------------------------------ CLI wiring


def parser_and_args(*argv):
    parser = argparse.ArgumentParser()
    parser.add_argument("--roles", default="cli")
    parser.add_argument("--role-model", action="append", default=[])
    parser.add_argument("--kong-max-tokens", type=int, default=4096)
    parser.add_argument("--kong-data-policy-ack", action="store_true")
    return parser, parser.parse_args(list(argv))


def test_cli_kong_transport_requires_ack_and_valid_models():
    assert cli.kong_transport(*parser_and_args()) is None
    transport = cli.kong_transport(
        *parser_and_args(
            "--roles", "kong", "--kong-data-policy-ack", "--role-model", "router=claude-haiku-4-5"
        )
    )
    assert transport["budget_guard"] == "off" and transport["max_tokens"] == 4096
    assert transport["models"]["router"] == "claude-haiku-4-5"
    assert transport["models"]["reviewer"] == DEFAULT_MODEL
    for argv in (
        ("--roles", "kong"),
        ("--roles", "kong", "--kong-data-policy-ack", "--role-model", "router"),
        ("--roles", "kong", "--kong-data-policy-ack", "--role-model", "router=gpt-5.6-sol"),
        ("--roles", "kong", "--kong-data-policy-ack", "--kong-max-tokens", "0"),
        ("--role-model", "router=claude-haiku-4-5"),
        ("--kong-data-policy-ack",),
    ):
        with pytest.raises(SystemExit):
            cli.kong_transport(*parser_and_args(*argv))


class RecordingController:
    def __init__(self, root):
        self.root = root
        self.events = []

    def save(self, name, value):
        from tools.workflow.persistence import atomic_json

        atomic_json(self.root / name, value)

    def dispatch_event(self, kind, **details):
        self.events.append((kind, details))


def test_cli_records_provenance_and_resume_inherits_transport(tmp_path):
    transport = cli.kong_transport(
        *parser_and_args(
            "--roles", "kong", "--kong-data-policy-ack", "--role-model", "verifier=claude-haiku-4-5"
        )
    )
    (tmp_path / "proc").mkdir()
    roles = cli.kong_roles(transport, Processes(tmp_path / "proc"), tmp_path / "ctx", 5)
    controller = RecordingController(tmp_path)
    cli.record_transport(controller, roles)
    stored = json.loads((tmp_path / cli.TRANSPORT_RECORD).read_text(encoding="utf-8"))
    assert stored == roles.provenance() and stored["budget_guard"] == "off"
    assert controller.events == [("role_transport", {"role_transport": stored})]
    assert cli.recorded_transport(tmp_path) == transport
    assert cli.recorded_transport(tmp_path / "proc") is None  # no record: CLI roles


def test_resume_refuses_an_unrecognised_transport_record(tmp_path):
    (tmp_path / cli.TRANSPORT_RECORD).write_text(json.dumps({"schema": "x"}), encoding="utf-8")
    with pytest.raises(Refused) as caught:
        cli.recorded_transport(tmp_path)
    assert caught.value.gate == "resume_verification"


# ------------------------------------------------------------ real worker, loopback FAKE


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


@needs_kit
def test_real_worker_echoes_sent_settings_without_content_or_key(tmp_path, mock_gateway):
    (tmp_path / "proc").mkdir()
    env = {
        "KONG_GATEWAY_URL": mock_gateway,
        "KONG_ANTHROPIC_KEY": "test-key",  # the mock's fixed fake key, not a credential
        "KONG_ANTHROPIC_API_KEY": "",
        "KONG_UNIFIED_AI_KEY": "",
    }
    agent = KongRoles(
        Processes(tmp_path / "proc"),
        tmp_path / "ctx",
        max_tokens=32,
        budget_guard="off",
        prices={},
        data_policy_ack=True,
        worker_env=env,
    )
    with pytest.raises(Refused):
        agent.ask("verifier", {"synthetic": True})  # the mock answers plain "OK", not JSON
    record = json.loads(next((tmp_path / "proc").glob("process-*.json")).read_text())
    worker = json.loads(record["stdout"])
    assert worker["sent_settings"] == {"model": DEFAULT_MODEL, "max_tokens": 32}
    assert worker["sent_keys"] == ["max_tokens", "messages", "model", "system"]
    assert "test-key" not in record["stdout"] + record["stderr"]
