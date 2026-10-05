# Independent end-to-end evaluation integration

The 48-case Opus development corpus is preserved byte-for-byte in
`fixtures/e2e-20261005/`. The SHA-256 values match the independently maintained
simkit corpus: dataset `9198f60612770b98e131419b4baca4489c441acf80c898746154906b4d23f054`;
requirements `a2f49b843ca8741d1055d0c9e8f352989ea3db79e58a8252b341275928054118`.
The full held-out dataset has not been opened.

## Implemented locally

- `tools/workflow/e2e_contracts.py`: immutable-corpus admission and conservative
  per-dimension evidence accounting. This is local accounting, not authority to
  ratify oracles or replace simkit's canonical scorer.
- `tools/workflow/e2e_injections.py`: scheduled role and gate patches, occurrence
  selectors, explicit plan-shape mapping, disposable source/onset mutation,
  injected denial, durable interruption and resume-request signalling. Applied,
  unsupported, erroneous and unreached injections are separate outcomes.
- `tools/workflow/runner.py`: after-tool hooks now expose the actual argument list
  so an adapter can distinguish per-artifact verification invocations.
- `tools/workflow/e2e_admission.py`: frozen-hash checks and reproducible probes of
  the independent synthetic fixture, with no lineage relabelling or scoring.

The final workflow suite passes **95 tests** (2026-10-05). The approved synthetic
admission policy is now implemented in `admission.py` and `synthetic_eval.py`.
Production remains the default and rejects synthetic sources. A trusted harness
registration binds honest synthetic lineage, seed, source, manifest and generator
hashes; review and resume retain that exact policy identity. Models and production
CLI arguments cannot grant synthetic admission. Both policies share scientific gates.
The end-to-end dispatcher and complete simkit capability adapter remain unfinished.
Do not advertise the hook primitives as a complete capability implementation.

## Implementation answers to the companion handoff

| Question | Observed implementation |
|---|---|
| UC-01 | Shadow routing runs at `AUTHORIZED` only, under role `router`, with `EXECUTE`/`STOP`. The question hash covers schema, state, binding, action set and evidence (`runner.py:590`, `policy.py:9`). The frozen E09 stage/action assumptions do not match. |
| UC-02 | One `verify-run` process per final artifact; each invocation supplies two search directories (`runner.py:619`). |
| UC-03 | Reviewer schema is closed: verdict, binding_hash, findings. Verifier receives binding, artifacts and gates; no reviewer waiver is forwarded and no basis-list output exists (`runner.py:568`, `runner.py:649`). |
| UC-04 | Both profiles require a nonempty string title. Data-only plans omit the plot step (`contracts.py:69`, `contracts.py:140`). Frozen null-title requests are not equivalent to the implemented contract. |
| UC-06 | Checkpoints fsync then atomically replace. `after_transition` fires after checkpoint completion. Pending operations persist before tool launch, and resume rejects uncertain pending outcomes (`persistence.py:13`, `runner.py:253`, `runner.py:368`). |
| UC-07 | Windows uses available physical memory from GlobalMemoryStatusEx; Linux uses MemAvailable (`processes.py:15`). |
| UC-08 | Plot CLI declares input and title; output is supplied by the core decorator. Actual-parser regression checks pass (`plot_onset.py:187`, `plot_onset.py:216`). |
| UC-09 | Compiler payload contains messages, schema and rules only (`runner.py:512`). No source metadata channel exists. AMD-010/011 are candidates for the human reviewer, not automatically applied amendments. |
| UC-10 | Undated onset cells are NaT; output variable is `onset_precip_date`. No-onset and no-data cells share missing-date representation (`onset_date.py:706`, `onset_date.py:1365`). |
| UC-11 | No midprocess hook exists. A before-tool hook cannot prove a crash after child start. Resource and uncertain-operation schedules remain unimplemented (`processes.py:75`). |
| UC-12 | Resolved for harness execution: explicit registered synthetic admission accepts honest Zarr fixtures. Default production admission still requires CHIRPS provenance and rejects synthetic markers. No relabelling or fake fetch history is used. |

## Execution status and next gate

The three subagents independently reviewed oracles, implemented injection hooks,
and implemented admission/evidence accounting. The independent audit identified
19 scoped issues; corpus admission found 24 dimension-applicability conflicts.
Neither number is a candidate-system failure rate.

The fixture probe is separate from a candidate end-to-end run. No 48-case scored
run or paid model call has occurred. The initial sandboxed probe could not terminate
its stalled scientific child; the exact identified child tree was subsequently
terminated outside the sandbox. The rerun records actual tool results. This
environment limitation is not a passing resource-containment test.

The earlier production-policy probe established that format-only conversion from
NetCDF to Zarr succeeds in the onset tool, while production admission rejects it.
Those historical BLOCKED_FIXTURE_ADMISSION records do not describe the new harness policy.

The user approved explicit test-only admission. Five independent synthetic journeys
passed their expected outcomes: clean release, interruption/resume, artifact tamper
stop, denied-write stop, and review/repair release. A separate real CHIRPS production
control passed strict replay with original and copied sources unchanged. An unscored
numerical diagnostic matched 22,000/22,000 independent reference onset cells.
These journey builds precede the final small content-hash, plot-label and negative
grading refinements. Final-build validation passed all 95 tests, checked registration
against the independent fixture and exact negative-stop predicates, rendered the
synthetic footer while preserving the requested title, and released an updated local
fixture through strict replay with its analytic oracle passing. Roles were fixture
doubles and scientific subprocesses were real; these runs made zero model calls.

Evidence is under `demo_output/synthetic-admission-20261005/`: `independent-1/summary.json`,
`independent-1/numerical-diagnostic.json`, `production-control/summary.json`, and
`final-validation/summary.json`. Ruff and `git diff --check` passed.

Human ratification of cases/amendments still precedes scoring; the independent ledger
has not been edited. A total live-model budget is pending. No 48-case scored run,
held-out evaluation or installation was performed. See root `HANDOFF.md` and live Git
for commit state; another session is handling the user-requested snapshot.

New synthetic harness command (new output directory required):

```powershell
.venv/Scripts/python.exe -m tools.workflow.synthetic_eval --simkit-root C:/Users/neilha/agentic-sim --run-dir demo_output/synthetic-admission-20261005/new-run
```

Historical production-policy probe command (new output directory required):

```powershell
.venv/Scripts/python.exe -m tools.workflow.e2e_admission --simkit-root C:/Users/neilha/agentic-sim --run-dir demo_output/controller-e2e-20261005/new-admission
```

Evidence: `demo_output/controller-e2e-20261005/oracle-review.json` and
`demo_output/controller-e2e-20261005/admission-3/`.
