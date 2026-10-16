import hashlib
import importlib.util
import json
import threading
from http.server import ThreadingHTTPServer

import pytest

from tools.workflow.agents import PROMPTS, role_prompt
from tools.workflow.contracts import Refused, digest
from tools.workflow.kong_roles import (
    ANSWER_EXCERPT_CHARS,
    COST_BASIS,
    KIT_PYTHON,
    KIT_ROOT,
    LIVE_ROLE_MAX_TOKENS,
    PINNED_PRICES,
    WORKER_SOURCE,
    KongOutcomeUnknown,
    KongRoles,
    answer_diagnostic,
    answer_shape,
)
from tools.workflow.processes import Processes
from tools.workflow.review_evidence import text_hash
from tools.workflow.role_schemas import answer_text, answer_tool, tool_hash

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
        "stop_reason": "tool_use",  # the forced answer tool (role_schemas)
        "usage": {"input_tokens": 1000, "output_tokens": 100},
        "text": json.dumps(ANSWER, sort_keys=True),  # role_schemas.answer_text
        "answer_source": "tool_use.input",
        "tool_use_count": 1,
        "content_types": ["tool_use"],
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
    # no key; the answer tool the worker forces (tools + tool_choice)
    assert set(sent) == {"model", "max_tokens", "temperature", "system", "user", "answer_tool"}
    tool = answer_tool("verifier", {"manifest": [1, 2, 3]})
    assert sent["answer_tool"] == tool
    settings = receipt["settings"]
    assert settings["structured_output"] == "forced_tool"
    assert settings["answer_tool"] == "submit_verifier_answer"
    assert settings["answer_tool_hash"] == tool_hash(tool)
    assert settings["answer_source"] == "tool_use.input"
    # output_hash is the sha256 of the exact text the parser was handed
    text = envelope()["text"]
    assert hashlib.sha256(text.encode()).hexdigest() == receipt["output_hash"]
    assert agent.provenance()["structured_output"] == "forced_tool"


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


class GuardedProcess(Process):
    """The process manager's RAM/time guard: refused before launch, or stopped after it."""

    def __init__(self, launched):
        super().__init__()
        self.serial = 0
        self.launched = launched

    def run(self, argv, cwd, env, stdin):
        if self.launched:
            self.serial += 1
            raise Refused("time budget exhausted", gate="resource_guard")
        raise Refused("insufficient free RAM to start a child", gate="resource_guard")


@pytest.mark.parametrize("launched", [False, True], ids=["refused_at_launch", "stopped"])
def test_process_manager_resource_guard_is_resource_guard_never_unknown(tmp_path, launched):
    """Owner ruling role_cli_resource_gate_2026-10-14: the worker refused or stopped by the
    RAM floor / wall-time guard raises the Refused with gate resource_guard."""
    agent = roles(tmp_path, GuardedProcess(launched))
    with pytest.raises(Refused) as caught:
        agent.ask("verifier", {})
    assert caught.value.gate == "resource_guard"
    assert not isinstance(caught.value, KongOutcomeUnknown) and agent.receipts == []
    if launched:  # the request may have left: reservation kept, outcome recorded UNKNOWN
        assert agent.spent == agent.budget
        (record,) = agent.unknown_outcomes
        assert record["outcome"] == "UNKNOWN" and record["failed_gate"] == "resource_guard"
    else:  # nothing launched, so nothing was sent
        assert agent.spent == 0.0 and agent.unknown_outcomes == []


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
        {"text": 'Verdict:\n```json\n{"verdict":"PASS"}\n```'},
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


def test_empty_answer_is_named_and_its_usage_is_recorded(tmp_path):
    usage = {"input_tokens": 12_433, "output_tokens": 0}
    changes = {"text": "", "stop_reason": "refusal", "content_types": [], "usage": usage}
    # The plain text path; the forced-tool path names the refusal first (tests below).
    agent = roles(tmp_path, Process(envelope(**changes)), structured_output=None)
    with pytest.raises(Refused, match=r"invalid verifier Kong response: empty answer") as caught:
        agent.ask("verifier", {})
    assert "stop_reason 'refusal'" in str(caught.value) and "output_tokens 0" in str(caught.value)
    last = agent.last_call
    # output_tokens 0 is what the gateway reported, recorded as observed, priced input-only
    assert last["usage"] == usage and last["cost_usd"] == pytest.approx(12_433 * 3 / 1e6)
    assert last["model_observed"] == "claude-sonnet-4-6" and last["stop_reason"] == "refusal"
    assert last["request_id"] == "req-fake" and last["response_id"] == "msg_fake"
    diagnostic = last["answer_diagnostic"]
    assert diagnostic["empty"] is True and diagnostic["text_chars"] == 0
    assert diagnostic["content_types"] == [] and diagnostic["output_tokens"] == 0


def test_answer_diagnostic_is_bounded_and_shape_drops_the_excerpt():
    text = "  ```python\n" + "a" * 1000 + "\n```  "
    diagnostic = answer_diagnostic(
        {
            "text": text,
            "stop_reason": "end_turn" + "x" * 200,
            "content_types": ["thinking", "text", *["text"] * 40],
            "usage": {"output_tokens": 300},
        }
    )
    assert diagnostic["fenced"] is True and diagnostic["empty"] is False
    assert diagnostic["starts_with_brace"] is False and diagnostic["text_chars"] == len(text)
    assert len(diagnostic["head"]) == len(diagnostic["tail"]) == ANSWER_EXCERPT_CHARS
    assert diagnostic["head"] == text[:300] and diagnostic["tail"] == text[-300:]
    assert len(diagnostic["stop_reason"]) == 64 and len(diagnostic["content_types"]) == 16
    assert diagnostic["content_types"][:2] == ["thinking", "text"]
    shape = answer_shape(diagnostic)
    assert "head" not in shape and "tail" not in shape and shape["text_chars"] == len(text)
    short = answer_diagnostic({"text": "{}"})
    assert short["head"] == "{}" and short["tail"] is None and short["content_types"] is None
    missing = answer_diagnostic({"text": None})
    assert missing["text_is_string"] is False and missing["head"] is None
    assert answer_shape(None) is None


@pytest.mark.parametrize(
    "text, form",
    [
        ('```json\n{"verdict": "PASS"}\n```', "json_fence"),
        ('\n\n  {"verdict": "PASS"}  \n', "bare"),
    ],
)
def test_single_json_fence_and_surrounding_whitespace_are_accepted(tmp_path, text, form):
    # The shared output rule (agents.parse_role_object) already tolerates both; the live
    # reviewer failure was not a fence (that would not fail at char 0 with 0 output tokens).
    agent = roles(tmp_path, Process(envelope(text=text)), structured_output=None)
    assert agent.ask("verifier", {}) == {"verdict": "PASS"}
    assert agent.receipts[0]["output_form"] == form
    assert "answer_diagnostic" not in agent.last_call


# ------------------------------------------------------------ forced-tool structured output
PROSE = "Let me review step by step.\n" * 200 + '```json\n{"verdict": "PASS"}\n```'


@pytest.mark.parametrize(
    "changes, reason",
    [
        # Run-4 shape: prose plus a trailing fence and no tool_use block at all.
        (
            {
                "text": PROSE,
                "answer_source": None,
                "tool_use_count": 0,
                "stop_reason": "end_turn",
                "content_types": ["text"],
            },
            r"no single submit_verifier_answer tool_use answer \(stop_reason 'end_turn', content "
            r"\['text'\], output_tokens 100, tool_use blocks 0\)",
        ),
        (
            {
                "text": "",
                "answer_source": None,
                "tool_use_count": 0,
                "stop_reason": "refusal",
                "content_types": [],
            },
            r"the model refused \(stop_reason 'refusal', content \[\], output_tokens 100\)",
        ),
        # A tool input cut off by max_tokens may still parse to a partial object.
        (
            {"stop_reason": "max_tokens"},
            r"forced tool answer incomplete \(stop_reason 'max_tokens'",
        ),
        ({"answer_source": None, "tool_use_count": 2}, r".*tool_use blocks 2"),
        # Not role_schemas.answer_text: output_hash would not be the hash of the parsed text.
        ({"text": json.dumps(ANSWER, sort_keys=True, indent=1)}, "tool answer text is not the"),
    ],
    ids=["prose_no_tool_use", "refusal", "max_tokens", "two_tool_uses", "non_canonical"],
)
def test_forced_tool_answer_is_required_and_refused_with_diagnostic(tmp_path, changes, reason):
    agent = roles(tmp_path, Process(envelope(**changes)))
    with pytest.raises(Refused, match=r"invalid verifier Kong response: " + reason):
        agent.ask("verifier", {})
    assert agent.receipts == [] and agent.last_call["sent"] is True
    diagnostic = agent.last_call["answer_diagnostic"]
    assert diagnostic["stop_reason"] == envelope(**changes)["stop_reason"]
    assert diagnostic["content_types"] == envelope(**changes)["content_types"]
    assert agent.last_call["structured_output"] == "forced_tool"


def test_forced_tool_answer_goes_through_the_unchanged_parser(tmp_path):
    finding = {"id": "R1", "severity": "blocking", "text": "t"}
    answer = {"verdict": "REJECT", "binding_hash": "b", "findings": [finding]}
    text = answer_text(answer)
    process = Process(envelope(text=text))
    agent = roles(tmp_path, process)
    payload = {"review_evidence": {"plan_hash": "p"}}
    assert agent.ask("reviewer", payload) == answer
    (receipt,) = agent.receipts
    assert receipt["output_form"] == "bare" and receipt["output_hash"] == digest(answer)
    assert hashlib.sha256(text.encode()).hexdigest() == digest(answer)
    # the reviewer's tool carries the review-evidence variant of its schema
    tool = process.calls[0]["stdin"]["answer_tool"]
    assert tool == answer_tool("reviewer", payload)
    assert tool["input_schema"]["required"] == ["verdict"]
    assert receipt["settings"]["system_prompt_hash"] == text_hash(role_prompt("reviewer", payload))
    with pytest.raises(ValueError):
        roles(tmp_path, Process(), structured_output="json_mode")


def test_worker_source_forces_the_answer_tool_and_reads_tool_use_input():
    for fragment in (
        'body["tools"] = [tool]',
        'body["tool_choice"] = {"type": "tool", "name": tool["name"]}',
        "json.dumps(answer, sort_keys=True, allow_nan=False)",
        'out["answer_source"] = "tool_use.input"',
        'out["tool_use_count"] = len(uses)',
    ):
        assert fragment in WORKER_SOURCE


def test_worker_records_content_block_types():
    assert "content_types=" in WORKER_SOURCE and "[:16]" in WORKER_SOURCE
    compile(WORKER_SOURCE, "<worker>", "exec")


def test_policy_ack_prices_and_prompt_variants_are_enforced(tmp_path):
    with pytest.raises(Refused, match="data policy"):
        KongRoles(Process(), tmp_path)
    with pytest.raises(Refused, match="cited source"):
        roles(tmp_path, Process(), prices={"m": {"input_per_mtok": 1, "output_per_mtok": 1}})
    process = Process(envelope(), envelope())
    agent = roles(tmp_path, process)
    for payload in ({"review_evidence": {}}, {"review_evidence_request": {}}):
        agent.ask("reviewer", payload)  # shared role_prompt variants, no longer refused
    assert process.calls[0]["stdin"]["system"] == role_prompt("reviewer", {"review_evidence": {}})
    with pytest.raises(Refused):
        agent.ask("not-a-role", {})


def test_pinned_list_price_reconciles_so_a_priced_run_makes_many_calls(tmp_path):
    python = tmp_path / "python.exe"
    python.write_text("")
    process = Process(*[envelope(response_id=f"msg_{i}") for i in range(12)])
    agent = KongRoles(process, tmp_path / "ctx", budget=5, data_policy_ack=True, kit_python=python)
    for _ in range(12):
        assert agent.ask("verifier", {}) == ANSWER
    assert agent.prices == PINNED_PRICES and len(agent.receipts) == 12
    assert {r["cost_basis"] for r in agent.receipts} == {COST_BASIS}
    assert COST_BASIS == "Anthropic list-price estimate; Kong billing not observed"
    assert agent.spent == pytest.approx(12 * (1000 * 3 + 100 * 15) / 1e6)
    off = KongRoles(Process(), tmp_path / "ctx", data_policy_ack=True, budget_guard="off")
    assert off.prices == {}  # untracked runs get no table by default


def test_role_max_tokens_and_temperature_reach_request_and_receipt(tmp_path):
    process = Process(envelope(), envelope())
    agent = roles(
        tmp_path, process, role_max_tokens=LIVE_ROLE_MAX_TOKENS, temperature=0, max_tokens=2048
    )
    agent.ask("planner", {})
    agent.ask("verifier", {})
    assert [c["stdin"]["max_tokens"] for c in process.calls] == [4096, 2048]
    assert [c["stdin"]["temperature"] for c in process.calls] == [0, 0]
    assert [r["settings"]["max_tokens"] for r in agent.receipts] == [4096, 2048]
    assert agent.provenance()["role_max_tokens"] == {"planner": 4096, "repairer": 4096}
    for bad in ({"nope": 10}, {"planner": 0}, {"planner": 64001}, {"planner": 1.5}):
        with pytest.raises(ValueError):
            roles(tmp_path, Process(), role_max_tokens=bad)


def test_response_id_session_source_is_observed_and_required(tmp_path):
    process = Process(envelope(response_id="msg_a"), envelope(response_id=None))
    agent = roles(tmp_path, process, session_source="response_id")
    agent.ask("verifier", {})
    assert agent.receipts[0]["session_id"] == "msg_a" == agent.receipts[0]["response_id"]
    with pytest.raises(Refused, match="no response id"):
        agent.ask("verifier", {})
    assert len(agent.receipts) == 1 and agent.last_call["sent"] is True
    with pytest.raises(ValueError):
        roles(tmp_path, Process(), session_source="minted")


def test_last_call_reports_whether_anything_can_have_left(tmp_path):
    agent = roles(tmp_path, Process({"sent": False, "error_type": "E", "error": {}}))
    with pytest.raises(Refused):
        agent.ask("verifier", {})
    assert agent.last_call["sent"] is False
    agent = roles(tmp_path, Process(error=TimeoutError("killed")))
    with pytest.raises(KongOutcomeUnknown):
        agent.ask("verifier", {})
    assert agent.last_call["sent"] is None  # may have left: unknown, never False
    agent = roles(tmp_path, Process(envelope()))
    agent.ask("verifier", {})
    assert agent.last_call["sent"] is True and agent.last_call["cost_usd"] == pytest.approx(0.0045)


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
    agent.structured_output = None  # the plain text path
    with pytest.raises(Refused, match="invalid verifier Kong response"):
        agent.ask("verifier", {"synthetic": True})  # the mock answers plain "OK", not JSON
    record = json.loads(next((tmp_path / "proc").glob("process-*.json")).read_text())
    worker = json.loads(record["stdout"])
    assert worker["sent"] and worker["ok"] and worker["mock"] is True
    assert "tools" not in worker["sent_keys"] and "answer_source" not in worker
    assert worker["model"] == "claude-sonnet-4-6" and worker["via"] == "1.1 kong/mock"
    assert "test-key" not in record["stdout"] + record["stderr"]


@needs_kit
def test_real_worker_sends_temperature_through_the_sdk_passthrough(tmp_path, mock_gateway):
    # anthropic-python 1.13 Messages.create has no typed temperature argument (this test used
    # to pin that it was refused pre-send). The live-wave brief sets temperature 0 for every
    # role, so the worker now sends the API field through the SDK's extra_body and echoes it.
    (tmp_path / "proc").mkdir()
    agent = mock_roles(tmp_path, mock_gateway)
    agent.temperature = 0.0
    # Forced tool (default): the kit mock answers any forced tool with a fixed input.
    assert agent.ask("verifier", {"synthetic": True}) == {"location": "Seattle"}
    record = json.loads(next((tmp_path / "proc").glob("process-*.json")).read_text())
    worker = json.loads(record["stdout"])
    assert worker["sent"] is True and "temperature" not in worker["sent_settings"]
    assert worker["sent_settings"]["extra_body"] == {"temperature": 0.0}
    assert {"extra_body", "tools", "tool_choice"} <= set(worker["sent_keys"])
    choice = worker["sent_settings"]["tool_choice"]
    assert choice == {"type": "tool", "name": "submit_verifier_answer"}
    assert worker["stop_reason"] == "tool_use" and worker["answer_source"] == "tool_use.input"
    assert worker["text"] == answer_text({"location": "Seattle"})
    receipt = agent.receipts[-1]
    assert receipt["settings"]["structured_output"] == "forced_tool"
    assert receipt["output_hash"] == digest({"location": "Seattle"})


@needs_kit
def test_real_worker_gateway_error_is_unknown(tmp_path, mock_gateway):
    (tmp_path / "proc").mkdir()
    agent = mock_roles(tmp_path, mock_gateway)
    agent.models["verifier"] = "claude-not-routable"  # bypasses the roster check on purpose
    with pytest.raises(KongOutcomeUnknown) as caught:
        agent.ask("verifier", {"synthetic": True})
    assert caught.value.evidence["status"] == 404
    assert caught.value.evidence["via"] == "1.1 kong/mock"
