# PROGRESS: frozen SIMKIT v2.7 cases on GitHub Actions (simkit-cases.yml)

Branch `feat/simkit-gh-cases-snowyowl` (repo H1nokam1Kagura/weather-skills). Push THIS branch only;
never main; never merge PRs. `gh` defaults to another repo on this host: always pass
`-R H1nokam1Kagura/weather-skills`.

Authority: WAVE3-BRIEF.md and decisions/OWNER-RULINGS.json under
`C:/Users/neilha/.agentbus/simkit-run-snowyowl/simkit/handoffs/completion-20261012/`
(container_route_ruling_2026-10-13: readings + run_conditions; live roles only via a host-side
Kong relay, key never in a container; D4: every declared capability needs a reviewed
capability-to-producer record).

## State

- Base: fast-forwarded to `origin/feat/simkit-completion-snowyowl` @ 39d127c (CI-green, run
  38463254522: 1201 passed, 39 skipped; already contains the container harness merge b3a6a0f).
  So `adapter_support.evidence_projection` IS available to the in-container projection.
- `container_harness/tests` are NOT in pyproject testpaths (ci.yml does not run them); the new
  workflow runs them in its admission job.

## Milestones

- [x] M1 case driver `container_harness/case_driver.py`: `admission` (frozen `simkit.runner.run`
      with an AdmissionProbe subclass of WeatherAdapter whose run_case raises instead of running,
      so admission is literally the frozen runner's; cases via frozen `effective_cases` over an
      empty ledger, as the frozen CLI dry-run), `stage` (pinned generator, untraced), `run`
      (traced root: frozen runner -> WeatherAdapter(enable_run_case=True).run_case; record on
      fd 3), `score` (pinned frozen guarded scorer, allow_unratified, in-container).
- [x] M2 entry.sh mode `case ID`; launch.py `run case --case-id` and `admission`; Dockerfile
      extracts + verifies the frozen package from tests/fixtures/frozen-v27.tar.xz into
      /opt/frozen-v27 (SIMKIT_FROZEN_V27); run_dir manifest; score.json copied out.
- [x] M3 `container_harness/cases_report.py` -> RESULTS.json/RESULTS.md (PROPOSED oracles header,
      per-case status, pair summary, frozen summarize()); 3 unit tests
      (`container_harness/tests/test_cases_report.py`). Local: 25 passed; ruff clean.
- [x] M4 `.github/workflows/simkit-cases.yml` (admission -> case matrix -> report).
- [ ] M5 GHA admission run (selector=admission) — see Log for the run id/result.
- [ ] M6 small substituted run (<=2 cases) — only if admission admits a case.

## Local admission result (39d127c, before GHA)

0 / 66 admitted; 66 NOT_IMPLEMENTED (33 pairs). implemented_capabilities = CAP-CONTROLLER-HASH,
CAP-DISPOSABLE-FIXTURE, CAP-GATE-REPORT-PATCH, CAP-NOT-IMPLEMENTED, CAP-REPAIR-CONFIG,
CAP-REVIEW-APPROVAL-HOOK, CAP-SCHEDULE-DEPENDENCIES. Needed by all 66 and undeclared:
- D4 reviewer pending (method exists, record PRODUCER_MAPPED, reviewer null): CAP-BOUND-SOURCES,
  CAP-LEDGER-SEED, CAP-PROFILE, CAP-REASON-MAP, CAP-ROLE-OUTPUT-SCHEMA, CAP-RUN-PROVENANCE
- D4 PARTIAL: CAP-RECEIPTS, CAP-SYMBOLS, CAP-HARNESS-OBS, CAP-SIGNAL-CAPTURE
- no adapter method: CAP-EXHAUSTIVE-WRITES (record NOT_DECLARED), CAP-EVIDENCE-CHECKER,
  CAP-OPERATOR-ACTIONS, CAP-PRE-REVIEW-GATES
Then CAP-ROLE-SUBSTITUTE (52), CAP-LIVE-ROLE (34), CAP-FIXTURE-OP (18), CAP-INTERRUPT/RESUME/
RESUME-ATTRIBUTION (14 each), CAP-BINDING-PASSTHROUGH (13), CAP-TRANSITION-REQUEST (10), ...
Role modes: 32 substituted-only, 20 live+substituted, 14 live-only; no case needs neither.

## Design decisions / open items

- Matrix dispositions for ADMITTED cases this workflow does not run: BLOCKED_LIVE_ROLE_TRANSPORT
  (needs CAP-LIVE-ROLE; relay not built), BLOCKED_SUBSTITUTED_ROLE_WIRING (the driver needs an
  adapter hook `WeatherAdapter.substituted_roles_factory(case, goal, run_dir)` returning the
  substituted role doubles for the case's injection_schedule; it does not exist yet).
- EXECUTED only if the controller wrote `run/checkpoint.json`; otherwise NOT_EXECUTED.
- Scoring uses the post-trace in-container projection (`project_run(run_dir,
  {observations_path})`). If it is UNAVAILABLE/ERROR the case is HARNESS_ERROR
  (projection_<status>); the in-process run_case evidence is never substituted. Known gap: the
  post-trace projection lacks run_case's in-process harness inputs (launch_observations,
  source_pre, authorized_goal, run_provenance) -> those fields come out MISSING. Next: have
  run_case persist those inputs (harness/evidence-projection-inputs.json) for re-projection.
- run_case stages the fixture inside the traced run, so the case root's source/harness files
  are created_paths of the traced tree (recorded; review against the frozen writes allow-list).
- definition_source is passed as None (no reference definition text in the controller repo):
  definition-bound review evidence stays MISSING.
- Image: built once in the admission job, `docker save | gzip` artifact (only if count > 0);
  case jobs check the loaded image id equals build.json image_id.
- push trigger = smoke: SMOKE_IDS (E02-P1 CTL+INJ) and only if admitted.

## Log
- 2026-10-13: merged integration (ff to 39d127c); built M1-M4 locally.
- 2026-10-13 GHA push smoke run 38465559689 (first push of this branch): success; admission
  in-container (frozen_root /opt/frozen-v27, FINAL-PINS 153f207e...; image sha256:63179befae8d...),
  0/66 admitted, case job skipped, report produced RESULTS (66 NOT_IMPLEMENTED, 33 pairs).
- 2026-10-13 GHA dispatch selector=admission run 38465652610: success; same result (admitted 0,
  NOT_IMPLEMENTED 66). Artifacts downloaded to %TEMP%/simkit-gh-cases/<run-id>/.
  https://github.com/H1nokam1Kagura/weather-skills/actions/runs/38465652610

## Next steps (new session)
1. Coordinator: fill reviewer/reviewed_at in adapter_support/CAPABILITY-PRODUCERS.json for the
   six PRODUCER_MAPPED D4-gated caps (BOUND-SOURCES, LEDGER-SEED, PROFILE, REASON-MAP,
   ROLE-OUTPUT-SCHEMA, RUN-PROVENANCE) once reviewed; that alone admits nothing (other caps
   are needed by all 66) but shrinks every case's missing list by 6.
2. Adapter track: upgrade PARTIAL -> PRODUCER_MAPPED for RECEIPTS, SYMBOLS, HARNESS-OBS,
   SIGNAL-CAPTURE (container trace now supplies the observations), add methods + records for
   EXHAUSTIVE-WRITES (container route), EVIDENCE-CHECKER, OPERATOR-ACTIONS, PRE-REVIEW-GATES,
   ROLE-SUBSTITUTE, and the per-case caps (FIXTURE-OP, INTERRUPT/RESUME, TRANSITION-REQUEST...).
3. Add `WeatherAdapter.substituted_roles_factory(case, goal, run_dir)` (driver contract), else
   admitted substituted cases are BLOCKED_SUBSTITUTED_ROLE_WIRING.
4. Persist run_case's harness inputs for the post-trace re-projection (see Design decisions).
5. When the first case is admitted: merge the latest integration branch, push (smoke = E02-P1
   pair), then `gh workflow run simkit-cases.yml -R H1nokam1Kagura/weather-skills --ref
   feat/simkit-gh-cases-snowyowl -f selector=<ids>`; expect to fix first-run infra issues in the
   case job (never exercised yet). Never retry a failed case automatically.
6. Live roles: host-side Kong relay (key never in the container) before BLOCKED_LIVE cases run.

## Relay wiring (Wave 3 follow-on, 2026-10-13)

Task: wire the host-side Kong relay into simkit-cases.yml so live-role cases can run on GitHub.
Rulings: container_route_ruling_2026-10-13 (run_conditions; live roles via host relay) and
kong_secret_ruling_2026-10-13 (secret KONG_ANTHROPIC_KEY used ONLY by the relay on the runner;
only a per-run relay token enters the container; synthetic only; logs status/via/request-id/
model/usage only).

- [x] R1 merged local branch feat/simkit-w3-relay-snowyowl @ 7c1c734 (not on origin) -> 4ca7ffe.
      Merge drift fixed: relay client receipt field set now matches the integration KongRoles
      receipt (+budget_guard, settings_sent, sent_keys, output_normalization, output_form;
      optional `dispatch` on reviewer receipts); one relay test followed the parse_role_object
      change (one ```json fence is now valid).
- [x] R2 runner Kong client: relay/runner_kit/scripts/kong_client.py (+ requirements.txt,
      anthropic==1.13.0 httpx==0.28.1 = kit venv) and `kong_relay_host.py --client runner`
      (default `kit` unchanged). Same WORKER_SOURCE -> same receipt/redaction/UNKNOWN.
- [x] R3 network: container_harness/relay_ci.sh (net-up / relay-start / teardown): docker
      --internal bridge simkit-relay0 172.30.99.0/24, relay bound to 172.30.99.1:18765 only,
      iptables DOCKER-USER drop in/out of bridge, INPUT drop except relay port, ip6tables drop.
      In-container probes: container_harness/relay_smoke.py.
- [x] R4 workflow: job `relay-smoke` (dispatch selector=relay-smoke); case job starts the relay
      for role_mode=live cases (`launch.py run case --relay-dir`). Secret only in relay-start and
      key-scan step env (pinned by container_harness/tests/test_relay_wiring.py). Key-absence:
      container_harness/kong_key_scan.py (docker export + inspect + artifacts; counts only).
      Assertions/summary: container_harness/relay_smoke_report.py.
      case_driver: live-only admitted cases are now RUNNABLE via RelayRoles (env-gated);
      live+substituted stays BLOCKED_LIVE_ROLE_TRANSPORT; selector `substituted`/push smoke
      never select live cases, new selector `live` does. UNTESTED end to end (0 admitted).
- [x] R5 live relay-smoke on GHA: attempt 2 run 38482123416 PASS (see Log). Live budget used: 2 of 2.
- 2026-10-13 relay-smoke attempt 1, run 38481482724 (eef7878): live call SUCCESS (container ->
  relay -> Kong: model_observed claude-sonnet-4-6, request e8fb4f2ca700033137789159eb5ef767,
  via 1.1 kong/3.15.0.5-enterprise-edition, 184 in / 38 out tokens); all 13 egress probes as
  expected (relay connected; Kong by name/IP, 1.1.1.1, 8.8.8.8 tcp+udp, github, IMDS, runner IP,
  docker0 unreachable; relay host ports 22/80 timeout); key scan 0 matches over 973 MB exported
  fs + inspect + 17 artifact files; Docker 28.0.4 assigned the internal bridge gateway itself.
  Job FAILED on one harness check only: the in-container credential-FILENAME heuristic matched
  "kong" inside Hongkong CA certs / Asia/Hong_Kong (false positive). Fixed (credential_like_name
  + test). Artifacts: %TEMP%/simkit-gh-relay/38481482724/.
- 2026-10-13 relay-smoke attempt 2 (FINAL; 2 of 2 live attempts used), run 38482123416
  (38146d8): PASS, every check true. Live call: model_observed claude-sonnet-4-6, request
  641c3a677aa1690f3857bf7729d52d1f, via 1.1 kong/3.15.0.5-enterprise-edition, 184 in / 38 out
  tokens, latency 1902 ms. Probes identical to attempt 1. Key scan: 0 matches (973 MB fs, inspect,
  17 artifacts). Push run 38482122727 (admission) also green. Artifacts:
  %TEMP%/simkit-gh-relay/38482123416/.

## Next (relay)
- Live-role cases are still NOT admitted (0/66; blockers in SIMKIT-HANDOFF next action #2 /
  "Local admission result" above). Once a live-only case is admitted, `selector=live` runs it
  via `launch.py run case --relay-dir` (case job wiring exists, untested end to end): the adapter
  must accept RelayRoles from profile["roles_factory"]; live+substituted cases need a combining
  hook (BLOCKED_LIVE_ROLE_TRANSPORT).
- Case-job relay limits are provisional: RELAY_MAX_REQUESTS 60, max_tokens 2048, all 7 roles.
- Do not run more live relay smokes without a new owner decision (attempt budget spent).
