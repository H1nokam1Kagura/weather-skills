# Decision reviewer (shadow only, throwaway)

> **We run the fast decision models in shadow and measure them before we'd trust them.**

This folder is a removable demo component. A small, fast decision model gives its own answer at
three points where the forecasting agent decides something. The answer is written to a log and
compared with what the real gate decided. **It never blocks, changes or delays a verdict.** It is
**off by default**, and the demo runs the same with it on or off.

## Why it is shadow-only

Neil's clm research tested whether a small model's *confidence* can tell a pipeline when to hand
off to a larger model or a human. It cannot, yet
(`C:\Users\neilha\wt\clm-weather-skills\REPORT_2026-10-02.md`, section E2):

- lev's own confidence: deferring the least-confident 20% of decisions caught **31.6%** of its
  errors (bar: 70%), with **24.5%** error on the 80% it kept (bar: 5%).
- Released CLM's confidence was no better: **77%** error at 80% coverage.
- Disagreement between two cheap scorers caught 90% of errors, but the scorers disagreed on
  **72%** of decisions, more than three times the 20% escalation cap.

Fast "System-1" decision models (Laya, Kev, CLM-8B) are being promoted for exactly this job. This
component lets us measure them on our own decisions without letting them touch a result.

**On CLM:** Neil's clm research evaluated a 0.6B Qwen3-Embedding stand-in, NOT CLM-8B; no result
about the real model exists yet. This backend is the harness for that test.

## The three decision points

| Point | Question type | Shadow answers | Real outcome to record |
|---|---|---|---|
| `escalate` | yes/no (`noul`) | ask the human before continuing? `escalate` / `proceed`, with p | did the agent (or human-boundary gate) actually ask? |
| `review_verdict` | `choice` | `approve` / `reject` a proposed skill pipeline, given the skill rules text in the state | the reviewer's verdict |
| `next_skill` | `choice` | one of the rule-valid candidate skills the caller passes (the shadow never widens the set) | the skill that actually ran next |

The question wording is fixed in `decision_shadow/templates.py`, so every backend is asked the same
thing. All four backends speak the same typed-question shape (TypeSafe `/v1/systemone`:
`noul` / `choice` / `score`).

## Backends

One interface: `score(question_type, state_text, options) -> {choice, probs, latency_ms, backend, model_sha}`.

| `WS_DECISION_SHADOW=` | What | Status |
|---|---|---|
| `off` (default) | nothing runs, nothing is logged | |
| `stub` | deterministic heuristic, standard library only, offline. `next_skill` = alphabetically first valid skill (the clm finding: 0.669 top-1 on held-out task types, above every small generative model tested). The baseline a model must beat. | tested |
| `laya` | `convaiinnovations/laya` @ `7b928d828b7b0e022f929d9bd2e44165aa270148` (Apache-2.0; 421M, ModernBERT-large + decision head), local CPU, via the `laya` package (PyPI 0.3.26, Convai Innovations) `laya.load(<verified local dir>).predict(state, questions)` | tested; real CPU smoke below |
| `kev` | client to a `python -m kev.serve` endpoint serving `jaredpalmer/kev-0.8b` @ `bf75a6a8848ea6960ff2ed108d9ed44c2941174f` (Apache-2.0; LoRA + pointer head on Qwen3.5-0.8B-Base). Set `KEV_URL` (+ optional `KEV_API_KEY`). No weights are downloaded here. | **untested against a real server**; fail-closed path and wire mapping tested |
| `clm` | client to a `clm-serve` endpoint (CLM-v0.1-8B head on a vLLM-served Qwen3-8B encoder; needs a GPU). Set `CLM_URL` (+ optional `CLM_API_KEY`). | **untested against a real server**; fail-closed path and wire mapping tested against a mocked endpoint |

Any failure (no endpoint configured, endpoint down, HTTP 503/504, package not installed, hash
mismatch) is logged as `status: unavailable` with the error. It is never raised into the run.

**CLM deployment note.** If CLM-8B is tested, the target is a governed Databricks GPU serving
endpoint. Expect the issues the clm research hit serving its D4 compiler (clm spec D53):
the GPU serving build force-reinstalls the platform torch over the pinned one (CUDA/torch
conflict: `No CUDA GPUs are available`); scale-to-zero returns 503/504 on cold start (logged here
as `unavailable`, which is why the client timeout is short); and it needs GPU_LARGE (1x A100),
because Azure offers no mid-size GPU and the T4 tier has no native bf16.

## Enable, use, report

```bash
# off by default; turn on for one shell
export WS_DECISION_SHADOW=stub          # or laya | kev | clm

# score (prints only the decision_id, never the shadow's choice, and always exits 0)
uv run --no-project python shadow/decision-reviewer/ds.py score --point escalate --state-file - < state.txt
uv run --no-project python shadow/decision-reviewer/ds.py score --point next_skill \
    --state-file - --option convert-to-totals --option plot --background < state.txt

# record what the real gate decided
uv run --no-project python shadow/decision-reviewer/ds.py record-outcome --id <decision_id> --actual escalate --point escalate

# measure
uv run --no-project python shadow/decision-reviewer/ds.py report
```

The laya backend needs the package and the pinned weights:

```bash
uv run --no-project python shadow/decision-reviewer/ds.py fetch-laya      # ~843 MB into cache/, verified
uv run --no-project --with laya==0.3.26 python shadow/decision-reviewer/ds.py score --point escalate --state "..." --backend laya --show
```

`--background` returns at once and scores in a detached child, so even a slow backend adds no wait
to the run. `--show` prints the logged record; it is for demos, not for agents.

**`report`** prints, per decision point and backend: decisions logged, unavailable, n with a known
outcome, agreement with the actual outcome, and median latency. For `escalate` it also applies the
clm E2 bar: flag the top 20% of decisions by shadow p(escalate); PASS only if they contain >= 70%
of the actual escalations AND the actual escalation rate among the other 80% is <= 5%. Below n=30
(and, for `escalate`, 10 actual escalations) it says so plainly: "advisory only; insufficient n".
One adaptation from E2: E2 measured recall over a scorer's *errors*; here recall is over the
decisions the real gate *escalated*, which is the label this demo can actually observe.

## Integration (not wired; Neil wires it later)

No existing file is edited. To wire it, add one line where a decision is made, for example in the
forecaster's human-boundary step:

```bash
uv run --no-project python shadow/decision-reviewer/ds.py score --point escalate --state-file - --background < "$STATE" || true
```

and one `record-outcome` call where the real outcome is known. The agent must not read the
shadow's answer; the CLI does not print it. There is deliberately no agent prompt file, so
removal stays one folder.

## Remove it

```bash
rm -r shadow/decision-reviewer
```

plus deleting the one integration line above if it was added (and `unset WS_DECISION_SHADOW`).

## Supply-chain rules

- Weights come **only** from the official repos: Laya from Hugging Face `convaiinnovations/laya`
  (code `github.com/NandhaKishorM/laya`); Kev from `jaredpalmer/kev-0.8b` / `github.com/jaredpalmer/kev`;
  CLM from `Contrastive-LM/CLM`. No third-party mirrors (`Mattepiu/laya-onnx`, `mys/laya-GGUF`),
  no ONNX/GGUF re-packs, no `ollaya`.
- Pinned by **full revision sha**, never `main` (Laya changed on 2026-10-03). `laya.lock.json`
  records the size and sha256 of every file. `fetch-laya` checks each download against the Hub's
  own record for that revision (LFS sha256 for the weights, git blob id for the small files).
  Every load re-hashes the cache and **refuses to load** on any mismatch or missing file. The lock
  rejects any other repo id and any non-40-hex revision.
- The model loads from the verified local directory with `HF_HUB_OFFLINE=1`, so loading never
  reaches the Hub.
- Model code comes from the `laya` PyPI package pinned to `0.3.26`, not from the `.py` files in the
  model repo (`rl_agent_api.py` etc. are not downloaded or executed).
- Endpoint keys (`KEV_API_KEY`, `CLM_API_KEY`) are read from the environment, sent only as a
  bearer header, and never logged.
- `cache/` and `logs/` are gitignored.

## Data governance

By default the log stores a **sha256 of the input**, not the text. Raw state text is stored only if
`WS_DECISION_SHADOW_LOG_TEXT=1`. The Kenya use is a **beta-forecaster shadow** (professional
users on approved beta accounts), not a public go-live (see
`C:\Users\neilha\wt\clm-weather-skills\KENYA_SHADOW_DESIGN.md`). Even so, beta users' requests
can name farms, places and people. Keep text logging off unless the beta's consent covers it,
keep logs on the local machine, and do not send state text to a remote backend (`kev`, `clm`)
outside a governed endpoint.

## Validation (2026-10-04)

- `uv run python -m pytest shadow/decision-reviewer/tests -q`: stub, gating, logging,
  record-outcome, report math on synthetic logs with known answers, hash-mismatch refusal,
  mirror/floating-revision refusal, kev/clm fail-closed (no endpoint, refused connection, 503),
  CLM request/response mapping against a mocked endpoint, key-not-logged.
- Real Laya CPU smoke (`eval/smoke_laya.py`, results in `eval/smoke_laya_2026-10-04.json`).
  n=3 synthetic decisions, no outcomes: this shows the backend runs, it is **not** an evaluation.

Smoke results (Windows 11, 12 logical CPUs, torch 2.14.1+cpu, 10 threads; **host at 100% CPU from
other concurrent jobs**, so the latencies are an upper bound, not Laya's speed):

| Point | Laya answer | Right? (by the skill rules) | First call | Warm median (5) |
|---|---|---|---|---|
| `escalate` (no place, dates or product given) | proceed, p(escalate)=0.11 | no | 5.8 s | 3.4 s |
| `review_verdict` (pipeline plots rates without convert-to-totals) | reject, 0.82 | yes | 4.4 s | 6.7 s |
| `next_skill` (after aggregate-temporal) | deaccumulate 0.404 vs convert-to-totals 0.398 | no (the rules say skip deaccumulate after fetch) | 2.9 s | 3.9 s |

One-time load: 68 s (includes re-hashing the 843 MB weights). The advertised 0.2-0.5 s per
decision was not reproduced under this load; re-measure on an idle host before quoting either.

## Upstream notes (for the issues log)

- **Laya, temperatures:** loading the pinned checkpoint warns `this checkpoint ships invalid
  temperatures or values outside [0.5, 5]; using choice:11+=0.1006 -> 0.5. Treat confidence from
  the affected entries as uncalibrated.` Choice questions with 11+ options are uncalibrated as shipped.
- **Laya, pins disagree:** the `laya` 0.3.26 package's own "reviewed" pin for
  `convaiinnovations/laya` is `55cf4c4e...`, not the current Hub head `7b928d82...` (pinned here per
  clm D56). Its loader also floats on the Hub default revision unless a revision is passed.
- **Laya, docs:** the Hugging Face card names separate repos `convaiinnovations/laya-multilingual` and
  `laya-typed-decisions`; the GitHub README and the code load them as subfolders of
  `convaiinnovations/laya` (both exist).
- **Kev, no separate escalate head:** the docs and code describe one pointer head answering
  `noul` / `choice` / `score`; "escalate" is just a `noul` question in the README example. clm D56's
  "Kev uses its own Act/Escalate decision" has no matching component; the Kev arm should be defined
  as a `noul` question.
- **Kev, revision:** the GitHub model card for kev-0.8b says the released weights are Hub revision
  `9a45d25e`; the Hub head (and the D56 pin) is `bf75a6a8` (2026-10-01). `head.pt` is a pickle, so
  loading it executes code: hash it before loading.
- **"confidence" differs by server for the same API:** Kev uses `(p_max - 1/K)/(1 - 1/K)`; CLM uses
  `p_max - mean(rest)`; Laya reports `max(p, 1-p)` for yes/no. Do not compare `confidence` across
  backends. This harness logs the probabilities only.
- **CLM:** the README says the head is 20M parameters (not the "75 MB head" description; ~80 MB at
  fp32 is consistent).

## What is not tested

- `kev` and `clm` against real servers (no weights downloaded; CLM needs a GPU).
- The `--background` CLI path under a real agent run. It was exercised once by hand (detached
  child scored and logged); the in-process background helper has a unit test.
- Any accuracy claim. No backend has n near the report's threshold.
