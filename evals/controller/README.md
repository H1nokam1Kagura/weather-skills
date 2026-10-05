# Controller development evaluation

The fixture `fixtures/seeded-agentic-eval.DEV-ONLY.json` is an unchanged import of the independently generated development set. SHA256: `bd8b9b98e9d67ffaf1237ba650f2d519cb7fc81976af242edf1cf604b6f7d224`. The full/heldout file is not stored here or read by the development harness.

Run `python -m tools.workflow eval --cases evals/controller/fixtures/seeded-agentic-eval.DEV-ONLY.json --run-dir <new-directory>`. Model-response probes require an explicit `--model-budget-usd`; default is zero. This entry point rejects non-DEV filenames before reading their contents.

The imported cases and expected outcomes are not rewritten. `admission-report.json` records mapping questions. Reports separate controller probes using explicit doubles, live model-response probes, real resource-process probes with simulated pressure, and complete external-case coverage. An earlier safe rejection is not silently marked as satisfying an oracle requiring a later specific diagnostic. PARTIAL_COVERAGE and UNRESOLVED_ORACLE are not full-case passes.

Lifecycle simulation is a separate evidence layer: `python -m tools.workflow simulate --run-dir <new-directory> --scenario clean|interrupt|tamper|denied|review-repair`. It uses actual scientific subprocesses and an independent algebraic oracle over visibly synthetic rainfall. Interpretation roles default to explicit doubles. Simulation output reports which tools actually executed and whether strict replay completed.

Latest development evidence: `demo_output/controller-eval-20261005/development-4/summary.json`: all 24 probes passed, with 22 partial-coverage cases and two unresolved oracle mappings. Earlier results remain intact. Run 2 had an invalid synthetic-metadata substitution for the reference-source control. Run 3 exposed a review binding that omitted the injected proposal; run 4 binds the complete candidate. These are adapter corrections, not changes to expected answers.

Successful real-data workflow evidence is separate: `demo_output/controller-rehearsal-20261005/attempt-2`. Six actual independent model sessions, strict replay of both final artifacts, unchanged source, no repair or supervisor intervention. Do not infer a deployment-wide reliability rate from this bounded scenario.
