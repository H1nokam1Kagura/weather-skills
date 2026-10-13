# PROGRESS — W3 recovery semantics (feat/simkit-w3-resume-snowyowl)

## Done so far
- Read WAVE3-BRIEF, OWNER-RULINGS, DECISIONS (CR-03, CP-E10/E11, CP-E01/E02/E05, CP-E03/E06/E12/E13, CR-07/CP-E08), P0-P6 / P0-P4-P5 entries, EVIDENCE_CONTRACT v2_4.resume / v2_4.interruptions / v2_5.resume_verification, score.py resume/repair/interruption checks.
- Read runner.py, e2e_injections.py, agents.py, kong_roles.py, persistence.py, existing tests.
- recovery.py written; runner.resume()/request_resume(), integrity() (delegates to recovery.verify), run() supervisor loop (_supervised_run/recover/bind_recovery); agents.py + kong_roles.py transport_call; e2e_injections drop-fault delivery, CR-03 durable observation, after_role->after_role_committed deferral, notification hook (opt-in), FX-TRANSPORT-RETRY arming.

## Files touched
- tools/workflow/{recovery.py(new),runner.py,agents.py,kong_roles.py,e2e_injections.py}, PROGRESS.md

## Design decided (next steps)
1. NEW tools/workflow/recovery.py: failure classification by exception type (ConnectionResetError->connection_reset, TimeoutError->timeout, PermissionError->write_denied, else unclassified); interruption ledger; itemized verification (request, source, build_runtime, artifacts, evidence, admission, binding, pending_operation); resume decision records; split counters (automatic_resumes_used cap 2, operator_resumes_used uncapped); diagnosis builder; mechanical-repair framework with EMPTY closed action vocabulary (kernel-only lock, D31); notification outbox lifecycle recorded->emitted; transport-retry recorder.
2. runner.py: resume(request=...) records a decision for every request; run() exception path records interruption, runs supervisor (auto resume <=2, transient only, durable pending null), diagnosis + notify-and-wait stop; emit after_operator_notification after durable recorded+emitted.
3. agents.py / kong_roles.py: one pre-write retry on classified transport fault inside same attempt_id/occurrence.
4. e2e_injections.py: interrupt mode harness_drop_model_connection raises classified ConnectionResetError/TimeoutError; accept pending null only when the durable checkpoint (read_checkpoint) observes null; keep legacy 'none'; defer after_role interrupts to after_role_committed on a real controller; after_operator_notification as opt-in known hook (flag) because test_schedule_dispatch_v27.py (not my file) asserts it is NOT_IMPLEMENTED.
5. NEW tests/test_recovery_v27.py with P0 negative tests.

## Resume session (2026-10-13, after network outage)
- Coordinator integration note APPLIED (was uncommitted at outage, reviewed and kept):
  (1) recovery.notify() calls consult_operator_ledger("operator_notification") after the
  notification is durably recorded+emitted (in finally, after the hook);
  (2) resume() binds Controller.operator_resume_authorization() -> initiated_by=operator_ledger
  + operator_record_id (explicit operator_ledger requests must match the handed-out record;
  unattributed requests are upgraded when a record is handed out; legacy cap of 2 applies only
  to still-unattributed resumes, itemized after verification);
  (3) requested_by=operator resume requests write controller_trace.transition_requests
  ACCEPTED/REJECTED (shape matches operator_ledger.submit_transition_request, requested_via
  resume_request) with a transition_request dispatch event.
- test_recovery_v27.py written; two assertions updated to follow (2)/(3).
- ruff format applied to changed files; ruff check clean.
- Per coordinator: full suite NOT run here (coordinator runs it at merge).

## Test status
- See final commit message / report.

## Open questions
- test_schedule_dispatch_v27.py asserts after_operator_notification is unproduced; flipping the default needs that file updated by main session.
- CAP-OWNED-STALE-LOCK-SETUP remains unimplemented (D31); E14-P2-CTL blocked by contract.
