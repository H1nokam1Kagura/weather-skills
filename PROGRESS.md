# PROGRESS: container harness (wave 3, container route)

Owner ruling: `container_route_ruling_2026-10-13` (OWNER-RULINGS.json).
Branch: `feat/simkit-w3-container-snowyowl` (base 0e862c8).

## Milestones
- [ ] M1 scaffold: Dockerfile, requirements, entrypoint, stage/traced-runner scripts, launcher
- [ ] M2 analyzer (strace -ff -> contract-form evidence) + unit tests
- [ ] M3 image builds (`simkit-harness-ctl:<sha>`), generator sha verified at build and run
- [ ] M4 probe tree scenarios rerun through the new analyzer (regression fixture + tests)
- [ ] M5 canary: real controller, fixture roles, synthetic fixture, cached-onset/v1
- [ ] M6 portable FINAL-PINS lookup in test_runner_lifecycle_v27.py
- [ ] M7 README, overhead measurement, final checks

## Coordinator items (controller changes this task must not make)
(none yet)

## Log
