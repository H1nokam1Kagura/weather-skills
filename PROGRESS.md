# P8 adapter evidence projection: progress

Branch `feat/simkit-w3-adapter-snowyowl`, base `0e862c8`.

## Done so far
- Read WAVE3-BRIEF, OWNER-RULINGS (D1-D4, write-capture ruling), P0-P1-P2 and P0-P3-P7-P8.
- Read the frozen EVIDENCE_CONTRACT structures and the score.py run_evidence validators
  (`run_evidence_findings`, `run_evidence_v2_findings`, record lists, decision reconciliation,
  provenance, claim resolution). Extracted the V24-EVIDENCE / V25-EVIDENCE /
  V25-PRE-REVIEW-RESOLVED operands from the frozen DEV-ONLY case set.
- Read the native producers at base: runner.py (dispatch events, process events, terminal,
  plan_versions/validations, claim_checks, verifier_verdicts, needs_info, review captures),
  processes.py (launch_check / process_launch / process_exit / signal_stop, start receipts),
  claim_gates.py, review_evidence.py, agents.py, signal_stop.py record shape.

## Files touched
- None yet except this file. No code written.

## Next steps
1. NEW `adapter_support/evidence_projection.py`: read a finished run dir (checkpoint, result.json,
   dispatch-event files, role files, process start receipts, claim-check files, review captures)
   and build the frozen evidence dict. Fields for records that do not exist at base return
   MISSING (key absent), never [].
2. NEW `adapter_support/CAPABILITY-PRODUCERS.json`: the D4 record per capability (producer
   file:line, evidence fields, tests, reviewer null).
3. `weather_adapter.py`: wire the projection in; add `run_case` behind an explicit opt-in that
   refuses unless every required capability is declared; keep `can_run()` honest.
4. NEW `tests/test_evidence_projection_v27.py`: drive the real Controller on synthetic tmp runs
   and validate against the frozen score.py validators.

## Test status
- Nothing run yet (no code changed).

## Open questions
- Is a capability "declared" before the coordinator fills the D4 reviewer field? Current plan:
  new P8 capabilities are gated on a reviewed D4 record, so they stay undeclared until review.
- CAP-EXHAUSTIVE-WRITES stays undeclared (confinement-candidate ruling); `created_paths` stays
  MISSING in frozen evidence.
