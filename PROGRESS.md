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
