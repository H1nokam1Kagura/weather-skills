# PROGRESS (wave 3: review/shadow/write-boundary/metric)

Branch: feat/simkit-w3-review-shadow-snowyowl. WIP file requested by the coordinator; delete before merge.

## Done so far
- Read brief, P0 entries (CAP-REREVIEW, CAP-SHADOW, CAP-DENY-WRITE, CAP-METRIC, CAP-ROLE-INPUT-PATCH, CAP-FIXTURE-OP), decisions CP-E09/E14/E17/E18, frozen E09/E05-P1/E15-P1 cases, EVIDENCE_CONTRACT structures.
- Code written: shadow.py, write_boundary.py, policy.py, agents.py prompt, catalog build_id, runner.py hunks, e2e_injections ops.

## Design decided
- shadow.py: role `shadow_policy`, invoked after GOAL_CHECKED/PLAN_VALIDATED/ONSET_CHECKED/DATA_VERIFIED in policy_mode "shadow";
  output {schema workflow.decision/1, state, question_fingerprint, action, score}; per-state mask PROCEED_TO_<next>/STOP;
  invalid -> record {state, accepted:false, action, score, question_hash, reason}; never applied; authority_granted false.
- Re-review: integrity() records material_changes {index, object, to_hash, sequence}; manifest/source/plan/request(goal unchanged)
  changes while an approval is active and nothing produced -> withdraw REVIEW_APPROVED/AUTHORIZED events, return to PLAN_VALIDATED;
  source change re-runs source check first (source_rechecks); 4th presentation of a plan -> stop review_binding at current state.
- write_boundary.py: producing tool writes into scratch; controller commits (moves) into artifacts path; injected PermissionError ->
  Refused(gate write_denial, reason write_denied) with denial_source injected_fault.
- catalog: runtime manifest gains build_id (from a disposable runtime-manifest file).
- metric monitor for budget readings (non-RAM), harness-only injected_reading records.

- Coordinator note handled: return_to_review consults getattr(self, 'consult_operator_ledger') at boundary 're_review' (allowed=('replan',)); an accepted replan (dict accepted True, action replan) rewinds to GOAL_CHECKED, clears plan/binding, planner runs again, same budgets. Return shape of consult_operator_ledger assumed; verify at merge.

## Next steps
1. Write shadow.py, write_boundary.py, policy/agents/catalog edits.
2. runner.py hunks (integrity, shadow call in advance, tool() commit path, run() re-entry).
3. e2e_injections fixture ops + mapping + deny_write + inject_metric.
4. DONE: tests file. Remaining: targeted suites, final commit, report.

## Test status
- tests/test_rereview_shadow_v27.py written (38 tests): 38 passed (but ~640s wall; durations being measured).
- Resumed session (2026-10-13): kept uncommitted integrity() hunk (records request material change before
  re-raising when the goal no longer parses); ruff format applied to policy.py/e2e_injections.py/new tests.
- Coordinator: no full suite; targeted suites only (e2e_injections, runner_lifecycle, workflow, agents,
  admission, schedule_dispatch, recovery, review_evidence, dispatch_evidence).

- Resume #2 (2026-10-13): reviewed coordinator checkpoint 3c98c36 (PROGRESS.md only; kept). Added
  test_forged_role_channel_replan_is_rejected_and_the_cap_still_stops (CAP-REREVIEW negative 4) and
  missing-variable negatives for FX-DEF-PARAM-EDIT / FX-DEF-TOUCH (CAP-FIXTURE-OP). File: 39 tests,
  36 passed + 3 transient host WinError 5 on checkpoint os.replace (persistence.atomic_json), all 3
  pass on rerun. Ruff check + format clean on all changed files.
- Targeted: test_e2e_injections.py + test_workflow.py + Refused census test: 42 passed.
- Not done by design: CAP-METRIC free_ram_gib injection (needs a Processes memory_reader seam in
  processes.py, outside declared files); stays UNSUPPORTED. request_transition replan delivery is
  the operator track's; here it stays UNSUPPORTED (tested).

## Open questions
- Re-entering _run after return-to-review needs a one-line change in run(); reported as outside strict region.
