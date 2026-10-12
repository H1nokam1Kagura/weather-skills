---
name: downscale
description: Downscale a weather-skills standard dataset Zarr onto a finer-or-equal grid, adding information via a chosen --algorithm (linear-interpolation or q-q empirical quantile mapping). The target is given by an integer factor, a target resolution, or a reference dataset's grid. Equal or near-equal resolution is a lateral realign (same spacing, different offset — including a half-cell shift). Use when a task needs higher spatial resolution; to make a grid coarser, use the coarsen skill.
license: MIT
compatibility: Requires Python 3.12 and uv.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/downscale.py *)
metadata:
  version: "0.0.2"
  catalog-group: transforms
---

# downscale

Source-agnostic spatial downscaling: produces a grid finer than or equal to
the input's and adds information through a pluggable `--algorithm`. The target grid is
specified one of three ways — an integer `--factor` (new spacing = input
spacing / factor), a `--target-resolution` in degrees, or a `--reference-grid`
dataset whose lat/lon grid becomes the target. The requested target must be
finer-or-equal to the input on each axis; equal (or near-equal) resolution
is accepted as a lateral realign — same spacing, different cell-center
offset, including a half-cell shift. Geometry is a no-op when the grids
already match — `--factor 1`, or a `--reference-grid` with the input's exact
coordinates, passes the input grid and values through unchanged (for
`--algorithm q-q` the value mapping still applies). A
meaningfully-coarser target is rejected with a pointer to the `coarsen`
skill. Near-equal spacings (within 1%, including float rounding or a
slightly irregular CHIRPS axis vs a regular 0.05° forecast) are not treated
as coarser, so this skill and `coarsen` do not ping-pong.

Algorithms:

- `linear-interpolation` — linearly interpolate the input onto the finer target
  grid (via `xarray-regrid`'s `.regrid.linear()` accessor). No distribution
  change.
- `q-q` — linearly interpolate onto the finer target grid, then apply per-grid-cell
  empirical quantile-quantile mapping along `--time-dim`, mapping the
  interpolated values onto the distribution of a `--qq-reference` dataset that
  must already be on the output (finer) grid.

## When to use

- A task needs a finer grid, or a lateral realign onto another dataset's
  coordinates before `difference` or `verify`. Plotting does not: `plot`
  with two heatmap traces keeps each input's own lat/lon.
- Matching the (finer) resolution of another dataset via its grid
  (`--reference-grid`).
- Bias-correcting interpolated output against an observational reference on the
  output grid (`--algorithm q-q` with `--qq-reference`).

Not for: coarsening a grid onto a strictly-coarser resolution — that is the
`coarsen` skill.

## Usage

```
uv run ${CLAUDE_SKILL_DIR}/scripts/downscale.py --input <in.zarr> --output <out.zarr> \
    --algorithm {linear-interpolation,q-q} \
    (--factor N | --target-resolution DEG | --reference-grid REF.zarr) \
    [--variable NAME] \
    [--qq-reference REF.zarr] [--time-dim DIM]
```

### Arguments
- `--input`, `-i` — input Zarr (any gridded dataset).
- `--output`, `-o` — output Zarr.
- `--algorithm` — `linear-interpolation` or `q-q`. Required.
- `--factor`, `-f` — integer refinement factor (>= 1). New spacing = input spacing / factor (factor 1 = identity). Mutually exclusive with `--target-resolution` and `--reference-grid`.
- `--target-resolution` — target spacing in degrees; must be finer-or-equal (<=) to the input on each axis (near-equal counts as equal). Mutually exclusive with `--factor` and `--reference-grid`.
- `--reference-grid` — path to a reference Zarr whose lat/lon grid defines the target. The reference grid must be finer-or-equal to the input (near-equal counts as equal, so a half-cell offset at the same nominal resolution is allowed). Mutually exclusive with `--factor` and `--target-resolution`.
- `--variable`, `-v` — restrict to a single data variable. Default: process all.
- `--qq-reference` — reference Zarr whose distribution the `q-q` method maps the output onto. Per-grid-cell empirical quantile mapping along `--time-dim`. The reference must already be on the post-downscale lat/lon grid; mismatches are an error. Every variable mapped (present in both) must carry the same `units` as the input (spelling-independent, e.g. `mm/day` = `mm day-1`); differing units, or units on only one side, exit 2 — convert first with `unit-convert`. Required for `--algorithm q-q`.
- `--time-dim` — time dimension used as the sample axis for q-q mapping. Default: `time`. Both the output and the reference must have a dimension by this name; a reference without it exits 2, even when it shares no variable with the input.

### Output

Same shape as input except the lat/lon dims are finer. Non-spatial dims
(`number`, `step`, `time`) are preserved. With `--algorithm q-q`, only data
variables present in both the interpolated output and the `--qq-reference` are
mapped; others pass through unchanged. Mapped variables keep the input's
`units`, which the units check above guarantees equal the reference's.

### Provenance

The output stamps a JSON-encoded `weather_skills_history` attr: an append-only array of
per-step entries `{skill, version, args, input}`. This skill reads the upstream
input's `weather_skills_history` (default `[]` with a stderr warning if absent) and
appends its own entry. `args` is the argparse namespace minus the
`--input`/`--output` path strings — so `algorithm`, `factor`, `target_resolution`,
`reference_grid`, `variable`, `qq_reference`, and `time_dim` are
recorded under their argparse dest names (underscored). `input` is a
`{basename, hash}` dict for `--input`. `--reference-grid` and `--qq-reference`
are recorded only as the path strings given in `args`: their contents are not
hashed, so editing a reference in place (same path, changed content) is not
visible in the provenance entry. `version` is the `_SKILL_VERSION` constant in
`scripts/downscale.py`. The skill performs no cache-hit check: every run
recomputes and rewrites `--output`.

## Examples

```bash
# Factor-4 finer, linear interpolation.
uv run ${CLAUDE_SKILL_DIR}/scripts/downscale.py -i /tmp/ecmwf.zarr -o /tmp/ecmwf_4x.zarr \
    --algorithm linear-interpolation --factor 4
```

```bash
# Onto a finer 0.05° grid, linear interpolation.
uv run ${CLAUDE_SKILL_DIR}/scripts/downscale.py -i /tmp/imerg.zarr -o /tmp/imerg_p05.zarr \
    --algorithm linear-interpolation --target-resolution 0.05
```

```bash
# Match the (finer) grid of another dataset.
uv run ${CLAUDE_SKILL_DIR}/scripts/downscale.py -i /tmp/ecmwf.zarr -o /tmp/ecmwf_on_imerg.zarr \
    --algorithm linear-interpolation --reference-grid /tmp/imerg.zarr
```

```bash
# Downscale ECMWF onto a finer 0.05° grid and q-q map onto ERA5 observations
# that already sit on that grid.
uv run ${CLAUDE_SKILL_DIR}/scripts/downscale.py -i /tmp/ecmwf.zarr -o /tmp/ecmwf_p05_qq.zarr \
    --algorithm q-q --target-resolution 0.05 --qq-reference /tmp/era5_p05.zarr
```
