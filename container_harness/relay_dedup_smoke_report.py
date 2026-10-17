"""Assert and summarise a relay-dedup-smoke run (host, stdlib only). Exit 1 when any check fails.

usage: python -I container_harness/relay_dedup_smoke_report.py OUT_DIR RELAY_DIR
       -> OUT_DIR/RELAY-DEDUP-SMOKE.json and .md (also appended to the job summary)

Inputs: launch.json + relay-dedup-smoke.container.json (``launch.py relay-smoke --client
relay_dedup_smoke``), key-scan.json (kong_key_scan.py), RELAY_DIR/net.json + relay-audit.jsonl
(relay_ci.sh). The isolation / probe / key checks are relay_smoke_report's, reused unchanged;
on top of them it checks the two things this smoke exists for:

(a) the body went ``dedup-sha256/1``-encoded, under the 88,000 B cap, for a payload that would
    NOT fit plain, and Kong answered it (SUCCESS, model_observed claude-sonnet-4-6);
(b) the model resolved the ``$shared`` reference: the reviewer answer's ``binding_hash`` (the
    field the reviewer contract always returns) equals the run's per-run binding, which occurs
    only inside a shared block (never in the role-input section, no top-level copy), and the
    answer validates against the reviewer output schema. No string value of the payload holds
    text addressed to the model (``no_instruction_text_in_payload``);
(c) structured output was forced and used (``structured_output_forced``): the request carried
    exactly the predicted answer tool with ``tool_choice`` naming it (receipt ``settings_sent``
    / ``sent_keys`` / ``settings.answer_tool_hash``), the response stopped on ``tool_use`` and
    the answer came from the tool_use input (``settings.answer_source``), on the receipt and on
    the relay audit line alike.

Every check is named and recorded with its observed value; nothing is inferred from an absent
file. PASS only when every check holds.
"""

import json
import math
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # -I drops the script dir

import relay_smoke_report as base  # noqa: E402

SCHEMA = "simkit.relay-dedup-smoke/1"
CONTAINER_JSON = "relay-dedup-smoke.container.json"
DEDUP_ENCODING = "dedup-sha256/1"
BODY_CAP = 88_000  # RelayRoles DEFAULT_MAX_BODY: the cap on the body actually sent
KNOWN_GOOD_GATEWAY_BODY = 97_600  # Kong kit KNOWN_GOOD_BODY_BYTES
EXPECTED_MODEL = "claude-sonnet-4-6"
REVIEWER_KEYS = {"verdict", "binding_hash", "findings"}
REVIEWER_VERDICTS = {"APPROVE", "REJECT", "NEEDS-INFO"}
# The frozen v2.7 evidence contract's reviewer finding record (agentic-e2e-injections.DEV-ONLY.json
# record_lists: path role_output.reviewer[*].findings, fields {id: str, severity: str, text: str},
# also_elements: str; EVIDENCE_CONTRACT.md reads findings[*].id / .severity / .text). The scorer
# (simkit.score.record_list_findings) accepts a string element or a record carrying each field as
# a string; extra keys are not checked and severity has no closed vocabulary. agents.PROMPTS
# ["reviewer"] names these fields (test_relay_dedup_smoke pins the two together).
REVIEWER_FINDING_FIELDS = ("id", "severity", "text")
TOKEN_KEYS = ("input_tokens", "output_tokens")
EXCERPT_KEYS = ("head", "tail")  # kong_roles.ANSWER_EXCERPT_KEYS: never in the relay audit
# role_schemas.STRUCTURED_OUTPUT / ANSWER_SOURCE (this report is stdlib only; a test pins them).
STRUCTURED_OUTPUT = "forced_tool"
ANSWER_SOURCE = "tool_use.input"
FORCED_SENT_KEYS = {"tools", "tool_choice"}
REFUSED_KEYS = (
    "outcome",
    "reason",
    "reason_code",
    "relay_id",
    "answer_diagnostic",
    "audit_usage",
    "audit_stop_reason",
    "audit_model_observed",
    "audit_answer_shape",
)


def finding_errors(findings):
    """Errors against the frozen contract's reviewer finding list (REVIEWER_FINDING_FIELDS),
    exactly as the scorer types it: each element a string, or an object carrying id, severity
    and text as strings."""
    if not isinstance(findings, list):
        return ["findings is not an array"]
    errors = []
    for k, item in enumerate(findings):
        if isinstance(item, str):
            continue
        if not isinstance(item, dict):
            errors.append(f"findings[{k}] is neither an object nor a string")
            continue
        for field in REVIEWER_FINDING_FIELDS:
            if field not in item:
                errors.append(
                    f"findings[{k}] lacks the required field {field} (has {sorted(item)})"
                )
            elif not isinstance(item[field], str):
                errors.append(f"findings[{k}].{field} is not a string")
    return errors


def reviewer_schema_errors(answer, binding_hash):
    """Errors against the reviewer output schema (agents.PROMPTS['reviewer'] + the controller's
    exact-keys branch in runner.py): exactly verdict / binding_hash / findings, the copied
    binding, and findings shaped as the frozen evidence contract reads them (finding_errors)."""
    if not isinstance(answer, dict):
        return ["answer is not an object"]
    errors = []
    if set(answer) != REVIEWER_KEYS:
        errors.append(f"keys {sorted(answer)} != {sorted(REVIEWER_KEYS)}")
    if answer.get("verdict") not in REVIEWER_VERDICTS:
        errors.append(f"verdict {answer.get('verdict')!r} not in {sorted(REVIEWER_VERDICTS)}")
    if answer.get("binding_hash") != binding_hash:
        errors.append("binding_hash is not the supplied binding")
    return errors + finding_errors(answer.get("findings"))


def structured_output_observed(receipt, ask, prediction):
    """Whether the forced answer tool was sent and answered, from the receipt and the relay
    audit ask line; returns (ok, the observed fields for the check record)."""
    settings = receipt.get("settings") or {}
    sent = receipt.get("settings_sent") or {}
    tools = sent.get("tools") if isinstance(sent.get("tools"), list) else []
    observed = {
        "structured_output": settings.get("structured_output"),
        "answer_source": settings.get("answer_source"),
        "answer_tool": settings.get("answer_tool"),
        "answer_tool_hash_match": settings.get("answer_tool_hash")
        == prediction.get("answer_tool_hash"),
        "tool_choice": sent.get("tool_choice"),
        "tools_sent": [t.get("name") for t in tools if isinstance(t, dict)],
        "sent_keys_forced": FORCED_SENT_KEYS <= set(receipt.get("sent_keys") or []),
        "stop_reason": receipt.get("stop_reason"),
        "audit_structured_output": ask.get("structured_output"),
        "audit_answer_source": ask.get("answer_source"),
        "audit_sent_keys_forced": FORCED_SENT_KEYS <= set(ask.get("sent_keys") or []),
    }
    name = prediction.get("answer_tool")
    ok = (
        isinstance(name, str)
        and observed["structured_output"] == STRUCTURED_OUTPUT
        and observed["answer_source"] == ANSWER_SOURCE
        and observed["answer_tool"] == name
        and observed["answer_tool_hash_match"] is True
        and observed["tool_choice"] == {"type": "tool", "name": name}
        and observed["tool_choice"] == prediction.get("tool_choice")
        and observed["tools_sent"] == [name]
        and observed["sent_keys_forced"]
        and observed["stop_reason"] == "tool_use"
        and observed["audit_structured_output"] == STRUCTURED_OUTPUT
        and observed["audit_answer_source"] == ANSWER_SOURCE
        and observed["audit_sent_keys_forced"]
    )
    return ok, observed


def is_binding(value):
    """A controller binding: ``contracts.digest``'s bare 64 lowercase hex characters."""
    return (
        isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)
    )


def temperature_sent(settings_sent):
    """The temperature the worker echoed it sent (direct field or the SDK extra_body)."""
    if not isinstance(settings_sent, dict):
        return None
    if "temperature" in settings_sent:
        return settings_sent["temperature"]
    extra = settings_sent.get("extra_body")
    return extra.get("temperature") if isinstance(extra, dict) else None


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def _ints(usage):
    return isinstance(usage, dict) and all(
        type(usage.get(k)) is int and usage[k] >= 0 for k in TOKEN_KEYS
    )


def refused_answer(smoke, ask):
    """Why the reviewer call was refused, from both sides: the container's view (reason, the
    relay's bounded ``answer_diagnostic`` of this synthetic answer) and the relay audit's
    non-content view (usage, stop_reason, ``answer_shape``). All null on SUCCESS."""
    if smoke.get("outcome") == "SUCCESS":
        return dict.fromkeys(REFUSED_KEYS)
    diagnostic = smoke.get("answer_diagnostic")
    return {
        "outcome": smoke.get("outcome"),
        "reason": smoke.get("reason"),
        "reason_code": smoke.get("reason_code") or ask.get("reason_code"),
        "relay_id": smoke.get("relay_id") or ask.get("relay_id"),
        "answer_diagnostic": diagnostic if isinstance(diagnostic, dict) else None,
        "audit_usage": ask.get("usage"),
        "audit_stop_reason": ask.get("stop_reason"),
        "audit_model_observed": ask.get("model_observed"),
        "audit_answer_shape": ask.get("answer_shape"),
    }


def load_evidence(out, relay, container_json):
    """(launch, container record or None, key scan, net, relay audit lines) of a smoke run."""
    out, relay = Path(out), Path(relay)
    launch = base.load(out / "launch.json") or {}
    smoke = base.load(out / container_json)
    scan = base.load(out / "key-scan.json") or {}
    net = base.load(relay / "net.json") or {}
    audit_path = relay / "relay-audit.jsonl"
    audit = (
        [json.loads(x) for x in audit_path.read_text(encoding="utf-8").splitlines() if x.strip()]
        if audit_path.is_file()
        else []
    )
    return launch, smoke, scan, net, audit


def isolation_checks(check, launch, smoke, scan, net, client, client_check, roles, roles_check):
    """Isolation, probes and key absence: exactly relay_smoke_report's checks, plus the
    launched client and the relay's role allow-list. Shared by the dedup and role smokes."""
    conditions = launch.get("run_conditions") or {}
    check("run_conditions_all_true", conditions and all(conditions.values()), conditions)
    check(client_check, launch.get("client") == client, launch.get("client"))
    check("network_internal", net.get("internal") is True, net.get("internal"))
    passed = set(launch.get("env_names_passed") or [])
    check("only_relay_env_names_passed", passed == base.EXPECTED_ENV_NAMES, sorted(passed))
    check("container_output_parsed", smoke is not None, launch.get("smoke_parsed"))
    smoke = smoke or {}
    for key in ("credential_like_env_names", "credential_like_files"):
        check(f"no_{key}", smoke.get(key) == [], smoke.get(key))
    check("ran_as_uid_1000", smoke.get("uid") == 1000, smoke.get("uid"))
    health = smoke.get("health") or {}
    check("relay_health_ok", health.get("outcome") == "OK", health)
    check(roles_check, health.get("allowed_roles") == roles, health.get("allowed_roles"))
    probes = {p["probe"]: p for p in smoke.get("egress_probes") or []}
    for name in base.REQUIRED_PROBES:
        row = probes.get(name) or {}
        check(f"probe:{name}", row.get("passed") is True, row.get("observed"))
    optional = [p for n, p in probes.items() if n not in base.REQUIRED_PROBES]
    check(
        "optional_probes_not_failed",
        all(p.get("passed") is not False for p in optional),
        {p["probe"]: p.get("observed") for p in optional},
    )
    check(
        "key_absent_container_and_artifacts",
        scan.get("key_absent") is True,
        {k: scan.get(k) for k in ("key_supplied_to_scanner", "key_length_ok", "key_absent")},
    )
    return smoke


def capped_asks(asks):
    """The asks the relay refused BEFORE forwarding at its spend cap (the request-cap probe)."""
    return [
        a
        for a in asks
        if a.get("outcome") == "REFUSED"
        and a.get("sent_to_gateway") is False
        and a.get("reason_code") == "relay_spend_cap"
    ]


def cap_stop_checks(check, probe, audit, capped):
    """The relay keeps serving at a full request cap and stops only once EXHAUSTED: the cap
    probe must be refused before forwarding (spend-cap refusal, client gate ``budget``), the
    only refusal of that kind, after the request cap was reached; then the relay's own stop
    line must name the request cap (teardown's kill writes none)."""
    caps_reached = [a.get("cap") for a in audit if a.get("event") == "spend_cap_reached"]
    check(
        "request_cap_refused_before_forwarding",
        probe.get("outcome") == "REFUSED"
        and probe.get("gate") == "budget"
        and "relay spend cap" in str(probe.get("reason"))
        and len(capped) == 1
        and caps_reached == ["max_requests"],
        {
            "probe": {k: probe.get(k) for k in ("outcome", "gate", "requests", "max_requests")},
            "spend_cap_reached": caps_reached,
        },
    )
    stops = [a.get("reason") for a in audit if a.get("event") == "relay_stop"]
    check("relay_stopped_itself", stops == ["spend cap reached: max_requests"], stops)


def evaluate(out, relay):
    launch, smoke, scan, net, audit = load_evidence(out, relay, CONTAINER_JSON)
    checks = {}

    def check(name, ok, observed):
        checks[name] = {"passed": bool(ok), "observed": observed}

    # ---- isolation, probes, key absence: exactly relay_smoke_report's checks
    smoke = isolation_checks(
        check,
        launch,
        smoke,
        scan,
        net,
        "relay_dedup_smoke",
        "launched_dedup_client",
        ["reviewer"],
        "relay_allows_reviewer_only",
    )

    # ---- (a) gateway accepted a reviewer-sized, dedup-encoded body
    receipt = smoke.get("receipt") or {}
    sizes = receipt.get("transport_bytes") or {}
    prediction = smoke.get("prediction") or {}
    check("role_call_success", smoke.get("outcome") == "SUCCESS", smoke.get("outcome"))
    check(
        "model_observed_sonnet_4_6",
        receipt.get("model_observed") == EXPECTED_MODEL,
        receipt.get("model_observed"),
    )
    check(
        "receipt_live_gateway",
        receipt.get("gateway_mock") is False
        and receipt.get("transport") == "kong-native-anthropic",
        {k: receipt.get(k) for k in ("gateway_mock", "transport")},
    )
    check(
        "body_dedup_encoded",
        receipt.get("transport_encoding") == DEDUP_ENCODING,
        receipt.get("transport_encoding"),
    )
    body = sizes.get("request_body_bytes")
    check(
        "encoded_body_under_cap",
        type(body) is int and 0 < body < BODY_CAP,
        {"request_body_bytes": body, "cap": BODY_CAP},
    )
    check(
        "payload_needs_dedup",
        type(sizes.get("payload_bytes")) is int and sizes["payload_bytes"] > BODY_CAP,
        sizes.get("payload_bytes"),
    )
    check(
        "encoded_body_as_predicted",
        body is not None and body == prediction.get("request_body_bytes"),
        {"observed": body, "predicted": prediction.get("request_body_bytes")},
    )
    check(
        "shared_block_sent",
        type(sizes.get("shared_blocks")) is int and sizes["shared_blocks"] >= 1,
        sizes.get("shared_blocks"),
    )
    gateway = smoke.get("gateway_body_bytes")
    check(
        "gateway_body_under_known_good",
        type(gateway) is int and 0 < gateway <= KNOWN_GOOD_GATEWAY_BODY,
        {"gateway_body_bytes": gateway, "known_good": KNOWN_GOOD_GATEWAY_BODY},
    )

    # ---- (b) the model resolved the $shared reference
    binding = smoke.get("binding_hash")
    check(
        "binding_only_in_shared_block",
        is_binding(binding)
        and prediction.get("binding_top_level") is False
        and prediction.get("binding_in_role_input_section") is False
        and prediction.get("binding_in_shared_blocks") == 1
        and prediction.get("binding_in_request_body") == 1
        and prediction.get("binding_in_model_facing") == 1
        and bool(prediction.get("binding_block_referenced_at")),
        {
            k: prediction.get(k)
            for k in (
                "binding_top_level",
                "binding_in_role_input_section",
                "binding_in_shared_blocks",
                "binding_in_request_body",
                "binding_in_model_facing",
                "binding_block_referenced_at",
            )
        },
    )
    terms = prediction.get("instruction_terms_in_payload")
    check(
        "no_instruction_text_in_payload",
        terms == [] and bool(prediction.get("instruction_terms_checked")),
        {"found": terms, "checked": prediction.get("instruction_terms_checked")},
    )
    settings = receipt.get("settings") or {}
    check(
        "model_facing_input_as_predicted",
        sizes.get("model_facing_hash") is not None
        and sizes.get("model_facing_hash") == prediction.get("model_facing_hash")
        and settings.get("system_prompt_hash") == prediction.get("system_prompt_hash"),
        {
            "model_facing_hash_match": sizes.get("model_facing_hash")
            == prediction.get("model_facing_hash"),
            "system_prompt_hash_match": settings.get("system_prompt_hash")
            == prediction.get("system_prompt_hash"),
        },
    )
    answer = smoke.get("answer")
    errors = reviewer_schema_errors(answer, binding)
    check("reviewer_schema_valid", not errors, errors)
    returned = answer.get("binding_hash") if isinstance(answer, dict) else None
    check(
        "answer_binding_resolved_from_shared_block",
        is_binding(binding) and returned == binding,
        {
            "returned": returned if isinstance(returned, str) else None,
            "expected": binding,
            "equal": is_binding(binding) and returned == binding,
        },
    )

    # ---- settings + priced usage, receipt and relay audit agree
    temperature = temperature_sent(receipt.get("settings_sent"))
    check(
        "temperature_zero_sent",
        type(temperature) in (int, float) and temperature == 0,
        receipt.get("settings_sent"),
    )
    asks = [a for a in audit if a.get("event") == "ask"]
    # Exactly one ask may reach the gateway, and it succeeded; the only other ask allowed is
    # the request-cap probe, refused before forwarding (relay_smoke_report's rule).
    capped = capped_asks(asks[1:])
    one_paid = bool(asks) and asks[0].get("outcome") == "SUCCESS" and len(capped) == len(asks) - 1
    ask = asks[0] if one_paid or len(asks) == 1 else {}  # a lone refused ask is still diagnosed
    check(
        "audit_one_successful_ask",
        one_paid,
        [(a.get("outcome"), a.get("sent_to_gateway"), a.get("reason_code")) for a in asks],
    )
    check(
        "audit_dedup_encoded_reviewer",
        ask.get("role") == "reviewer"
        and ask.get("transport_encoding") == DEDUP_ENCODING
        and (ask.get("transport_bytes") or {}).get("request_body_bytes") == body,
        {k: ask.get(k) for k in ("role", "transport_encoding")},
    )
    audit_temperature = temperature_sent(ask.get("settings_sent"))
    check(
        "audit_temperature_zero",
        ask.get("temperature_requested") == 0
        and type(audit_temperature) in (int, float)
        and audit_temperature == 0,
        {"requested": ask.get("temperature_requested"), "sent": ask.get("settings_sent")},
    )
    charge = ask.get("charge") or {}
    check(
        "audit_priced_usage",
        _ints(ask.get("usage"))
        and _finite(ask.get("cost_usd"))
        and charge.get("basis") == "observed"
        and ask.get("usage") == receipt.get("usage"),
        {
            "usage": ask.get("usage"),
            "cost_usd": ask.get("cost_usd"),
            "charge_basis": charge.get("basis"),
        },
    )
    forced, forced_observed = structured_output_observed(receipt, ask, prediction)
    check("structured_output_forced", forced, forced_observed)
    leaked = sorted({k for a in audit for k in a if k in base.AUDIT_FORBIDDEN_KEYS})
    check("audit_redacted_fields_only", not leaked, leaked)
    shape = ask.get("answer_shape")
    check(
        "audit_answer_shape_has_no_excerpt",
        shape is None or (isinstance(shape, dict) and not set(shape) & set(EXCERPT_KEYS)),
        sorted(shape) if isinstance(shape, dict) else shape,
    )
    refused = refused_answer(smoke, ask)
    check(
        "invalid_answer_diagnosable",
        smoke.get("outcome") == "SUCCESS"
        or (
            refused["reason_code"] != "invalid_answer"
            and smoke.get("outcome") in ("REFUSED", "UNKNOWN")
        )
        or (
            isinstance(refused["answer_diagnostic"], dict)
            and isinstance(refused["audit_answer_shape"], dict)
            and _ints(refused["audit_usage"])
        ),
        {k: refused[k] for k in ("reason_code", "audit_usage", "audit_stop_reason")},
    )
    # Past the request cap the relay answers with a spend-cap refusal (client gate "budget"),
    # never by having already stopped; that refusal exhausts the guard, so the relay stops.
    cap_stop_checks(check, smoke.get("cap_probe") or {}, audit, capped)

    usage = receipt.get("usage") or {}
    measured = {
        "bytes": {
            "logical_payload": sizes.get("payload_bytes"),
            "encoded_request_body": body,
            "model_facing": sizes.get("model_facing_bytes"),
            "shared_blocks": sizes.get("shared_blocks"),
            "shared_bytes": sizes.get("shared_bytes"),
            "gateway_body": gateway,
            "gateway_body_basis": smoke.get("gateway_body_basis"),
        },
        "tokens": {k: usage.get(k) for k in TOKEN_KEYS},
        "estimated_usd": receipt.get("cost_usd"),
        "cost_basis": receipt.get("cost_basis"),
        "temperature_sent": temperature,
        "structured_output": settings.get("structured_output"),
        "answer_source": settings.get("answer_source"),
        "max_tokens": settings.get("max_tokens"),
        "model_observed": receipt.get("model_observed"),
        "stop_reason": receipt.get("stop_reason"),
        "request_id": receipt.get("request_id"),
        "via": receipt.get("via"),
        "latency_ms": receipt.get("latency_ms"),
        "relay_id": receipt.get("relay_id"),
        "response_id": receipt.get("response_id"),
    }
    ok = all(c["passed"] for c in checks.values())
    return {
        "schema": SCHEMA,
        "passed": ok,
        "run_url": os.environ.get("RUN_URL"),
        "payload_classification": "synthetic generated text, Not Sensitive",
        "live_call": measured,
        "answer": answer,
        "refused_answer": refused,
        "binding_hash": binding,
        "prediction": prediction,
        "egress_probes": smoke.get("egress_probes"),
        "key_scan": scan,
        "network": net,
        "checks": checks,
    }


def markdown(result):
    lines = [
        f"### Relay dedup smoke: {'PASS' if result['passed'] else 'FAIL'}",
        "",
        f"live call: `{json.dumps(result['live_call'])}`",
        "",
    ]
    refused = result.get("refused_answer") or {}
    if refused.get("outcome") is not None:
        diagnostic = refused.get("answer_diagnostic") or {}
        shape = {k: v for k, v in diagnostic.items() if k not in EXCERPT_KEYS}
        lines += [
            f"refused: `{refused.get('reason_code')}` ({refused.get('relay_id')}): "
            f"{json.dumps(refused.get('reason'))}",
            "",
            f"relay audit: usage `{json.dumps(refused.get('audit_usage'))}`, stop_reason "
            f"`{json.dumps(refused.get('audit_stop_reason'))}`, answer_shape "
            f"`{json.dumps(refused.get('audit_answer_shape'))}`",
            "",
            f"answer diagnostic (synthetic, bounded): `{json.dumps(shape)}`",
            "",
        ]
        for key in EXCERPT_KEYS:
            if diagnostic.get(key) is not None:
                # A four-backtick fence, so a ```json-fenced answer excerpt renders verbatim.
                lines += [f"answer {key}:", "", "````text", diagnostic[key], "````", ""]
    lines += [
        "| check | pass | observed |",
        "|---|---|---|",
    ]
    for name, c in result["checks"].items():
        lines.append(f"| {name} | {c['passed']} | `{json.dumps(c['observed'])[:160]}` |")
    return "\n".join(lines) + "\n"


def main(argv):
    out, relay = Path(argv[1]), Path(argv[2])
    result = evaluate(out, relay)
    (out / "RELAY-DEDUP-SMOKE.json").write_text(json.dumps(result, indent=1) + "\n", "utf-8")
    md = markdown(result)
    (out / "RELAY-DEDUP-SMOKE.md").write_text(md, encoding="utf-8")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as handle:
            handle.write(md)
    print(md)
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
