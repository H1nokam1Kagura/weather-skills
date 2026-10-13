"""Turn the frozen capability inventory into an explicit completion work queue."""

import json
from pathlib import Path

root = Path(__file__).resolve().parent
manifest = json.loads(
    (
        root
        / "frozen-baseline/casesets/cached-onset-e2e-v2_7/frozen/agentic-e2e-injections.ADAPTER-REQUIREMENTS.json"
    ).read_text()
)
matrix = json.loads((root / "CAPABILITY_MATRIX.json").read_text())["cases"]
groups = [
    (
        "P1",
        "Native receipts and evidence identity",
        [],
        "RECEIPTS BOUND-SOURCES CONTROLLER-HASH RUN-PROVENANCE REASON-MAP ROLE-OUTPUT-SCHEMA",
        [
            "tools/workflow/runner.py",
            "tools/workflow/agents.py",
            "tools/workflow/review_evidence.py",
            "adapter_support/weather_adapter.py",
        ],
        "Extend existing opt-in native attempt records to the exact frozen receipt schema; map only observed fields. Bind failed attempts, successful receipts, reviewer inputs and durable transitions. Do not equate content hashes with authenticated producers.",
        "Success/failure/cancellation/resume fixtures preserve exact identity, sequence and source binding. Tampered or absent evidence is rejected. Both modes and adapter-off regressions pass.",
    ),
    (
        "P2",
        "Disposable fixtures and configuration",
        ["P1"],
        "DISPOSABLE-FIXTURE PROFILE REPAIR-CONFIG LEDGER-SEED LEDGER-APPEND",
        [
            "adapter_support/fixture_staging.py",
            "adapter_support/weather_adapter.py",
            "tools/workflow/runner.py",
        ],
        "Wire current synthetic NetCDF-to-Zarr staging into new owned case roots; select the real controller profile and native repair/budget configuration. Persist truthful fixture provenance separately from SUT inputs.",
        "Per-case roots are disjoint; reuse and path escape refused; exact decoded values preserved; seeded/appended ledgers are actually consumed by controller; no network fetch or paid call.",
    ),
    (
        "P3",
        "Observed gates, claims, writes and symbol resolution",
        ["P1", "P2"],
        "EVIDENCE-CHECKER EXHAUSTIVE-WRITES HARNESS-OBS PRE-REVIEW-GATES SYMBOLS BINDING-PASSTHROUGH",
        [
            "adapter_support/weather_adapter.py",
            "tools/workflow/review_evidence.py",
            "tools/workflow/runner.py",
        ],
        "Read frozen assertions and structures before designing producers. Current citation identity is not entailment. Implement deterministic existing semantics where supported; identify any missing reviewed claim-grader or write-observation mechanism explicitly.",
        "Every asserted field traces to native bytes or a named trusted observation. Negative tests reject unsupported claim verdicts, missing writes, stale bindings and invented empty streams. Symbol resolution covers each used frozen symbol.",
    ),
    (
        "P4",
        "Process boundary alternative",
        ["P1"],
        "SIGNAL-CAPTURE DECOY INTERRUPT MIDPROCESS",
        [
            "tools/workflow/processes.py",
            "adapter_support/process_observation.py",
            "adapter_support/signal_capture.py",
            "completion-evidence/containment_canary.py",
        ],
        "Execute remaining CONTAINMENT-PLAN canaries before opt-in backend integration. Keep original CAP-SIGNAL-CAPTURE false: Job containment cannot satisfy it. Prepare a separately versioned candidate contract and exact changed assertions only when authority covers that change.",
        "Owned normal/timeout/cancellation/restart/crash/breakaway/broker cases never promote UNKNOWN to CLEAN. Decoy survival is not proof of no signals. No frozen comparison runs until original capability is proven or a distinct candidate package is explicitly established.",
    ),
    (
        "P5",
        "Truthful schedule dispatch and fault delivery",
        ["P1", "P2"],
        "ROLE-SUBSTITUTE ROLE-INPUT-PATCH FIXTURE-OP GATE-REPORT-PATCH DENY-WRITE METRIC TRANSITION-REQUEST SCHEDULE-DEPENDENCIES REVIEW-APPROVAL-HOOK",
        ["adapter_support/weather_adapter.py", "tools/workflow/runner.py"],
        "Map each frozen schedule hook to an actual controller boundary. Record scheduled versus delivered faults, occurrence selection, dependencies and legitimate skip separately. Never manufacture DELIVERY from intention.",
        "Positive/negative hook tests prove exact occurrence and payload affected, no duplicate delivery, dependencies honored, rejected transitions unchanged, injection errors distinct from SUT failures and cleanup preserved.",
    ),
    (
        "P6",
        "Resume, operator, notification and retry semantics",
        ["P1", "P5"],
        "AUTO-RESUME DIAGNOSIS MECHANICAL-REPAIR OPERATOR-ACTIONS OPERATOR-NOTIFY OPERATOR-NOTIFY-HOOK OPERATOR-RECORD-SETUP OWNED-STALE-LOCK-SETUP REREVIEW RESUME RESUME-ATTRIBUTION TRANSPORT-RETRY TRANSPORT-RETRY-SETUP SHADOW",
        [
            "tools/workflow/runner.py",
            "tools/workflow/agents.py",
            "tools/workflow/persistence.py",
            "adapter_support/weather_adapter.py",
        ],
        "First distinguish existing controller behavior from missing SUT semantics. Implement approved native behavior before adapter mapping. Preserve budget and attribution across restart; kernel lock release is not evidence of PID-bearing stale-lock repair. Never invent operator acceptance or delivery.",
        "Cases cover duplicate/late notification, failed dispatch, same-invocation retry, exhausted budgets, unknown outcome resume refusal, attributed operator action and re-review after changed plan. Shared policy premises handled once, not as 66 blanket HITL requests.",
    ),
    (
        "P7",
        "Role execution mode",
        ["P1", "P5"],
        "LIVE-ROLE",
        ["tools/workflow/agents.py", "adapter_support/weather_adapter.py"],
        "Inspect each of the 34 CAP-LIVE-ROLE cases: determine the required boundary and whether an authorized cached transport is genuinely compliant. Do not substitute canned outputs under the live capability name. Current work order prohibits paid model calls.",
        "Record per-case compliant execution mode or concrete remaining authorization/capability gap. Model receipts are real for the chosen mode; cached and paid/live results remain distinguished.",
    ),
    (
        "P8",
        "Case lifecycle, preflight and reporting",
        ["P1", "P2", "P3", "P4", "P5", "P6", "P7"],
        "NOT-IMPLEMENTED",
        ["adapter_support/weather_adapter.py", "completion-evidence/check_case_admission.py"],
        "Implement run_case only after a complete slice is supported. Preflight against the exact selected package before side effects, stage fixture, execute native controller, persist evidence after every attempt and resume only verified completions.",
        "Start with a fully supported pair among E02-P1/E04-P1/E13-P1/E13-P3 after checking every assertion. Then expand by complete pairs. Persist 66 dispositions/33 pairs; score only eligible executed evidence with pinned scorer. No excluded/unsupported/UNKNOWN result becomes PASS.",
    ),
]
packages = []
assignment = {}
for ident, title, deps, caps, files, work, acceptance in groups:
    ids = ["CAP-" + x for x in caps.split()]
    for cap in ids:
        assert cap not in assignment, cap
        assignment[cap] = ident
    packages.append(
        dict(
            id=ident,
            title=title,
            depends_on=["P0", *deps],
            capabilities=ids,
            candidate_files=files,
            implementation=work,
            acceptance=acceptance,
            owner="Next controller/adapter implementation session",
            status="NOT_COMPLETE",
        )
    )
assert set(assignment) == {c["id"] for c in manifest["capabilities"]}
capabilities = []
for c in manifest["capabilities"]:
    capabilities.append(
        {
            **c,
            "work_package": assignment[c["id"]],
            "case_ids": [r["case_id"] for r in matrix if c["id"] in r["required_capabilities"]],
            "source_mapping_status": "Candidate file scope; exact producer/interface/assertion mapping must be verified in P0 before implementation",
        }
    )
plan = dict(
    schema="simkit-adapter-completion-plan/1",
    readiness="ORDERED_WORK_QUEUE_WITH_EXPLICIT_DESIGN_GAPS_NOT_A_COMPLETION_GUARANTEE",
    goal="Make every authorized case runnable against an explicitly identified evidence package; preserve unsupported dispositions until proven.",
    first_task=dict(
        id="P0",
        title="Close requirements and authority before implementation",
        inputs=[
            "WORK-ORDER.json",
            "CAPABILITY_MATRIX.json",
            "BASELINE-SELECTION.json",
            "CONTAINMENT-PLAN.json",
            "BLOCKERS.json",
        ],
        deliverable="For each capability and dependent assertion: exact native producer/hook, interface signature, evidence fields, negative tests, execution mode, owner and package identity. Review this mapping independently.",
        acceptance="No capability marked implementable based only on method name. Resolve frozen-versus-candidate target; identify claim/grader, exhaustive-write, authentication and live-role gaps. Planning and bounded authorized canaries need no new HITL. Verify existing authority before requesting any actual policy change.",
        stop_condition="A work package with missing native semantics remains explicit; continue independent packages rather than fabricate evidence.",
    ),
    packages=packages,
    capabilities=capabilities,
    case_work_packages=[
        dict(
            case_id=r["case_id"],
            pair_id=r["pair_id"],
            packages=sorted({assignment[c] for c in r["required_capabilities"]}),
            required_capabilities=r["required_capabilities"],
        )
        for r in matrix
    ],
    completion_gate=[
        "All 66 named cases and 33 pairs accounted for",
        "Required producers and methods validated before capability promotion",
        "Per-case actual execution/evidence/scorer result, not component tests",
        "Independent review and adapter-off regressions",
        "GS1 evaluated under named unchanged frozen or separately adopted candidate package; no equivalence assertion",
    ],
)
(root / "IMPLEMENTATION-PLAN.json").write_text(json.dumps(plan, indent=2) + "\n")
print(
    f"{len(capabilities)} capabilities, {len(matrix)} cases, {len(packages)} work packages; design gaps explicit"
)
