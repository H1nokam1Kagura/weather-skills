"""Assert and summarise a relay-role-smoke run (host, stdlib only). Exit 1 when any check fails.

usage: python -I container_harness/relay_role_smoke_report.py OUT_DIR RELAY_DIR
       -> OUT_DIR/RELAY-ROLE-SMOKE.json and .md (also appended to the job summary)

Inputs: launch.json + relay-role-smoke.container.json (``launch.py relay-smoke --client
relay_role_smoke``), key-scan.json (kong_key_scan.py), RELAY_DIR/net.json + relay-audit.jsonl
(relay_ci.sh). The isolation / probe / key-absence checks and the request-cap / self-stop
checks are relay_dedup_smoke_report's, reused unchanged. On top of them, per planned call
(one per live role and payload variant, ``EXPECTED_CALLS``):

- the call reached Kong and came back (SUCCESS, model_observed claude-sonnet-4-6, a live
  kong-native-anthropic receipt), as one forwarded relay ask matched by relay_id;
- structured output was forced and used (``structured_output_forced``: exactly the predicted
  answer tool with tool_choice naming it, stop_reason tool_use, answer from the tool_use input,
  on the receipt and on the relay audit line alike); temperature 0 sent (receipt and audit);
  priced usage; the model saw exactly the predicted input (model-facing + system prompt hash);
  the body under the cap and the gateway body under the known-good size;
- ``controller_accepts``: the CONTROLLER'S OWN acceptance decision on the answer, made in the
  container by replaying the real controller with that answer (relay_role_smoke.judge), on the
  byte-identical payload that went live (``payload_identical``). The refusal text is reported.

``schema_errors`` (the answer against the role's forced-tool schema) and each answer's
judgement (verdict, needs_info, shadow action) are reported, never gating: the schema guides
the model and the controller alone decides. PASS only when every check holds, i.e. every live
role's answer was accepted by the controller. Nothing is inferred from an absent file.
"""

import json
import math
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # -I drops the script dir

import relay_dedup_smoke_report as dedup  # noqa: E402
import relay_smoke_report as base  # noqa: E402

SCHEMA = "simkit.relay-role-smoke/1"
CLIENT = "relay_role_smoke"
CONTAINER_JSON = "relay-role-smoke.container.json"
# relay_role_smoke.CALLS (call id, native role), in call order; a test pins the two together.
EXPECTED_CALLS = (
    ("compiler", "compiler"),
    ("planner", "planner"),
    ("evidence_checker", "evidence_checker"),
    ("reviewer", "reviewer"),
    ("repairer", "repairer"),
    ("reviewer_rationale", "reviewer"),
    ("verifier", "verifier"),
    ("shadow_policy", "shadow_policy"),
)
ROLES = sorted({role for _, role in EXPECTED_CALLS})
BUDGET_USD = 2.0  # the workflow's RELAY_BUDGET for this job
PER_CALL_CHECKS = (
    "role_call_success",
    "model_observed_sonnet_4_6",
    "receipt_live_gateway",
    "audit_one_forwarded_successful_ask",
    "structured_output_forced",
    "temperature_zero_sent",
    "audit_temperature_zero",
    "audit_priced_usage",
    "model_facing_input_as_predicted",
    "transport_as_predicted",
    "body_under_cap",
    "gateway_body_under_known_good",
    "controller_judged_the_sent_payload",
    "controller_accepts",
)


def _cost(receipt):
    value = receipt.get("cost_usd")
    return value if type(value) in (int, float) and math.isfinite(value) else None


def call_checks(record, asks_by_relay):
    """(checks, summary) of one planned call. ``asks_by_relay``: relay audit ask lines by
    relay_id; the call's own line is the one its receipt (or refusal) names."""
    checks = {}

    def check(name, ok, observed):
        checks[name] = {"passed": bool(ok), "observed": observed}

    receipt = record.get("receipt") or {}
    prediction = record.get("prediction") or {}
    sizes = receipt.get("transport_bytes") or {}
    settings = receipt.get("settings") or {}
    relay_id = receipt.get("relay_id") or record.get("relay_id")
    lines = (asks_by_relay.get(relay_id) or []) if relay_id else []
    ask = lines[0] if len(lines) == 1 else {}
    check("role_call_success", record.get("outcome") == "SUCCESS", record.get("outcome"))
    check(
        "model_observed_sonnet_4_6",
        receipt.get("model_observed") == dedup.EXPECTED_MODEL,
        receipt.get("model_observed"),
    )
    check(
        "receipt_live_gateway",
        receipt.get("gateway_mock") is False
        and receipt.get("transport") == "kong-native-anthropic"
        and receipt.get("role") == record.get("role"),
        {k: receipt.get(k) for k in ("gateway_mock", "transport", "role")},
    )
    check(
        "audit_one_forwarded_successful_ask",
        len(lines) == 1
        and ask.get("outcome") == "SUCCESS"
        and ask.get("role") == record.get("role")
        and ask.get("sent_to_gateway") is not False,
        [(a.get("outcome"), a.get("role"), a.get("sent_to_gateway")) for a in lines],
    )
    forced, forced_observed = dedup.structured_output_observed(receipt, ask, prediction)
    check("structured_output_forced", forced, forced_observed)
    temperature = dedup.temperature_sent(receipt.get("settings_sent"))
    check(
        "temperature_zero_sent",
        type(temperature) in (int, float) and temperature == 0,
        receipt.get("settings_sent"),
    )
    audit_temperature = dedup.temperature_sent(ask.get("settings_sent"))
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
        dedup._ints(ask.get("usage"))
        and dedup._finite(ask.get("cost_usd"))
        and charge.get("basis") == "observed"
        and ask.get("usage") == receipt.get("usage"),
        {
            "usage": ask.get("usage"),
            "cost_usd": ask.get("cost_usd"),
            "charge_basis": charge.get("basis"),
        },
    )
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
    body = sizes.get("request_body_bytes")
    check(
        "transport_as_predicted",
        "transport_encoding" in prediction
        and receipt.get("transport_encoding") == prediction.get("transport_encoding")
        and body == prediction.get("request_body_bytes"),
        {
            "encoding": receipt.get("transport_encoding"),
            "predicted_encoding": prediction.get("transport_encoding"),
            "request_body_bytes": body,
            "predicted": prediction.get("request_body_bytes"),
        },
    )
    check(
        "body_under_cap",
        type(body) is int and 0 < body < dedup.BODY_CAP,
        {"request_body_bytes": body, "cap": dedup.BODY_CAP},
    )
    gateway = record.get("gateway_body_bytes")
    check(
        "gateway_body_under_known_good",
        type(gateway) is int and 0 < gateway <= dedup.KNOWN_GOOD_GATEWAY_BODY,
        {"gateway_body_bytes": gateway, "known_good": dedup.KNOWN_GOOD_GATEWAY_BODY},
    )
    controller = record.get("controller") or {}
    check(
        "controller_judged_the_sent_payload",
        controller.get("payload_identical") is True,
        {k: controller.get(k) for k in ("payload_identical", "replay_stop")},
    )
    check(
        "controller_accepts",
        controller.get("controller_accepts") is True,
        {k: controller.get(k) for k in ("controller_outcome", "controller_refusal")},
    )
    usage = receipt.get("usage") or {}
    summary = {
        "call": record.get("call"),
        "role": record.get("role"),
        "variant": record.get("variant"),
        "variant_keys": record.get("variant_keys"),
        "outcome": record.get("outcome"),
        "controller_accepts": controller.get("controller_accepts"),
        "controller_outcome": controller.get("controller_outcome"),
        "controller_refusal": controller.get("controller_refusal"),
        "judgment": controller.get("judgment"),
        "needs_info_question": controller.get("needs_info_question"),
        "schema_errors": record.get("schema_errors"),
        "structured_output": settings.get("structured_output"),
        "answer_source": settings.get("answer_source"),
        "answer_tool": settings.get("answer_tool"),
        "answer_tool_hash_match": forced_observed["answer_tool_hash_match"],
        "stop_reason": receipt.get("stop_reason"),
        "temperature_sent": temperature,
        "max_tokens": settings.get("max_tokens"),
        "tokens": {k: usage.get(k) for k in dedup.TOKEN_KEYS},
        "estimated_usd": _cost(receipt),
        "cost_basis": receipt.get("cost_basis"),
        "model_observed": receipt.get("model_observed"),
        "relay_id": relay_id,
        "response_id": receipt.get("response_id"),
        "latency_ms": receipt.get("latency_ms"),
        "bytes": {
            "logical_payload": sizes.get("payload_bytes"),
            "request_body": body,
            "transport_encoding": receipt.get("transport_encoding"),
            "model_facing": sizes.get("model_facing_bytes"),
            "gateway_body": gateway,
        },
        "refused_answer": dedup.refused_answer(record, ask)
        if record.get("outcome") != "SUCCESS"
        else None,
        "answer": record.get("answer"),
    }
    return checks, summary


def evaluate(out, relay):
    launch, smoke, scan, net, audit = dedup.load_evidence(out, relay, CONTAINER_JSON)
    checks = {}

    def check(name, ok, observed):
        checks[name] = {"passed": bool(ok), "observed": observed}

    smoke = dedup.isolation_checks(
        check,
        launch,
        smoke,
        scan,
        net,
        CLIENT,
        "launched_role_client",
        ROLES,
        "relay_allows_exactly_the_smoke_roles",
    )
    check(
        "payloads_built_by_the_controller",
        "capture_error" not in smoke,
        smoke.get("capture_error"),
    )
    expected = [c for c, _ in EXPECTED_CALLS]
    calls = [c for c in smoke.get("calls") or [] if isinstance(c, dict)]
    recorded = [(c.get("call"), c.get("role")) for c in calls]
    check(
        "every_planned_call_recorded",
        smoke.get("planned_calls") == expected and recorded == list(EXPECTED_CALLS),
        {"planned": smoke.get("planned_calls"), "recorded": [c.get("call") for c in calls]},
    )
    asks = [a for a in audit if a.get("event") == "ask"]
    asks_by_relay = {}
    for a in asks:
        asks_by_relay.setdefault(a.get("relay_id"), []).append(a)
    summaries = []
    for record in calls:
        sub, summary = call_checks(record, asks_by_relay)
        summaries.append(summary)
        for name, value in sub.items():
            checks[f"{record.get('call')}:{name}"] = value
    for call, _ in EXPECTED_CALLS:  # a planned call with no record fails, never vanishes
        if not any(f"{call}:{n}" in checks for n in PER_CALL_CHECKS):
            check(f"{call}:role_call_success", False, "no record")
    capped = dedup.capped_asks(asks)
    forwarded = [a for a in asks if a not in capped]
    check(
        "audit_paid_asks_are_the_planned_calls",
        len(forwarded) == len(EXPECTED_CALLS)
        and all(a.get("outcome") == "SUCCESS" for a in forwarded)
        and [a.get("role") for a in forwarded] == [r for _, r in EXPECTED_CALLS],
        [(a.get("role"), a.get("outcome"), a.get("sent_to_gateway")) for a in asks],
    )
    dedup.cap_stop_checks(check, smoke.get("cap_probe") or {}, audit, capped)
    leaked = sorted({k for a in audit for k in a if k in base.AUDIT_FORBIDDEN_KEYS})
    check("audit_redacted_fields_only", not leaked, leaked)
    shapes = [a.get("answer_shape") for a in asks if a.get("answer_shape") is not None]
    check(
        "audit_answer_shape_has_no_excerpt",
        all(isinstance(s, dict) and not set(s) & set(dedup.EXCERPT_KEYS) for s in shapes),
        [sorted(s) if isinstance(s, dict) else s for s in shapes],
    )
    costs = [s["estimated_usd"] for s in summaries]
    total = round(sum(costs), 6) if costs and all(c is not None for c in costs) else None
    check(
        "spend_within_budget",
        total is not None and total <= BUDGET_USD,
        {"estimated_usd": total, "budget_usd": BUDGET_USD},
    )
    ok = all(c["passed"] for c in checks.values())
    warnings = [
        f"{s['call']}: {s['controller_outcome']} (the live case would stop for a human answer)"
        for s in summaries
        if s["controller_outcome"] == "NEEDS_INFO"
    ] + [
        f"{s['call']}: schema_errors {s['schema_errors']} on an answer the controller accepted"
        for s in summaries
        if s["schema_errors"] and s["controller_accepts"] is True
    ]
    return {
        "schema": SCHEMA,
        "passed": ok,
        "run_url": os.environ.get("RUN_URL"),
        "payload_classification": "frozen synthetic v2.7 reference scenario, Not Sensitive",
        "calls": summaries,
        "estimated_usd_total": total,
        "warnings": warnings,
        "egress_probes": smoke.get("egress_probes"),
        "key_scan": scan,
        "network": net,
        "checks": checks,
    }


def markdown(result):
    lines = [
        f"### Relay role smoke: {'PASS' if result['passed'] else 'FAIL'}",
        "",
        f"estimated USD (list price, all calls): `{result['estimated_usd_total']}`",
        "",
        "| call | role | variant | outcome | controller accepts | controller refusal | "
        "schema errors | forced tool | temp | tokens in/out | USD | model |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for s in result["calls"]:
        refusal = s.get("controller_refusal") or {}
        tokens = s.get("tokens") or {}
        lines.append(
            f"| {s['call']} | {s['role']} | {s['variant']} | {s['outcome']} | "
            f"{s['controller_accepts']} ({s['controller_outcome']}) | "
            f"{json.dumps(refusal.get('text'))[:160] if refusal else ''} | "
            f"{len(s['schema_errors']) if isinstance(s['schema_errors'], list) else None} | "
            f"{s['structured_output']}/{s['answer_source']} hash={s['answer_tool_hash_match']} | "
            f"{s['temperature_sent']} | {tokens.get('input_tokens')}/{tokens.get('output_tokens')} "
            f"| {s['estimated_usd']} | {s['model_observed']} |"
        )
    lines.append("")
    for w in result.get("warnings") or []:
        lines.append(f"- WARNING {w}")
    for s in result["calls"]:
        refused = s.get("refused_answer") or {}
        if refused.get("outcome") is not None:
            lines.append(
                f"- {s['call']} refused by the relay: `{refused.get('reason_code')}` "
                f"{json.dumps(refused.get('reason'))}"
            )
    lines += ["", "| check | pass | observed |", "|---|---|---|"]
    for name, c in result["checks"].items():
        lines.append(f"| {name} | {c['passed']} | `{json.dumps(c['observed'])[:160]}` |")
    return "\n".join(lines) + "\n"


def main(argv):
    out, relay = Path(argv[1]), Path(argv[2])
    result = evaluate(out, relay)
    (out / "RELAY-ROLE-SMOKE.json").write_text(json.dumps(result, indent=1) + "\n", "utf-8")
    md = markdown(result)
    (out / "RELAY-ROLE-SMOKE.md").write_text(md, encoding="utf-8")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as handle:
            handle.write(md)
    print(md)
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
