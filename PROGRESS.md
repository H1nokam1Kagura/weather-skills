# P8 adapter evidence projection: progress

Branch `feat/simkit-w3-adapter-snowyowl`, base `0e862c8`.

## Done
- `adapter_support/evidence_projection.py`: projection of a finished run dir onto the frozen
  v2.7 evidence shape; MISSING (key absent + `missing[]` entry with owning capability) for
  producers not built yet, never `[]`.
- Container route (OWNER-RULINGS container_route_ruling_2026-10-13): `project_run(run_dir,
  {"observations_path": ...})` loads the container harness analyzer file
  (`simkit.container-harness-evidence/1`) and merges `filesystem_snapshot.created_paths`,
  `process_tree`, `controller_trace.signalled_pids`, `harness_observations.signal_capture_exhaustive`,
  `symbols.HARNESS_CONTROLLER_PID`, `harness_observations.max_concurrent_children` and
  `shell_launches`. Checked against native records (controller pid, every launch in the tree
  with the same creating parent, every native signal_stop pid observed); inconsistency refuses.
  Harness-reported missing fields stay MISSING with the harness reason. CAP-EXHAUSTIVE-WRITES and
  CAP-SIGNAL-CAPTURE are NOT declared.
- `adapter_support/weather_adapter.py`: projection methods, D4 gate, opt-in `run_case`.
- `adapter_support/CAPABILITY-PRODUCERS.json`: D4 record per capability, reviewer null.
- `tests/test_evidence_projection_v27.py`: real Controller on synthetic tmp runs, validated by
  the frozen score.py; three new container-input tests.
- The launch RAM guard: synthetic runs inject a fixed memory reading via the existing
  `Processes(memory_reader=...)` (launch checks record `reading_source: "injected"`); the guard
  itself is unchanged. The earlier debug file `test_zz_dbg_tmp.py` was not present in the tree.

## Test status
- `tests/test_evidence_projection_v27.py tests/test_weather_adapter.py tests/test_weather_adapter_p2.py`:
  see final commit message / report. Full suite runs in GitHub CI (not run locally by brief).
- ruff check + format --check clean on changed files.

## Coordinator notes
- `created_paths` is compared by exact string with `artifacts.<id>.path` in frozen score.py: the
  projection must run in the trace's path namespace (inside the container), or V25 fails.
- `run_case` does not take the observations file: the analyzer runs after the traced run exits,
  so re-project with `capture_evidence(run_dir, {..., "observations_path": p})`.
