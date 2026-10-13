# PROGRESS: W3 wave-2 review fixes (feat/simkit-w3-w2fixes-snowyowl, base bf290bc)

Input: review/kong-review-wave2-TRIAGE.json (22 CONFIRMED: 4 medium, 18 low) plus 2 side
observations. Scope rule: GitHub CI runs the full suite. Locally I ran only the tests for the
touched files plus the Refused census test.

## Status: done (final commit on branch)

| # | Finding | Result | Regression test |
|---|---|---|---|
| M1 | processes.py:352 start-receipt failure after Popen gives exit without launch | FIXED: `process_launch` is emitted from the except path when it was not yet emitted; note on the exception | test_processes_events::test_start_receipt_failure_still_emits_process_launch (failed before, passes after) |
| M2 | processes.py:375 failed second stop leaks the child and loses process_exit | FIXED: the stop is guarded. Fallback `_fallback_kill` does taskkill /T /F or killpg, terminate_job under capture, then kill() and a bounded wait. process_exit is always emitted with stop_reason `exception:<T>+stop_failed`, and the original exception is re-raised with a note | test_processes_events::test_double_stop_failure_falls_back_and_still_emits_exit (failed before, passes after) |
| M3 | runner.py:89 dispatch-evidence mode releases with a silent process manager | FIXED: new `process_receipts.verify`, called from Controller.tool once processes.run returns. It needs exactly one launch_check, process_launch and process_exit, in order, with the same serial and pid (and the returned record's), plus exactly one signal_stop iff the record has one. Otherwise `Refused(gate="gate_evidence", reason="process_receipts_missing")` | test_runner_lifecycle_v27::test_process_record_without_receipts_is_refused[SilentProcesses, NoExitProcesses] (failed before, passes after) |
| M4 | signal_stop.py:713 denied TerminateProcess listed as signalled, duplicated each round | FIXED: denied attempts now go to a new `signal_denied` list (once per PID, dropped if a later round signals the PID) plus an exhaustive reason. signalled_pids/signalled are deduped per PID in first-signal order | test_signal_stop::test_denied_signal_is_recorded_and_not_exhaustive (**updated on purpose**: :303 used to assert 2001 IN signalled_pids), test_signal_stop::test_pid_signalled_in_two_rounds_is_listed_once |
| L1-L5 | write_capture_canary.py:254/281/388/418/444 None coerced to 0/False/[] | FIXED: helpers created_count / elide_created_paths / outside_recorded / _overflow_forces_non_exhaustive / _outside_write_unrecorded / _escaped_paths keep None as None | completion-evidence/test_write_capture_canary.py (4 tests; Windows-only module, not in CI testpaths) |
| L6 | test_weather_adapter_p2.py:367 raw_report_hash unchecked on the unpatched branch | FIXED (assertion added) | test_gate_report_patch_delivers_only_selected_report |
| L7 | __main__.py:127 resume reads the checkpoint before run_lock | FIXED: read_checkpoint and recorded_transport now run inside `with run_lock(root)` | test_runner_lifecycle_v27::test_cli_resume_reads_the_checkpoint_under_the_run_lock (failed before) |
| L8 | __main__.py:130 budget-exhausted resume leaves no terminal | FIXED: CLI pre-check dropped. Controller.resume refuses (gate budget, reason resume_refused) and writes the REFUSED decision, terminal and result.json (recovery.record_refused). The CLI prints a status line and exits 1 | test_runner_lifecycle_v27::test_cli_resume_budget_refusal_records_a_terminal (failed before) |
| L9 | processes.py:405 post-exit memory reading used as minimum; a raising reader skips process_exit | FIXED: `min_free_gib` is None with no in-run sample; reporting.py ignores None | test_processes_events::test_post_exit_memory_read_is_not_reported_as_a_minimum |
| L10 | runner.py:1586 verifier record drops the returned binding/scope | FIXED: `returned_binding_hash`, `returned_scope` (JSON-detached) | test_runner_lifecycle_v27::test_verifier_record_keeps_the_returned_binding_and_scope; test_verifier_pass_is_recorded_and_releases **updated on purpose** (exact-dict assertion gains the two keys) |
| L11 | signal_stop.py:401 launch-failure cleanup skips close_job | FIXED: kill and wait each guarded, close_job in finally, original exception propagates | test_signal_stop::test_launch_failure_cleanup_closes_job_when_wait_raises |
| L12 | test_processes_events.py:261 tree-cleanup test raises before a descendant exists | FIXED: the handler waits for the grandchild's sentinel, then asserts >= 2 signalled and one descendant | test_on_event_failure_still_cleans_up_tree_signal_path |
| L13 | test_processes_events.py:245 abnormal paths do not check byte counts | FIXED: child writes non-UTF-8 bytes; RAM-guard and exception paths assert exact counts | test_ram_guard_stop_counts_exact_bytes, test_exception_stop_counts_exact_bytes |
| L14 | test_runner_lifecycle_v27.py:31 hard-coded frozen path | NOT FIXED HERE: owned by the parallel `simkit-w3-ci-frozen-snowyowl` agent (resolver + fixture for CI). Editing the same line would conflict | n/a |
| L15 | test_runner_lifecycle_v27.py:198 census compares counts only | FIXED: OUTSIDE_INVENTORY is now a Counter of (module, message); observed minus inventory must be empty. Mutation-checked: swapping one processes.py untagged raise for another fails the census | test_static_census_every_refused_raise_carries_a_declared_gate |
| L16 | test_write_confinement.py:97 subset of targets accepted | FIXED: `verdicts == {str(t): "PermissionError" for t in targets}` | test_writes_outside_run_dir_fail_and_nothing_appears |
| L17 | test_write_confinement.py:210 missing write_through defaults to PermissionError | FIXED: one explicit branch | test_junction_and_cross_process_escapes_are_blocked |
| L18 | test_write_confinement.py:290 fresh-SID test does not prove launch | FIXED: `exit_code is not None and tree_pids` | test_fresh_restricting_sid_grants_nothing |
| S1 | processes.py:273 numeric wall_seconds with null exit_code on stopped runs | FIXED per EVIDENCE_CONTRACT.md v2_5.process_reconciliation ("null/null only on a stopped run, with null wall_seconds"): process_exit carries wall_seconds null when exit_code is null. process-NNN.json keeps returncode/elapsed_s as before | test_injected_reader_triggers_ram_guard_at_1_8 (+ wall_seconds None) |
| S2 | runner.py:67/573 role-CLI RAM/time refusal tagged with the role gate | KEPT AS DOCUMENTED, owner note: a Processes RAM/time refusal during a role CLI launch is attributed to that role's gate (e.g. review), not resource_guard. Tagging at the raise site (gate="resource_guard") would fix it, but that changes the census inventory and the role-gate mapping; left for the owner's decision | n/a |

## Notes for the merger / owner
- **Intentional semantic change (M4):** frozen CAP-SIGNAL-CAPTURE text says "Record every PID
  the controller signals (terminated or not)". Denied attempts are still recorded, in
  `signal_attempts` and the new `signal_denied`, but are no longer in `signalled_pids`.
  This is per the W3 brief/triage ruling.
- M3 adds `process_receipts.py` (new module, tagged raises). Controller.tool hunks are around
  `processes.run` only; the artifact write path is untouched.
- Not guarded (out of scope): the post-normal-exit "surviving descendants" `_stop` in
  processes.py. If it raises, the job close (KILL_ON_JOB_CLOSE) still cleans up, but
  process_exit for that serial is not emitted. The root has already exited at that point.
- `recorded_transport` refusals on resume still happen before Controller.resume, so they
  leave no terminal (unchanged; the triage did not raise it).
- The census inventory is now message-pinned. A parallel branch that rewords an untagged
  raise in an outside module will fail the census after merge. That is the intended
  strictness, but expect it.
