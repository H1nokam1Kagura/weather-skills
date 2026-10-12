# SKILL.md vs code conformance: concat, rename, unit-convert, select, downscale

Base: `origin/dev` @ 52d211e. Verified offline against `weather-skills-core` 0.2.0
(cached `dev` checkout a4110e3, 2026-09-25 — the newest copy available offline;
core `dev` may have moved since). Synthetic xarray stores only.

"Verified" means a reproducing test was written and observed against the
**unchanged** scripts (HEAD tree with the new tests overlaid): every refuse
test listed below FAILED there and passes after the fix, except where noted.

**Version bumps: none, deliberately.** CONTRIBUTING.md: "Authors **must not**
edit any `metadata.version` field by hand"; the `version-bump` workflow bumps
`metadata.version` and `_SKILL_VERSION` in lockstep on merge to `main`. All five
skills change under `skills/<name>/`, so each will get a patch bump on merge
(apply a `release: minor` label if the new exit-2 refusals should count as
minor).

## concat

| Finding | Verified? | Fix side | What changed | Tests added |
|---|---|---|---|---|
| Mismatched `units` not refused (SKILL.md "Input units" promises exit 2) | Yes. `mm day-1` + `m day-1` inputs: run succeeded, the second input was silently converted (pint) to the first's units. Values were numerically right; the promised refusal did not happen. | Code (SKILL.md section is explicit and detailed; refusing keeps the conversion an explicit, recorded `unit-convert` step) | `scripts/concat.py:18-51` `_check_compatible()` compares units per shared variable (spelling-independent via `units_equal`, missing units ignored, as documented); called at `:83` | `test_concat_rejects_mismatched_units` (refuse), `test_concat_accepts_equivalent_unit_spellings` (accept `mm/day` vs `mm day-1`) |
| Inputs not sharing "all other dims and variables" are outer-joined | Yes. Different data-var sets: output had both vars, NaN-filled. Different latitude values or longitude size: outer-joined, 16 / 12 NaN cells. | Code | Same function: refuses a different data-variable set, a non-concat dim missing on one input, a size mismatch, or differing coordinate values. `SKILL.md:14` now spells out the check and the exit-2 outcome | `test_concat_rejects_different_data_variables`, `..._mismatched_other_dim_coords`, `..._mismatched_other_dim_size`; existing `test_concat_along_time` / `..._new_dim_with_coords` cover the accept path |

## rename

| Finding | Verified? | Fix side | What changed | Tests added |
|---|---|---|---|---|
| Several data vars, no `--variable`: silently renames the first | Yes (renamed `precip`, left `tmax`, exit 0) | Code (SKILL.md promises exit 2) | `scripts/rename.py:23-40` | `test_rename_multi_var_requires_variable` (refuse), `test_rename_multi_var_with_variable` (accept) |
| Coords/dims can be renamed (`-v latitude` renamed the dim) | Yes | Code: restrict. SKILL.md already says coords/dims are out of scope and that a non-data-variable `--variable` exits 2 | Same block | `test_rename_rejects_non_data_variable[latitude/time/nope]` |
| (found while verifying) unknown `--variable`, or `--to-name` clashing with an existing var/coord, crashed with uncaught `ValueError` (exit 1 + traceback), not the promised exit 2 | Yes | Code | Same block | `test_rename_rejects_clashing_name[tmax/latitude]`, `test_rename_same_name_is_noop` (accept the documented no-op) |
| Output path that is a plain file: SKILL.md promised exit 2; actual is uncaught `NotADirectoryError` | Yes | Doc (output writing belongs to the core decorator, see below) | `SKILL.md:54-60` | none (documents existing behaviour) |
| Same-or-nested output rejection; "cache-hits" | See cross-cutting section | Doc | `SKILL.md:32-34`, `:38`, `:68-73` | none |

## unit-convert

| Finding | Verified? | Fix side | What changed | Tests added |
|---|---|---|---|---|
| No `--variable` + `--to-units` on a multi-var store converts every variable (SKILL.md: "Omit with `--to-units` only when the store has a single data var") | **Partly, and worse than reported.** It did not convert everything; it refused every input. **`--to-units` was broken for all inputs**: core opens inputs pint-quantified, which moves `units` off `attrs`, and the script read `attrs["units"]`, so every run exited 2 with "variable 'precip' has no units attr". The multi-var case would have converted all variables once that was fixed. | Code (both) | `scripts/unit-convert.py:77` reads units via core `variable_units()` (quantity first, attr fallback); `:65-70` refuses a multi-var store without `--variable`; `:60-61` refuses an unknown `--variable` | `test_unit_convert_to_units_single_var` (accept: the main path, absent from the existing tests), `..._multi_var_requires_variable` (refuse; it also passed on base, but for the wrong reason, see above), `..._multi_var_with_variable` (accept), `..._rejects_unknown_variable` |
| `--standard-name` with `--to-standard` silently ignored | Yes (exit 0, `standard_name` left as `lwe_precipitation_rate`) | Code: refuse. SKILL.md already says "only with `--to-units`"; `--help` said "Ignored", which now reads "(only with --to-units)" | `scripts/unit-convert.py:58-59`, help text `:48`; `SKILL.md:41-47` | `..._rejects_standard_name_with_to_standard` (refuse), `..._standard_name_with_to_units` (accept) |

Note: the converted output's `units` attr carries pint's long spelling (for
example `millimeter / hour`), because the core writer re-serialises the quantity.
It is pint-equivalent to the target, and `--to-standard` already behaved this way.
Left alone.

## select

| Finding | Verified? | Fix side | What changed | Tests added |
|---|---|---|---|---|
| `--value` on a dim with no coordinate variable matched positions through xarray's virtual integer index (SKILL.md promises refusal) | Yes (`--dim member --value 1`, exit 0) | Code | `scripts/select_dim.py:111-115` | `test_select_value_rejects_dim_without_coord` (refuse), `test_select_index_accepts_dim_without_coord` (accept) |
| Unknown dim crashed with `KeyError`, out-of-range `--index` with `IndexError`, unparseable datetime/timedelta `--value` with `ValueError` (all exit 1 + traceback; promised exit 2) | Yes, all four | Code | `:94-95` (dim), `:104-105` (range, names the position, dim and size as documented), `:55-69` (parse errors name the dtype and expected form) | `test_select_bad_selector_exits_2[5 cases]`, `test_select_bad_timedelta_value_exits_2`, `test_select_negative_index_and_values_accepted` (accept `-3` and `0D`, identical result) |
| Plain-file output promised exit 2; actual is uncaught `NotADirectoryError` | Yes | Doc (core) | `SKILL.md:48-53`, `:96-101` | none |
| Same-or-nested output rejection; cache-hit no-op | See cross-cutting section | Doc | `SKILL.md:48-53`, `:114-117` | none |

## downscale

| Finding | Verified? | Fix side | What changed | Tests added |
|---|---|---|---|---|
| SKILL.md lists a `dims` arg that does not exist | Yes (`--dims` is rejected as an unrecognized argument; `dims` was named only in the provenance paragraph) | Doc | Removed from the `SKILL.md` provenance arg list (`:86-88`) | none |
| q-q reference units never checked | Yes. A reference in `m day-1` produced output values `0.011..0.014` labelled `mm/day` (1000x wrong); a `K` reference was mapped into a precip variable; a unitless reference was accepted. | Code | `scripts/downscale.py:130-131` captures input units before regridding (regridding drops the pint quantity); `:141-152` refuses differing units, or units on only one side; `SKILL.md:69` documents it | `test_downscale_qq_maps_onto_reference_with_matching_units` (accept, values mapped; the skill had no q-q test before), `test_downscale_qq_rejects_reference_units_mismatch[m day-1 / K / None]` |
| Output keeps the input's units | Yes, but **not a defect once the check above exists**: mapped values are in the reference's units, which are now guaranteed equal to the input's | Not-a-defect (documented) | `SKILL.md:77-78` | covered by the accept test |
| `reference_inputs` hashing + cache-hit | See cross-cutting section | Doc | `SKILL.md:86-94` | none |

## Cross-cutting: cache-hit no-op, reference hashing, output-path guards

The promises, verbatim location before this change:

- **select** `SKILL.md`: "Re-running with identical arguments ... is a cheap no-op ... A cache hit requires ..." (Provenance section), and under `--output`: "the same store as the input (or one nested inside the other) is rejected ... a plain file is rejected".
- **rename** `SKILL.md`: "the skill rejects a run where `--input` and `--output` resolve to the same store or one nested inside the other"; "the output exists and is not a directory" in the exit-2 list; "a renamed-but-unchanged input still cache-hits".
- **downscale** `SKILL.md`: the `reference_inputs` field ("invalidates the cache and forces a recompute") and the whole "Cache-hit comparison reads the existing output's ..." paragraph.

What exists: none of the three scripts implements any of these, and neither does
core 0.2.0. `write_output` unconditionally `rmtree`s and rewrites `--output`;
`build_history` records `{basename, hash}` for `Dataset`-typed inputs only.
`--reference-grid` and `--qq-reference` are plain strings opened with
`xr.open_zarr`, so they appear in `args` as paths and are never hashed. No other
skill implements a cache-hit check or a same/nested-path guard locally (grep
across `skills/*/scripts`), so there is no in-repo pattern to follow.

Observed hazard: `select -i a.zarr -o a.zarr` exits 0 and leaves `a.zarr`
holding the selected output, so **the input is destroyed**. `-o a.zarr/sub.zarr`
writes into the input store.

**Decision: doc fix, flagged for core.** The decorator owns `-o/--output` (it
refuses skills that declare it) and does all output writing. Per-skill copies
of cache, hashing, or path guards would also miss the ~40 other skills that
share the writer. The SKILL.md text now says what happens: no cache check,
references recorded by path only, and an explicit warning that same or nested
output paths are not checked.

**Left for core** (not implemented here):
1. Same-or-nested `--output` vs every input path → `UsageError` (data-loss guard).
2. Existing plain-file `--output` → `UsageError` (currently an uncaught `NotADirectoryError`, exit 1).
3. Cache-hit no-op on matching history, if wanted at all.
4. A way to declare secondary reference Zarrs so they are hashed into provenance without turning the entry into a multi-input join.

Out of scope but same stale text: `coarsen`, `convert-calendar`, `difference`,
`step-to-time`, `summarize-dim` and `deaccumulate` SKILL.md also describe
cache-hit behaviour. They were not audited here.

## Checks run (offline, as CI)

| Check | Result |
|---|---|
| `uvx --offline ruff@0.16.0 format --check` | 142 files already formatted |
| `uvx --offline ruff@0.16.0 check` | All checks passed |
| `tools/check_inline_deps.py skills/` | exit 0 (concat avoids a new `numpy` import) |
| `tools/check_cftime_deps.py skills/` | exit 0 |
| `tools/check_version_consistency.py .` | OK across 43 skills |
| pytest, one skill at a time | concat 8, rename 11, unit-convert 9, select 13, downscale 9: all pass |
| `--help` on each changed script | exit 0 for all five |

The full-repo pytest run was not done, on purpose: memory on the host was
limited, so only the five touched skills were run. `--help` was run with the
local venv Python, not `uv run --script` (which needs network to resolve the
inline `git+` dependency).
