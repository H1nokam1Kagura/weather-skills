# Controlled agentic workflow

## Source admission policies

Production runs admit direct cached CHIRPS. A trusted Python evaluation harness can
instead provide `SyntheticAdmission`, registered with `register_synthetic(source,
manifest, generator)`. There is no production CLI flag or model-supplied request
field that enables it. The scientific profile, tools, review, authorization,
quality gates, strict replay and independent verifier are shared.

Synthetic registration binds source content and path, fixture manifest, generator
code, generator identity and seed. Fixtures must retain explicit synthetic lineage
and have no invented fetch history. The policy and registration enter review
bindings, checkpoint evidence and reports. Resume requires the identical grant;
the ordinary production resume command cannot resume an evaluation run. Older
checkpoints without admission evidence require a new reviewed attempt.

Synthetic maps carry a separate evaluation annotation, preserving requested titles.
Agent inputs distinguish the
scenario's CHIRPS-shaped task from the actual synthetic source; they receive no
case ID, injection label or oracle. Reports and released results carry the
`synthetic-evaluation` classification. Ordinary production admission explicitly
rejects synthetic-labelled sources, even if they also claim CHIRPS identity.

`synthetic_eval` generates an independent simkit fixture, verifies a format-only
NetCDF-to-Zarr conversion, then exercises the shared controller with fixture role
responses and real scientific processes. It does not score or amend frozen cases:

```powershell
.venv/Scripts/python.exe -m tools.workflow.synthetic_eval --simkit-root C:/Users/neilha/agentic-sim --run-dir demo_output/new-synthetic-evaluation
```

The registration is a trusted-harness capability, not an OS security boundary
against arbitrary code running as the same user. Synthetic runs test workflow
behavior and calculation replay, not observational truth or live-agent judgment.

The registered profiles admit cached daily CHIRPS: `cached-onset/v1` produces an
onset dataset and map; `cached-onset-data/v1` produces only the onset dataset.
The profile registry owns stage sequences and declared final artifacts. New
scientific workflows require reviewed profiles rather than model-created tools.

## Authority and contracts

An operator supplies `workflow.request/1`: attributed `messages` (`id`, `speaker`,
`text`), a `constraints` ledger and `authorization`. Each constraint has `value`,
`message_id` and a verbatim `quote`. Required fields are `task`, `bbox` (N/W/S/E),
`start_date`, `end_date`, `dataset`, `variable`, `definition_ref`, `missing_policy`,
`baseline`, `ensemble_claim` and `title`. Authorization references an actual user
message and quote, the canonical goal hash, and `actions: ["cached-onset"]`.
The operator owns this source ledger: substring validation checks attribution,
not whether a quote semantically entails a value or whether a message is genuine.
Models cannot create authorization or modify the ledger.

Three isolated compiler invocations must each preserve the entire canonical goal.
A planner proposes a typed plan using artifact IDs. This profile admits exactly
onset calculation followed by plotting, with the registered 30-day definition,
no baseline or ensemble claim, and preservation of missing values. The controller
constructs argv and parses it through each actual skill parser without opening data.
An independent reviewer approves a hash binding request, goal, plan, code,
definition, runtime versions and input contents. Any drift invalidates that review.

## Execution and release

The controller enforces source check → compilation → plan validation → review →
authorization → onset → onset check → plot → strict replay of both final artifacts
→ independent verifier → release. No skipped state, fallback output path, existing
output reuse or agent-authored PASS file can advance the state machine.
Gate JSON must match the expected artifact and actual process exit. Both final
artifacts need a successful final data replay comparison. The verifier is a separate
model invocation interpreting that evidence; it cannot upgrade failed gates.

Agents run with no tools, no configured MCP servers and fresh sessions. They return
JSON; the controller alone executes allowlisted scripts with `shell=False`.
Controller-owned evidence includes attributed input, build manifest, compiled
goals, bound plan/review, subprocess receipts, checkpoints and final result.
This prevents model-directed arbitrary execution. It is not an OS sandbox against
malicious same-account processes or compromised allowlisted Python/dependencies.
Replay certifies calculated data, not rendered pixels or observational truth.

## Run

Use the repository environment with the pinned core revision and Claude CLI login:

```powershell
.venv/Scripts/python.exe -m tools.workflow --request request.json --source C:/cache/raw.zarr --run-dir demo_output/controller/new-attempt --budget-usd 5
```

The run directory must be new and disjoint from the source. Calls are sequential,
numeric libraries use one thread, launch requires 3 GiB free RAM, and children are
stopped below 2 GiB. Limits: $5 aggregate model budget, 30 minutes total, 15 minutes
per process, 4 MB combined process output. A failure produces INCOMPLETE and a report. Bounded repair and resume obey the
contracts below; neither can create new authority or replenish run budgets.

## Evaluation

`tools/workflow/tests` uses explicit process/model doubles for failure injection;
these fixtures are not evidence of a live agent rehearsal. Tests cover lost
constraints, wrong-region consensus, forged authority, malformed tool plans,
skipped stages, stale review, denied writes, forged success and missing replay or
independent verifier receipts. Live validation additionally requires actual unique
CLI sessions, scientific artifacts, gate processes and replay results. A single
successful scenario demonstrates this profile, not a general reliability rate.


## Lifecycle and recovery

```powershell
.venv/Scripts/python.exe -m tools.workflow profiles
.venv/Scripts/python.exe -m tools.workflow doctor
.venv/Scripts/python.exe -m tools.workflow run --request request.json --source C:/cache/raw.zarr --run-dir demo_output/run-01 --repair-limit 1
.venv/Scripts/python.exe -m tools.workflow status --run-dir demo_output/run-01
.venv/Scripts/python.exe -m tools.workflow resume --run-dir demo_output/run-01
.venv/Scripts/python.exe -m tools.workflow report --run-dir demo_output/run-01
```

A kernel-held lock prevents concurrent writers and releases if the controller exits.
Atomic checkpoints record complete transition evidence, file fingerprints, role
receipts, model cost, consumed time, pending operations and repair/resume counts.
Resume verifies the exact request, source, build/runtime, prior evidence and produced
artifacts. Completed producing steps are not rerun. A crash with a pending operation
has an uncertain outcome and cannot resume automatically. Existing partial outputs
are never overwritten. At most two resumes are permitted, within the original budget.
The checkpoint checksum detects corruption; it is not a signature against a malicious
same-account process. The trusted controller directory is part of the operator boundary.

Plan validation or a critic rejection can invoke a separate repair role, at most the
configured 0–2 times across the entire run. It must return the exact authorized plan
and a bounded response to the actual findings. The corrected plan passes real parser
preflight and fresh independent review. Repeated rejection cannot be bypassed by
resuming. Requests for clarification require a new attributed request after the human
answer; no automatic scope expansion occurs. Scientific failures, permission denials
and data mutation do not trigger model-driven fallback writes.

## Typed policy and execution environment

`workflow.decision/1` binds a finite candidate set to a state and question fingerprint.
The policy adapter rejects stale identities, masked actions and non-finite/out-of-range
scores. `--policy shadow` runs a tool-free router at the authorized execution boundary,
records its prediction and retains the deterministic action. Model promotion is not
exposed on the CLI: shadow evidence must precede a separately reviewed promotion.

Tool argv is controller-owned and shell-free. Models have no tools, configured MCP
servers or project customizations. Registered Python tools execute as trusted host
code. Run-local scratch holds temporary computation and matplotlib caches. The CLI
and dependency resolver may maintain their own host caches; these are not scientific
outputs. The workflow does not claim OS-level isolation from compromised Python or
other processes using the same account. `doctor` reports dependency provenance,
CLI availability and available RAM; it does not claim authentication succeeded.

Run output includes `report.json` and a portable `report.html`: stage history,
status and failure, model cost, observed process count, memory minimum, repair and
resume counts, artifacts and verification scope. Fixture role receipts remain marked
as fixture evidence. A successful mocked run is never presented as live-agent proof.

## Development evaluation and synthetic journeys

```powershell
.venv/Scripts/python.exe -m tools.workflow eval --cases evals/controller/fixtures/seeded-agentic-eval.DEV-ONLY.json --run-dir demo_output/eval-01
# Explicitly budget the three live model-response probes:
.venv/Scripts/python.exe -m tools.workflow eval --cases evals/controller/fixtures/seeded-agentic-eval.DEV-ONLY.json --run-dir demo_output/eval-02 --model-budget-usd 1
.venv/Scripts/python.exe -m tools.workflow simulate --run-dir demo_output/sim-01 --scenario clean
.venv/Scripts/python.exe -m tools.workflow simulate --run-dir demo_output/sim-02 --scenario interrupt
```

Other simulation scenarios: `tamper`, `denied`, `review-repair`. Use `--task onset_data`
for the dataset-only profile. Simulations default to explicit role doubles but use
actual scientific tools and strict replay; `--model-budget-usd` enables live roles.
Their source is visibly synthetic, never observational truth. Rainfall pulses have
an algebraic expected onset date independent of the calculation implementation;
missing cells must remain undated. The interrupted scenario resumes from a durable
checkpoint. Tampering and denied writes must prevent release and preserve the source.

The imported Opus DEV cases and oracles remain unchanged. Adapters map their declared
stimuli to real controller boundaries, with source hashing, before/after effects and
actual resource-process termination. Reports distinguish controller conformance,
live model interpretation and whole-case coverage. `PARTIAL_COVERAGE`, unresolved
oracles, harness errors and unexecuted cases are not passes. The full heldout file is
not accepted by this development entry point. Keep it under separate custody until
the candidate build is frozen.

Trusted Python harnesses can attach hooks at `before_run`, `before_role`, `after_role`,
`before_tool`, `after_tool` and `after_transition`. These support scheduled injection
at specific lifecycle boundaries. Hooks are not agent capabilities or CLI permissions.
Injected runs are marked and require their harness on resume. A future independent
end-to-end set can use these hooks and fixtures without changing production policy.
