# Wave-3 relay agent progress (feat/simkit-w3-relay-snowyowl)

Owner ruling: container_route_ruling_2026-10-13.live_roles — live role calls go from the
container to a host-side Kong relay; the Kong key never enters the container.

## Milestones
- [x] M0 read brief, ruling, Kong skill rules, KongRoles, runner role attributes
- [x] M0 probe: Rancher Desktop container DNS: host.docker.internal = 192.168.127.254,
      gateway.docker.internal = 192.168.127.1; host has no 192.168.127.x interface
      (networking tunnel forwards to host loopback) => bind 127.0.0.1 only. Confirmed by the
      live attempt: the relay audit records the container's request as remote 127.0.0.1.
- [x] M1 relay_roles.py + kong_relay_host.py + offline tests + README (WIP 3369dec)
- [x] M2 ruff check + format --check clean on all four changed Python files.
      Relay tests: 39 pass, 1 fail (environmental, below). Full suite NOT run, on coordinator
      instruction (coordinator runs it at merge).
- [ ] M3 live end-to-end smoke — ONE attempt made (before the outage), outcome HARNESS_ERROR;
      not re-run (no-retries rule; host currently cannot run it anyway). See below.

## Environmental failure (not a code defect)
`test_relay_through_real_worker_to_the_kit_mock_gateway` expects `Refused` (mock answers "OK",
not JSON) but gets `RelayOutcomeUnknown("worker process failed: Refused")`. Cause, verified
directly: `Processes(...).run(...)` raises `Refused("insufficient free RAM to start a child")` —
`free_gib()` read 2.02 then 1.31 GiB against the required `ram_floor + 1` = 3 GiB. The
Rancher Desktop VM (vmmemWSL ~9 GB) holds most host RAM. KongRoles correctly classifies a
process-launch failure as UNKNOWN. Re-run the test when >= 3 GiB is free; test left unchanged.

## Live attempt 1 (completion-evidence/kong-live-probe/RELAY-RESULT.json)
- Harness: completion-evidence/kong-live-probe/relay_probe.py (relay under the Kong kit
  Python, 127.0.0.1 only, max 1 request; container python:3.12-slim, --pull never,
  cap-drop ALL, no mounts, only the four stdlib modules + client copied).
- Key-absence evidence collected before the call: container env names are only image
  defaults + SIMKIT_RELAY_{URL,TOKEN,RUN_ID,MODEL}; zero credential-like env names; no mounts.
  The in-container filesystem scan result was not captured (client output lost to the timeout).
- The one relay request reached the relay (auth passed), forwarded to KongRoles, and the
  worker launch failed: `worker process failed: Refused` (same RAM-floor refusal as above,
  most likely), audit wall_ms 900469; relay returned 502 UNKNOWN and stopped cleanly (exit 0).
  `docker start -a` hit its timeout with a negative remaining time, consistent with a host
  suspend / outage during the call.
- `container.removed: false` — `docker rm -f` failed because the Rancher backend was
  unreachable. **Main session: once Rancher is back, run
  `docker rm -f simkit-relay-smoke-6913721c`** (it holds only the run token of a dead relay).
- Current host state: `docker` returns "failed to connect to the backend: timed out dialing
  Hyper-V socket"; free RAM 1.3 GiB. A live smoke cannot run until both recover. A second
  attempt is a retry and needs a main-session decision.

## Wiring needed outside the declared files (not done here)
- runner.py / __main__.py: select `RelayRoles.from_env()` as the roles implementation when the
  run executes inside the container (e.g. when SIMKIT_RELAY_URL is set), instead of KongRoles.
- The container launcher must pass SIMKIT_RELAY_URL / _TOKEN / _RUN_ID / _MODEL via the docker
  client's environment (`-e NAME`), never argv, and start relay/kong_relay_host.py on the host
  under the Kong kit Python first (see relay/README.md, "Running").
- agents.py / kong_roles.py: no change required; RelayRoles mirrors the KongRoles `ask`,
  receipts, budget and dispatch_observation surface.
