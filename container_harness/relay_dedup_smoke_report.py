"""Assert and summarise a relay-dedup-smoke run (host, stdlib only). Exit 1 when any check fails.

usage: python -I container_harness/relay_dedup_smoke_report.py OUT_DIR RELAY_DIR
       -> OUT_DIR/RELAY-DEDUP-SMOKE.json and .md (also appended to the job summary)

Inputs: launch.json + relay-dedup-smoke.container.json (``launch.py relay-smoke --client
relay_dedup_smoke``), key-scan.json (kong_key_scan.py), RELAY_DIR/net.json + relay-audit.jsonl
(relay_ci.sh). The isolation / probe / key checks are relay_smoke_report's, reused unchanged;
on top of them it checks the two things this smoke exists for:

(a) the body went ``dedup-sha256/1``-encoded, under the 88,000 B cap, for a payload that would
    NOT fit plain, and Kong answered it (SUCCESS, model_observed claude-sonnet-4-6);
(b) the model resolved the ``$shared`` reference: the reviewer answer quotes the run's random
    canary, which occurs only inside the shared block (never in the role-input section), and
    the answer still validates against the reviewer output schema.

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
TOKEN_KEYS = ("input_tokens", "output_tokens")
EXCERPT_KEYS = ("head", "tail")  # kong_roles.ANSWER_EXCERPT_KEYS: never in the relay audit
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


def reviewer_schema_errors(answer, binding_hash):
    """Errors against the reviewer output schema (agents.PROMPTS['reviewer'] + the controller's
    exact-keys branch in runner.py): exactly verdict / binding_hash / findings, the copied
    binding, a findings array of strings."""
    if not isinstance(answer, dict):
        return ["answer is not an object"]
    errors = []
    if set(answer) != REVIEWER_KEYS:
        errors.append(f"keys {sorted(answer)} != {sorted(REVIEWER_KEYS)}")
    if answer.get("verdict") not in REVIEWER_VERDICTS:
        errors.append(f"verdict {answer.get('verdict')!r} not in {sorted(REVIEWER_VERDICTS)}")
    if answer.get("binding_hash") != binding_hash:
        errors.append("binding_hash is not the supplied binding")
    findings = answer.get("findings")
    if not isinstance(findings, list) or not all(isinstance(f, str) for f in findings):
        errors.append("findings is not an array of strings")
    return errors


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


def evaluate(out, relay):
    out, relay = Path(out), Path(relay)
    launch = base.load(out / "launch.json") or {}
    smoke = base.load(out / CONTAINER_JSON)
    scan = base.load(out / "key-scan.json") or {}
    net = base.load(relay / "net.json") or {}
    audit_path = relay / "relay-audit.jsonl"
    audit = (
        [json.loads(x) for x in audit_path.read_text(encoding="utf-8").splitlines() if x.strip()]
        if audit_path.is_file()
        else []
    )
    checks = {}

    def check(name, ok, observed):
        checks[name] = {"passed": bool(ok), "observed": observed}

    # ---- isolation, probes, key absence: exactly relay_smoke_report's checks
    conditions = launch.get("run_conditions") or {}
    check("run_conditions_all_true", conditions and all(conditions.values()), conditions)
    check(
        "launched_dedup_client", launch.get("client") == "relay_dedup_smoke", launch.get("client")
    )
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
    check(
        "relay_allows_reviewer_only",
        health.get("allowed_roles") == ["reviewer"],
        health.get("allowed_roles"),
    )
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
    canary = smoke.get("canary")
    check(
        "canary_only_in_shared_block",
        isinstance(canary, str)
        and len(canary) >= 16
        and prediction.get("canary_in_role_input_section") is False
        and prediction.get("canary_in_shared_blocks") == 1
        and prediction.get("canary_in_request_body") == 1,
        {
            k: prediction.get(k)
            for k in (
                "canary_in_role_input_section",
                "canary_in_shared_blocks",
                "canary_in_request_body",
                "canary_offset_in_shared_block",
            )
        },
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
    errors = reviewer_schema_errors(answer, smoke.get("binding_hash"))
    check("reviewer_schema_valid", not errors, errors)
    answer_text = json.dumps(answer) if answer is not None else ""
    check(
        "answer_quotes_canary",
        isinstance(canary, str) and bool(canary) and canary in answer_text,
        {"canary_found": isinstance(canary, str) and bool(canary) and canary in answer_text},
    )

    # ---- settings + priced usage, receipt and relay audit agree
    temperature = temperature_sent(receipt.get("settings_sent"))
    check(
        "temperature_zero_sent",
        type(temperature) in (int, float) and temperature == 0,
        receipt.get("settings_sent"),
    )
    asks = [a for a in audit if a.get("event") == "ask"]
    ask = asks[0] if len(asks) == 1 else {}
    check(
        "audit_one_successful_ask",
        len(asks) == 1 and ask.get("outcome") == "SUCCESS",
        [a.get("outcome") for a in asks],
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
    check(
        "relay_stopped_itself",
        any(a.get("event") == "relay_stop" for a in audit),
        [a.get("reason") for a in audit if a.get("event") == "relay_stop"],
    )

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
        "canary_sha256": prediction.get("canary_sha256"),
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
