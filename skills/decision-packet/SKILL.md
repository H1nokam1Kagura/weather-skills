---
name: decision-packet
description: Gate every hand-off to or from the human. Before a question, approval or plan read-back is shown, check the decision packet is complete (one question; options each with a definition, evidence with sources and downstream effect; the default and what happens if nobody answers; the cheap checks already run; nothing pre-selected) and BLOCK it if anything is missing or if a rule already settles it. Render ready packets as a self-contained review page whose Download writes a DECISION CSV, and parse the human's reply (chat text or that CSV) back into typed state with an exact echo of what changed. Exit 0 ready/answered, 1 incomplete or unusable, 3 needs follow-up, 4 rule-decidable (do not ask). No model call inside.
license: MIT
compatibility: Requires Python 3.12 and uv. Reads packet JSON and decision CSV files; html mode writes one HTML page and its spec JSON per packet; no network.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/decision_packet.py *)
metadata:
  version: "0.0.1"
  catalog-group: agent-tooling
---

# decision-packet

A person can only adjudicate what is in front of them. The failure this skill exists for is the
**under-specified ask**: a question that names options without defining them, without the
evidence for each, without saying what changes downstream, or that asks something a rule or a
cheap query would have settled. It refuses to let such a packet reach the human.

## Modes

```
uv run ${CLAUDE_SKILL_DIR}/scripts/decision_packet.py --mode check  --packet P.json [--packet Q.json] [--channel chat|page]
uv run ${CLAUDE_SKILL_DIR}/scripts/decision_packet.py --mode html   --packet P.json [...] --html-dir DIR
uv run ${CLAUDE_SKILL_DIR}/scripts/decision_packet.py --mode import --packet P.json --reply "<the human's reply>"
uv run ${CLAUDE_SKILL_DIR}/scripts/decision_packet.py --mode import --packet P.json [...] --decisions exported.csv
```

| mode | exit 0 | exit 1 | exit 3 | exit 4 |
|---|---|---|---|---|
| `check` | every packet ready | BLOCKED: incomplete; each missing element is named | — | BLOCKED: a rule / check already settles it, or only one option |
| `html` | pages written (only if every packet is ready) | as `check` | — | as `check` |
| `import` | answered; `echo` says exactly what changed | reply is not an option, or contradicts the goal | ambiguous (one clarifying question supplied), unanswered, or **insufficient information** | — |

Exit 2 is a usage error. `--channel chat` (default) allows at most **2 questions per round**;
`--channel page` allows a batch.

## The packet (`rhiza-decision-packet/1`)

```jsonc
{
  "schema": "rhiza-decision-packet/1",
  "id": "goal-period",
  "kind": "goal_slot | approval | plan_readback | other",
  "slot": "period",                       // goal_slot only
  "request": "<the human's own words>",   // goal_slot only, required
  "goal": { "...": "current typed goal" },// goal_slot: lets import validate and echo
  "context": "optional plain-language lead-in",
  "question": "Should the values be grouped week by week or month by month?",
  "options": [
    {"code": "weekly", "label": "Week by week",
     "definition": "what choosing it means, in plain words",
     "evidence": [{"text": "...", "source": "where this came from"}],
     "downstream": "which steps or outputs differ if chosen"}
  ],
  "default": {"option": null, "if_no_reply": "Nothing runs until you answer.", "source": "..."},
  "checks_run": [{"check": "deterministic request rules", "settled": false, "result": "..."}],
  "clarifications_asked": 0
}
```

### What `check` blocks on (each is named in the report)

- `question` missing, or not exactly one question ending in `?`.
- fewer than 2 options (1 option = rule-decidable), more than 8, duplicate codes, or the reserved
  code `CANT_TELL`.
- any option without `code`, `label`, `definition` (and not just the label repeated),
  `evidence` (each item with `text` **and** `source`), or `downstream`.
- `default` missing, no `if_no_reply`, a default option that is not an option, or a default
  option without a `source`.
- `checks_run` empty, or an entry without `result` or a boolean `settled`.
- anything pre-selected (`preselected`, `current`, `selected`, `recommended` on the packet or an
  option): the human's own judgement is what is being recorded.
- **Premature ask (exit 4):** a `checks_run` entry with `settled: true`; or, for a `goal_slot`
  packet, the goal-check rules decide that slot for this request (for example a station-vs-grid
  question about a request that names no station or gauge).

## The review page

`html` maps each ready packet onto the stack's standard review tool, vendored byte-for-byte in
`vendor/build_review_ui.py` (source path and git sha in `vendor/SOURCE.json`). One question per
page; every answer carries its definition beside it; the request, each option's evidence and
downstream effect, the default and the checks already run are on screen; nothing is
pre-selected (`purpose: ground_truth`); an "I can't tell from this" answer is always offered;
progress survives closing the tab; **Download decisions** writes
`decision-<id>.csv` with columns `packet_id, slot, question, DECISION, correction_notes`. The
report prints the exact `--mode import` command for that CSV. The spec is written beside the
page as `review_spec_<id>.json`.

## Import (inbound)

- A CSV `DECISION` must be an option code or `CANT_TELL`; a chat reply may be the number, the
  letter, the code, the label, or contain exactly one label.
- `CANT_TELL`, or a reply such as "not sure" / "I can't tell" that does not name exactly one
  option, is `insufficient_information`: improve the packet with the missing evidence, then
  re-check; do not simply ask again.
- A reply naming none or several options returns **one** `clarifying_question`. If the packet
  already records `clarifications_asked >= 1`, it returns `unresolved_after_clarification`: hold,
  and offer the review page instead of a second clarifying question.
- For a `goal_slot` packet carrying `goal`, the answer is patched in and re-validated with the
  goal-check rules; a contradiction is exit 1. `echo` states the one change, e.g.
  `Changed period: (not set) -> Week by week ('weekly'). Nothing else changed.`
