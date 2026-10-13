"""Assert and summarise a relay-smoke run (host, stdlib only). Exit 1 when any assertion fails.

usage: python -I container_harness/relay_smoke_report.py OUT_DIR RELAY_DIR
       -> OUT_DIR/RELAY-SMOKE.json and OUT_DIR/RELAY-SMOKE.md (also appended to the job summary)

Inputs: launch.json + relay-smoke.container.json (launch.py relay-smoke), key-scan.json
(kong_key_scan.py), RELAY_DIR/net.json + relay-audit.jsonl (relay_ci.sh). Every check is named
and recorded with its observed value; nothing is inferred from an absent file.
"""

import json
import os
import sys
from pathlib import Path

REQUIRED_PROBES = (
    "relay_port_tcp",
    "kong_by_name_443",
    "cloudflare_1.1.1.1_443",
    "google_dns_8.8.8.8_53_tcp",
    "google_dns_8.8.8.8_53_udp",
    "github.com_443",
    "cloud_metadata_169.254.169.254_80",
    "relay_host_other_port_22",
    "relay_host_other_port_80",
    "dns_resolve_example.com",
)
EXPECTED_ENV_NAMES = {
    "SIMKIT_RELAY_URL",
    "SIMKIT_RELAY_TOKEN",
    "SIMKIT_RELAY_RUN_ID",
    "SIMKIT_RELAY_MODEL",
    "SIMKIT_PROBE_TARGETS",
    "PYTHONDONTWRITEBYTECODE",
    "HOME",
}
AUDIT_FORBIDDEN_KEYS = {"payload", "value", "text", "token", "answer", "key", "system", "user"}


def load(path):
    path = Path(path)
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None


def main(argv):
    out, relay = Path(argv[1]), Path(argv[2])
    launch = load(out / "launch.json") or {}
    smoke = load(out / "relay-smoke.container.json")
    scan = load(out / "key-scan.json") or {}
    net = load(relay / "net.json") or {}
    audit_path = relay / "relay-audit.jsonl"
    audit = (
        [json.loads(x) for x in audit_path.read_text(encoding="utf-8").splitlines() if x.strip()]
        if audit_path.is_file()
        else []
    )
    checks = {}

    def check(name, ok, observed):
        checks[name] = {"passed": bool(ok), "observed": observed}

    conditions = launch.get("run_conditions") or {}
    check("run_conditions_all_true", conditions and all(conditions.values()), conditions)
    check("network_internal", net.get("internal") is True, net.get("internal"))
    passed = set(launch.get("env_names_passed") or [])
    check("only_relay_env_names_passed", passed == EXPECTED_ENV_NAMES, sorted(passed))
    check("container_output_parsed", smoke is not None, launch.get("smoke_parsed"))
    smoke = smoke or {}
    check(
        "no_credential_like_env_names",
        smoke.get("credential_like_env_names") == [],
        smoke.get("credential_like_env_names"),
    )
    check(
        "no_credential_like_files",
        smoke.get("credential_like_files") == [],
        smoke.get("credential_like_files"),
    )
    check("ran_as_uid_1000", smoke.get("uid") == 1000, smoke.get("uid"))
    check(
        "relay_health_ok", (smoke.get("health") or {}).get("outcome") == "OK", smoke.get("health")
    )
    probes = {p["probe"]: p for p in smoke.get("egress_probes") or []}
    for name in REQUIRED_PROBES:
        row = probes.get(name) or {}
        check(f"probe:{name}", row.get("passed") is True, row.get("observed"))
    optional = [p for n, p in probes.items() if n not in REQUIRED_PROBES]
    check(
        "optional_probes_not_failed",
        all(p.get("passed") is not False for p in optional),
        {p["probe"]: p.get("observed") for p in optional},
    )
    receipt = smoke.get("receipt") or {}
    check("role_call_success", smoke.get("outcome") == "SUCCESS", smoke.get("outcome"))
    check(
        "receipt_model_observed",
        isinstance(receipt.get("model_observed"), str) and bool(receipt.get("model_observed")),
        receipt.get("model_observed"),
    )
    check(
        "receipt_live_gateway",
        receipt.get("gateway_mock") is False
        and receipt.get("transport") == "kong-native-anthropic",
        {k: receipt.get(k) for k in ("gateway_mock", "transport")},
    )
    check(
        "key_absent_container_and_artifacts",
        scan.get("key_absent") is True,
        {k: scan.get(k) for k in ("key_supplied_to_scanner", "key_length_ok", "key_absent")},
    )
    asks = [a for a in audit if a.get("event") == "ask"]
    check(
        "audit_one_successful_ask",
        len(asks) == 1 and asks[0].get("outcome") == "SUCCESS",
        [a.get("outcome") for a in asks],
    )
    leaked = sorted({k for a in audit for k in a if k in AUDIT_FORBIDDEN_KEYS})
    check("audit_redacted_fields_only", not leaked, leaked)
    check(
        "relay_stopped_itself",
        any(a.get("event") == "relay_stop" for a in audit),
        [a.get("reason") for a in audit if a.get("event") == "relay_stop"],
    )

    ok = all(c["passed"] for c in checks.values())
    call = {
        k: receipt.get(k)
        for k in (
            "model_requested",
            "model_observed",
            "model_match",
            "usage",
            "stop_reason",
            "request_id",
            "via",
            "latency_ms",
            "relay_id",
            "response_id",
        )
    }
    result = {
        "schema": "simkit.relay-smoke/1",
        "passed": ok,
        "run_url": os.environ.get("RUN_URL"),
        "payload_classification": "synthetic, Not Sensitive",
        "live_call": call,
        "egress_probes": smoke.get("egress_probes"),
        "key_scan": scan,
        "network": net,
        "checks": checks,
    }
    (out / "RELAY-SMOKE.json").write_text(json.dumps(result, indent=1) + "\n", encoding="utf-8")
    lines = [
        f"### Relay smoke: {'PASS' if ok else 'FAIL'}",
        "",
        "| probe | target | expected | observed | pass |",
        "|---|---|---|---|---|",
    ]
    for p in smoke.get("egress_probes") or []:
        target = f"{p.get('target')}:{p.get('port')}" if p.get("port") else p.get("target")
        lines.append(
            f"| {p['probe']} | {target} | {p['expected']} | {p['observed']} | {p['passed']} |"
        )
    lines += [
        "",
        f"live call: `{json.dumps(call)}`",
        "",
        f"key scan: `{json.dumps({k: v for k, v in scan.items() if k != 'container'})}`",
        f"container scan: `{json.dumps({k: v for k, v in (scan.get('container') or {}).items() if k != 'env_names'})}`",
        "",
        "| check | pass |",
        "|---|---|",
    ]
    lines += [f"| {n} | {c['passed']} |" for n, c in checks.items()]
    md = "\n".join(lines) + "\n"
    (out / "RELAY-SMOKE.md").write_text(md, encoding="utf-8")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as handle:
            handle.write(md)
    print(md)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
