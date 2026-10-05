# simkit for weather-skills: design and plan

**Status 2026-10-05.** Built and verified; nothing has been run against the weather-skills candidate.
Canonical copy: `C:\Users\neilha\agentic-sim\DESIGN-AND-PLAN.md` (git repo, local only).

## What this is

A reusable fault-injection evaluation platform for agentic pipelines, built first for the
cached-onset rainfall workflow described in the evaluation brief
(`C:\Users\neilha\Downloads\e2e-injection-generation.CLEAN-SESSION.json`). It turns that brief
into a frozen set of 48 development cases (24 fault/control pairs, 12 families) and gives an
implementation team the machinery to run and score them. It was built **without reading any
weather-skills code**, on purpose: the cases must test the specification, not the implementation.

## Design

| Layer | Module | Responsibility |
|---|---|---|
| Case shape | `simkit/core.py` | The case schema, injection primitives, strict never-overwriting JSON |
| System facts | `simkit/profile.py`, `profiles/cached-onset/profile.json` | States, roles, tools, fixture and definition, copied verbatim from the brief |
| Case set | `casesets/cached-onset-e2e/` | Case module, requirements, `frozen/` deliverables (SHA-pinned) |
| Validation | `simkit/validate.py` | Counts, pairing, references, payload hygiene, coverage |
| Adapter interface | `simkit/adapter.py` | One method per capability; a capability is implemented only if overridden |
| Runner | `simkit/runner.py` | Missing capability -> NOT_IMPLEMENTED; exception -> HARNESS_ERROR; never scored |
| Scoring | `simkit/score.py` | Four dimensions (judgment, containment, recovery, utility), terminal-outcome check, rates with denominators |
| Ratification | `simkit/ratify.py` | Append-only ledger; amendments apply only when a human ratifies; self-ratification refused |
| Evidence contract | `simkit/contract.py` -> `EVIDENCE_CONTRACT.json` | Every evidence path, symbol and event key the scorer reads, derived from the cases |
| Fixture | `simkit/fixtures/onset_synth.py` | Deterministic synthetic rainfall cache matching the brief's source metadata |

**Rules the code enforces**
- Missing evidence is never PASS: a missing stream or unresolved symbol is NOT_OBSERVED; inside a present stream an absent value is a real absence.
- PROPOSED oracles are UNRATIFIED (not scored) until the ledger ratifies them.
- Rates use only decided cases as denominators; NOT_IMPLEMENTED / HARNESS_ERROR / NOT_OBSERVED / UNRESOLVED / UNRATIFIED are reported, never counted.
- Synthetic data is labelled synthetic; it never claims CHIRPS lineage.

**Independence boundary**
1. Case writer: brief + development input only. Done; dataset frozen.
2. Adapter builder: reads the implementation, implements the adapter, answers the open questions. Never edits cases, oracles, profile or ledger.
3. Specification reviewer (human): ratifies or rejects oracles and amendments.
4. Scored run: separately authorised, after ratification.

## Artifacts

| Artifact | Path (under `C:\Users\neilha\agentic-sim\`) | SHA256 |
|---|---|---|
| Frozen dataset (48 cases) | `casesets/cached-onset-e2e/frozen/agentic-e2e-injections.DEV-ONLY.json` | `9198f606…3f054` |
| Frozen adapter requirements | `casesets/cached-onset-e2e/frozen/agentic-e2e-injections.ADAPTER-REQUIREMENTS.json` | `a2f49b84…28054118` |
| Evidence contract | `casesets/cached-onset-e2e/EVIDENCE_CONTRACT.{json,md}` | generated |
| Open questions, routed | `casesets/cached-onset-e2e/OPEN-QUESTIONS.json` | |
| Proposed amendments (rev 3) | `casesets/cached-onset-e2e/amendments.json` | |
| Reviewer packet | `casesets/cached-onset-e2e/REVIEW-PACKET.md` | |
| Adapter-build handoff | `handoffs/adapter-build.HANDOFF.json` | |
| Verification records | `casesets/cached-onset-e2e/verification/` | |

## Verified so far
- **Port:** the library regenerates the delivered dataset exactly (test-pinned); 51 tests pass.
- **Amendments:** four independent adversarial reads by a separate agent (refutations per round: 5, 3, 3, 2). Rounds 3-4 also found scorer defects (missing evidence passing conditions; `absent` not distinguishing an empty selection from a missing list) and frozen-case defects (checks that could never fail, one-based indexes, a receipt-shape conflict across 24 cases). All fixed in scorer rev 5 and amendments rev 5 (22 live, 4 withdrawn). Revision 5 has NOT yet had an independent read. Mechanical validation: 95 probes, including every objection from all four rounds and frozen-assertion probes, all pass; 60 tests.
- **Fixture:** expected onsets agree with two independent methods on all 22,000 cells; identical across processes; disk round trip exact; conforms to every source-metadata field. The brief permits synthetic data, so no CHIRPS cache is needed.

## Plan

| # | Step | Owner | Gate |
|---|---|---|---|
| 1 | **UC-12:** do the source check and the scientific tools accept the synthetic fixture (`python -m simkit.cli fixture --out <new dir>`)? If the source check demands CHIRPS lineage, STOP: every case would block at the first gate, and relabelling is a human decision | adapter session | first |
| 2 | **UC-09:** do compiler invocations receive source metadata? (If not, E02-P2 is NOT_IMPLEMENTED and is redesigned in v2) | adapter session | first |
| 3 | Answer the remaining open questions (UC-01..04, 06..11) from the code, with file:line evidence | adapter session | |
| 4 | Implement the adapter (subclass `simkit.adapter.Adapter`) in the implementation repo, producing evidence to `EVIDENCE_CONTRACT.json`; dry-run without live models | adapter session | steps 1-2 not blocking |
| 5 | Decide: one more independent read of revision 5, or regenerate a v2 case set in a clean session folding in every lesson; then ratify or reject (`REVIEW-PACKET.md` -> `ratification-ledger.json`) | specification reviewer | before any scored run |
| 6 | Propose and ratify evaluators for the 3 rubric assertions (prose judgments) | reviewer + whoever builds the grader | before judgment is scored |
| 7 | Authorise budget and model for live trials (upper bound $140 per single pass: 28 live/scientific cases x $5) | Neil | |
| 8 | Scored run, then declare the build frozen and release the held-out set | Neil / custodian | after 5-7 |

## Rules for the receiving session
- Read `handoffs/adapter-build.HANDOFF.json` and follow it; set its `implementation_repo` to the repo that actually holds the controller/workflow code (confirm; do not assume this demo workspace is it).
- Never open `C:\Users\neilha\Downloads\seeded-agentic-eval.json` (the sealed held-out set) or any held-out oracle.
- Never edit the frozen dataset, profile, requirements, amendments or the ratification ledger. If a case cannot be implemented as written, record NOT_IMPLEMENTED with the reason.
- Evidence comes only from controller and harness observations, never model self-reports. Follow the receipt shapes and absence rules in EVIDENCE_CONTRACT.json exactly (lists, not joined strings; emit empty lists rather than omitting them; zero-based indexes).
- Report findings back as a separate document; do not push to any remote without Neil's say-so.

## Commands
```
cd C:\Users\neilha\agentic-sim
python -m pytest -q
python -m simkit.cli validate --caseset casesets/cached-onset-e2e --dataset casesets/cached-onset-e2e/frozen/agentic-e2e-injections.DEV-ONLY.json --manifest casesets/cached-onset-e2e/frozen/agentic-e2e-injections.ADAPTER-REQUIREMENTS.json
python -m simkit.cli fixture --out <new empty dir>
python -m simkit.cli dry-run --caseset casesets/cached-onset-e2e --dataset casesets/cached-onset-e2e/frozen/agentic-e2e-injections.DEV-ONLY.json --adapter <module>:<Class>
```
