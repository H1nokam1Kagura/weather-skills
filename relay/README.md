# Host-side Kong relay for containerised runs

Owner ruling `container_route_ruling_2026-10-13.live_roles`: live role calls go from the Linux
run container to a **host-side** Kong relay, and the Kong key never enters the container.

```
container                                  host
---------                                  ----
RelayRoles.ask(role, payload)  --HTTP-->   relay/kong_relay_host.py  (kit interpreter)
  token: per-run random, from env            KongRoles.ask(role, payload)
  no Kong key, no .env                         -> kit worker process (python -I)
                                                  loads the key from the kit .env,
                                                  calls Kong, prints a key-free envelope
```

| Piece | File |
|---|---|
| Container client (`ask(role, payload)`, Roles-compatible) | `tools/workflow/relay_roles.py` |
| Host relay server | `relay/kong_relay_host.py` |
| Offline tests (fake relay, real relay over faked worker, kit mock gateway) | `tools/workflow/tests/test_relay_roles.py` |

## Security properties

- **Key isolation.** The relay process never reads a key or a `.env`. It forwards through the
  existing `KongRoles`, whose worker runs under the kit interpreter with its cwd at the kit
  root; only that worker loads the key. The container receives only the relay URL, a run id and
  a per-run token (`secrets.token_urlsafe(32)`), none of which is a Kong credential.
- **Narrow bind.** Default `127.0.0.1` only; wildcard binds (`0.0.0.0`, `::`) are refused. On
  Rancher Desktop (networking tunnel, observed 2026-10-13) `host.docker.internal` resolves in a
  container to `192.168.127.254`, which the host-switch forwards to the host's loopback; the host
  has no `192.168.127.x` interface. So the container reaches a loopback-only relay and nothing
  on the LAN can.
- **Authentication.** Every request needs `Authorization: Bearer <run token>` and
  `X-Simkit-Run: <run id>` (constant-time compare). Rejections are audited without the
  presented values.
- **Allow-list.** `--allow-role` (repeatable, each must be a KongRoles prompt role).
- **Size cap.** Request body default 64 KiB, hard max 90,000 bytes, under the gateway's
  known-good ~97.6 KB body (the system prompt is added on the host). Oversized bodies are
  refused from `Content-Length` and never read.
- **One in flight per run.** A concurrent request gets 409, never queued.
- **No retries.** One relay request is at most one `KongRoles.ask`; the worker runs with
  `KONG_SDK_MAX_RETRIES=0`, overriding any kit `.env` value (python-dotenv does not override
  process env). The client never retries either.
- **Audit.** Append-only JSONL, one line per event (`relay_start`, `ask`, `auth_rejected`,
  `relay_stop`) with status, via, request id, model, usage, hashes, sizes and outcome only.
  Never the payload, answer text, run token or any key.

## Outcomes

| Relay envelope | HTTP | Client raises | Budget reservation |
|---|---|---|---|
| `SUCCESS` + value + receipt (KongRoles receipt + `relay_id`) | 200 | returns value | cost if priced, else kept |
| `REFUSED`, `sent_to_gateway: false` (auth, role, size, busy, Kong pre-send) | 401/403/409/411/413/415/422 | `Refused` | released |
| `REFUSED`, `sent_to_gateway: true` (answer came back invalid) | 422 | `Refused` | `billed_usd` if known, else kept |
| `UNKNOWN` (KongRoles `KongOutcomeUnknown`, relay internal error) | 502/500 | `RelayOutcomeUnknown` | kept |
| transport failure after bytes were written; unparseable/oversized reply | any | `RelayOutcomeUnknown` | kept |
| connect failed (nothing written) | none | `Refused` | released |

Every `Refused` raised by the client carries a frozen gate code: the runner's role gate
(`ROLE_GATES`, parity-tested) or `budget` for the client's own budget check.

## Running

Host (Kong kit interpreter; the relay refuses any other):

```powershell
& "C:/Users/neilha/gf-agent-publish-env/.claude/skills/kong-ai-gateway/.venv/Scripts/python.exe" -B `
  relay/kong_relay_host.py --ack-not-sensitive --allow-role verifier --max-requests 1 `
  --audit-log $dir/relay-audit.jsonl --token-file $dir/relay.token --ready-file $dir/relay-ready.json
```

`relay-ready.json` holds run id, port and binds (no token). The token file is created
exclusively; hand its content to the container as `SIMKIT_RELAY_TOKEN` via the launcher's
environment (`docker run -e SIMKIT_RELAY_TOKEN ...`), never on a command line.

Container:

```python
from tools.workflow.relay_roles import RelayRoles
roles = RelayRoles.from_env()  # SIMKIT_RELAY_URL / _TOKEN / _RUN_ID / optional _MODEL
roles.ask("verifier", {...synthetic...})
```

Copy only `tools/workflow/{__init__,contracts,profiles,relay_roles}.py` into the container: the
client is stdlib + `contracts` (a test pins this).

Data policy: synthetic, Not Sensitive payloads only.
