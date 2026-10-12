# SKILL.md vs code conformance: concat, rename, unit-convert, select, downscale

Base: `origin/dev` @ 52d211e. **Resolved core: `weather-skills-core` 0.2.0 @
`a4110e30c8637ea99d79f752499d00e4cd65fafb`** (uv cache checkout of `dev`; the
same SHA the independent conformance run resolved from every script's inline
metadata on 2026-10-12). All tests and the conformance harness below ran on
that one SHA. Synthetic xarray stores only, plus the two recorded CHIRPS stores
the harness ships.

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
| **Round 2 (D7, partly refutes the round-1 "missing units ignored" claim):** an input omitting `units` crashed in pint (`DimensionalityError`, exit 1). The round-1 check did ignore it; `xr.concat` on pint arrays then failed | Yes (`astra/concat.omitted-units-allowed`, `astra/concat.later-input-units-agree`). **The refutation is right.** | Code | `scripts/concat.py`: inputs are dequantified before `xr.concat` (units already proven equal where present) and the present units are stamped on the output | `test_concat_accepts_input_omitting_units[True/False]` (fails on 3ec71cf) |
| **Round 2 (D8):** mixed calendars on the `--dim` coordinate: uncaught `ValueError`, exit 1 | Yes (`concat.shared-calendar.refuse`) | Code + SKILL.md | `_calendar()` + check in `_check_compatible` (exit 2, names each calendar) | `test_concat_rejects_mixed_calendars` (fails on 3ec71cf), `test_concat_accepts_matching_cftime_calendars` |
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
| **Round 2 (D13):** provenance recorded `--index` as the raw strings (`["-1", "00"]`); SKILL.md promises canonical integers | Yes (`astra/select.ordered-negative-indices`) | Code | `scripts/select_dim.py`: `_strict_index` argparse `type=` validates the strict literal and returns `int`; it raises `UsageError` so the exit-2 `Error:` path is unchanged | `test_select_records_index_as_canonical_integers` (fails on 3ec71cf), `test_select_index_rejects_non_integer_literal[4]`, `test_select_index_zero_aliases_are_duplicates` |
| Plain-file output promised exit 2; actual is uncaught `NotADirectoryError` | Yes | Doc (core) | `SKILL.md:48-53`, `:96-101` | none |
| Same-or-nested output rejection; cache-hit no-op | See cross-cutting section | Doc | `SKILL.md:48-53`, `:114-117` | none |

## downscale

| Finding | Verified? | Fix side | What changed | Tests added |
|---|---|---|---|---|
| SKILL.md lists a `dims` arg that does not exist | Yes (`--dims` is rejected as an unrecognized argument; `dims` was named only in the provenance paragraph) | Doc | Removed from the `SKILL.md` provenance arg list (`:86-88`) | none |
| q-q reference units never checked | Yes. A reference in `m day-1` produced output values `0.011..0.014` labelled `mm/day` (1000x wrong); a `K` reference was mapped into a precip variable; a unitless reference was accepted. | Code | `scripts/downscale.py:130-131` captures input units before regridding (regridding drops the pint quantity); `:141-152` refuses differing units, or units on only one side; `SKILL.md:69` documents it | `test_downscale_qq_maps_onto_reference_with_matching_units` (accept, values mapped; the skill had no q-q test before), `test_downscale_qq_rejects_reference_units_mismatch[m day-1 / K / None]` |
| Output keeps the input's units | Yes, but **not a defect once the check above exists**: mapped values are in the reference's units, which are now guaranteed equal to the input's | Not-a-defect (documented) | `SKILL.md:77-78` | covered by the accept test |
| `reference_inputs` hashing + cache-hit | See cross-cutting section | Doc | `SKILL.md:86-94` | none |
| **Round 2 (D19):** q-q reference with no `--time-dim` accepted when it shares no variable; q-q silently degraded to linear while provenance said `q-q` | Yes (`astra/downscale.qq-no-shared-vars-still-needs-time`) | Code + SKILL.md | `scripts/downscale.py`: `time_dim not in ref.dims` → exit 2 before any mapping. A reference WITH the time dim but no shared variable still passes through, as documented | `test_downscale_qq_rejects_reference_without_time_dim` (fails on 3ec71cf), `test_downscale_qq_no_shared_variable_passes_through` |
| **Round 2 (D20):** `--factor 1` regenerated coords (`vmin + spacing*arange`), which drift on float32 grids, so q-q against a same-grid reference was refused (real CHIRPS) | Yes (`downscale.real.qq-same-grid.accept`); reproduced synthetically with a 204-point float32 0.05° grid (the real fixture lives outside this repo) | Code + SKILL.md | `--factor 1` keeps the input coords; when target coords equal input coords the input passes through (no regrid) | `test_downscale_factor_one_is_exact_identity_on_float32_grid`, `test_downscale_qq_factor_one_accepts_reference_on_input_grid` (both fail on 3ec71cf) |
| **Round 2 (D21):** q-q reference grid of another shape: uncaught broadcast `ValueError`, exit 1 | Yes (`downscale.qq-reference-other-shape.refuse`, `astra/downscale.qq-grid-length-mismatch`) | Code | shape / missing-coord check before `allclose` → exit 2 | `test_downscale_qq_rejects_reference_grid_of_other_shape` (fails on 3ec71cf) |

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

**Left for core, deliberately** (not implemented here; defect ids from the
independent conformance run):

- **D14: `-i X -o X` replaces the input, on all five skills** (exit 0; a nested `-o X/child.zarr` writes into the input store).
  **Proposed core fix**, in `weather_skills_core/decorator.py`, inside `wrapper()`, immediately after
  `params, input_paths, upstream, first_ds = open_dataset_params(params, arguments)` and after `output_paths` is built,
  **before `result = fn(**params)`** (so it also covers skills that write their own output and return `None`):

  ```python
  for out in output_paths:
      o = out.resolve()
      for inp in input_paths:
          i = Path(inp).resolve()
          if o == i or o.is_relative_to(i) or i.is_relative_to(o):
              raise UsageError(
                  f"--output {out} is the same as, inside, or contains input {inp}; "
                  "use a distinct path"
              )
  ```

  `UsageError` → exit 2, before any computation or write. `input_paths` already includes `Dataset`-typed args and
  `zarr_paths()` holders. Plain-string reference args (downscale `--reference-grid` / `--qq-reference`) are not in it,
  so either make those `Dataset`-typed (see D17) or have the skill check them. Add a core test that runs a stub skill
  with `-i X -o X` and asserts exit 2 and an unchanged input tree.
- **D15: plain-file `--output`** → uncaught `NotADirectoryError`, exit 1. Proposed: in the same pre-`fn` block,
  `if out.exists() and not out.is_dir(): raise UsageError(...)` for skills that write Zarr. Figure-writing skills
  legitimately overwrite files, so gate it on the declared output kind, or put it in `write_output`'s Dataset branch
  before `shutil.rmtree`.
- **D16: cache-hit no-op.** Not in core and not in its authoring guide. The SKILL.md text was corrected instead; adding
  one is a core design decision.
- **D17: reference hashing** for secondary Zarrs (`--reference-grid`, `--qq-reference`). This needs a core way to
  declare a hashed reference input without turning provenance into a multi-input join.
- **D23: the core dependency is unpinned.** Every script declares `weather-skills-core @ git+...@dev`, a moving branch,
  and provenance records only the skill commit. This is packaging/release policy for the repo, not a per-skill fix.
  Proposed: pin a tag or SHA in each script's inline metadata (CI's `check_inline_deps.py` could enforce it), or have
  core stamp its own version/SHA into each history entry.

Not fixed and not core: **D24** (the writer re-serialises units in pint long form, e.g. `K` → `kelvin`). They are
pint-equal; it is cosmetic.

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
| pytest, one skill at a time (final head) | concat 12, rename 11, unit-convert 10, select 20, downscale 16: all pass |
| `--help` on each changed script | exit 0 for all five |

The full-repo pytest run was not done, on purpose: memory on the host was
limited, so only the five touched skills were run. `--help` was run with the
local venv Python, not `uv run --script` (which needs network to resolve the
inline `git+` dependency).

## Round 2: independent conformance run

The source was the tier-1 conformance harness (354 cases: 133 Claude-authored, 221 written blind by a second model),
at `C:\Users\neilha\wt\aws-ws2\decision-stack\llmp\conformance\tier1\` (`CONFORMANCE.md`, UPSTREAM-DEFECTS section).
It confirmed 14 round-1 claims. **It partly refuted one: "concat: missing units are ignored".** The check did
ignore them, but `xr.concat` then crashed in pint (D7). The refutation is correct, and D7 is now fixed.

Each D-defect fix has a regression test in the skill's `tests/` that fails on 3ec71cf; the tables above give the
names. The conformance cases are not copied into this repo, because the harness DSL and its recorded CHIRPS fixtures
live in the decision-stack repo. Each fixed case has an equivalent skill test; D20 uses a synthetic float32 0.05°
grid in place of the real CHIRPS fixture. To run the harness against this branch locally (no network, no Databricks):

```
set PYTHONDONTWRITEBYTECODE=1 & set PYTHONWARNINGS=ignore
<venv with weather-skills-core @ a4110e30c8637ea99d79f752499d00e4cd65fafb>\python.exe ^
  C:\Users\neilha\wt\aws-ws2\decision-stack\llmp\conformance\tier1\run_conformance.py ^
  --skills-root C:\Users\neilha\wt\ws-upstream-remed\skills --record --only <skill> ^
  --out <scratch>\rec_<skill>.json --work <scratch>\work_<skill>
```

Run it one skill at a time. A single 354-case process died silently (exit 255) at case 105 on this memory-loaded host,
and the harness writes results only at the end. Its default gate mode compares against outcomes pinned at
**52d211e**, so on any fixed tree it fails by design. Use `--record` and compare (below).

### Harness result on this branch

Core: `a4110e30c8637ea99d79f752499d00e4cd65fafb`, one SHA for every case. Python 3.12.13; xarray 2026.9.0, zarr
3.4.1, numpy 2.5.4, pandas 3.0.6, pint 0.26.1, pint-xarray 0.6.1 (the same versions the independent run used).
concat and rename were recorded on 5a48aed; select, unit-convert and downscale on 39a7274. Neither concat nor rename
changed after 5a48aed.

- **354/354 cases ran.** Against the authors' expected outcome: **330 match, 20 differ, 4 untestable.** All 4
  untestable cases are DSL limits the case author declared.
- **Changed vs the 3ec71cf record: 16 cases. All 15 skill-behaviour changes move to the author's expected outcome**
  (D7 ×2, D8, D13 ×2 provenance checks, D19 ×3, D20, D21 ×3, zero-spacing, unparseable `--to-units`, repeated
  `--dim`). **0 regressions.** The 16th, `astra/downscale.cache-qq-content-change`, is a harness setup error on this
  host: Windows refuses the harness's own `rmtree` + `rename` of `QQ.zarr.tmp` between its 27 `set_value` steps,
  before the skill under test runs. It reproduces identically with the **3ec71cf** scripts on this host, so it is not
  a skill change. The independent run was on Linux (Databricks) and did not hit it.
- **The 20 that still differ from the authors' expectation are all accounted for:**
  - 16 are the core output-path cases (D14: `-i X -o X`, nested, input-inside-output, on all five skills). They are
    deliberately left for core (proposal below).
  - 3 are deliberate refusals the updated SKILL.md requires, where the case was authored against the 52d211e doc:
    - `concat.coords-new-dim.accept` (new-dim stack with differing `time`; row 2);
    - `unit-convert.standard-name-with-to-standard.ignored` (row 9);
    - `downscale.qq-units-differ.runs` (row 15; the encoder typed it "scientific-validity, runs", but running produced
      values labelled kelvin from a degC reference).
  - 1 is the Windows harness setup error above (`astra/downscale.cache-qq-content-change`).
- **Refusals still exiting 1 instead of 2:** 27. All of them refuse correctly; only the exit code is off. Most are
  core-owned:
  - unreadable input store (`GroupNotFoundError`), all five skills;
  - plain-file `--output` (`NotADirectoryError`, D15);
  - two `-o` with one result (`SkillError`, exit 1 by core design).

  The rest are left as-is to keep the blast radius tight, because none was in the requested defect list:
  - downscale: unknown `--variable` (`KeyError`), NaN `--target-resolution`, a singleton spatial axis (core
    `grid_spacing`), a missing `--reference-grid`/`--qq-reference` store, a reference missing an axis, and a shared
    q-q variable without the sample axis;
  - concat: `--dim` present on some inputs only (`Dimension model already exists`).

## Disagreements with the independent run

None on substance. Every D-row reproduced as described. Three qualifications:
- **D2/D3/D4, D9-D12, D18** were already fixed in 3ec71cf, as the run itself records.
- **The three "expected accept" cases now refused** (listed above) are expectations written against the old SKILL.md.
  They are not defects of this branch, and the contracts should be re-encoded against the new docs (the run says the
  same).
- **The `cache-qq-content-change` setup error** is the harness's own Windows file-handling limitation, not a skill
  outcome.

## Blast radius

The diff is confined to the five skills' `scripts/`, `tests/` and `SKILL.md`. No shared helper and no core change was
needed; every fix is a local check or a local argparse `type=`/`action=` in one script.

Classes: **(a)** an input that used to crash (uncaught exception, exit 1) now gets a clean exit-2 refusal;
**(b)** an input that used to be ACCEPTED is now REFUSED (breaking for callers); **(c)** an input that is still
accepted now produces DIFFERENT output (values, attrs or provenance); **(d)** doc-only. A fifth class appears too:
**(e)** an input that used to be refused (or crash) is now ACCEPTED.

### Per behaviour change

| # | Skill: change | Class | Exactly what is affected | Guaranteed unchanged | Tests pinning both sides |
|---|---|---|---|---|---|
| 1 | concat: differing present `units` of a shared variable refused (3ec71cf) | b | Inputs whose shared variable carries pint-different units (e.g. `K` + `degC`). Before: pint silently converted later inputs to the first's units; values were correct | Equal or spelling-equivalent units (`mm/day` = `mm day-1`); inputs that omit units | `test_concat_rejects_mismatched_units`, `test_concat_accepts_equivalent_unit_spellings`, `test_concat_accepts_input_omitting_units` |
| 2 | concat: different data-variable sets, a missing non-concat dim, a size mismatch, or differing coordinate values refused (3ec71cf) | b (a for inputs that used to crash, e.g. `step` on one input only) | Any pair whose non-`--dim` dims or coords are not identical. **This includes stacking inputs along a NEW dim when their `time` coords differ**, and model outputs on different grids (the `select` SKILL.md example only works when the models share a grid). Before: outer join with NaN fill | Identical grids and coords; concat along an existing dim whose own coords differ (e.g. stitching time windows) | `test_concat_rejects_different_data_variables`, `..._mismatched_other_dim_coords`, `..._mismatched_other_dim_size`, `test_concat_along_time`, `test_concat_along_new_dim_with_coords` |
| 3 | concat: input omitting `units` no longer crashes (D7) | e | A shared variable with units on some inputs and none on others. Before: pint `DimensionalityError`, exit 1. Now accepted, and the output carries the present units | Values and units for inputs with equal units (now joined as magnitudes: equal units, so no numeric change, same pint-form units string) | `test_concat_accepts_input_omitting_units[True/False]`, `test_concat_accepts_equivalent_unit_spellings`, `test_concat_along_time` |
| 4 | concat: mixed calendars on the `--dim` coordinate refused (D8) | a (b only for cftime-vs-cftime mixes that xarray could write as a mixed object axis) | `--dim` coordinate calendars differ (e.g. `proleptic_gregorian` vs `noleap`) | Same calendar on every input, including all-cftime `noleap` | `test_concat_rejects_mixed_calendars`, `test_concat_accepts_matching_cftime_calendars` |
| 5 | rename: several data vars and no `--variable` refused (3ec71cf) | b | Multi-variable stores without `-v`. Before: renamed the first variable silently | Single-variable stores without `-v`; any store with `-v` | `test_rename_multi_var_requires_variable`, `test_rename_multi_var_with_variable`, `test_rename_default_variable_and_stamps_history` |
| 6 | rename: coordinate/dimension `--variable` refused; unknown variable and clashing `--to-name` exit 2 (3ec71cf) | b (coordinate/dimension source, bare-dim target); a (unknown, clash) | `-v latitude`, `-v time`, `--to-name <existing dim>` | Data-variable renames; same-name no-op | `test_rename_rejects_non_data_variable[3]`, `test_rename_rejects_clashing_name[2]`, `test_rename_same_name_is_noop`, `test_rename_explicit_variable` |
| 7 | unit-convert: `--to-units` reads units from the pint quantity (3ec71cf, D1) | e | EVERY `--to-units` run. Before: all refused, exit 2 "has no units attr" | `--to-standard` path | `test_unit_convert_to_units_single_var`, `..._multi_var_with_variable`, `..._standard_name_with_to_units`, `test_unit_convert_to_standard` |
| 8 | unit-convert: multi-var `--to-units` without `--variable` refused; unknown or coordinate `--variable` exit 2 (3ec71cf) | a, plus b for a coordinate `--variable` with `--to-standard` (it used to run) | Multi-var stores without `-v`: refused before too, for the wrong reason, so there is no caller-visible change except the message | Single-var stores; named data variables | `..._multi_var_requires_variable`, `..._rejects_unknown_variable`, `..._multi_var_with_variable` |
| 9 | unit-convert: `--standard-name` with `--to-standard` refused (3ec71cf) | b | That flag combination only. Before: the flag was silently ignored | `--standard-name` with `--to-units` | `..._rejects_standard_name_with_to_standard`, `..._standard_name_with_to_units` |
| 10 | unit-convert: unparseable `--to-units` exit 2 (round 2) | a | E.g. `--to-units definitely_not_a_unit`. At 3ec71cf: pint `UndefinedUnitError`, exit 1 (at 52d211e: exit 2, masked by D1) | Valid unit strings | `test_unit_convert_rejects_unparseable_target_units`, `test_unit_convert_to_units_single_var` |
| 11 | select: `--value` on a dim with no coordinate variable refused (3ec71cf) | b | Numeric `--value` on a coord-less dim. Before: matched by position | `--index` on the same dim; `--value` on dims with coords | `test_select_value_rejects_dim_without_coord`, `test_select_index_accepts_dim_without_coord` |
| 12 | select: unknown dim, out-of-range `--index`, unparseable datetime/timedelta `--value` exit 2 (3ec71cf) | a | Those inputs only (still refused; exit 1 became exit 2) | Valid selectors | `test_select_bad_selector_exits_2[5]`, `test_select_bad_timedelta_value_exits_2`, `test_select_negative_index_and_values_accepted` |
| 13 | select: `--index` recorded as canonical integers in provenance (D13) | c | Every `--index` run: the history entry's `args.index` changes from strings (`["-1","00"]`) to ints (`[-1, 0]`). Output data is unchanged. Hits anything that compares or replays history args | `--value` runs; the selected data | `test_select_records_index_as_canonical_integers`, `test_select_index_rejects_non_integer_literal[4]`, `test_select_index_zero_aliases_are_duplicates`, `test_select_by_index_collapses_dim` |
| 14 | select: repeated `--dim` refused (round 2) | b | `--dim A --dim B`. Before: argparse kept the last one silently | A single `--dim` | `test_select_rejects_repeated_dim`, every other select test |
| 15 | downscale: q-q reference units must equal the input's (3ec71cf) | b | `--algorithm q-q` where a shared variable's units differ, or are present on one side only. Before: ran and mislabelled values (×1000, or K labels on °C values) | Linear interpolation; q-q with equal or spelling-equivalent units | `test_downscale_qq_rejects_reference_units_mismatch[3]`, `test_downscale_qq_maps_onto_reference_with_matching_units` |
| 16 | downscale: q-q reference without `--time-dim` refused (D19) | b (no shared variable; it used to silently fall back to linear), a (shared variable; it used to crash) | q-q references lacking the time dim | q-q references with the time dim, including those that share no variable (documented pass-through) | `test_downscale_qq_rejects_reference_without_time_dim`, `test_downscale_qq_no_shared_variable_passes_through` |
| 17 | downscale: `--factor 1` and exactly-matching target grids pass through without regridding (D20) | c (factor 1 output is now the input's exact coords and values, not a re-interpolation onto regenerated coords that drift on float32 grids); e (q-q against a same-grid reference, which used to be refused) | `--factor 1`; `--reference-grid` whose coords equal the input's exactly | factor > 1, `--target-resolution`, lateral-realign reference grids | `test_downscale_factor_one_is_exact_identity_on_float32_grid`, `test_downscale_qq_factor_one_accepts_reference_on_input_grid`, `test_downscale_linear_factor2`, `test_downscale_reference_grid_allows_lateral_shift` |
| 18 | downscale: q-q reference grid of another shape, or missing a spatial coord, exit 2 (D21) | a | Those references only | Matching grids | `test_downscale_qq_rejects_reference_grid_of_other_shape`, `test_downscale_qq_maps_onto_reference_with_matching_units` |
| 19 | downscale: zero-spacing input grid refused (round 2) | a | Repeated lat/lon values (factor > 1 used to raise ZeroDivisionError; factor 1 would otherwise have passed it through after #17) | Valid grids | `test_downscale_rejects_zero_spacing_grid[1/2]` |
| 20 | Docs: cache-hit, reference hashing, output-path guards, `dims` arg, exit-2 lists | d | none | all behaviour | none |

### Who can be hit: in-repo uses (grepped)

Each recipe was run as a tiny synthetic chain against 52d211e and against this head
(`chains.py`, kept outside the repo):

| Use | Where | Under the new behaviour |
|---|---|---|
| `select --dim time --value <dates>` → `convert-to-totals` | `skills/clim-fetch/SKILL.md:237`; referenced from `skills/standardize-anomaly/SKILL.md:142-145`, `skills/convert-to-totals/SKILL.md:57-59`, `skills/indicator/SKILL.md:46` | Works (OK on both) |
| `select --dim level --value 500` | `skills/weathernext-fetch/SKILL.md:165` | Works (OK on both) |
| `rename --variable precipitation_surface --to-name precip` | `README.md:252` | Works (explicit `-v`) |
| `rename` ×2 then `concat --dim time` | `skills/rename/SKILL.md:94` | Works when both stores share the grid. **Breaks (row 2) if the obs and forecast grids differ**; that was already scientifically wrong (NaN outer join). Realign with `coarsen`/`downscale --reference-grid` first |
| `select --dim step --index 0` ×2 then `concat --dim model --coords ECMWF,GFS` | `skills/select/SKILL.md:24-25, 120-134` | Works on a shared grid. **Refused (row 2) if the two models' lat/lon differ**; it used to NaN-fill. Same remedy |
| `downscale ... --method linear-interpolation` | `demo/run_pipeline.sh:54`, `README.md:53` | **Already broken at 52d211e**, not caused here: the flag is `--algorithm`, so `--method` is an argparse error (exit 2 on both). With `--algorithm` it works on both. Left alone (outside the five skill folders) |
| "use `select` before single-variable transforms", then `rename` without `-v` | `agents/forecaster.md:58-60`; `skills/rename/scripts/rename.py:8` docstring | Single-variable store: works. **Multi-variable store without `-v`: now exit 2 (row 5)**, where before the first variable was silently renamed. `select` does not drop variables, so an agent following the guidance on a multi-variable store must pass `-v` |
| "convert with `unit-convert` first" | `skills/difference/SKILL.md:91`, `skills/point-value/SKILL.md:53`, `skills/summarize-dim/SKILL.md:70`, `skills/inspect-zarr/SKILL.md:36`, `skills/standardize-anomaly/scripts/standardize_anomaly.py:100` | **Now works (row 7)**; at 52d211e every `--to-units` refused. On multi-variable stores, pass `--variable` |
| `concat` named as a join / a new-dim builder | `skills/provenance/SKILL.md:62,85`, `skills/provenance/tests/test_provenance.py:103-155` (synthetic history, never runs concat), `skills/summarize-dim/SKILL.md:27` | Unaffected (history shape unchanged) |
| `downscale` as the realign/refine pointer | `skills/coarsen/scripts/coarsen.py:127,139`, `skills/coarsen/SKILL.md:21-65`, `skills/difference/SKILL.md:31,64`, `skills/verify/SKILL.md:47`, `skills/verify/scripts/verify.py:90`, `skills/kenya-forecast-fetch/SKILL.md:129-184` | Unaffected (pointers only; factor > 1 and reference-grid realign are unchanged) |
| Skill listing | `agents/forecaster.md:11`, `README.md:52-56`, `ALTERNATIVES.md:5` | Unaffected |
| `tests/`, `tools/`, `.claude-plugin/` | `tests/conftest.py` (shared fixtures only); `tools/*` and `.claude-plugin/*.json` name no skill behaviour | Unaffected |

### Outside this repo (listed only, not modified)

| Consumer | What it uses | Relies on a changed behaviour? |
|---|---|---|
| `C:\Users\neilha\weather-skills-demo` | Its own copy of `skills/` (same `clim-fetch`/`weathernext-fetch` recipes as above); `demo/run_pipeline.sh:54` (same pre-existing `--method` break); `skills/onset-date/scripts/onset_date.py:689,789` tells users to run `unit-convert --to-units mm` | No. The onset hint now works (row 7). The frozen eval snapshots under `evals/intake/runs/*/_plugin/rhiza-forecasting/skills/` vendor their own copies of the scripts and are unaffected until re-vendored |
| `C:\Users\neilha\weather-skills-plotting-demo`, `...-plotting-public` | `docs/plotting.md:97` ("do not `coarsen` or `downscale` solely to plot") | No invocation; unaffected |
| decision-stack simulator `C:\Users\neilha\wt\aws-a5\decision-stack\clm_ws\contracts_a5.py:225-241` (`_select`) | Models `select` picking a calendar week by coordinate on a valid-time axis (`sel_valid`), or by lead position on `step` (`sel_lead`) | No. It never relies on a `--value` on a coord-less dim, on a repeated `--dim`, or on string-valued provenance. One nuance: a datetime literal given as `--value` on a timedelta `step` axis is still refused, now with exit 2 instead of exit 1. The model treats that call as invalid anyway |

### Release impact

There is no manual version bump (CONTRIBUTING.md). The version-bump workflow patch-bumps every skill whose folder
changed. Rows of class **(b)** (refusals of previously accepted input) and **(c)** (row 13 provenance arg types,
row 17 factor-1 output) are caller-visible contract changes, and every one of the five skills has at least one
(concat 1-2, rename 5-6, unit-convert 9, select 11/13/14, downscale 15-17). So **label the PR `release: minor`**; the
label applies to all skills bumped in that run. Class (a), (d) and (e) rows alone would justify only the default patch.
