# PROGRESS — feat/simkit-w3-kong-wire-snowyowl (Kong live-role wiring, D16 / CP-F1)

## Done so far
- Read WAVE3-BRIEF, Kong SKILL.md, kit models.md / probe-log / probe-results.jsonl, kong_roles.py,
  agents.py, __main__.py, runner.py role dispatch + checkpoint/resume budget logic, the static
  Refused census test, existing test_kong_roles.py / test_agents.py, prior probe RESULT.json.
- Code implemented and committed: f16e564 (declared files), 310ab9b (follow-up edits to
  test_agents.py / test_kong_roles.py, outside declared set, flagged).
- Live smoke script written: completion-evidence/kong-live-probe/wiring_smoke.py (3 calls).

## Findings driving the design
- Prior live probe: sonnet-4-6 returned text that is not a bare JSON object (45 tokens; almost
  certainly a ```json fence) -> REFUSED; and unpriced reservation set spent=budget (one call/run).
- Native route models: only `claude-sonnet-4-6` is PROBED on /v2/anthropic. `claude-haiku-4-5`
  is CODE (ET native fallback roster) and PROBED routable only on the Unified route.
- Every role is judgment-measured in at least one live frozen case (E02 compiler injection,
  E08 verifier, E09 router/shadow policy, reviewer/evidence_checker/planner/repairer broadly).
- Kit SDK anthropic-python 1.13 rejects `temperature` pre-send (existing mock test).

## Decisions (planned)
1. Output normalization: ONE shared strict parser in agents.py (`parse_role_object`) used by both
   transports: exactly one JSON object, optionally wrapped in exactly one ```json fence, nothing
   else (no prose, no duplicate keys, no NaN/Infinity). Receipt field `output_normalization`.
   Prefill '{' rejected as option: unprobed and likely refused by 4.6-generation models.
   CONSEQUENCE: tests/test_agents.py has a parametrize case asserting a fenced CLI result is
   refused; it must change (outside declared set) -> separate labelled commit, flagged.
2. Per-role model map (`models=` / `--role-model role=id`), ids validated against the kit's
   native roster; default all roles sonnet-4-6 (only PROBED native model) with rationale table.
3. Receipt: model_observed from response; worker echoes the exact settings it sent.
4. budget_guard "on"|"off": off => no reservation, cost_usd null, cost_basis "untracked";
   off requires no price table. __main__ `--roles kong` sets off and records role-transport.json
   provenance (atomic) + a dispatch_event when dispatch evidence is on; resume inherits it.
5. Review-evidence prompt suffixes moved to agents.py constants + `role_prompt()`; KongRoles reuses
   them and mirrors the reviewer `dispatch` receipt block.

## Next steps
- Implement agents.py hunk, kong_roles.py, __main__.py flags; tests/test_kong_wiring.py;
  update test_kong_roles.py cases that follow the intentional change.
- Run pytest (tools/workflow/tests + tests/test_weather_adapter*.py) and ruff.
- Live smoke (one call per changed role shape, synthetic) -> completion-evidence/kong-live-probe/WIRING-RESULT.json.

## Test status (final)
- Full suite stopped on coordinator instruction (coordinator runs it at merge).
- Touched files (test_kong_roles, test_kong_wiring, test_agents, test_runner_lifecycle_v27):
  124 passed, 4 failed in 1222s. The 4 are the kit-subprocess real_worker tests; no traceback
  was captured (output was tailed). Rerun: 1 alone passed (25.6s), then all 4 together passed
  (4 passed in 100s). Reading: load or timing sensitive (each test starts the kit Python,
  18-33s), not a code regression. Unconfirmed.
- ruff check + format --check on the 7 changed .py files: clean.

## Status: COMPLETE (pending coordinator merge)

## Live Kong smoke (DONE 2026-10-13T14:41Z; do NOT rerun)
- 3 calls, one each, no retry -> completion-evidence/kong-live-probe/WIRING-RESULT.json.
- A verifier/plain (sonnet-4-6): SUCCESS, fenced output normalized (output_form json_fence).
- B reviewer/review_evidence (sonnet-4-6): SUCCESS, dispatch receipt block present.
- C planner/review_evidence_request (haiku-4-5): UNKNOWN, HTTP 400 at gateway model validator
  (request f2c338128953ca05ed84677558b20f2f). Diagnosed once: native route rejects haiku-4-5.
  Follow-up: haiku moved to NATIVE_REJECTED; roster = sonnet-4-6 only; tests use a fixture id.
  Shape C prompt suffix NOT proven live (would need a new call; not retried by rule).
- 310ab9b kept: it only follows intentional changes (shared fence rule, shared prompt variants,
  construction-time roster check).

## Open questions
- 310ab9b (test_agents.py / test_kong_roles.py) is outside the declared set; kept because it
  only follows the intentional changes. Coordinator to accept or drop.
- Shape C (planner/repairer review_evidence_request suffix) not proven live.
