---
name: verify-run
description: Deterministic evaluation gate for a weather-skills artifact (a standard dataset Zarr or a stamped plot PNG). Reads its weather_skills_history, re-hashes every recorded input against the sha256 the chain recorded, optionally replays the recorded transform steps into a temp dir and compares data fingerprints, and prints a gate card with VERDICT PASS / BLOCK / UNVERIFIABLE (exit 0 / 1 / 2). Use before presenting final numbers or a figure, to prove the result still follows from its inputs. Not forecast skill scoring — that is `verify`.
license: MIT
compatibility: Requires Python 3.12 and uv. Reads a zarr directory or a stamped figure; writes nothing outside a temp dir. --replay re-runs sibling skill scripts via uv run.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/verify_run.py *)
metadata:
  version: "0.0.1"
  catalog-group: agent-tooling
---

# verify-run

A pass/fail gate over the provenance an artifact already carries. The
verdict comes from fixed rules over hashes and array values — no model, no
network, no judgement. The same artifact and workspace always give the same
card.

## When to use

- Before reporting final numbers or showing a figure: hand the artifact to
  verify-run and show its verdict with the result.
- After a long session, to confirm that the inputs an artifact was built from
  have not been overwritten or edited since.
- With `--replay`, to confirm the recorded steps, re-run on the recorded
  inputs, regenerate the same data.

Use `provenance` to *read* the lineage; use verify-run to *gate* on it. Use
`verify` (a different skill) to score a forecast against observations.

## Usage

```
uv run ${CLAUDE_SKILL_DIR}/scripts/verify_run.py --input <artifact> [--require-replay | --replay] [--rtol R] [--search-dir DIR]
```

### Arguments
- `--input`, `-i` — the artifact to gate: a weather-skills Zarr (a directory) or a
  stamped figure (`.png`, `.jpg`, `.html`). Required.
- `--replay` — also re-run the recorded transform steps in a temp dir and
  compare the regenerated data with the artifact.
- `--require-replay` — use for final-result claims. Implies `--replay` and
  requires a successful final data comparison. Input hashes or intermediate
  comparisons alone cannot pass: missing final replay is UNVERIFIABLE (2).
  A detected mismatch remains BLOCK (1). A figure with only a remote fetch
  upstream has no replayable calculation and is UNVERIFIABLE in this mode.
- `--rtol` — relative tolerance for the replay value comparison. Default `0`:
  values must be bit-identical (NaNs in the same places).
- `--search-dir` — extra directory to look for recorded inputs, repeatable.
  The artifact's own directory is always searched first. History records
  input basenames, not full paths.
  Supply the parent of every reused input outside the artifact directory.
  Use one `--input` per invocation; repeat `--search-dir` for multiple locations.

## Checks

Every check prints its id, what produced it, the observed value and the
expected value.

- `provenance` — the artifact carries a non-empty, schema-valid
  `weather_skills_history`. A PNG with several stamped branches gets one check
  per branch.
- `input-hash` — for every recorded input (every step, including each parent
  of a join), the file with that basename is found and its sha256 (the same
  `hash_zarr` the decorator stamped) equals the recorded hash.
- `replay` / `replay-step` (with `--replay`) — starting at the earliest step
  whose inputs are hash-verified on disk, each recorded step is re-run with its
  recorded args by invoking the sibling skill's script with `uv run`, writing
  into a temp dir. Each regenerated intermediate is compared with the on-disk
  intermediate (`replay-step`) and the final result with the target
  (`replay`). The comparison is a per-variable fingerprint of dims, shape,
  dtype, units and values (coordinates included), not file bytes — the
  history attrs legitimately differ between runs. With `--rtol > 0`, numeric
  variables that differ in bits are accepted when `allclose` within `rtol`.
  - For a **Zarr**, the target is the artifact itself.
  - For a **figure**, pixels are never compared. The target is the data
    artifact the plot step consumed (its recorded input, which must be
    hash-verified on disk); the plot step itself is not re-run. If only remote
    fetch steps precede that data, there is nothing to replay and the check is
    `INFO`.
- `skill-version`, `dirty`, `scope` — `INFO` notes: a replayed skill's local
  version differs from the recorded one; a step ran from a dirty working tree;
  remote fetch steps are never re-executed (their outputs are checked by hash
  only).

## Verdict

| Verdict | Exit | Rule |
|---|---|---|
| `BLOCK` | 1 | any check failed: an input changed since the step ran, or the replay regenerated different data |
| `UNVERIFIABLE` | 2 | no BLOCK, but evidence is missing: no or invalid provenance, a recorded input is gone, a replay could not run, or nothing substantive could be checked |
| `PASS` | 0 | every input/replay check passed and at least one ran |

Absence is never success: an artifact with no history, or a fetch-only chain
with nothing on disk to re-hash, is `UNVERIFIABLE`, never `PASS`. A `PASS`
without `--replay` means the recorded inputs are intact; the card's `scope`
line says so.

For final numbers or a calculated figure, use `--require-replay`. A hashes-only
PASS must be described as input integrity only, never as verified output values.
Strict replay still does not compare figure pixels or validate source observations.

The card goes to stdout; a one-line `verify-run: VERDICT …` goes to stderr on
a non-zero exit.

## Replay limits

For a controller, use `--require-replay --format json`. The single stdout object
has schema `verify-run.gate/1`, resolved `artifact`, `verdict`, `exit_code`, `scope`
and structured `checks`. Cross-check its exit code against the actual process;
release requires a `kind: replay`, `status: PASS` check, not only input hashes.

- Args are replayed as `--<dest>=<value>` from the recorded argparse dests.
  A skill whose flag spelling differs from its dest, or whose extra Zarr
  inputs are not `--input`, will fail to replay; that is reported as
  `UNVERIFIABLE`, not `BLOCK`.
- Replay uses the skills installed next to verify-run, not the recorded
  commit. A version difference is noted (`skill-version`); if the data then
  differs, the verdict is still `BLOCK` — the result does not reproduce with
  the code in hand.

## Example

```bash
# Inputs intact?
uv run ${CLAUDE_SKILL_DIR}/scripts/verify_run.py -i kenya_weekly_totals.zarr

# Inputs intact AND the chain regenerates the same numbers?
uv run ${CLAUDE_SKILL_DIR}/scripts/verify_run.py -i kenya_weekly_totals.zarr --replay

# A figure: gates the data upstream of the plot, never the pixels.
uv run ${CLAUDE_SKILL_DIR}/scripts/verify_run.py -i kenya_totals.png --replay --rtol 1e-6
```
