---
name: goal-check
description: Validate a typed analysis goal (the JSON the human-boundary agent compiles from a plain-language request) against the goal schema and the deterministic request rules, fill every rule-decidable default with its source, write the plain-language read-back, and — given 2-3 independently compiled goals for the same request — report slot-level disagreement. Exit 0 resolved, 1 invalid, 3 needs a human answer. Use before planning any pipeline from a user request, and again after every human reply that changes the goal. No model call inside; it never asks the human anything itself.
license: MIT
compatibility: Requires Python 3.12 and uv. Reads goal JSON files; writes nothing and makes no network call. The optional sampling recipe below calls the `claude` CLI.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/goal_check.py *)
metadata:
  version: "0.0.1"
  catalog-group: agent-tooling
---

# goal-check

Deterministic gate between a person's request and a pipeline. It takes the typed goal a
model compiled from the request and answers three questions without a model:

1. **Is it a valid goal?** Vocabulary, types, and the hard contract rules. A violation is
   **exit 1**: the compile is wrong, so fix or redraw it. Never ask the human about a
   contradiction the compiler made.
2. **What do the rules and defaults settle?** The two v3rr request rules are applied, every
   unstated slot that has a default gets one, and every default is reported with the source it
   came from. Nothing here is ever a question.
3. **What is left that only the human can say?** A required slot the request does not fill, or
   (with 2-3 samples) a slot the independent readings disagree on, is **exit 3**, with at most two
   ready-made question packets for `decision-packet`.

## Usage

```
uv run ${CLAUDE_SKILL_DIR}/scripts/goal_check.py --goal G1.json [--goal G2.json --goal G3.json] \
    (--request "<the user's words>" | --request-file REQ.txt) [--format json|text]
```

- `--goal` — goal JSON; repeat with 2-3 **independently** compiled samples for the disagreement
  check. A sample that is not JSON counts as one invalid sample, not a usage error.
- `--request` / `--request-file` — the user's own words. Without them the v3rr rules cannot run
  and the report says so.
- `--format` — `json` (default; the agent reads this) or `text`.

### Exit codes

| code | status | meaning | what the caller does |
|---|---|---|---|
| 0 | `resolved` | valid, every required slot filled, samples agree | read back and plan |
| 1 | `invalid` | every sample is malformed or contradicts a hard rule | redraw the compile; do not ask |
| 2 | — | usage error (unreadable file, >3 samples) | fix the call |
| 3 | `needs_human` | a required slot is missing, samples disagree, or a sample failed (`resample: true`) | redraw failed samples once; then send `questions` through `decision-packet` |

## The goal schema (`rhiza-goal/1`)

Core slots are the clm-weather-skills compile schema v2/v3 verbatim (validated there; see
`references/SOURCE.json`). Extension slots are this plugin's and are **not** benchmark-validated.

| slot | values | required | default when unstated (source) |
|---|---|---|---|
| `task` | `map`, `spread_map`, `fcst_vs_obs`, `timeseries`, `bias_map`, `station_vs_sat`, `change_map` | yes | — |
| `variable` | `precip`, `t2m`, `sst`, `soil_moisture` | yes | — |
| `region` | a named place, or null | no | whole source domain (schema v2: null if no place named) |
| `time_window` | `relative`, `fixed`, null | no | latest available data (forecaster latest-data probe); change maps use the scenario period |
| `period` | `weekly`, `monthly`, null | **yes for rainfall** (except spread maps) | the data's own time steps (schema v2: never infer a resolution) |
| `legacy_cumulative` | bool | no | false |
| `obs_source` | `station`, `grid` | no | `grid` (rule v3rr.obs_station); only a forecast-vs-observation comparison chooses it |
| `window_phrase`, `window_start`, `window_end` | user's words; `YYYY-MM-DD` only if the user gave the date | no | relative windows are dated at run time against today (UTC), never by the compiler |
| `baseline` | `YYYY-YYYY` | change maps only | `1991-2020` (WMO standard normal, WMO-No. 1203) |
| `season`, `decision` | the user's words | no | none |
| `admin_level`, `output` | derived | — | country / place lookup; the task's standard figure (contract `FIGURE_FOR`) |

Hard rules (exit 1), from clm `clm_ws/tasks.py`: a spread map has no period; a change map is
never relative; an accumulated-since-start archive is rainfall only; `window_start` ≤
`window_end`. v3rr rules (rewrite, never ask): `obs_source="station"` needs a station / gauge /
in-situ cue in the request; `time_window="relative"` needs a relative cue ("last", "next",
"recent", …). The regexes are copied verbatim from clm `probes/o1_compile.py`.

## Getting 2-3 independent samples inside Claude Code

The clm result this rests on (D45, confirmed on a fresh held-out set in D47): ask when
**independent** samples disagree on a consumed slot; a model's stated confidence does not find
its own errors (E2/D39, H25b). Independence matters: three passes in one context see each
other and anchor, so they are not samples. The recipe uses three separate headless model calls
with no tools and this plugin's compile prompt:

```bash
P="$(cat ${CLAUDE_SKILL_DIR}/references/compile_prompt_rhiza.txt)"
for k in 1 2 3; do
  claude -p --model sonnet --tools "" --strict-mcp-config --setting-sources "" \
    --no-session-persistence --system-prompt "$P" "$REQUEST" > "goal_$k.json" &
done; wait
uv run ${CLAUDE_SKILL_DIR}/scripts/goal_check.py --goal goal_1.json --goal goal_2.json \
  --goal goal_3.json --request "$REQUEST"
```

About 9 s per call (measured once, 2026-10-04); the three run in parallel. If the `claude` CLI
is not callable from the session, run one compile, pass it alone, and say in the goal card that
the disagreement check did not run: a single sample can still fail on a missing required slot,
but nothing else triggers a question.

## Output (JSON)

`status`, `exit_code`, `goal` (filled), `rules_applied`, `defaults` (each with `source` for the
audit trail and `why` / `plain_value` in plain words), `notes`, `unresolved`, `disagreements`,
`invalid_samples`, `resample`, `questions` (≤ 2 decision packets, `rhiza-decision-packet/1`),
`queued_questions`, `readback` (plain language, no skill or tool names), and `plan_hints` (the
steps each slot implies, for the forecaster).

## Examples

```bash
# One goal, resolved: exit 0 and a read-back.
uv run ${CLAUDE_SKILL_DIR}/scripts/goal_check.py --goal goal.json \
  --request "Weekly rainfall map for Ethiopia for the next month." --format text

# Three samples that disagree on period: exit 3 with one question packet.
uv run ${CLAUDE_SKILL_DIR}/scripts/goal_check.py --goal g1.json --goal g2.json --goal g3.json \
  --request-file request.txt
```
