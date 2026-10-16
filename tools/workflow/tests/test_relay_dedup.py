"""Lossless ``dedup-sha256/1`` transport encoding for live relay role calls.

Live sub-wave 2 measured relay bodies over the 88,000-byte client cap (reviewer 122,678 B,
repairer 115,288 B, planner 113,947 B: ``live-role-payload-sizes.json``) because the
controller payload carries the ~55.5 KB of skill documentation twice, under ``documentation``
and again under ``review_evidence(_request).sources``. The payloads here mirror those measured
shapes key by key; nothing reaches Kong (the KongRoles worker is a scripted fake).
"""

import hashlib
import importlib.util
import itertools
import json
import random
from pathlib import Path

import pytest

from tools.workflow import review_evidence
from tools.workflow.agents import role_prompt
from tools.workflow.contracts import Refused, digest
from tools.workflow.kong_roles import LIVE_ROLE_MAX_TOKENS, PINNED_PRICES, KongRoles
from tools.workflow.relay_roles import (
    DEDUP_ENCODING,
    DEFAULT_MAX_BODY,
    ENCODED_KEYS,
    KONG_RECEIPT_FIELDS,
    PLAIN_KEYS,
    RECEIPT_FIELDS,
    RELAY_RECEIPT_FIELDS,
    SHARED_KEY,
    DedupMismatch,
    RelayRoles,
    dedup_decode,
    dedup_encode,
)

ROOT = Path(__file__).resolve().parents[3]
TOKEN = "d" * 43
RUN = "relay-run-dedup"
KNOWN_GOOD_BODY_BYTES = 97_600  # the Kong kit's largest body observed to pass
MEASURED = {"planner": 113_947, "repairer": 115_288, "reviewer": 122_678}
IDS = itertools.count(1)


def load_host():
    spec = importlib.util.spec_from_file_location(
        "kong_relay_host_dedup", ROOT / "relay" / "kong_relay_host.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


host = load_host()


# ------------------------------------------------------------------ measured payload shapes


def text_of(size, seed):
    """Deterministic doc-like text whose json.dumps form is exactly ``size`` bytes."""
    rng = random.Random(seed)
    words = ["rolling", "window", "onset", "rainfall", "dekad", '"quoted"', "mm", "SOS"]
    out, length = [], 2  # the two JSON quotes
    while length < size:
        word = rng.choice(words) + ("\n" if rng.random() < 0.1 else " ")
        out.append(word)
        length += len(json.dumps(word)) - 2
    text = "".join(out)
    while len(json.dumps(text)) > size:
        text = text[:-1]
    return text + "x" * (size - len(json.dumps(text)))


def filler(size, seed=0):
    return text_of(size, f"filler-{seed}")


# documentation key sizes from the real run-copy reviewer input (4 skill docs) plus the bound
# definition text, sized so json.dumps(documentation) is the measured 55,479 bytes.
DOC_SIZES = {
    "onset-date": 25_226,
    "plot-onset": 6_727,
    "check-artifact": 12_058,
    "verify-run": 6_961,
}


def documentation():
    docs = {name: text_of(size, name) for name, size in DOC_SIZES.items()}
    overhead = len(json.dumps({**docs, review_evidence.REFERENCE_DEFINITION_SOURCE_ID: ""}))
    definition = text_of(55_479 - overhead + 2, "definition")
    docs[review_evidence.REFERENCE_DEFINITION_SOURCE_ID] = definition
    assert len(json.dumps(docs)) == 55_479
    return docs


def evidence_request(docs):
    return {
        "schema": review_evidence.SCHEMA,
        "sources": review_evidence.sources_from_docs(docs),
        "instruction": filler(400, "instr"),
    }


def payload_for(role):
    docs = documentation()
    common = {
        "execution_context": {"note": filler(212, "ctx")},
        "source": {"id": filler(160, "src")},
        "documentation": docs,
        "date_contract": {"rule": filler(366, "date")},
    }
    if role in ("planner", "repairer"):
        value = {
            "execution_context": common["execution_context"],
            "goal": {"text": filler(300, "goal")},
            "goal_hash": "0" * 64,
            "source": common["source"],
            "documentation": docs,
            "date_contract": common["date_contract"],
            "profile_template": {"t": filler(440, "profile")},
            "instruction": filler(91 if role == "planner" else 285, "planner-instr"),
            "review_evidence_request": evidence_request(docs),
        }
        if role == "repairer":
            value["failure"] = {"finding": filler(650, "failure")}
            value["prior_plan"] = {"p": filler(440, "prior")}
        return value
    sources = review_evidence.sources_from_docs(docs)
    return {
        "execution_context": common["execution_context"],
        "binding_hash": "1" * 64,
        "request": {"r": filler(5_670, "request")},
        "plan": {"p": filler(254, "plan")},
        "source": common["source"],
        "source_gate": {"g": filler(290, "gate")},
        "documentation": docs,
        "date_contract": common["date_contract"],
        "preflight": {"ok": True},
        "prior_review": None,
        "repair_response": None,
        "controller_gates": {"g": filler(95, "cg")},
        "review_evidence": {
            "schema": review_evidence.SCHEMA,
            "binding_hash": "1" * 64,
            "plan_claims": [{"claim_id": "c1", "text": filler(900, "claim")}],
            "sources": sources,
            "unsupported": ["claim_entailment"],
            "capability": "PRE_REVIEW_CLAIM_GATES",
        },
        "claim_check": {"decision": "PRESENT_FOR_REVIEW", "detail": filler(1_790, "cc")},
    }


def plain_body(role, payload):
    return json.dumps({"role": role, "payload": payload}, allow_nan=False).encode()


def encoded_body(role, payload):
    return json.dumps({"role": role, **dedup_encode(payload)}, allow_nan=False).encode()


# ------------------------------------------------------------------ codec


@pytest.mark.parametrize("role", sorted(MEASURED))
def test_measured_shapes_round_trip_and_fit_well_under_the_cap(role):
    payload = payload_for(role)
    plain = plain_body(role, payload)
    # The fixture mirrors the measured body to within 2%; over the cap without dedup.
    assert abs(len(plain) - MEASURED[role]) <= 0.02 * MEASURED[role]
    assert len(plain) > DEFAULT_MAX_BODY
    encoded = dedup_encode(payload)
    body = encoded_body(role, payload)
    assert set(json.loads(body)) == ENCODED_KEYS
    assert len(body) < 70_000 < DEFAULT_MAX_BODY  # well under the 88,000 cap
    # Every one of the five docs is shared once; nothing else repeats at >= 2 KB.
    assert len(encoded["shared"]) == 5
    decoded = dedup_decode(json.loads(body))
    assert decoded == payload and json.dumps(decoded) == json.dumps(payload)
    assert digest(decoded) == digest(payload) == encoded["original_sha256"]


def test_encoding_is_deterministic_and_keyed_by_the_value_text():
    payload = payload_for("reviewer")
    first, second = dedup_encode(payload), dedup_encode(json.loads(json.dumps(payload)))
    assert json.dumps(first) == json.dumps(second)
    for ref, value in first["shared"].items():
        assert ref == hashlib.sha256(json.dumps(value).encode()).hexdigest()
    # First-appearance order: the documentation dict comes before review_evidence.
    order = list(first["shared"])
    names = list(payload["documentation"])
    assert order == [
        hashlib.sha256(json.dumps(payload["documentation"][n]).encode()).hexdigest() for n in names
    ]


def test_whole_repeated_subtree_is_shared_once_with_nested_references():
    doc = "y" * 5000
    block = {"a": doc, "b": [1, 2, 3]}
    payload = {"one": block, "two": block, "three": doc, "small": "z" * 10}
    encoded = dedup_encode(payload)
    assert len(encoded["shared"]) == 2  # the block and the doc, each once
    assert dedup_decode({"role": "planner", **encoded}) == payload
    text = json.dumps(encoded)
    assert text.count(json.dumps(doc)) == 1


def test_nothing_repeating_or_a_reference_shaped_payload_is_sent_plain():
    assert dedup_encode({"a": "x" * 5000, "b": "y" * 5000}) is None  # large but unique
    assert dedup_encode({"a": "x" * 100, "b": "x" * 100}) is None  # repeats below 2 KB
    # A payload that itself holds {"$shared": ...} could not be told apart: never encoded.
    tricky = {"a": "x" * 5000, "b": "x" * 5000, "c": {SHARED_KEY: "0" * 64}}
    assert dedup_encode(tricky) is None
    # The measured evidence_checker shape: sources once, documentation absent.
    docs = documentation()
    checker = {"sources": review_evidence.sources_from_docs(docs), "instruction": "x"}
    assert dedup_encode(checker) is None


def tampered(change):
    request = {"role": "reviewer", **dedup_encode(payload_for("reviewer"))}
    change(request)
    return request


def _first(request):
    return next(iter(request["shared"]))


@pytest.mark.parametrize(
    "change",
    [
        lambda r: r.update(original_sha256="0" * 64),
        lambda r: r.update(original_text_sha256="0" * 64),
        lambda r: r["shared"].__setitem__(_first(r), "tampered text"),
        lambda r: r["shared"].__setitem__("f" * 64, "unreferenced"),
        lambda r: r["payload"].__setitem__("documentation", {SHARED_KEY: "e" * 64}),
        lambda r: r["payload"]["documentation"].pop("plot-onset"),
        lambda r: r.update(transport_encoding="dedup-sha256/2"),
        lambda r: r.update(shared={}),
    ],
)
def test_any_mismatch_is_refused_on_decode(change):
    with pytest.raises(DedupMismatch):
        dedup_decode(tampered(change))


def test_cyclic_references_are_refused():
    a, b = "a" * 64, "b" * 64
    request = {
        "role": "planner",
        "payload": {"x": {SHARED_KEY: a}},
        "transport_encoding": DEDUP_ENCODING,
        "shared": {a: {"n": {SHARED_KEY: b}}, b: {"n": {SHARED_KEY: a}}},
        "original_sha256": "0" * 64,
        "original_text_sha256": "0" * 64,
    }
    with pytest.raises(DedupMismatch, match="cyclic"):
        dedup_decode(request)


# ------------------------------------------------------------------ host relay end to end


ANSWER = {"verdict": "PASS", "binding_hash": "h", "scope": "data-replay"}
SENT_SETTINGS = {"model": "claude-sonnet-4-6", "max_tokens": 2048, "extra_body": {"temperature": 0}}


def worker_envelope(**changes):
    value = {
        "sent": True,
        "ok": True,
        "model": "claude-sonnet-4-6",
        "response_id": f"msg_dedup_{next(IDS)}",
        "stop_reason": "tool_use",  # the forced answer tool (role_schemas)
        "usage": {"input_tokens": 20_000, "output_tokens": 300},
        "text": json.dumps(ANSWER, sort_keys=True),  # role_schemas.answer_text
        "answer_source": "tool_use.input",
        "tool_use_count": 1,
        "content_types": ["tool_use"],
        "via": "1.1 kong/fake",
        "request_id": "req-fake",
        "latency_ms": 5,
        "sdk": "anthropic-python 0.0",
        "mock": True,
        "sent_settings": SENT_SETTINGS,
        "sent_keys": ["extra_body", "max_tokens", "messages", "model", "system"],
    }
    value.update(changes)
    return value


class Process:
    def __init__(self, *outputs):
        self.outputs = list(outputs)
        self.calls = []

    def run(self, argv, cwd, env, stdin):
        self.calls.append(json.loads(stdin))
        return {"exit_code": 0, "stdout": json.dumps(self.outputs.pop(0)), "stderr": ""}


@pytest.fixture
def relay(tmp_path):
    started = []

    def start(*outputs, allowed=("planner", "repairer", "reviewer", "verifier")):
        python = tmp_path / "python.exe"
        python.write_text("")
        process = Process(*outputs)
        roles = KongRoles(
            process,
            tmp_path / "ctx",
            budget=5,
            temperature=0,
            prices=PINNED_PRICES,
            role_max_tokens=LIVE_ROLE_MAX_TOKENS,
            session_source="response_id",
            data_policy_ack=True,
            kit_python=python,
        )
        audit = host.AuditLog(tmp_path / "audit.jsonl")
        relay = host.Relay(roles, run_id=RUN, token=TOKEN, allowed_roles=allowed, audit=audit)
        servers = host.serve(relay, ["127.0.0.1"], 0)
        started.extend(servers)
        return f"http://127.0.0.1:{servers[0].server_address[1]}", relay, process

    yield start
    for server in started:
        server.shutdown()
        server.server_close()


def asks(tmp_path):
    lines = (tmp_path / "audit.jsonl").read_text().splitlines()
    return [x for x in map(json.loads, lines) if x["event"] == "ask"]


def test_receipt_fields_are_the_kong_receipt_plus_the_relay_fields():
    assert RECEIPT_FIELDS == KONG_RECEIPT_FIELDS | RELAY_RECEIPT_FIELDS
    assert RELAY_RECEIPT_FIELDS == {"relay_id", "transport_encoding", "transport_bytes"}


@pytest.mark.parametrize("role", sorted(MEASURED))
def test_dedup_call_end_to_end_model_sees_each_block_once(tmp_path, relay, role):
    url, served, process = relay(worker_envelope())
    payload = payload_for(role)
    agent = RelayRoles(url, TOKEN, RUN, budget=5)
    assert agent.ask(role, payload) == ANSWER
    (receipt,) = agent.receipts
    (call,) = process.calls
    user, system = call["user"], call["system"]
    # Controller bindings untouched: input_hash, stdin_hash and the system prompt are the
    # controller payload's; only the user message is the model-facing rendering.
    logical = json.dumps(payload)
    assert receipt["input_hash"] == digest(payload)
    assert receipt["stdin_hash"] == review_evidence.text_hash(logical)
    assert system == role_prompt(role, payload)
    if role == "reviewer":
        assert receipt["dispatch"]["stdin_hash"] == review_evidence.text_hash(logical)
    # Every shared document appears in the model-facing text exactly once.
    for text in payload["documentation"].values():
        assert user.count(json.dumps(text)) == 1
    assert user.startswith(host.MODEL_INPUT_PREAMBLE)
    assert "=== SHARED DOCUMENTS (referenced by $shared sha256; 5 blocks) ===" in user
    # The kit's pre-send check sees a gateway body under its known-good size.
    gateway = {
        "model": call["model"],
        "max_tokens": call["max_tokens"],
        "system": system,
        "messages": [{"role": "user", "content": user}],
        "extra_body": {"temperature": 0},
    }
    assert len(json.dumps(gateway).encode()) < KNOWN_GOOD_BODY_BYTES
    # The receipt and the audit ask line record the encoding and the sizes.
    sizes = receipt["transport_bytes"]
    assert receipt["transport_encoding"] == DEDUP_ENCODING
    assert sizes["payload_bytes"] == len(logical.encode())
    assert sizes["model_facing_bytes"] == len(user.encode())
    assert sizes["model_facing_hash"] == review_evidence.text_hash(user)
    assert sizes["request_body_bytes"] < 70_000 and sizes["shared_blocks"] == 5
    (line,) = asks(tmp_path)
    assert line["outcome"] == "SUCCESS" and line["transport_encoding"] == DEDUP_ENCODING
    assert line["transport_bytes"] == sizes
    assert line["settings_sent"] == SENT_SETTINGS and line["temperature_requested"] == 0
    assert line["sent_keys"] == ["extra_body", "max_tokens", "messages", "model", "system"]
    raw = (tmp_path / "audit.jsonl").read_text()
    assert payload["documentation"]["plot-onset"][:200] not in raw  # never the content


def test_plain_call_has_null_encoding_and_settings_on_the_audit_line(tmp_path, relay):
    url, _, process = relay(worker_envelope())
    payload = {"artifact_manifest": ["synthetic gate PASS"], "binding_hash": "synthetic"}
    agent = RelayRoles(url, TOKEN, RUN, budget=5)
    assert agent.ask("verifier", payload) == ANSWER
    (receipt,) = agent.receipts
    assert receipt["transport_encoding"] is None
    assert receipt["transport_bytes"]["shared_blocks"] == 0
    assert process.calls[0]["user"] == json.dumps(payload)  # byte-identical to before
    (line,) = asks(tmp_path)
    assert line["transport_encoding"] is None
    assert line["settings_sent"]["extra_body"] == {"temperature": 0}
    assert "extra_body" in line["sent_keys"]


def test_settings_are_on_a_refused_after_send_audit_line_too(tmp_path, relay):
    url, _, _ = relay(worker_envelope(text="not json"))
    agent = RelayRoles(url, TOKEN, RUN, budget=5)
    with pytest.raises(Refused):
        agent.ask("verifier", {"x": 1})
    (line,) = asks(tmp_path)
    assert line["outcome"] == "REFUSED" and line["settings_sent"] == SENT_SETTINGS


def post(relay_obj, request):
    status, body = relay_obj.ask(json.dumps(request).encode(), "127.0.0.1")
    relay_obj.responded()
    return status, body


def test_host_refuses_a_mismatched_encoding_deterministically(tmp_path, relay):
    _, served, process = relay(worker_envelope())
    request = tampered(lambda r: r.update(original_sha256="0" * 64))
    status, body = post(served, request)
    assert status == 400 and body["outcome"] == "REFUSED"
    assert body["sent_to_gateway"] is False and body["reason_code"] == "dedup_mismatch"
    assert process.calls == [] and served.guard.requests == 0
    (line,) = asks(tmp_path)
    assert line["reason_code"] == "dedup_mismatch" and line["fault_class"] == "DETERMINISTIC"
    assert line["sent_to_gateway"] is False


def test_client_maps_a_dedup_refusal_to_the_role_gate_and_releases(tmp_path, relay):
    url, served, process = relay(worker_envelope())
    agent = RelayRoles(url, TOKEN, RUN, budget=5)
    original = dedup_encode

    def corrupt(payload, *args):
        value = original(payload, *args)
        if value is not None:
            value["original_text_sha256"] = "0" * 64
        return value

    import tools.workflow.relay_roles as module

    module.dedup_encode, saved = corrupt, module.dedup_encode
    try:
        with pytest.raises(Refused, match="does not verify") as caught:
            agent.ask("reviewer", payload_for("reviewer"))
    finally:
        module.dedup_encode = saved
    assert caught.value.gate == "review" and agent.spent == 0 and process.calls == []


def test_client_refuses_a_receipt_whose_encoding_differs_from_the_request():
    agent = RelayRoles("http://127.0.0.1:1", TOKEN, RUN, budget=5)
    payload = payload_for("planner")
    body = encoded_body("planner", payload)
    receipt = {key: None for key in RECEIPT_FIELDS}
    receipt.update(
        role="planner",
        session_id="s",
        response_id="s",
        input_hash=digest(payload),
        output_hash=digest(ANSWER),
        model_observed="claude-sonnet-4-6",
        transport="kong-native-anthropic",
        mode="live",
        relay_id="r",
        transport_encoding=None,  # the relay claims it got a plain body
        transport_bytes={"request_body_bytes": len(body)},
    )
    reply = {"outcome": "SUCCESS", "relay_id": "r", "value": ANSWER, "receipt": receipt}

    class Fixed:
        serial = 0

        def run(self, *args):
            self.serial += 1
            return 200, json.dumps(reply).encode()

    agent.processes = Fixed()
    with pytest.raises(Refused, match="transport encoding differs"):
        agent.ask("planner", payload)


def test_kong_roles_refuses_an_empty_model_input_before_launch(tmp_path, relay):
    _, served, process = relay(worker_envelope())
    for bad in ("", b"bytes", 7):
        with pytest.raises(Refused) as caught:
            served.roles.ask("verifier", {"x": 1}, model_input=bad)
        assert caught.value.gate == "transition"
    assert process.calls == [] and served.roles.spent == 0


def test_plain_body_keys_are_unchanged_when_nothing_repeats():
    assert PLAIN_KEYS == {"role", "payload"}
    assert ENCODED_KEYS - PLAIN_KEYS == {
        "transport_encoding",
        "shared",
        "original_sha256",
        "original_text_sha256",
    }
