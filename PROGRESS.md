# PROGRESS — W3 operator channel (feat/simkit-w3-operator-snowyowl)

## Done so far
- Read WAVE3-BRIEF, P0 entries (CAP-OPERATOR-ACTIONS, CAP-LEDGER-APPEND, CAP-OPERATOR-RECORD-SETUP,
  CAP-TRANSITION-REQUEST), decisions D14/D20/D21/D26/D28/D30, frozen DEV-ONLY cases E01-P1, E03-P3,
  E07-P1, E14-P3, E15-P2, scorer use of operator_actions / transition_requests.
- Code written: operator_ledger.py, runner.py hunks, e2e_injections.py hunks, test_operator_channel_v27.py.

## Design (planned)
- NEW tools/workflow/operator_ledger.py: JSONL ledger created exclusively by the controller at capture
  (refused if path pre-exists); records {record_id, speaker, text, action?, run_id?}; consumed-prefix
  sha256 detects edit-not-append; decisions -> context["operator_actions"] (frozen fields + sequence,
  reasons), per-decision atomic file + dispatch_event("operator_action", record_id=...). Duplicate
  record_id -> operator_channel.ledger_rejections (keeps operator_actions identity unique).
  answer -> replan (D30/CR-04). Only source=operator_ledger AND speaker=user accepted.
- runner.py: ctor params operator_ledger / operator_run_id; consult_operator_ledger(boundary);
  operator_resume_authorization(); request_transition(payload) (never advance(); REJECTED leaves state);
  stop_needs_info consults ledger; accepted replan re-invokes planner (planner payload passed through),
  repair()/accept_planner() return after a replan.
- e2e_injections.py: FX-OPERATOR-RECORD / FX-LEDGER-APPEND (before_run -> request messages; mid-run ->
  ledger), role-channel/non-resume request_transition -> controller.request_transition.

## Next steps
1. Write operator_ledger.py; 2. runner hunks; 3. e2e_injections hunks; 4. tests
   test_operator_channel_v27.py; 5. pytest full tools/workflow/tests + tests/test_weather_adapter*.py; ruff.

## Test status
- test_operator_channel_v27.py: 37 passed. Ruff check+format clean. Full suite tools/workflow/tests + tests/test_weather_adapter*.py: 424 passed, 1 skipped (626s). DONE.

## Open questions
- Notify-and-wait stop and before-resume call sites belong to other agents (resume()/notify not in my
  region): method provided, wiring left to them.
- E15-P2-CTL replan at after_review_approval sits in the review loop (other agent): not wired.
