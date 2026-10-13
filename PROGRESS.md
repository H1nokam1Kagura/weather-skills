# PROGRESS — W3 wave-2 review fixes (feat/simkit-w3-w2fixes-snowyowl, base bf290bc)

Input: review/kong-review-wave2-TRIAGE.json (22 CONFIRMED: 4 medium, 18 low) + 2 side observations.
Scope rule: full suite is run by GitHub CI; locally only touched tests + the Refused census test.

## Status
- [ ] processes.py:352 start-receipt failure -> process_launch still emitted (medium)
- [ ] processes.py:375 exception-path stop failure guarded + fallback cleanup (medium)
- [ ] runner.py:89 dispatch-evidence: process record without receipts -> Refused gate_evidence (medium)
- [ ] signal_stop.py:713 denied TerminateProcess not signalled; per-PID dedupe (medium)
- [ ] low findings (see table at end)
