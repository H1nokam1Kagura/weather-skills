"""Relay DEDUP smoke, offline (no docker, no network beyond 127.0.0.1, no secret, no Kong).

Pins: (1) the synthetic payload is reviewer-sized, needs dedup, and the production client
(RelayRoles) actually sends it ``dedup-sha256/1``-encoded under the body cap; (2) the canary
lives ONLY in the shared block, never in the role-input section the model reads first; (3) the
report passes on evidence recorded from a full offline pass (real RelayRoles -> real host
Relay -> real KongRoles with a fake worker) and fails closed when the canary is missing, the
body was not encoded, or the other guarantees break; (4) the workflow job is wired exactly
like relay-smoke (secret scoping, isolation, key scan) and runs only for its selector.
"""

import copy
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
for _path in (str(HERE.parent), str(REPO)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import launch  # noqa: E402
import relay_dedup_smoke as smoke  # noqa: E402
import relay_dedup_smoke_report as report  # noqa: E402
import relay_smoke_report  # noqa: E402

from relay import kong_relay_host as host  # noqa: E402
from tools.workflow.contracts import Refused  # noqa: E402
from tools.workflow.kong_roles import PINNED_PRICES, KongRoles  # noqa: E402
from tools.workflow.relay_roles import (  # noqa: E402
    DEDUP_ENCODING,
    DEFAULT_MAX_BODY,
    RelayRoles,
    dedup_decode,
)

WORKFLOW = REPO / ".github" / "workflows" / "simkit-cases.yml"
CANARY = "canary-0123456789abcdef01234567"  # fixed for the offline tests; live runs are random
TOKEN = "e" * 43
RUN = "relay-run-dedup-smoke"
SENT_SETTINGS = {
    "model": "claude-sonnet-4-6",
    "max_tokens": 512,
    "extra_body": {"temperature": 0.0},
}


# ------------------------------------------------------------------ payload construction


def test_payload_is_reviewer_sized_and_needs_dedup():
    payload = smoke.build_payload(CANARY)
    p = smoke.predict(payload, CANARY)
    # Measured reviewer: 122,678 B logical, documentation 55,479 B (live-role-payload-sizes).
    assert 110_000 < p["logical_body_bytes"] < 135_000
    assert p["logical_body_bytes"] > DEFAULT_MAX_BODY  # would be refused plain
    assert 54_000 < len(json.dumps(payload["documentation"])) < 58_000
    assert p["transport_encoding"] == DEDUP_ENCODING and p["shared_blocks"] == 1
    assert 60_000 < p["request_body_bytes"] < DEFAULT_MAX_BODY  # ~70 KB, under the cap
    assert p["gateway_body_bytes_bound"] < smoke.KNOWN_GOOD_GATEWAY_BODY_BYTES
    assert p["reference_count_in_role_input"] == 2  # documentation + review_evidence.sources


def test_sizes_do_not_depend_on_the_random_canary():
    a, b = smoke.make_canary(), smoke.make_canary()
    assert a != b and len(a) == len(b)
    pa = smoke.predict(smoke.build_payload(a), a)
    pb = smoke.predict(smoke.build_payload(b), b)
    for key in ("logical_body_bytes", "request_body_bytes", "model_facing_bytes", "shared_bytes"):
        assert pa[key] == pb[key]


def test_canary_only_in_the_shared_block():
    payload = smoke.build_payload(CANARY)
    doc = payload["documentation"][smoke.DOC_NAME]
    assert doc.count(CANARY) == 1
    assert payload["review_evidence"]["sources"][0]["text"] == doc
    # The canary is only ever inside the documentation string (which occurs twice).
    without_doc = copy.deepcopy(payload)
    without_doc["documentation"] = {}
    without_doc["review_evidence"]["sources"][0]["text"] = ""
    assert CANARY not in json.dumps(without_doc)
    assert CANARY not in smoke.INSTRUCTION
    p = smoke.predict(payload, CANARY)
    assert p["canary_in_role_input_section"] is False
    assert p["canary_in_shared_blocks"] == 1 and p["canary_in_request_body"] == 1
    assert p["canary_in_model_facing"] == 1
    assert 0.55 < p["canary_offset_in_shared_block"] / p["shared_bytes"] < 0.70
    # In the model-facing rendering the canary sits after the shared-documents header only.
    _, render = smoke._prompt_and_render()
    from tools.workflow.relay_roles import dedup_encode

    encoded = dedup_encode(payload)
    user = render(encoded["payload"], encoded["shared"])
    head, _, tail = user.partition("=== SHARED DOCUMENTS")
    assert CANARY not in head and tail.count(CANARY) == 1


def test_production_client_sends_it_dedup_encoded(monkeypatch):
    """RelayRoles itself (not the test's own encoder) produces the encoded body."""
    payload = smoke.build_payload(CANARY)
    roles = RelayRoles("http://127.0.0.1:9", TOKEN, RUN, budget=1)
    sent = []

    def run(method, path, body, headers):
        roles.processes.serial += 1
        sent.append(body)
        refusal = {"outcome": "REFUSED", "relay_id": "r1", "sent_to_gateway": False}
        return 422, json.dumps(refusal).encode()

    monkeypatch.setattr(roles.processes, "run", run)
    with pytest.raises(Refused):
        roles.ask("reviewer", payload)
    (body,) = sent
    request = json.loads(body)
    assert request["transport_encoding"] == DEDUP_ENCODING and request["role"] == "reviewer"
    assert len(body) == smoke.predict(payload, CANARY)["request_body_bytes"] < DEFAULT_MAX_BODY
    assert dedup_decode(request) == payload  # lossless
    assert body.decode().count(CANARY) == 1
    assert CANARY not in json.dumps(request["payload"])
    assert sum(json.dumps(v).count(CANARY) for v in request["shared"].values()) == 1


# ------------------------------------------------------------------ offline pass -> evidence


class Worker:
    """Fake kit worker: answers like a model that resolved the reference (or not)."""

    def __init__(self, quote=True, override=None):
        self.quote = quote
        self.override = override or {}  # envelope fields of an invalid answer
        self.calls = []

    def run(self, argv, cwd, env, stdin):
        request = json.loads(stdin)
        self.calls.append(request)
        found = re.search(r"Transport integrity marker: (canary-[0-9a-f]+)", request["user"])
        marker = found.group(1) if (found and self.quote) else "NOT FOUND"
        answer = {
            "verdict": "APPROVE",
            "binding_hash": smoke.BINDING_HASH,
            "findings": [smoke.CANARY_FINDING_PREFIX + marker],
        }
        envelope = {
            "sent": True,
            "ok": True,
            "model": "claude-sonnet-4-6",
            "response_id": f"msg_dedup_smoke_{len(self.calls)}",
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 17_000, "output_tokens": 90},
            "text": json.dumps(answer),
            "via": "1.1 kong/fake",
            "request_id": "req-fake",
            "latency_ms": 5,
            "sdk": "anthropic-python 0.0",
            "mock": False,  # recorded as a live receipt so the report's live checks apply
            "sent_settings": SENT_SETTINGS,
            "sent_keys": ["extra_body", "max_tokens", "messages", "model", "system"],
            "content_types": ["text"],
        }
        envelope.update(self.override)
        return {"exit_code": 0, "stdout": json.dumps(envelope), "stderr": ""}


def record_offline_run(tmp_path, quote=True, override=None):
    """Drive the real client -> relay -> KongRoles path offline and write the evidence files
    the workflow would (launch.json, the container JSON, key-scan.json, net.json, audit)."""
    python = tmp_path / "python.exe"
    python.write_text("")
    worker = Worker(quote, override)
    roles = KongRoles(
        worker,
        tmp_path / "ctx",
        budget=1,
        max_tokens=512,
        temperature=0.0,
        prices=PINNED_PRICES,
        session_source="response_id",
        data_policy_ack=True,
        kit_python=python,
    )
    relay_dir, out = tmp_path / "relay", tmp_path / "out"
    relay_dir.mkdir()
    out.mkdir()
    audit = host.AuditLog(relay_dir / "relay-audit.jsonl")
    relay = host.Relay(roles, run_id=RUN, token=TOKEN, allowed_roles=["reviewer"], audit=audit)
    (server,) = host.serve(relay, ["127.0.0.1"], 0)
    try:
        client = RelayRoles(f"http://127.0.0.1:{server.server_address[1]}", TOKEN, RUN, budget=1)
        payload = smoke.build_payload(CANARY)
        prediction = smoke.predict(payload, CANARY)
        system, user = prediction.pop("_system"), prediction.pop("_user")
        health = client.health()
        try:
            value, refused = client.ask("reviewer", payload), None
        except Refused as exc:
            value, refused = None, smoke.refused_fields(client, exc)
    finally:
        server.shutdown()
        server.server_close()
    audit.write("relay_stop", run_id=RUN, reason="max_requests reached")
    receipt = client.receipts[-1] if client.receipts else {}
    (call,) = worker.calls
    assert call["user"] == user and call["system"] == system  # prediction is exact
    probes = [
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
    container = {
        "schema": smoke.SCHEMA,
        "uid": 1000,
        "credential_like_env_names": [],
        "credential_like_files": [],
        "health": {k: health.get(k) for k in ("outcome", "run_id", "allowed_roles", "model")},
        "egress_probes": probes,
        "canary": CANARY,
        "binding_hash": smoke.BINDING_HASH,
        "prediction": prediction,
        "outcome": "SUCCESS",
        "answer": value,
        "receipt": {k: receipt.get(k) for k in smoke.RECEIPT_KEEP},
        "gateway_body_bytes": smoke.gateway_body_bytes(system, user, receipt.get("settings_sent")),
        "gateway_body_basis": "reconstructed",
    }
    if refused is not None:  # what relay_dedup_smoke.main records for a refused call
        for key in ("answer", "receipt", "gateway_body_bytes", "gateway_body_basis"):
            container.pop(key)
        container.update(refused)
    launch_rec = {
        "client": "relay_dedup_smoke",
        "run_conditions": {"never_privileged": True, "relay_only_network": True},
        "env_names_passed": sorted(relay_smoke_report.EXPECTED_ENV_NAMES),
        "smoke_parsed": True,
    }
    (out / "launch.json").write_text(json.dumps(launch_rec))
    (out / report.CONTAINER_JSON).write_text(json.dumps(container))
    (out / "key-scan.json").write_text(json.dumps({"key_absent": True}))
    (relay_dir / "net.json").write_text(json.dumps({"internal": True}))
    return out, relay_dir


@pytest.fixture
def recorded(tmp_path, monkeypatch):
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    return record_offline_run(tmp_path)


def edit(path, fn):
    data = json.loads(path.read_text())
    fn(data)
    path.write_text(json.dumps(data))


def edit_audit(relay_dir, fn):
    path = relay_dir / "relay-audit.jsonl"
    lines = [json.loads(x) for x in path.read_text().splitlines() if x.strip()]
    for line in lines:
        if line.get("event") == "ask":
            fn(line)
    path.write_text("\n".join(json.dumps(x) for x in lines) + "\n")


def test_report_passes_on_recorded_evidence(recorded):
    out, relay_dir = recorded
    assert report.main(["x", str(out), str(relay_dir)]) == 0
    result = json.loads((out / "RELAY-DEDUP-SMOKE.json").read_text())
    assert result["passed"] is True
    failed = [n for n, c in result["checks"].items() if not c["passed"]]
    assert failed == []
    call = result["live_call"]
    assert call["bytes"]["encoded_request_body"] < DEFAULT_MAX_BODY
    assert call["bytes"]["logical_payload"] > DEFAULT_MAX_BODY
    assert call["tokens"] == {"input_tokens": 17_000, "output_tokens": 90}
    assert call["estimated_usd"] == pytest.approx((17_000 * 3 + 90 * 15) / 1e6)
    assert call["temperature_sent"] == 0
    assert set(result["refused_answer"].values()) == {None}  # nothing refused
    # the audit never holds the content or the canary
    assert CANARY not in (relay_dir / "relay-audit.jsonl").read_text()


EMPTY_ANSWER = {  # the shape smoke 1 (run 38855128796) is consistent with: 0 output tokens
    "text": "",
    "stop_reason": "refusal",
    "content_types": [],
    "usage": {"input_tokens": 12_433, "output_tokens": 0},
}


def test_empty_answer_is_named_and_diagnosable(tmp_path, monkeypatch):
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    out, relay_dir = record_offline_run(tmp_path, override=EMPTY_ANSWER)
    assert report.main(["x", str(out), str(relay_dir)]) == 1
    result = json.loads((out / "RELAY-DEDUP-SMOKE.json").read_text())
    refused = result["refused_answer"]
    assert refused["outcome"] == "REFUSED" and refused["reason_code"] == "invalid_answer"
    assert "empty answer (stop_reason 'refusal', content [], output_tokens 0)" in refused["reason"]
    diagnostic = refused["answer_diagnostic"]
    assert diagnostic["empty"] is True and diagnostic["stop_reason"] == "refusal"
    assert diagnostic["output_tokens"] == 0 and diagnostic["content_types"] == []
    assert diagnostic["head"] == "" and diagnostic["tail"] is None
    # the relay audit now records the observed usage on the invalid-answer line (not a gap)
    assert refused["audit_usage"] == EMPTY_ANSWER["usage"]
    assert refused["audit_stop_reason"] == "refusal"
    assert refused["audit_model_observed"] == "claude-sonnet-4-6"
    assert "head" not in refused["audit_answer_shape"]
    checks = result["checks"]
    for name in ("invalid_answer_diagnosable", "audit_answer_shape_has_no_excerpt"):
        assert checks[name]["passed"] is True, name
    assert checks["audit_redacted_fields_only"]["passed"] is True
    assert checks["role_call_success"]["passed"] is False  # diagnosable is not a pass
    md = (out / "RELAY-DEDUP-SMOKE.md").read_text()
    assert "refused: `invalid_answer`" in md and '"stop_reason": "refusal"' in md


def test_prose_answer_excerpt_reaches_the_report_never_the_audit(tmp_path, monkeypatch):
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    opening = "PROSE-OPENING I reviewed the synthetic plan carefully."
    text = opening + " " + "detail " * 200 + "PROSE-CLOSING"
    override = {"text": text, "usage": {"input_tokens": 12_433, "output_tokens": 300}}
    out, relay_dir = record_offline_run(tmp_path, override=override)
    assert report.main(["x", str(out), str(relay_dir)]) == 1
    result = json.loads((out / "RELAY-DEDUP-SMOKE.json").read_text())
    diagnostic = result["refused_answer"]["answer_diagnostic"]
    assert diagnostic["head"].startswith(opening) and len(diagnostic["head"]) == 300
    assert diagnostic["tail"].endswith("PROSE-CLOSING") and len(diagnostic["tail"]) == 300
    assert diagnostic["text_chars"] == len(text) and diagnostic["fenced"] is False
    assert "Expecting value" in result["refused_answer"]["reason"]
    audit = (relay_dir / "relay-audit.jsonl").read_text()
    assert "PROSE-OPENING" not in audit and "PROSE-CLOSING" not in audit
    (ask,) = [json.loads(x) for x in audit.splitlines() if '"event": "ask"' in x]
    assert ask["usage"] == override["usage"] and ask["answer_shape"]["text_chars"] == len(text)
    assert ask["cost_usd"] == pytest.approx((12_433 * 3 + 300 * 15) / 1e6)
    md = (out / "RELAY-DEDUP-SMOKE.md").read_text()
    assert "answer head:" in md and "PROSE-OPENING" in md and "PROSE-CLOSING" in md


@pytest.mark.parametrize(
    "check, change",
    [
        ("audit_answer_shape_has_no_excerpt", lambda a: a["answer_shape"].update(head="x")),
        ("invalid_answer_diagnosable", lambda a: a.pop("usage")),
        ("invalid_answer_diagnosable", lambda a: a.pop("answer_shape")),
    ],
)
def test_invalid_answer_evidence_fails_closed(tmp_path, monkeypatch, check, change):
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    out, relay_dir = record_offline_run(tmp_path, override=EMPTY_ANSWER)
    edit_audit(relay_dir, change)
    report.main(["x", str(out), str(relay_dir)])
    checks = json.loads((out / "RELAY-DEDUP-SMOKE.json").read_text())["checks"]
    assert checks[check]["passed"] is False


def test_report_fails_when_the_answer_misses_the_canary(tmp_path, monkeypatch):
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    out, relay_dir = record_offline_run(tmp_path, quote=False)
    assert report.main(["x", str(out), str(relay_dir)]) == 1
    checks = json.loads((out / "RELAY-DEDUP-SMOKE.json").read_text())["checks"]
    assert checks["answer_quotes_canary"]["passed"] is False
    assert checks["reviewer_schema_valid"]["passed"] is True  # only the canary check fails


def test_report_fails_when_the_body_was_not_encoded(recorded):
    out, relay_dir = recorded

    def plain(c):
        c["receipt"]["transport_encoding"] = None

    edit(out / report.CONTAINER_JSON, plain)
    edit_audit(relay_dir, lambda a: a.update(transport_encoding=None))
    assert report.main(["x", str(out), str(relay_dir)]) == 1
    checks = json.loads((out / "RELAY-DEDUP-SMOKE.json").read_text())["checks"]
    assert not checks["body_dedup_encoded"]["passed"]
    assert not checks["audit_dedup_encoded_reviewer"]["passed"]


def _receipt(field, value):
    def change(c):
        c["receipt"][field] = value

    return change


@pytest.mark.parametrize(
    "check, change",
    [
        (
            "encoded_body_under_cap",
            lambda c: c["receipt"]["transport_bytes"].update(
                request_body_bytes=DEFAULT_MAX_BODY + 1
            ),
        ),
        ("model_observed_sonnet_4_6", _receipt("model_observed", "claude-other")),
        ("receipt_live_gateway", _receipt("gateway_mock", True)),
        ("temperature_zero_sent", _receipt("settings_sent", {"extra_body": {"temperature": 1}})),
        ("reviewer_schema_valid", lambda c: c["answer"].update(note="extra key")),
        (
            "canary_only_in_shared_block",
            lambda c: c["prediction"].update(canary_in_role_input_section=True),
        ),
        (
            "model_facing_input_as_predicted",
            lambda c: c["prediction"].update(model_facing_hash="sha256:0"),
        ),
        ("role_call_success", lambda c: c.update(outcome="UNKNOWN")),
        ("gateway_body_under_known_good", lambda c: c.update(gateway_body_bytes=97_601)),
        ("probe:relay_port_tcp", lambda c: c.update(egress_probes=[])),
        (
            "no_credential_like_env_names",
            lambda c: c.update(credential_like_env_names=["KONG_ANTHROPIC_KEY"]),
        ),
    ],
)
def test_report_fails_closed_on_container_evidence(recorded, check, change):
    out, relay_dir = recorded
    edit(out / report.CONTAINER_JSON, change)
    assert report.main(["x", str(out), str(relay_dir)]) == 1
    checks = json.loads((out / "RELAY-DEDUP-SMOKE.json").read_text())["checks"]
    assert checks[check]["passed"] is False


@pytest.mark.parametrize(
    "check, change",
    [
        ("audit_priced_usage", lambda a: a.update(cost_usd=None)),
        ("audit_temperature_zero", lambda a: a.update(temperature_requested=None)),
        ("audit_one_successful_ask", lambda a: a.update(outcome="UNKNOWN")),
    ],
)
def test_report_fails_closed_on_audit_evidence(recorded, check, change):
    out, relay_dir = recorded
    edit_audit(relay_dir, change)
    assert report.main(["x", str(out), str(relay_dir)]) == 1
    checks = json.loads((out / "RELAY-DEDUP-SMOKE.json").read_text())["checks"]
    assert checks[check]["passed"] is False


def test_report_fails_without_key_scan_or_container_output(recorded):
    out, relay_dir = recorded
    (out / "key-scan.json").unlink()
    (out / report.CONTAINER_JSON).unlink()
    assert report.main(["x", str(out), str(relay_dir)]) == 1


def test_reviewer_schema_and_temperature_helpers():
    good = {"verdict": "REJECT", "binding_hash": "b", "findings": ["x"]}
    assert report.reviewer_schema_errors(good, "b") == []
    assert report.reviewer_schema_errors({**good, "verdict": "MAYBE"}, "b")
    assert report.reviewer_schema_errors({**good, "findings": "x"}, "b")
    assert report.reviewer_schema_errors(good, "other")
    assert report.temperature_sent({"temperature": 0}) == 0
    assert report.temperature_sent({"extra_body": {"temperature": 0.0}}) == 0
    assert report.temperature_sent({"max_tokens": 1}) is None


# ------------------------------------------------------------------ launcher


def test_launch_relay_smoke_client_runs_the_dedup_script(tmp_path, monkeypatch):
    calls = []
    line = json.dumps({"schema": smoke.SCHEMA, "outcome": "SUCCESS"})

    def sh(cmd, **kw):
        calls.append(cmd)
        stdout = '{"ExitCode": 0}' if "inspect" in cmd else ""
        return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")

    def run(cmd, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout=line + "\n", stderr="")

    net = {"network": "simkit-relay-net", "internal": True}
    monkeypatch.setattr(launch, "sh", sh)
    monkeypatch.setattr(launch.subprocess, "run", run)
    monkeypatch.setattr(launch, "relay_settings", lambda d, probe_targets=False: ([], {}, net))
    monkeypatch.setattr(launch, "conditions_from_inspect", lambda n, r=None: ({}, {"a": True}))
    out = tmp_path / "out"
    argv = ["x", "relay-smoke", "--client", "relay_dedup_smoke", "--image", "img"]
    assert launch.main([*argv, "--out", str(out), "--relay-dir", str(tmp_path)]) == 0
    create = calls[0]
    assert create[-1] == "/opt/ctl/container_harness/relay_dedup_smoke.py"
    assert (REPO / "container_harness" / "relay_dedup_smoke.py").is_file()
    rec = json.loads((out / "launch.json").read_text())
    assert rec["client"] == "relay_dedup_smoke" and rec["container"].startswith(
        "simkit-relay-dedup-smoke-"
    )
    assert json.loads((out / report.CONTAINER_JSON).read_text())["outcome"] == "SUCCESS"
    with pytest.raises(SystemExit):
        launch.main(
            [*argv[:2], "--client", "other", "--image", "i", "--out", "o", "--relay-dir", "r"]
        )


# ------------------------------------------------------------------ workflow (parsed YAML)


def workflow():
    import yaml  # installed by the workflow for these tests: a missing one fails, never skips

    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def step(job, prefix):
    (hit,) = [s for s in job["steps"] if (s.get("name") or "").startswith(prefix)]
    return hit


def secret_steps(job):
    return [s for s in job["steps"] if "secrets." in json.dumps(s)]


def test_dedup_job_runs_only_for_its_selector_and_admission_skips_it():
    jobs = workflow()["jobs"]
    dedup = jobs["relay-dedup-smoke"]
    assert dedup["if"] == (
        "github.event_name == 'workflow_dispatch' && "
        "github.event.inputs.selector == 'relay-dedup-smoke'"
    )
    assert "github.event.inputs.selector != 'relay-dedup-smoke'" in jobs["admission"]["if"]
    assert "github.event.inputs.selector != 'relay-smoke'" in jobs["admission"]["if"]
    assert "needs" not in dedup  # independent of admission/case/report
    selected = [n for n, j in jobs.items() if "== 'relay-dedup-smoke'" in j.get("if", "")]
    assert selected == ["relay-dedup-smoke"]


def test_dedup_job_secret_scoping_is_identical_to_relay_smoke():
    jobs = workflow()["jobs"]
    base, dedup = jobs["relay-smoke"], jobs["relay-dedup-smoke"]
    assert "secrets." not in json.dumps(dedup.get("env") or {})
    base_secret, dedup_secret = secret_steps(base), secret_steps(dedup)
    assert [s["name"] for s in dedup_secret] == [s["name"] for s in base_secret]
    for b, d in zip(base_secret, dedup_secret, strict=True):
        assert d["env"]["KONG_ANTHROPIC_KEY"] == b["env"]["KONG_ANTHROPIC_KEY"]
        assert d["env"]["KONG_ANTHROPIC_KEY"] == "${{ secrets.KONG_ANTHROPIC_KEY }}"
        assert d.get("if") == b.get("if") and d["run"] == b["run"]
        assert "secrets." not in d["run"]
        assert [k for k, v in d["env"].items() if "secrets." in v] == ["KONG_ANTHROPIC_KEY"]
    # the relay serves only the reviewer, one request, small output, and a USD 1 cap
    start = step(dedup, "Start host relay")
    assert start["env"]["RELAY_ROLES"] == "reviewer"
    assert start["env"]["RELAY_MAX_REQUESTS"] == "1"
    assert start["env"]["RELAY_MAX_TOKENS"] == "512"
    assert start["env"]["RELAY_BUDGET"] == "1"
    assert int(start["env"]["RELAY_MAX_TOTAL_TOKENS"]) >= 100_000  # worst case ~71k


def test_dedup_job_reuses_relay_smoke_isolation_and_teardown():
    jobs = workflow()["jobs"]
    base, dedup = jobs["relay-smoke"], jobs["relay-dedup-smoke"]
    assert dedup["env"]["RELAY_DIR"] == base["env"]["RELAY_DIR"]
    assert dedup["env"]["OUT"] == "${{ github.workspace }}/relay-dedup-smoke-out"
    for prefix in ("Build harness image", "Relay-only network", "Key-absence proof", "Tear down"):
        assert step(dedup, prefix)["run"] == step(base, prefix)["run"], prefix
    names = [s.get("name") or s.get("uses") for s in dedup["steps"]]
    order = [
        "Relay-only network",
        "Start host relay",
        "Dedup smoke container",
        "Key-absence proof",
        "Tear down",
        "Assert dedup smoke",
        "Upload relay dedup smoke evidence",
    ]
    idx = [next(i for i, n in enumerate(names) if n.startswith(p)) for p in order]
    assert idx == sorted(idx)
    run = step(dedup, "Dedup smoke container")["run"]
    assert "launch.py relay-smoke --client relay_dedup_smoke" in run
    assert "env" not in step(dedup, "Dedup smoke container")
    assert "relay_dedup_smoke_report.py" in step(dedup, "Assert dedup smoke")["run"]
    up = step(dedup, "Upload relay dedup smoke evidence")
    assert up["if"] == "always()"
    assert up["with"]["name"] == "simkit-relay-dedup-smoke"
    assert up["with"]["path"] == "relay-dedup-smoke-out"
    # the payload tests run before anything is built or spent
    tests = step(dedup, "Runner Kong client")
    assert "container_harness/tests/test_relay_dedup_smoke.py" in tests["run"]
    assert "pyyaml==" in tests["run"] and "env" not in tests
    # relay-smoke itself still launches the default client
    assert "--client" not in step(base, "Smoke container")["run"]
