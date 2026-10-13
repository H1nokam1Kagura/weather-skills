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

## Next steps
1. Write shadow.py, write_boundary.py, policy/agents/catalog edits.
2. runner.py hunks (integrity, shadow call in advance, tool() commit path, run() re-entry).
3. e2e_injections fixture ops + mapping + deny_write + inject_metric.
4. tests/test_rereview_shadow_v27.py; run full suites + ruff.

## Test status
Existing tools/workflow/tests all pass after code changes; new test file not yet written.

## Open questions
- Re-entering _run after return-to-review needs a one-line change in run(); reported as outside strict region.
