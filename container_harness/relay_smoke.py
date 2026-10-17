"""Relay smoke, run INSIDE the harness container (uid 1000) on the relay-only network.

Started by ``launch.py relay-smoke`` as the container's own command (never via docker exec).
Prints exactly one JSON object on stdout:

1. key-absence (container side): environment variable NAMES only (never values), and any file
   whose name looks like a credential store. The host additionally scans the exported
   filesystem for the secret's VALUE (``kong_key_scan.py``), which this side cannot do without
   the value.
2. egress probes: the relay must be reachable (authenticated health); the Kong gateway (by name
   and by the host-resolved IP), 1.1.1.1:443, public DNS (TCP and UDP), github.com, the cloud
   metadata endpoint, other ports on the relay host, the runner's own address and docker0 must
   NOT be.
3. exactly one synthetic ``verifier`` call through RelayRoles (no retries), with the redacted
   receipt.
4. the request-cap probe: when that call succeeded AND the relay's health reports its request
   cap full (``spend.requests >= caps.max_requests``, the smoke relay runs with
   ``--max-requests 1``), one more ask, which the relay must refuse BEFORE forwarding
   (``relay_spend_cap``, the client's ``Refused(gate="budget")``). It cannot reach the gateway
   and spends nothing; it EXHAUSTS the relay's spend guard, so the relay stops itself after
   writing that refusal. Skipped (and recorded as skipped) otherwise, so it can never become a
   second live call.

Data policy: the payload is synthetic, Not Sensitive.
"""

import json
import os
import socket
import sys
import time

sys.path.insert(0, "/opt/ctl")

CREDENTIAL_NAME_MARKERS = ("KONG", "ANTHROPIC", "OPENAI", "API_KEY", "SECRET")
ALLOWED_RELAY_NAMES = {
    "SIMKIT_RELAY_URL",
    "SIMKIT_RELAY_TOKEN",
    "SIMKIT_RELAY_RUN_ID",
    "SIMKIT_RELAY_MODEL",
}
PAYLOAD = {
    "binding_hash": "synthetic-binding-0001",
    "artifact_manifest": [
        "gate-a exit 0 verdict PASS",
        "gate-b exit 0 verdict PASS",
        "final replay comparison PASS",
    ],
    "output_format": "a single raw JSON object, no markdown code fences",
}
RECEIPT_KEEP = (
    "relay_id",
    "role",
    "input_hash",
    "output_hash",
    "cost_usd",
    "cost_basis",
    "model_requested",
    "model_observed",
    "model_match",
    "usage",
    "stop_reason",
    "response_id",
    "request_id",
    "via",
    "latency_ms",
    "gateway_mock",
    "transport",
    "mode",
)
TIMEOUT = 4.0


def tcp_probe(host, port):
    started = time.monotonic()
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        return {"observed": "dns_failed", "detail": f"gaierror {exc.errno}"}
    address = infos[0][4]
    sock = socket.socket(infos[0][0], socket.SOCK_STREAM)
    sock.settimeout(TIMEOUT)
    try:
        sock.connect(address)
        observed = "connected"
    except TimeoutError:
        observed = "timeout"
    except ConnectionRefusedError:
        observed = "refused"
    except OSError as exc:
        observed = f"error:{type(exc).__name__}:{exc.errno}"
    finally:
        sock.close()
    return {
        "observed": observed,
        "address": address[0],
        "ms": round((time.monotonic() - started) * 1000),
    }


def udp_dns_probe(server):
    # Minimal DNS query (id 0x5151, RD, one question: example.com A). Any reply = reachable.
    query = bytes.fromhex("515101000001000000000000") + b"\x07example\x03com\x00\x00\x01\x00\x01"
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(TIMEOUT)
    try:
        sock.sendto(query, (server, 53))
        sock.recvfrom(512)
        return {"observed": "connected", "address": server}
    except TimeoutError:
        return {"observed": "timeout", "address": server}
    except OSError as exc:
        return {"observed": f"error:{type(exc).__name__}:{exc.errno}", "address": server}
    finally:
        sock.close()


def resolve_probe(name):
    try:
        addresses = sorted({i[4][0] for i in socket.getaddrinfo(name, 443)})
        return {"observed": "resolved", "addresses": addresses}
    except socket.gaierror as exc:
        return {"observed": "dns_failed", "detail": f"gaierror {exc.errno}"}


def probes(targets, relay_host, relay_port):
    plan = [
        ("kong_by_name_443", "tcp", "ai-kong-gateway.bmgf.io", 443),
        ("kong_by_host_resolved_ip_443", "tcp", targets.get("kong_ip_host_resolved"), 443),
        ("cloudflare_1.1.1.1_443", "tcp", "1.1.1.1", 443),
        ("google_dns_8.8.8.8_53_tcp", "tcp", "8.8.8.8", 53),
        ("google_dns_8.8.8.8_53_udp", "udp", "8.8.8.8", 53),
        ("github.com_443", "tcp", "github.com", 443),
        ("cloud_metadata_169.254.169.254_80", "tcp", "169.254.169.254", 80),
        ("relay_host_other_port_22", "tcp", relay_host, 22),
        ("relay_host_other_port_80", "tcp", relay_host, 80),
        ("runner_host_ip_22", "tcp", targets.get("runner_host_ip"), 22),
        ("docker0_gateway_22", "tcp", targets.get("docker0_gateway"), 22),
        ("dns_resolve_example.com", "dns", "example.com", None),
    ]
    rows = []
    for name, kind, host, port in plan:
        row = {"probe": name, "kind": kind, "target": host, "port": port, "expected": "blocked"}
        if not host:
            row.update(observed="not_probed", detail="target unknown on host", passed=None)
            rows.append(row)
            continue
        if kind == "tcp":
            row.update(tcp_probe(host, port))
        elif kind == "udp":
            row.update(udp_dns_probe(host))
        else:
            row.update(resolve_probe(host))
        row["passed"] = row["observed"] not in ("connected", "resolved")
        rows.append(row)
    relay_row = {
        "probe": "relay_port_tcp",
        "kind": "tcp",
        "target": relay_host,
        "port": relay_port,
        "expected": "reachable",
    }
    relay_row.update(tcp_probe(relay_host, relay_port))
    relay_row["passed"] = relay_row["observed"] == "connected"
    return [relay_row, *rows]


def credential_like_name(path):
    """A .env file, or a Kong-named file whose name also says key/token/secret/cred/env."""
    low = os.path.basename(path).lower()
    if low == ".env" or low.endswith(".env"):
        return True
    return "kong" in low and any(m in low for m in ("key", "token", "secret", "cred", "env"))


def main():
    out = {"schema": "simkit.relay-smoke.container/1", "uid": os.getuid()}
    names = sorted(os.environ)
    out["env_names"] = names
    out["credential_like_env_names"] = [
        n
        for n in names
        if any(m in n.upper() for m in CREDENTIAL_NAME_MARKERS) and n not in ALLOWED_RELAY_NAMES
    ]
    found = []
    for top, dirs, files in os.walk("/"):
        if top == "/":
            dirs[:] = [d for d in dirs if d not in ("proc", "sys", "dev")]
        for name in files:
            low = name.lower()
            if low == ".env" or low.endswith(".env") or "kong" in low:
                found.append(os.path.join(top, name))
    # The repo legitimately ships Kong-named source files, and "kong" is a substring of
    # unrelated system files (Hongkong CA certs, Asia/Hong_Kong); list them all, flag only
    # credential-shaped names. The authoritative proof is the host-side VALUE scan.
    out["kong_named_files"] = sorted(found)
    out["credential_like_files"] = sorted(
        f for f in found if not f.startswith("/opt/ctl/") and credential_like_name(f)
    )
    targets = json.loads(os.environ.get("SIMKIT_PROBE_TARGETS") or "{}")

    from tools.workflow.contracts import Refused
    from tools.workflow.relay_roles import RelayOutcomeUnknown, RelayRoles

    roles = RelayRoles.from_env(budget=1, timeout=240)
    out["relay"] = {"host": roles.host, "port": roles.port, "run_id": roles.run_id}
    out["egress_probes"] = probes(targets, roles.host, roles.port)
    try:
        health = roles.health()
        out["health"] = {k: health.get(k) for k in ("outcome", "run_id", "allowed_roles", "model")}
    except Refused as exc:
        out["health"] = {"outcome": "REFUSED", "reason": str(exc)}
    try:
        value = roles.ask("verifier", PAYLOAD)
    except RelayOutcomeUnknown as exc:
        out.update(outcome="UNKNOWN", evidence=exc.evidence)
    except Refused as exc:
        out.update(outcome="REFUSED", reason=str(exc), gate=exc.gate)
    else:
        receipt = roles.receipts[-1]
        out.update(
            outcome="SUCCESS",
            answer_keys=sorted(value),
            receipt={k: receipt.get(k) for k in RECEIPT_KEEP},
        )
        out["cap_probe"] = cap_probe(roles, Refused)
    out["client_spent_reserved"] = roles.spent
    print(json.dumps(out))
    return 0


def cap_probe(roles, refused_type, role="verifier", payload=None):
    """One ask past a FULL request cap; must be refused before forwarding (step 4). ``role``
    must be one the relay allows, or the role gate refuses it before the cap is tested."""
    try:
        spend = roles.health().get("spend") or {}
    except refused_type as exc:
        return {"outcome": "SKIPPED", "reason": f"health refused: {exc}"}
    cap = (spend.get("caps") or {}).get("max_requests")
    used = spend.get("requests")
    seen = {"requests": used, "max_requests": cap, "state": spend.get("state")}
    if type(cap) is not int or type(used) is not int or used < cap:
        return {"outcome": "SKIPPED", "reason": "request cap not full", **seen}
    try:
        roles.ask(role, PAYLOAD if payload is None else payload)
    except refused_type as exc:
        return {"outcome": "REFUSED", "gate": exc.gate, "reason": str(exc), **seen}
    except Exception as exc:  # noqa: BLE001 - recorded, the report fails it
        return {"outcome": "ERROR", "error_type": type(exc).__name__, **seen}
    return {"outcome": "SUCCESS", **seen}  # a forwarded call past the cap: the report fails it


if __name__ == "__main__":
    sys.exit(main())
