# PROGRESS: container harness (wave 3, container route)

Owner ruling: `container_route_ruling_2026-10-13` (OWNER-RULINGS.json).
Branch: `feat/simkit-w3-container-snowyowl` (base 0e862c8). Retargeted 2026-10-13: the harness
runs on GitHub Actions ubuntu runners (`.github/workflows/simkit-container-trace.yml`), not the
laptop's Docker (memory-starved).

## Milestones
- [x] M1 scaffold: Dockerfile (controller dev deps + strace), entry.sh (modes canary / tree /
      kill-tracer; --no-trace; --kill-tracer-after), stage_case.py, traced_controller.py, launch.py
- [x] M2 analyzer (strace -ff -> contract-form evidence) + 22 unit tests over synthetic fixtures
- [x] M3 pinned inputs vendored under container_harness/pinned/ (raw bytes, -text), hashes in
      pinned_inputs.json, checked on host, at build and at run
- [ ] M4 probe tree scenarios through the analyzer on GHA (tree, tree-untraced, kill-tracer,
      tracer-death) -> SUMMARY.json regression table
- [ ] M5 canary on GHA: real controller, fixture roles, synthetic fixture, cached-onset/v1
- [x] M6 portable FINAL-PINS lookup in test_runner_lifecycle_v27.py (coordinator WIP commit)
- [ ] M7 overhead measurement, final report

## Design notes
- Authoritative evidence is analysed INSIDE the container after the traced tree exits (entry.sh
  step 5, untraced, root), so created_paths are in the trace's own path namespace; frozen
  score.py compares created_paths to artifacts.<id>.path by exact string (coordinator note from
  the adapter track). The canary then runs the adapter projection in the same container
  (`project_in_container.py` -> projection.json); it reports UNAVAILABLE until the adapter track
  (feat/simkit-w3-adapter-snowyowl, adapter_support.evidence_projection.project_run) is merged.
  The host re-analysis of the copied trace is only a determinism cross-check.
- Output schema `simkit.container-harness-evidence/1` (consumed by the adapter's
  load_harness_observations): fields + per-field `missing` {path: reason}; signal_capture_exhaustive
  is never true without signalled_pids.
- Local dry run (Windows venv, untraced) of staging + controller with fixture roles: RELEASED.

## Coordinator items (controller changes this task must not make)
- Fixture role doubles (tools.workflow.simulation.SyntheticRoles / evaluation.FixtureRoles) record
  no input_hash/output_hash, so `--dispatch-evidence` refuses them ("dispatch receipt binding
  mismatch"). The harness wraps them (traced_controller.BoundFixtureRoles: adds the two hashes of
  what the double actually received/returned, keeps mode=fixture). Consider moving that into
  simulation.py.
- Controller signal_capture stays off on POSIX (processes.py raises for signal_capture on non-nt);
  on this route signal capture comes from the harness trace, and the controller's own records
  keep exhaustive_signal_capture=false.

## Log
- 2026-10-13 resume: analyzer hardened (benign strace messages, per-event errors -> MISSING,
  receiver-side delivery cross-check, kernel deliveries counted, clone-time pgid/cwd inheritance,
  TRACER_KILLED), launcher + report + workflow written, unit tests 22 passed, ruff clean.
