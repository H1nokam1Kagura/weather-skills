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
| Spend guard, pricing, sessions, env hygiene, transport-retry parity | `tools/workflow/tests/test_relay_spend_guard.py` |

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
- **Size cap.** Request body default 88,000 bytes on both sides (client `DEFAULT_MAX_BODY`,
  relay `--max-body`), under the relay's hard max of 90,000 (`HARD_MAX_BODY` in
  `kong_relay_host.py`), which sits under the Kong kit's `KNOWN_GOOD_BODY_BYTES` 97,600 (the
  largest body observed to pass; larger ones have failed with a bodiless 500 at the edge).
  Oversized bodies are refused from `Content-Length` and never read. Both caps apply to the
  body actually sent, which is the ENCODED body when the `dedup-sha256/1` transport encoding
  applies (below). The host adds the system
  prompt and the payload is re-serialised as message text, so the gateway body is larger than
  the relay body: the kit worker re-checks it against 97,600 and refuses before sending
  (`sent_to_gateway: false`, nothing billed) when it does not fit.
- **Spend guard** (below). Caps are enforced before forwarding.
- **One in flight per run.** A concurrent request gets 409, never queued.
- **No retries.** One relay request is at most one `KongRoles.ask`; the worker runs with
  `KONG_SDK_MAX_RETRIES=0`, overriding any kit `.env` value (python-dotenv does not override
  process env). The client retries only the frozen pre-write transport fault
  (CAP-TRANSPORT-RETRY): a connection reset/abort before any byte was written
  (`RelayTransport.serial` unchanged), once, and only when the controller's
  `transport_retry_recorder` recorded it. `_transport_call` mirrors `recovery.transport_call`
  (parity-tested).
- **Token never inherited.** `RelayRoles.from_env` removes every `SIMKIT_RELAY_*` name from
  `os.environ`, so tool children (`catalog.child_environment` copies the environment) never
  see the run token.
- **Audit.** Append-only JSONL, one line per event (`relay_start`, `ask`, `auth_rejected`,
  `spend_cap_reached`, `relay_stop`) with status, via, request id, model, usage, sent
  settings, hashes, sizes, spend totals, outcome and fault labels only. Never the payload,
  answer text, run token or any key. Per-call fields: see "Per-call audit fields" below.

## Transport encoding `dedup-sha256/1`

Why: live sub-wave 2 measured the planner, repairer and reviewer request bodies at 113.9 to
122.7 KB, over the 88,000-byte cap, because the same ~55 KB of skill documentation is sent
twice (`documentation` and `review_evidence(_request).sources`). The LIVE TRANSPORT now
deduplicates it losslessly. The controller payload, its recorded role input files and
`input_hash` are untouched. Source of truth: the docstrings and code of
`tools/workflow/relay_roles.py` (`dedup_encode`, `dedup_decode`) and
`relay/kong_relay_host.py` (`render_model_input`, `wire_view`).

**What is shared, and when (client, `dedup_encode`).**

- Candidates are JSON values (strings, objects and arrays; never numbers, booleans or null)
  whose `json.dumps` text is at least `DEDUP_MIN_BYTES` = 2,048 UTF-8 bytes. A candidate is
  shared only if the same text occurs **two or more times** in the payload. The payload root is
  never itself a reference.
- Each shared value is written once in a top-level `shared` table keyed by the sha256 (hex) of
  its exact `json.dumps` text (key order included), in first-appearance order, outer value
  before inner. Every place it occurred holds the reference object `{"$shared": "<sha256>"}`.
  Table entries are encoded the same way, so a shared value may itself contain references.
- Encoding is deterministic: the same payload always yields the same body.
- **Skipped entirely** when no value of at least 2,048 bytes repeats, or when the payload
  already holds a reference-shaped object (a one-key object with key `$shared`, which could not
  be told apart). The body is then exactly `{"role", "payload"}` as before and the receipt says
  `transport_encoding: null`.

An encoded body has exactly these keys: `role`, `payload` (with references), `transport_encoding`
(`"dedup-sha256/1"`), `shared`, `original_sha256` (`contracts.digest` of the original payload,
which is the receipt `input_hash`) and `original_text_sha256` (sha256 of
`json.dumps(payload)`, which also pins key order). Any other key set is refused as `malformed`
(HTTP 400).

**Host decode and verification (`dedup_decode`).** After the role allow-list check and before
the in-flight lock and the spend guard, the relay decodes the body and verifies that the
`transport_encoding` is `dedup-sha256/1`; the `shared` table is a non-empty object; every
reference resolves to a table entry, with no cycle; every table entry hashes to its key; every
entry is referenced; and the decoded payload matches both `original_sha256` and
`original_text_sha256`. Any failure is refused before anything is forwarded:

| Field | Value |
|---|---|
| HTTP status | 400 |
| `outcome` | `REFUSED` |
| `sent_to_gateway` | `false` (`billed_usd: null`) |
| `reason_code` | `dedup_mismatch` |
| `fault_class` / `fault_kind` (audit line) | `DETERMINISTIC` / `dedup_mismatch` |
| `reason` | `transport encoding does not verify: <which check failed>` |

The client raises `Refused` with the role's gate and releases its budget reservation (the relay
proved nothing was forwarded). A dedup mismatch is never rerun: it is deterministic.

**What the model sees (`render_model_input`).** KongRoles receives the DECODED payload, so
`input_hash`, `stdin_hash`, the system prompt and the reviewer `dispatch` binding are those of
the controller payload. The system prompt is NOT changed, so the controller's review binding
still holds. Only the user message differs: instead of `json.dumps(payload)` it is this
deterministic rendering, lines joined with `\n`:

```
TRANSPORT ENCODING dedup-sha256/1. This message is your complete role input. ...  (preamble)

=== ROLE INPUT (JSON) ===
<json.dumps of the encoded payload, references kept>
=== END ROLE INPUT ===

=== SHARED DOCUMENTS (referenced by $shared sha256; <N> blocks) ===
--- $shared <sha256> ---
<json.dumps of the shared value, as stored: a JSON string literal for text; may hold references>
--- end $shared <sha256> ---
...                                                    (one block per table entry, table order)
=== END SHARED DOCUMENTS ===
```

The preamble (`MODEL_INPUT_PREAMBLE`) tells the model that each large value that occurred more
than once is written once in SHARED DOCUMENTS, that every reference is to be read exactly as if
the referenced value were written in full at that position, that nothing was removed, shortened
or summarised, and that its answer rules are unchanged. The spend guard bounds the call's worst
case on this model-facing text, not on the decoded payload.

**Receipts.** The relay adds two fields to the KongRoles receipt (besides `relay_id`):

- `transport_encoding`: `null` (sent plain) or `"dedup-sha256/1"`;
- `transport_bytes`: `request_body_bytes` (the body as received), `payload_bytes`
  (`json.dumps` of the decoded payload), `model_facing_bytes` and `model_facing_hash`
  (`sha256:<hex>` of the user message actually sent), `shared_blocks` and `shared_bytes`
  (0 for a plain request, whose model-facing text is `json.dumps(payload)`).

`input_hash` stays the controller payload's (`contracts.digest` of the decoded payload). The
client refuses a receipt whose `input_hash` is not its own payload's, whose
`transport_encoding` differs from the encoding it sent, whose `transport_bytes` is not an
object, or (when encoded) whose `request_body_bytes` is not the length of the body it sent.

## Per-call audit fields

Every `ask` audit line for a call that reached KongRoles (`SUCCESS`, `UNKNOWN`, the 422
`REFUSED` from KongRoles, and the 500 relay internal error) carries:

| Field | Meaning |
|---|---|
| `transport_encoding`, `transport_bytes` | as in the receipt (above) |
| `temperature_requested` | the relay's configured `--temperature` (`null` when `none`) |
| `settings_sent` | the non-content settings the worker echoed from the request body it built: every body key except `system` and `messages` (`model`, `max_tokens`, and the temperature, which the pinned SDK sends as `{"extra_body": {"temperature": 0}}`) |
| `sent_keys` | the sorted top-level keys of that request body |

`settings_sent` and `sent_keys` are `null` when no worker envelope echoed them (no request
body was built), so a
temperature-0 run is provable per call rather than inferred from a smoke. Refusals made before
forwarding (auth, role, size, malformed, `dedup_mismatch`, busy, spend cap) do not carry them.

## Spend guard and pricing

Prices: `kong_roles.PINNED_PRICES`, claude-sonnet-4-6 at $3 input / $15 output per million
tokens (Anthropic list price, LIVE-WAVE-BRIEF). Every priced receipt says
`cost_basis: "Anthropic list-price estimate; Kong billing not observed"`. Kong exposes no
per-call cost, so every dollar figure here is an estimate.

| Cap | Relay flag | Env (`relay_ci.sh`) | Host default |
|---|---|---|---|
| Calls that may reach the gateway | `--max-requests` | `RELAY_MAX_REQUESTS` | 24 |
| Observed + worst-case-charged tokens | `--max-total-tokens` | `RELAY_MAX_TOTAL_TOKENS` | 1,000,000 |
| USD, list-price estimate | `--budget` | `RELAY_BUDGET` | 5 |
| Per-call output ceiling | `--max-tokens` / `--role-max-tokens ROLE=N` | `RELAY_MAX_TOKENS` / `RELAY_ROLE_MAX_TOKENS` | 2048; planner and repairer 4096 |
| Temperature | `--temperature` (`none` omits it) | | 0 |

The host reads the same env names as argument defaults; flags win.

- **Before forwarding.** Each call's worst case is bounded: input tokens at most one per UTF-8
  byte of the longest system prompt the role can get plus the payload text, plus 64 framing
  tokens; output at the role's `max_tokens`; priced at the requested model's rate. A call
  that could exceed any cap is refused (`HTTP 402`, `outcome: REFUSED`,
  `sent_to_gateway: false`, `reason_code: relay_spend_cap`), and the client raises
  `Refused(gate="budget")`.
- **After each call.** Observed usage is charged; an UNKNOWN outcome (no observed usage) is
  charged at its worst case; a call whose observed model has no price is charged the whole
  remaining budget (fail closed). A pre-send refusal is not charged or counted.
- **First cap hit.** The guard goes `EXHAUSTED` and stays so: one `spend_cap_reached` event
  (with the cap and the totals), every later call refused the same way, and the relay stops
  after writing the response (`relay_stop` reason `spend cap reached: <cap>`). `relay_stop`
  records requests, input/output tokens, estimated USD and the cap state.
- **Request cap.** Reaching `--max-requests` does NOT stop the relay: it keeps serving, so the
  next call is answered with the `relay_spend_cap` refusal (`Refused(gate="budget")`, nothing
  forwarded), which exhausts the guard (`spend_cap_reached` with `cap: max_requests`) and then
  stops the relay. Before 2026-10-15 the relay stopped right after the last allowed call, so the
  next call failed as "unreachable before sending" with the role's gate and the report could
  not label it `RELAY_GUARD_ABORT`. The relay stops itself only when EXHAUSTED (after writing
  that refusal) or after `--run-seconds`; a run that ends exactly at the cap is stopped by
  teardown. The `relay-smoke` job keeps its "relay stopped itself" check meaningful this way:
  after its one live call `relay_smoke.py` reads the relay's health, and only when the request
  cap is full (`--max-requests 1`) sends one probe ask, which the relay must refuse before
  forwarding; the report requires that refusal, `spend_cap_reached` with `cap: max_requests`,
  exactly one call that reached the gateway, and `relay_stop` reason
  `spend cap reached: max_requests` (teardown's kill writes no `relay_stop` line).
- **Sessions.** The relay's KongRoles uses `session_source="response_id"`: the receipt's
  `session_id` is the provider's response id, observed after the call, and the client refuses
  a receipt whose `session_id` differs from its `response_id`.
- **Temperature.** The pinned SDK (anthropic-python 1.13.0) has no typed `temperature`
  argument; the worker sends it through the SDK's `extra_body`, and `settings_sent` echoes
  `{"extra_body": {"temperature": 0}}`. Temperature 0 lowers variance; it does not make a
  model deterministic.

## Fault labels (rerun ruling)

OWNER-RULINGS `live_spend_authorization_2026-10-15.rerun_on_gateway_fault`. Every
non-success `ask` audit line and every client UNKNOWN record carries `fault_class` and
`fault_kind` (`relay_roles.classify_fault`). UNKNOWN lines also carry `gateway_status`,
`error_type` and `retry_after` (seconds, a number, only when the gateway sent `Retry-After`).

| `fault_class` | `fault_kind` |
|---|---|
| `TRANSIENT_GATEWAY_FAULT` | `http_429`, `http_5xx`, `timeout`, `connection_reset`, `no_gateway_response` (no HTTP status observed) |
| `DETERMINISTIC` | `http_4xx` (other than 429), `relay_spend_cap`, `body_cap`, `kong_pre_send` (e.g. gateway body over 97,600), `invalid_answer`, `harness_error`, the other relay refusal codes |

These are labels; the report decides rerun eligibility.

## Outcomes

| Relay envelope | HTTP | Client raises | Budget reservation |
|---|---|---|---|
| `SUCCESS` + value + receipt (KongRoles receipt + `relay_id`) | 200 | returns value | cost if priced, else kept |
| `REFUSED`, `sent_to_gateway: false`, `reason_code: relay_spend_cap` | 402 | `Refused(gate="budget")` | released |
| `REFUSED`, `sent_to_gateway: false` (malformed, `dedup_mismatch`, auth, role, size, busy, Kong pre-send) | 400/401/403/409/411/413/415/422 | `Refused` | released |
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

## GitHub runner (no Kong kit)

`--client runner` runs the same KongRoles worker under the relay's own interpreter against
`relay/runner_kit/scripts/kong_client.py`, a minimal stand-in for the kit's `kong_client`
(pinned SDK: `relay/runner_kit/requirements.txt`). The route is fixed to
`https://ai-kong-gateway.bmgf.io/v2/anthropic` with the key in `x-kong-key` (placeholder
`api_key`); only a loopback `http` mock with the kit's test keys may replace it, so a real key
cannot be sent elsewhere. The key comes only from `KONG_ANTHROPIC_KEY` in the relay's
environment (the Actions secret, scoped to the relay-start step); the relay checks presence and
never reads or logs the value; SDK debug logging is disabled in the worker. Receipts,
redaction and UNKNOWN semantics are KongRoles' own, unchanged. The laptop `--client kit` path is
unchanged and remains the default.

Network path on the runner (`container_harness/relay_ci.sh`): a docker `--internal` bridge
`simkit-relay0` (172.30.99.0/24); the relay binds only the bridge gateway 172.30.99.1:18765;
iptables drops all forwarded traffic in/out of the bridge (DOCKER-USER), drops all INPUT from it
except TCP to the relay port, and ip6tables drops IPv6 from it. The `relay-smoke` selector of
`.github/workflows/simkit-cases.yml` proves it from inside the container and proves key absence
with a host-side scan of `docker export` (counts only).
