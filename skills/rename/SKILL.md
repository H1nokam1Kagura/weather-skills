---
name: rename
description: Rename one data variable in a weather-skills standard dataset Zarr to a new name, writing a new standard dataset. The renamed variable keeps its values and attributes; all other variables, coordinates, and dimensions pass through unchanged.
license: MIT
compatibility: Requires Python 3.12 and uv.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/rename.py *)
metadata:
  version: "0.0.2"
  catalog-group: transforms
---

# rename

Source-agnostic variable-rename primitive. Renames one data variable
(`--variable`) to a new name (`--to-name`) via xarray's `ds.rename`, writing a
new Zarr whose renamed variable keeps all of its own attrs. Coordinates and
dimensions are out of scope; only a data variable is renamed.

## When to use

- A data variable needs a different name.
- Two datasets carry the same quantity under different variable names and need
  to match.

## Usage

```
uv run ${CLAUDE_SKILL_DIR}/scripts/rename.py --input <in.zarr> --output <out.zarr> \
    --to-name <NAME> [--variable NAME]
```

Write to a new path: `--output` is replaced, so pointing it at the input
destroys the input.

### Arguments
- `--input`, `-i` — input Zarr containing a weather-skills standard dataset.
- `--output`, `-o` — output Zarr.
- `--to-name` — the new variable name; becomes the output variable's name.
- `--variable`, `-v` — source data variable to rename. If omitted, the input
  must have exactly one data variable, and that one is used.

Renaming `--variable` to a name it already has is a valid no-op: the output is
written with a fresh provenance entry so it can still be the store a downstream
step reads.

### Output

Same dims, coords, and data variables as the input, except the selected variable
is named `--to-name`. The renamed variable keeps its values and attrs; all other
variables, coords, dims, and dataset attrs are unchanged apart from the appended
provenance entry. Like every skill output, precip amounts (e.g. `kg m-2`, `mm`)
are re-stamped with the standard amount `standard_name` on write.

The skill exits with code 2 and a clear message when: the input is missing; the
output exists and is not a directory; `--variable` is not a data variable (a
coordinate such as `latitude` or `time` is refused, not renamed); `--variable`
is omitted and the input does not have exactly one data var; `--to-name` is
empty or contains `/` (Zarr would store it as a nested group and the variable
would vanish); or `--to-name` already names a different existing variable,
coordinate, or dimension (renaming onto it would clash).

### Provenance

The output carries a `weather_skills_history` provenance chain; inspect it
with the `provenance` skill.

## Example

IMERG names its precipitation variable `precip`; IFS names it
`precipitation_surface`. Rename each to a shared `precipitation`:

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/rename.py -i /tmp/imerg.zarr -o /tmp/imerg_renamed.zarr \
    --variable precip --to-name precipitation

uv run ${CLAUDE_SKILL_DIR}/scripts/rename.py -i /tmp/ifs.zarr -o /tmp/ifs_renamed.zarr \
    --variable precipitation_surface --to-name precipitation
```

Both renamed stores now carry the same variable name. To stack them with the
`concat` skill along a new `source` dim, first put them on one grid and one
time axis (`step-to-time` on the forecast, `coarsen --reference-grid`, `select`
to a shared window) — concat refuses inputs whose other coordinates differ.
