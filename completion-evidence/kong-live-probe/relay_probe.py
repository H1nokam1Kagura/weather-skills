"""ONE live synthetic role call from a throwaway container through the host-side Kong relay.

Owner ruling container_route_ruling_2026-10-13.live_roles. Steps: start
relay/kong_relay_host.py under the Kong kit interpreter (127.0.0.1 only, one request), copy
only tools/workflow/{__init__,contracts,profiles,relay_roles}.py plus a tiny client into a
fresh python:3.12-slim container, inspect the container's env names and filesystem for any
Kong credential, run exactly one synthetic ``verifier`` request through the relay, remove the
container, and write redacted metadata to RELAY-RESULT.json.

No retries. Never reads or writes a Kong key or .env; the relay token (not a Kong credential)
reaches the container only through the docker client's environment (``-e NAME``), never argv.

Run from the repo root:  python -B completion-evidence/kong-live-probe/relay_probe.py
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from tools.workflow.kong_roles import KIT_PYTHON  # noqa: E402

IMAGE = "python:3.12-slim"
MODULES = ("__init__.py", "contracts.py", "profiles.py", "relay_roles.py")
CREDENTIAL_NAME_MARKERS = ("KONG", "ANTHROPIC", "OPENAI", "X_KONG", "API_KEY")

# Runs inside the container under `python -I`. Prints exactly one JSON object.
CLIENT = r"""
import json, os, sys
sys.path.insert(0, "/app")
MARKERS = %(markers)r
PAYLOAD = {
    "binding_hash": "synthetic-binding-0001",
    "artifact_manifest": [
        "gate-a exit 0 verdict PASS",
        "gate-b exit 0 verdict PASS",
        "final replay comparison PASS",
    ],
    "output_format": "a single raw JSON object, no markdown code fences",
}
RECEIPT_KEEP = ("relay_id", "role", "input_hash", "output_hash", "cost_usd", "cost_basis",
                "model_requested", "model_observed", "model_match", "usage", "stop_reason",
                "request_id", "via", "latency_ms", "gateway_mock", "transport", "mode")
out = {"env_names": sorted(os.environ)}
out["credential_like_env_names"] = [n for n in out["env_names"]
                                    if any(m in n.upper() for m in MARKERS)]
found = []
for top, dirs, files in os.walk("/"):
    if top == "/":
        dirs[:] = [d for d in dirs if d not in ("proc", "sys", "dev")]
    for name in files:
        low = name.lower()
        if low == ".env" or low.endswith(".env") or "kong" in low:
            found.append(os.path.join(top, name))
out["credential_like_files"] = sorted(found)
out["app_files"] = sorted(os.path.relpath(os.path.join(t, f), "/app")
                          for t, _, fs in os.walk("/app") for f in fs)
from tools.workflow.contracts import Refused
from tools.workflow.relay_roles import RelayOutcomeUnknown, RelayRoles
roles = RelayRoles.from_env(budget=1, timeout=240)
out["relay_url_host"] = roles.host
health = roles.health()
out["health"] = {k: health.get(k) for k in ("outcome", "run_id", "allowed_roles", "model",
                                            "max_body_bytes")}
try:
    value = roles.ask("verifier", PAYLOAD)
except RelayOutcomeUnknown as exc:
    out.update(outcome="UNKNOWN", evidence=exc.evidence)
except Refused as exc:
    out.update(outcome="REFUSED", reason=str(exc), gate=exc.gate)
else:
    receipt = roles.receipts[-1]
    out.update(outcome="SUCCESS", answer_keys=sorted(value),
               receipt={k: receipt.get(k) for k in RECEIPT_KEEP})
out["client_spent_reserved"] = roles.spent
print(json.dumps(out))
"""


def run(argv, env=None, timeout=120):
    return subprocess.run(
        argv, capture_output=True, text=True, encoding="utf-8", env=env, timeout=timeout
    )


def main():
    out_path = Path(__file__).with_name("RELAY-RESULT.json")
    scratch = Path(tempfile.mkdtemp(prefix="simkit-relay-smoke-"))
    ready_file, token_file = scratch / "relay-ready.json", scratch / "relay.token"
    audit_file = scratch / "relay-audit.jsonl"
    name = "simkit-relay-smoke-" + uuid.uuid4().hex[:8]
    result = {
        "schema": "kong-relay-probe/1",
        "generated_utc": datetime.now(UTC).isoformat(),
        "owner_ruling": "container_route_ruling_2026-10-13.live_roles",
        "payload_classification": "synthetic, Not Sensitive",
        "role": "verifier",
        "attempts": 1,
        "retries": 0,
    }
    relay_log = (scratch / "relay.stdout").open("w"), (scratch / "relay.stderr").open("w")
    relay = subprocess.Popen(
        [
            str(KIT_PYTHON),
            "-B",
            str(ROOT / "relay" / "kong_relay_host.py"),
            "--ack-not-sensitive",
            "--allow-role",
            "verifier",
            "--max-requests",
            "1",
            "--run-seconds",
            "420",
            "--call-seconds",
            "180",
            "--max-tokens",
            "256",
            "--budget",
            "1",
            "--audit-log",
            str(audit_file),
            "--token-file",
            str(token_file),
            "--ready-file",
            str(ready_file),
        ],
        cwd=ROOT,
        stdout=relay_log[0],
        stderr=relay_log[1],
    )
    started = time.monotonic()
    container_removed = None
    try:
        while not ready_file.exists():
            if relay.poll() is not None or time.monotonic() - started > 60:
                raise RuntimeError(f"relay did not become ready (exit {relay.poll()})")
            time.sleep(0.2)
        time.sleep(0.2)
        ready = json.loads(ready_file.read_text(encoding="utf-8"))
        result["relay"] = {
            k: ready[k]
            for k in (
                "run_id",
                "port",
                "binds",
                "allowed_roles",
                "model",
                "max_body_bytes",
                "max_requests",
            )
        }
        result["relay"]["interpreter"] = "Kong kit .venv python"
        app = scratch / "stage" / "app"
        (app / "tools" / "workflow").mkdir(parents=True)
        for module in MODULES:
            shutil.copy2(ROOT / "tools" / "workflow" / module, app / "tools" / "workflow" / module)
        (app / "client.py").write_text(
            CLIENT % {"markers": CREDENTIAL_NAME_MARKERS}, encoding="utf-8"
        )
        env = dict(os.environ)
        env.update(
            SIMKIT_RELAY_URL=f"http://host.docker.internal:{ready['port']}",
            SIMKIT_RELAY_TOKEN=token_file.read_text(encoding="utf-8"),
            SIMKIT_RELAY_RUN_ID=ready["run_id"],
            SIMKIT_RELAY_MODEL=ready["model"],
        )
        create = run(
            [
                "docker", "create", "--name", name, "--network", "bridge",
                "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                "--ulimit", "core=0", "--pull", "never",
                "-e", "SIMKIT_RELAY_URL", "-e", "SIMKIT_RELAY_TOKEN",
                "-e", "SIMKIT_RELAY_RUN_ID", "-e", "SIMKIT_RELAY_MODEL",
                IMAGE, "python", "-I", "-B", "/app/client.py",
            ],
            env=env,
        )  # fmt: skip
        if create.returncode != 0:
            raise RuntimeError(f"docker create failed: {create.stderr.strip()[:300]}")
        copy = run(["docker", "cp", f"{app}{os.sep}.", f"{name}:/app"])
        if copy.returncode != 0:
            raise RuntimeError(f"docker cp failed: {copy.stderr.strip()[:300]}")
        inspect = json.loads(run(["docker", "inspect", name]).stdout)[0]
        env_names = sorted(item.split("=", 1)[0] for item in inspect["Config"]["Env"])
        result["container"] = {
            "name": name,
            "image": IMAGE,
            "image_id": inspect["Image"],
            "network": inspect["HostConfig"]["NetworkMode"],
            "privileged": inspect["HostConfig"]["Privileged"],
            "cap_drop": inspect["HostConfig"]["CapDrop"],
            "mounts": inspect["Mounts"],
            "env_names_inspected": env_names,
            "credential_like_env_names_inspected": [
                n for n in env_names if any(m in n.upper() for m in CREDENTIAL_NAME_MARKERS)
            ],
            "files_copied": [f"tools/workflow/{m}" for m in MODULES] + ["client.py"],
        }
        t0 = time.monotonic()
        start = run(["docker", "start", "-a", name], timeout=360)
        result["container"]["exit_code"] = start.returncode
        result["container"]["wall_ms"] = round((time.monotonic() - t0) * 1000)
        try:
            client = json.loads(start.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            client = {"outcome": "NO_CLIENT_OUTPUT", "stderr_tail": start.stderr[-400:]}
        result["client"] = client
        result["outcome"] = client.get("outcome")
    except Exception as exc:
        result["outcome"] = "HARNESS_ERROR"
        result["harness_error"] = f"{type(exc).__name__}: {exc}"
    finally:
        removed = run(["docker", "rm", "-f", name])
        container_removed = removed.returncode == 0 or "No such container" in removed.stderr
        try:
            relay.wait(timeout=30)
        except subprocess.TimeoutExpired:
            relay.terminate()
            relay.wait(timeout=10)
        for handle in relay_log:
            handle.close()
    result.setdefault("container", {})["removed"] = container_removed
    result["relay_exit_code"] = relay.returncode
    if audit_file.exists():  # already redacted metadata only
        result["relay_audit"] = [json.loads(x) for x in audit_file.read_text().splitlines()]
    stderr = (scratch / "relay.stderr").read_text(encoding="utf-8", errors="replace")
    result["relay_stderr_bytes"] = len(stderr)
    token_file.unlink(missing_ok=True)
    out_path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"outcome": result["outcome"], "result": str(out_path)}))
    return 0 if result["outcome"] in ("SUCCESS", "REFUSED", "UNKNOWN") else 1


if __name__ == "__main__":
    sys.exit(main())
