# Is the human interaction working? Metrics for the human-boundary agent

The boundary agent sits on every hand-off between the assistant and the person. Two things can go
wrong, in opposite directions, and both must be measured:

- **It fails to ask** when it should: the pipeline runs a valid but wrong workflow, silently.
  Nothing downstream catches this, because the rules accept the goal (clm-weather-skills H24).
- **It asks badly**: too often, about things a rule could settle, or with too little in front of
  the person to decide. The person then rubber-stamps, guesses, or gives up.

A human in the loop is only effective if the person is asked rarely, about things only they know,
with enough information to decide, and their answers actually change outcomes.

## 1. Offline, before release: `run_intake_eval.py`

On the cleared 79-request sample (D4 training population: 48 clear + 31 audit-"unfaithful";
4 phrasing styles present), repeated `--reps 3`.

**Every unit-run gets exactly one outcome.** Two of them are never scored and never enter a rate:

- **error**: no usable card (timeout, non-zero exit, auth / rate limit, agent error envelope,
  empty or unparseable card, unknown status, a ready card with no goal, answer-key access).
- **degraded**: a card, but not from the measured design: the card says `sampling` is not
  `independent` or `samples < 3`; fewer than 3 nested compile calls succeeded (each nested
  `claude -p` is observed through a PATH wrapper that logs exit code, model, cost and the compile
  prompt's hash); a nested call used a different compile prompt; or the plugin was not loaded
  from the staged repo copy.

If error + degraded exceed 10% of a population, or any phrasing style is entirely error /
degraded, or identical cards come back for requests with different truths, or the run aborted,
the headline is **INCONCLUSIVE** and the exit code is non-zero. Before any full run two controls
must pass (`--preflight`): a fully specified request that must compile exact with no ask, and a
rainfall request with no period that must ask about `period`.

**What "underdetermined" means here.** It is the fidelity auditor's `faithful=false`, not "the
right behaviour is to ask". Read by hand on 2026-10-04: 21 of the 31 are accumulated-archive
requests that state every slot; 5 say "gauge" / "in-situ" while their truth says gridded
observations (the request contradicts the truth); the rest are complete. So the population has
no verdict and no ask-correctness rule: asks there are neither credited nor penalised, exact /
mismatch is reported against the truth, and where a station cue contradicts the truth the
scorer uses the request-implied value (the v3rr station rule's reading) and records it. The
verdict is read on CLEAR only.

| metric | definition | bar | source of the bar |
|---|---|---|---|
| **silent-wrong rate** | not asked, goal valid, goal ≠ truth; over all SCORED unit-runs | upper 90% bound ≤ **0.02** | clm H24 / H7; F1 rule (D47) |
| ask rate | unit-runs with a goal question | ≤ **0.15** | clm F1 rule (D47) |
| exact among non-asked | goal = truth, among unit-runs not asked | lower 90% bound ≥ **0.90** | clm H7 bar at 90% |
| unnecessary-ask rate | asked on a CLEAR request (both fidelity auditors agree it conveys its goal) | report; it equals the ask rate on the clear population | — |
| rule-decidable asks | asked about a slot a deterministic rule settles for that request | **0** | premature-ask guard |
| loud failures | invalid goal or unparseable card | report | — |
| repeat stability | units whose outcome changes across reps | report | — |

Verdicts are read on the CLEAR population; UNDERDETERMINED is reported beside it as a diagnostic.
An ask is never scored as exact or as silent-wrong. Below 30 scored goals the runner prints
*not decidable* whatever the numbers: a smoke is not a verdict. A rule-decidable ask also fails
the bar (it must be 0). For reference, clm's confirmed design (Qwen3-8B, v3rr rules, 3
samples, ask on disagreement) measured 0.5% silent-wrong at 4.4% asks on a fresh held-out set;
Sonnet 4.6 alone, 0.8% at no asks (D47). **This plugin's compile prompt adds extension slots to
the validated v3 prompt, so those figures are a target, not a property of this build.**

### Finite-sample uncertainty and verdicts

Run-weighted rates describe the observed scored unit-runs. Their goal-cluster bootstrap
intervals are descriptive only: with all zeros or all ones they collapse, so they cannot
certify rare-failure bars. Repeating a goal does not create another independent goal.

The runner also averages each metric within a distinct goal, then gives each goal equal
weight. For n independent, representative goals whose averages lie in [0,1], its two-sided
90% Hoeffding interval is mean +/- sqrt(log(20)/(2n)), clipped to [0,1]. Dependence within a
goal is unrestricted. This conservative bound is valid at zero/one boundaries and for mixed
within-goal outcomes; it is deliberately wider than a model-dependent binomial interval.
See [Hoeffding (1963), Theorem 2](https://www.cs.rpi.edu/academics/courses/spring06/random/hoefding.pdf).
These assumptions do not establish representativeness of a curated request set or future users.

The equal-goal estimand differs from a run-weighted one when cluster sizes differ. The
runner therefore only certifies the existing run-weighted bars where scored cluster sizes
are equal for silent-wrong and for exact among non-asked, and both bounds clear their bars.
Otherwise the primary verdict is INCONCLUSIVE (exit 2), apart from the existing small-smoke
rule or direct ask/rule-ask failures. Diagnostic populations still carry no verdict.
Confidence bounds are per metric, not a simultaneous guarantee for all bars. Missing or
excluded records can bias population inference even when they pass the error-share guard.

For example, 44 independent goals with zero observed silent errors have a Hoeffding upper
bound about 0.185, not zero. This supports an observed zero count, not a below-2% population
assurance. More repetitions of those same goals do not narrow the bound. No additional
model calls are needed to correct the report's interpretation.

## 2. Every outbound packet: completeness, measured at the gate

Logged per packet from `decision-packet --mode check`:

| metric | definition | target |
|---|---|---|
| **packet completeness rate** | packets `ready` on the FIRST check / all packets checked | rising; every block is a near-miss the gate caught |
| block reasons | count of each named missing element (`evidence[*].source`, `downstream`, `checks_run`, …) | tells you which element authors skip |
| rule-decidable blocks | packets blocked with exit 4 | each one is a question the person was spared; if frequent, push the rule upstream |
| questions per round | packets sent together in chat | ≤ 2 |
| sent-incomplete rate | packets the person saw that did not pass the gate | **0** (any non-zero is a process breach) |

## 3. Every inbound reply: is the person able to decide?

| metric | definition | what a bad value means |
|---|---|---|
| **"insufficient information" rate** | replies parsed as `insufficient_information` (the "I can't tell from this" answer on the page, or "not sure / I can't tell / need more" in chat) / replies | the packet, not the person, failed: evidence or definitions were missing |
| clarification rate | replies needing the one clarifying question | options overlap or labels are unclear |
| unresolved-after-clarification | replies still ambiguous after one clarifying question | switch that decision to the review page |
| **change rate on read-back** | approvals where the person changed at least one slot / all read-backs | **0% over many approvals = rubber stamp.** If people never change anything, either the compile is perfect (check with the audit below) or they are not reading. Expect a small, non-zero rate; a sudden drop to 0 is a warning, not a success |
| changes by slot | which slots people correct at read-back | each recurring correction is a missing rule, a bad default, or a compile error |
| **time-to-approve** | from card shown to the person's reply (median, p90) | very short (seconds) on long cards = not read; very long = card too dense or decision hard |
| answer-change on page | `changed` / `radio_changes` / `ms_on_item` from the page's telemetry CSV | radio never moved and seconds per item = rubber stamp |

## 4. After the fact: the silent-wrong audit

The one failure the person cannot report is the one they never saw. Each week (or each 50
approved cards), draw a random sample of approved, un-asked cards (at least 20), and have
someone who did not see them re-derive the goal from the original request alone, blind to the
card. Count:

- **post-hoc silent-wrong rate** = cards whose goal the auditor would have set differently on a
  consumed slot / cards audited. Same bar as offline: upper 90% bound ≤ 0.02. Grow the sample
  until the interval can decide.
- For each miss: was it a disagreement the three readings missed (all three wrong the same way),
  a rule that should exist, or a default the person did not notice in the read-back?

## 5. What "effective" means, in one line each

1. Silent-wrong ≤ 2% (offline and post-hoc), **and**
2. ask rate ≤ 15%, with zero rule-decidable asks and zero packets sent incomplete, **and**
3. insufficient-information replies rare and falling, **and**
4. read-back change rate small but non-zero, with time-to-approve consistent with reading.

If (1) fails, ask more (or improve the compile). If (2) or (3) fails, ask better, not more.
If (4) shows rubber-stamping, the read-back is too long or too confident: shorten it and put the
defaults the person is most likely to change first.
