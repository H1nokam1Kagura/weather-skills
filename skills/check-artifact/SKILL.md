---
name: check-artifact
description: Deterministic sanity gate for one weather-skills Zarr artifact — checks units (present, and in the allow-list for precipitation or temperature), physical range (no negative rain, nothing above record rainfall, temperature within -90..60 degC), missing-value fraction, lat/lon inside an expected N/W/S/E box, time axis monotonic with no duplicates (gaps reported) and inside an expected window, and that provenance history is present. Prints a check card (PASS / FAIL / WARN per check, observed vs expected, and the threshold's basis) and exits 0 all pass / 1 any FAIL / 2 unreadable or nothing checkable. Use on any intermediate or final Zarr before building on it or reporting from it. Not lineage replay (that is provenance) and not forecast skill (that is verify).
license: MIT
compatibility: Requires Python 3.12 and uv. Reads a zarr directory; writes nothing; no network.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/check_artifact.py *)
metadata:
  version: "0.0.1"
  catalog-group: agent-tooling
---

# check-artifact

A fixed-rule check of one Zarr artifact. No model, no judgement, no network:
the same artifact and flags always give the same card and the same exit code.
It opens the store read-only and writes nothing.

It answers "is this artifact physically and structurally plausible, and does
it cover what the task asked for?" It does not answer "is this the right
analysis?" — a field that is plausible but computed the wrong way passes.
A PASS is a floor, not a proof of correctness.

## When to use

- After a fetch or transform, before the next step builds on the output.
- Before reporting a number or plotting: confirm units, range, coverage.
- With `--bbox` / `--start-time` / `--end-time` to confirm a clip or a
  window actually landed where the task said (a W/E-swapped `--bbox`
  silently selects the complement of the box; this catches it).

Use `inspect-zarr` to *look* at a store, `provenance` to read its lineage,
and this skill to *gate* on it.

## Usage

```
uv run ${CLAUDE_SKILL_DIR}/scripts/check_artifact.py --input <in.zarr> \
    [--variable NAME ...] [--bbox N/W/S/E] \
    [--start-time YYYY-MM-DD] [--end-time YYYY-MM-DD] \
    [--expect-units UNITS] [--max-nan-frac F]
```

### Arguments

- `--input`, `-i` — the Zarr to check. A missing path, a plain file, or a
  directory that is not a Zarr exits 2.
- `--variable`, `-v` — repeatable; data variable(s) to check. Default: every
  numeric data variable. Datetime/duration variables (e.g. `onset-date`
  output) are listed as not checked; if nothing numeric is left, the skill
  exits 2 — the absence of a check is never reported as a pass.
- `--bbox` — expected extent, `N/W/S/E` decimal degrees. Every lat/lon value
  must lie inside it, within one grid cell. North below south exits 2.
- `--start-time` — expected first valid time, `YYYY-MM-DD`, within one step.
- `--end-time` — expected last valid time, `YYYY-MM-DD`, within one step.
- `--expect-units` — units every checked variable must carry. Compared with
  pint, so `mm/day`, `mm d-1` and `mm day-1` are the same.
- `--max-nan-frac` — maximum fraction of missing cells per variable, 0–1.
  Default `0.5`.

## Checks and thresholds

Each line of the card is `[STATUS] check-id (subject)` followed by
`observed`, `expected` and `basis` (where the threshold comes from).

| Check | FAIL when | WARN when | Basis / rationale |
|---|---|---|---|
| `units-present` | a precipitation or temperature variable has no `units` | another variable has none (its range cannot be checked) | weather-skills-core treats precip and temperature as units-required kinds |
| `units-allowed` | the units are not in the family's allow-list (below) | — | a value in the wrong unit family cannot be compared with any threshold |
| `expect-units` | units differ from `--expect-units` | — | the caller's expectation |
| `nan-fraction` | missing fraction > `--max-nan-frac`, or every value missing (always) | — | see "NaN fraction" |
| `empty-slices` | — | whole time/step slices are all missing | unpublished forecast leads or a fetch gap |
| `range-min` (precip) | min < −0.001 mm/day equivalent | −0.001 ≤ min < 0 | rain cannot be negative; tiny negatives are float noise from deaccumulation |
| `range-max` (precip, ≥ 1-day sampling) | max > 1825 mm/day | max > 1000 mm/day | 1825 mm is the 24-hour world record (Foc-Foc, La Réunion, January 1966); above 1000 mm/day only a handful of station records exist |
| `range-max` (precip, sub-daily sampling) | max > 500 mm/h (12000 mm/day) | — | above every reported 1-hour rainfall record (about 300–400 mm) |
| `range` (temperature) | outside −90 … 60 °C after conversion | — | surface air temperature records: −89.2 °C (Vostok, 1983), 56.7 °C (Death Valley, 1913) |
| `aggregation-coverage` | — | any `aggregation_coverage` below 1.0 | `aggregate-temporal` keeps incomplete bins; only `convert-to-totals --min-coverage` drops them, and only while the time/step axis is still present |
| `time-monotonic` | duplicate or decreasing time/step values | — | time-ordered skills assume a sorted, unique axis |
| `time-gaps` | — | a step wider than 1.5× the median step | the median step is the inferred frequency; 1.5× tolerates calendar months |
| `time-range` | valid times start/end more than one step away from `--start-time`/`--end-time`, or the axis is lead-time only | — | one step of tolerance because aggregated bins are labelled at their left edge |
| `bbox` | any lat/lon outside `--bbox` by more than one grid cell | — | one cell tolerates cell-centre vs cell-edge clipping (`clip-region --region` keeps straddling cells) |
| `bbox-coverage` | — | the data stop more than one cell short of an edge of `--bbox` | over-clipped, or a source grid smaller than the box |
| `provenance` | — | `weather_skills_history` absent, empty or malformed | every catalog skill stamps it; its absence means hand-made or unknown lineage |

### Units allow-list

The family comes from CF `standard_name`, then the variable name (`tp`, `pr`,
`precip*`, `*rain*`; `t2m`, `tas`, `tmax`, `tmin`, `sst`, `*temperature*`) —
never from the units, so a precipitation variable carrying `K` fails rather
than being skipped. A dimensionless variable (`units: "1"`, an index or a
probability) has no family and only gets the structural checks.

- Precipitation rate: `mm day-1`, `mm h-1`, `mm s-1`, `m s-1`, `m day-1`,
  `kg m-2 s-1`, `kg m-2 h-1`, `kg m-2 day-1`. Mass fluxes are converted with
  liquid-water density (1000 kg m-3) before the range check.
- Precipitation amount: `mm`, `m`, `kg m-2`. The upper bound uses the
  variable's `aggregation_period` (amount ÷ period days); with no
  `aggregation_period` the upper bound is reported as WARN "not checked".
- Temperature: `K`, `degree_Celsius`. Fahrenheit converts but no catalog
  skill writes it, so it fails here to be caught before a °C threshold is
  applied to it.

The sampling interval for the precipitation ceiling comes from the
variable's `data_interval`, then `aggregation_period`, then the median step
of the time axis; if none is known the daily ceiling applies.

### Differences and anomalies

A difference or anomaly field can legitimately be negative. When the
variable's name or attributes mention `anom`, `diff`, `change`, `bias` or
`error`, or the provenance history contains `difference`,
`standardize-anomaly` or `verify`, the range check is reported as WARN
"not checked" instead of applied.

### NaN fraction

The default `0.5` is deliberately loose. Land-only products (CHIRPS) carry
structural NaN over the ocean, and a coastal box can be 20–40 % ocean, so a
strict default would fail correct artifacts. A field more than half missing
cannot support a regional statistic. Tighten it per task (`--max-nan-frac
0.05` for a land-only box). An all-missing variable always fails.

### Time axis

The time axis is `time` when it is a dimension. A classic forecast (`step`
dimension plus a scalar `time` init) is checked on its valid times,
`init + step`. A `step` axis with no init is lead time only: monotonicity and
gaps are checked, but `--start-time`/`--end-time` fail (run `step-to-time`
first, or check a store that still carries its init).

## Exit codes and output

- `0` — every check PASS or WARN.
- `1` — at least one check FAIL.
- `2` — the artifact cannot be read, a requested `--variable` is missing or
  not numeric, nothing numeric is present, or a flag is invalid.

The card goes to stdout and ends with
`RESULT: PASS|FAIL - n FAIL, n WARN, n PASS (exit n)`. On exit 1 a one-line
summary also goes to stderr. Nothing is written to disk, so there is no
`--output` and no provenance entry for this skill.

## Example

```bash
# Gate a Kenya clip of daily CHIRPS for March-May 2026.
uv run ${CLAUDE_SKILL_DIR}/scripts/check_artifact.py -i /tmp/chirps_kenya.zarr \
    --bbox 5.506/33.894/-4.677/41.855 --start-time 2026-03-01 --end-time 2026-05-31 \
    --expect-units 'mm day-1' --max-nan-frac 0.05
```

```
check-artifact: /tmp/chirps_kenya.zarr
variables checked: precip

[PASS] units-present (precip)
    observed: 'mm day-1'
    expected: a units attribute
    basis:    CF
...
[FAIL] bbox (latitude/longitude)
    observed: lat -4.675..5.475, lon 28.025..47.975 (cell 0.05 x 0.05 deg)
    expected: inside 5.506/33.894/-4.677/41.855 (N/W/S/E) +/- one grid cell
    basis:    --bbox; one-cell tolerance for cell-centre vs edge clipping

RESULT: FAIL - 1 FAIL, 0 WARN, 10 PASS (exit 1)
```
