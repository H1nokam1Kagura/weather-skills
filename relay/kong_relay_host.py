"""Host-side Kong relay for containerised controller runs (one relay process = one run).

Owner ruling ``container_route_ruling_2026-10-13.live_roles``: live role calls go from the
container to this relay; the Kong key never enters the container.

Key isolation: this process never reads a key or a ``.env``. Each forwarded call goes through
the existing ``tools.workflow.kong_roles.KongRoles``, which launches the Kong kit worker under
the kit interpreter; the kit loads the consumer key into *that* worker process only and hands
back a key-free envelope. The container authenticates to this relay with a per-run random
token generated here (``secrets.token_urlsafe``). That token is not a Kong credential.

Controls:
- binds 127.0.0.1 by default; extra ``--bind`` addresses must be specific (no wildcard).
  On Rancher Desktop (networking tunnel) ``host.docker.internal`` resolves inside containers
  to 192.168.127.254, which the host-switch forwards to the host's loopback, so 127.0.0.1 is
  both sufficient and the narrowest bind.
- every request needs ``Authorization: Bearer <run token>`` and ``X-Simkit-Run: <run id>``;
- role allow-list (explicit ``--allow-role``, each must be a KongRoles prompt role);
- request body cap (default 64 KiB, hard max 90 KB: under the gateway's known-good ~97.6 KB);
- one request in flight per run (a concurrent request is refused, never queued);
- no retries: one relay request is at most one KongRoles call, and the worker runs with
  ``KONG_SDK_MAX_RETRIES=0``;
- append-only JSONL audit log with redacted metadata only (status, via, request id, model,
  usage, hashes, sizes). Never the payload, the answer text, the run token or any key.

Two clients (``--client``), same KongRoles worker, receipt, redaction and UNKNOWN semantics:

- ``kit`` (default, laptop): run under the Kong kit's interpreter; the kit worker loads the key
  from the kit's own ``.env`` into the worker only::

    <kit>/.venv/Scripts/python.exe -B relay/kong_relay_host.py --ack-not-sensitive \
        --allow-role verifier --audit-log <dir>/relay-audit.jsonl \
        --token-file <dir>/relay.token --ready-file <dir>/relay-ready.json

- ``runner`` (GitHub runner, no kit): the worker imports
  ``relay/runner_kit/scripts/kong_client.py`` under this interpreter (pinned SDK:
  ``relay/runner_kit/requirements.txt``) and reads ``KONG_ANTHROPIC_KEY`` from the environment
  this relay was started with (a workflow secret scoped to the relay's step). This process only
  checks that the variable is present; it never reads, stores or logs its value. The worker
  inherits it.

Data policy: only Not Sensitive, synthetic content may be relayed (``--ack-not-sensitive``).
"""

import argparse
import hmac
import ipaddress
import json
import os
import secrets
import sys
import tempfile
import threading
import time
import uuid
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.workflow.agents import PROMPTS  # noqa: E402
from tools.workflow.contracts import Refused  # noqa: E402
from tools.workflow.kong_roles import KIT_PYTHON, KongOutcomeUnknown, KongRoles  # noqa: E402
from tools.workflow.relay_roles import (  # noqa: E402
    ASK_PATH,
    DEFAULT_MAX_BODY,
    HEALTH_PATH,
)

RUNNER_KIT = ROOT / "relay" / "runner_kit"
RUNNER_SDK = {"anthropic": "1.13.0"}  # relay/runner_kit/requirements.txt
KEY_ENV = "KONG_ANTHROPIC_KEY"
HARD_MAX_BODY = 90_000  # under the kit's KNOWN_GOOD_BODY_BYTES (97,600) with prompt headroom
SCHEMA = "simkit-kong-relay-audit/1"
UNKNOWN_EVIDENCE = ("status", "via", "request_id", "error_type", "hint", "reason")
RECEIPT_AUDIT = (
    "invocation_id",
    "session_id",
    "input_hash",
    "output_hash",
    "cost_usd",
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
)


def _now():
    return datetime.now(UTC).isoformat()


class AuditLog:
    """Append-only JSONL; one flushed line per event. Callers pass redacted fields only."""

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()

    def write(self, event, **fields):
        line = json.dumps({"schema": SCHEMA, "ts": _now(), "event": event, **fields})
        with self.lock, self.path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
            handle.flush()


class Relay:
    """Transport-free relay logic: authenticate, admit, forward once, audit."""

    def __init__(self, roles, *, run_id, token, allowed_roles, audit, max_body=DEFAULT_MAX_BODY):
        if not isinstance(token, str) or len(token) < 32:
            raise ValueError("run token must be at least 32 characters")
        allowed = frozenset(allowed_roles)
        if not allowed or not allowed <= set(PROMPTS):
            raise ValueError(f"allowed roles must be a non-empty subset of {sorted(PROMPTS)}")
        if type(max_body) is not int or not 1 <= max_body <= HARD_MAX_BODY:
            raise ValueError(f"max_body must be an integer in 1..{HARD_MAX_BODY}")
        self.roles = roles
        self.run_id = run_id
        self._token = token
        self.allowed_roles = allowed
        self.audit = audit
        self.max_body = max_body
        self.in_flight = threading.Lock()
        self.asks = 0
        self.asks_done = threading.Event()
        self.max_asks = None

    def authenticate(self, headers):
        presented = headers.get("Authorization") or ""
        scheme, _, value = presented.partition(" ")
        token_ok = scheme == "Bearer" and hmac.compare_digest(
            value.encode("utf-8", "replace"), self._token.encode()
        )
        run_ok = hmac.compare_digest(
            (headers.get("X-Simkit-Run") or "").encode("utf-8", "replace"), self.run_id.encode()
        )
        return token_ok and run_ok

    def health(self):
        return 200, {
            "outcome": "OK",
            "run_id": self.run_id,
            "allowed_roles": sorted(self.allowed_roles),
            "model": getattr(self.roles, "model", None),
            "max_body_bytes": self.max_body,
            "in_flight": self.in_flight.locked(),
        }

    def refuse(self, status, relay_id, reason, remote, event="ask", **audit_fields):
        relay_id = relay_id or str(uuid.uuid4())  # every definite refusal is identifiable
        self.audit.write(
            event,
            run_id=self.run_id,
            relay_id=relay_id,
            remote=remote,
            http_status=status,
            outcome="REFUSED",
            sent_to_gateway=False,
            reason=reason,
            **audit_fields,
        )
        return status, {
            "outcome": "REFUSED",
            "relay_id": relay_id,
            "sent_to_gateway": False,
            "billed_usd": None,
            "reason": reason,
        }

    def ask(self, body, remote):
        relay_id = str(uuid.uuid4())
        size = {"body_bytes": len(body)}
        try:
            request = json.loads(body)
            if not isinstance(request, dict) or set(request) != {"role", "payload"}:
                raise ValueError("body must be exactly {role, payload}")
        except ValueError:
            return self.refuse(400, relay_id, "malformed relay request", remote, **size)
        role = request["role"]
        if not isinstance(role, str) or role not in self.allowed_roles:
            # Name the role only when it is a known prompt role: never echo arbitrary input.
            shown = role if role in PROMPTS else None
            return self.refuse(403, relay_id, "role not allowed", remote, role=shown, **size)
        if not self.in_flight.acquire(blocking=False):
            return self.refuse(409, relay_id, "a request is already in flight", remote, role=role)
        try:
            return self._forward(relay_id, role, request["payload"], remote, size)
        finally:
            self.asks += 1
            self.in_flight.release()

    def responded(self):
        """Called after a response is fully written: stop once max_asks were forwarded."""
        if self.max_asks is not None and self.asks >= self.max_asks:
            self.asks_done.set()

    def _forward(self, relay_id, role, payload, remote, size):
        spent_before = self.roles.spent
        base = {"run_id": self.run_id, "relay_id": relay_id, "remote": remote, "role": role}
        started = time.monotonic()
        try:
            value = self.roles.ask(role, payload)
        except KongOutcomeUnknown as exc:
            evidence = {k: exc.evidence.get(k) for k in UNKNOWN_EVIDENCE}
            self.audit.write(
                "ask",
                **base,
                **size,
                http_status=502,
                outcome="UNKNOWN",
                invocation_id=exc.evidence.get("invocation_id"),
                wall_ms=round((time.monotonic() - started) * 1000),
                **evidence,
            )
            return 502, {"outcome": "UNKNOWN", "relay_id": relay_id, "evidence": evidence}
        except Refused as exc:
            spent_after = self.roles.spent
            # KongRoles restores its reservation only when provably nothing was sent.
            sent = spent_after != spent_before
            billed = None
            if sent and spent_after < self.roles.budget:
                billed = spent_after - spent_before
            reason = str(exc)
            self.audit.write(
                "ask",
                **base,
                **size,
                http_status=422,
                outcome="REFUSED",
                sent_to_gateway=sent,
                billed_usd=billed,
                reason=reason,
                wall_ms=round((time.monotonic() - started) * 1000),
            )
            return 422, {
                "outcome": "REFUSED",
                "relay_id": relay_id,
                "sent_to_gateway": sent,
                "billed_usd": billed,
                "reason": reason,
            }
        except Exception as exc:  # anything else: the call may have left, never a success
            evidence = {k: None for k in UNKNOWN_EVIDENCE}
            evidence.update(error_type=type(exc).__name__, reason="relay internal error")
            self.audit.write("ask", **base, **size, http_status=500, outcome="UNKNOWN", **evidence)
            return 500, {"outcome": "UNKNOWN", "relay_id": relay_id, "evidence": evidence}
        receipt = dict(self.roles.receipts[-1])
        receipt["relay_id"] = relay_id
        self.audit.write(
            "ask",
            **base,
            **size,
            http_status=200,
            outcome="SUCCESS",
            wall_ms=round((time.monotonic() - started) * 1000),
            **{k: receipt.get(k) for k in RECEIPT_AUDIT},
        )
        return 200, {"outcome": "SUCCESS", "relay_id": relay_id, "value": value, "receipt": receipt}


class Handler(BaseHTTPRequestHandler):
    relay = None  # set per server class
    server_version = "simkit-kong-relay/1"
    sys_version = ""

    def log_message(self, format, *args):  # noqa: A002 - stdlib signature
        pass  # the audit log is the only record; no default request logging

    def _send(self, status, value):
        data = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(data)
        self.close_connection = True

    def _remote(self):
        return self.client_address[0]

    def _authorized(self):
        if self.relay.authenticate(self.headers):
            return True
        # The presented credentials are never recorded.
        self._send(
            *self.relay.refuse(
                401, None, "unauthorized", self._remote(), event="auth_rejected", path=self.path
            )
        )
        return False

    def do_GET(self):  # noqa: N802 - stdlib name
        if self.path != HEALTH_PATH:
            self._send(404, {"outcome": "REFUSED", "relay_id": None, "reason": "not found"})
            return
        if self._authorized():
            self._send(*self.relay.health())

    def do_POST(self):  # noqa: N802 - stdlib name
        relay = self.relay
        if self.path != ASK_PATH:
            self._send(404, {"outcome": "REFUSED", "relay_id": None, "reason": "not found"})
            return
        if not self._authorized():
            return
        if self.headers.get("Transfer-Encoding"):
            self._send(*relay.refuse(411, None, "chunked bodies are not accepted", self._remote()))
            return
        if (self.headers.get("Content-Type") or "").split(";")[0].strip() != "application/json":
            self._send(*relay.refuse(415, None, "content type must be JSON", self._remote()))
            return
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            self._send(*relay.refuse(411, None, "content length required", self._remote()))
            return
        if not 0 <= length <= relay.max_body:
            # Not read: the oversized body never enters this process.
            reason = f"body over the {relay.max_body}-byte cap"
            self._send(*relay.refuse(413, None, reason, self._remote(), body_bytes=length))
            return
        self._send(*relay.ask(self.rfile.read(length), self._remote()))
        relay.responded()


def validate_binds(binds):
    out = []
    for bind in binds:
        address = ipaddress.ip_address(bind)  # raises on a name or empty string
        if address.is_unspecified:
            raise ValueError(f"wildcard bind {bind!r} is refused; name a specific interface")
        out.append(str(address))
    return out


def serve(relay, binds, port):
    """Start one server per bind address on the same port; return the servers."""
    handler = type("RelayHandler", (Handler,), {"relay": relay})
    servers = []
    try:
        for bind in validate_binds(binds):
            server = ThreadingHTTPServer((bind, port), handler)
            server.daemon_threads = True
            port = server.server_address[1]  # first bind picks the port if 0
            servers.append(server)
    except BaseException:
        for server in servers:
            server.server_close()
        raise
    for server in servers:
        threading.Thread(target=server.serve_forever, daemon=True).start()
    return servers


def write_secret_file(path, text):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)


def client_paths(client, environ=None, executable=None):
    """(kit_root, kit_python) for ``--client``; ValueError when this host cannot serve it."""
    environ = os.environ if environ is None else environ
    executable = Path(executable or sys.executable)
    if client == "kit":
        if executable.resolve() != KIT_PYTHON.resolve():
            raise ValueError(f"run under the Kong kit interpreter ({KIT_PYTHON}), not {executable}")
        return None, None  # KongRoles defaults: the kit root and its interpreter
    if client != "runner":
        raise ValueError(f"unknown client {client!r}")
    if not environ.get(KEY_ENV):  # presence only; the value is never kept or logged
        raise ValueError(f"--client runner needs {KEY_ENV} in the relay's environment")
    from importlib.metadata import PackageNotFoundError, version

    for package, pinned in RUNNER_SDK.items():
        try:
            found = version(package)
        except PackageNotFoundError:
            found = None
        if found != pinned:
            raise ValueError(f"--client runner needs {package}=={pinned}, found {found}")
    return RUNNER_KIT, executable


def build_roles(args, kit_root=None, kit_python=None):
    from tools.workflow.processes import Processes

    scratch = Path(tempfile.mkdtemp(prefix="simkit-relay-"))
    (scratch / "proc").mkdir()
    return KongRoles(
        Processes(scratch / "proc", timeout=args.call_seconds, total_seconds=args.run_seconds),
        scratch / "ctx",
        budget=args.budget,
        model=args.model,
        max_tokens=args.max_tokens,
        prices={},  # no cited price table is pinned: cost stays null, reservation kept
        data_policy_ack=True,
        worker_env={"KONG_SDK_MAX_RETRIES": "0"},  # no retries, overriding any .env value
        **({"kit_root": kit_root, "kit_python": kit_python} if kit_root else {}),
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--ack-not-sensitive", action="store_true", required=True)
    parser.add_argument("--allow-role", action="append", required=True, choices=sorted(PROMPTS))
    parser.add_argument("--bind", action="append", default=None)
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--audit-log", type=Path, required=True)
    parser.add_argument("--token-file", type=Path, required=True)
    parser.add_argument("--ready-file", type=Path, required=True)
    parser.add_argument("--max-body", type=int, default=DEFAULT_MAX_BODY)
    parser.add_argument("--max-requests", type=int, default=None)
    parser.add_argument("--run-seconds", type=int, default=1800)
    parser.add_argument("--call-seconds", type=int, default=120)
    parser.add_argument("--budget", type=float, default=1.0)
    parser.add_argument("--model", default="claude-sonnet-4-6")
    parser.add_argument("--max-tokens", type=int, default=512)
    parser.add_argument("--client", choices=("kit", "runner"), default="kit")
    args = parser.parse_args(argv)
    try:
        kit_root, kit_python = client_paths(args.client)
    except ValueError as exc:
        parser.error(str(exc))
    run_id = "relay-run-" + uuid.uuid4().hex[:12]
    token = secrets.token_urlsafe(32)
    audit = AuditLog(args.audit_log)
    relay = Relay(
        build_roles(args, kit_root, kit_python),
        run_id=run_id,
        token=token,
        allowed_roles=args.allow_role,
        audit=audit,
        max_body=args.max_body,
    )
    relay.max_asks = args.max_requests
    binds = args.bind or ["127.0.0.1"]
    servers = serve(relay, binds, args.port)
    port = servers[0].server_address[1]
    write_secret_file(args.token_file, token)
    ready = {
        "run_id": run_id,
        "port": port,
        "binds": [s.server_address[0] for s in servers],
        "allowed_roles": sorted(relay.allowed_roles),
        "model": args.model,
        "max_body_bytes": relay.max_body,
        "max_requests": args.max_requests,
        "client": args.client,
        "pid": os.getpid(),
    }
    audit.write("relay_start", **ready, python=sys.version.split()[0])
    args.ready_file.write_text(json.dumps(ready), encoding="utf-8")
    print(json.dumps({"ready": ready}), flush=True)
    reason = "run_seconds elapsed"
    try:
        if relay.asks_done.wait(args.run_seconds):
            reason = "max_requests reached"
    except KeyboardInterrupt:
        reason = "interrupted"
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()
        audit.write("relay_stop", run_id=run_id, reason=reason, asks=relay.asks)
    return 0


if __name__ == "__main__":
    sys.exit(main())
