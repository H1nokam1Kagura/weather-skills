# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core @ git+https://github.com/rhiza-research/weather-skills-core@main",
#   "cftime>=1.6",
#   "numpy",
#   "xarray",
# ]
# ///
"""Rainy season onset date along a time-like dim, via a chosen --definition.

For each selected data variable, finds the first day satisfying an onset
criterion along the time dim (``time``, or a lead-time dim such as ``step``)
and returns that day's own coordinate value (a duration if the dim is a
lead-time axis, an absolute date if it is already ``time``). Data variables
that don't carry the time dim pass through untouched.

Three onset definitions are supported via ``--definition``:

- ``ICPAC`` — a wet spell (``--wet-spell-days`` days totaling more than
  ``--wet-spell-thresh``) with no disqualifying dry spell
  (``--dry-spell-days`` or more consecutive days below ``--dry-spell-thresh``)
  within the following ``--search-days`` days.
- ``CHC_start_grow_season`` — the Climate Hazards Center two-window
  definition: the first day where the following ``--period1-days`` days
  accumulate at least ``--period1-thresh``, and the ``--period2-days`` days
  immediately after that accumulate more than ``--period2-thresh``.
- ``Moron_Robertson`` — Moron & Robertson (2014): the first day of a
  ``--mr-window-days`` run of wet days (each ``>= --mr-wet-day-thresh``)
  whose total exceeds a per-cell threshold (``--mr-thresh`` or
  ``--mr-thresh-field``), not followed within ``--mr-follow-days`` by a dry
  spell (``--mr-veto``: a low-total window, or a consecutive dry-day run).
"""

import re
import sys

from weather_skills_core import Dataset, UsageError, weather_skill
from weather_skills_core.standard_dataset import ALIASES, PREDICTION_TIMEDELTA, detect_time_dim
from weather_skills_core.units import units_equal

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.1.0"


def _rainfall_onset_nd(block, wet_thresh, wet_days, dry_thresh, dry_days, search_days):
    """ICPAC onset search, vectorized over every leading (batch) dim at once.

    block : ndarray, shape (..., n_time) — time dim must be the last axis,
        daily rainfall accumulation (same units as wet_thresh/dry_thresh)

    Returns the 0-based time index of the onset day per gridpoint/member/etc
    (float, NaN where no qualifying onset was found within the series),
    shape (...).
    """
    import numpy as np

    n_time = block.shape[-1]
    dry = block < dry_thresh

    onset_idx = np.full(block.shape[:-1], np.nan)
    found = np.zeros(block.shape[:-1], dtype=bool)

    # last candidate start day that still leaves room for both the wet-spell
    # window and the full dry-spell search window
    t_max = n_time - max(wet_days, search_days)
    for t in range(0, t_max + 1):
        wet_window = block[..., t : t + wet_days]
        wet_sum = wet_window.sum(axis=-1)
        wet_nan = np.isnan(wet_window).any(axis=-1)
        candidate = (wet_sum > wet_thresh) & ~wet_nan

        # max consecutive dry-day run within the next search_days days
        counter = np.zeros(block.shape[:-1])
        max_run = np.zeros(block.shape[:-1])
        for w in range(search_days):
            counter = (counter + 1) * dry[..., t + w]
            np.maximum(max_run, counter, out=max_run)
        dry_nan = np.isnan(block[..., t : t + search_days]).any(axis=-1)
        no_dry_spell = (max_run < dry_days) & ~dry_nan

        qualifies = candidate & no_dry_spell & ~found
        onset_idx = np.where(qualifies, t, onset_idx)
        found = found | qualifies

    return onset_idx


def _rainfall_onset_accum_nd(block, period1_days, period1_thresh, period2_days, period2_thresh):
    """CHC_start_grow_season onset search, vectorized over every leading
    (batch) dim at once.

    block : ndarray, shape (..., n_time) — time dim must be the last axis,
        daily rainfall accumulation (same units as period1_thresh/period2_thresh)

    Returns the 0-based time index of the onset day per gridpoint/member/etc
    (float, NaN where no qualifying onset was found within the series),
    shape (...).
    """
    import numpy as np

    n_time = block.shape[-1]
    total_window = period1_days + period2_days

    onset_idx = np.full(block.shape[:-1], np.nan)
    found = np.zeros(block.shape[:-1], dtype=bool)

    # last candidate start day that still leaves room for both windows
    t_max = n_time - total_window
    for t in range(0, t_max + 1):
        first_window = block[..., t : t + period1_days]
        first_sum = first_window.sum(axis=-1)
        first_nan = np.isnan(first_window).any(axis=-1)

        second_window = block[..., t + period1_days : t + total_window]
        second_sum = second_window.sum(axis=-1)
        second_nan = np.isnan(second_window).any(axis=-1)

        qualifies = (
            (first_sum >= period1_thresh)
            & (second_sum > period2_thresh)
            & ~first_nan
            & ~second_nan
            & ~found
        )
        onset_idx = np.where(qualifies, t, onset_idx)
        found = found | qualifies

    return onset_idx


# The Moron_Robertson kernel below is a vectorized port of `find_onset` /
# `_find_onset_core` / `_precompute_onset` in python/prepare_data/onset_utils.py
# of https://github.com/amarchakitus/onset_blending (commit 10ec8e3),
# MIT License, Copyright (c) 2026 University of Chicago. Its trigger, both
# veto modes, the "continue to the next trigger candidate when vetoed"
# search, the short-series behaviour and `reject_if_short_followup` are
# reproduced index-for-index (the test suite carries that function as an
# oracle). One deliberate deviation, to follow this skill's NaN convention:
# a NaN anywhere in a candidate's trigger or follow-up window disqualifies
# that candidate here, whereas the reference only disqualifies on a NaN in
# the trigger window and otherwise treats a NaN follow-up day as not dry.
def _moron_robertson_onset_nd(
    block,
    thresh,
    window_days,
    wet_day_thresh,
    follow_days,
    veto,
    dry_spell_days,
    dry_day_thresh,
    sum_window_days,
    sum_thresh,
    start_idx,
    reject_short_followup,
):
    """Moron_Robertson onset search, vectorized over every leading (batch)
    dim at once.

    block : ndarray, shape (..., n_time) — time dim must be the last axis,
        daily rainfall accumulation (same units as every threshold)
    thresh : scalar or ndarray broadcastable to block.shape[:-1] — the
        per-cell trigger accumulation threshold (NaN -> no onset there)

    Candidate day t (0-based) triggers when every day of
    [t, t+window_days) is wet (>= wet_day_thresh) and that window's total
    exceeds thresh. It is vetoed when the follow-up period
    [t+window_days, t+window_days+follow_days), clipped to the series end,
    contains a dry spell:

    - ``window_sum``: a sum_window_days-day window lying wholly inside the
      follow-up period totals less than sum_thresh;
    - ``consecutive_dry``: a run of >= dry_spell_days consecutive dry days
      (< dry_day_thresh) *starts* inside the follow-up period (the run may
      extend past it).

    A vetoed candidate does not end the search: the next triggering day is
    tried. Candidates before start_idx are skipped; with
    reject_short_followup, a candidate whose follow-up period runs past the
    series end is rejected rather than checked over the days available.

    Returns the 0-based time index of the onset day (float, NaN where none),
    shape (...).
    """
    import numpy as np

    n_time = block.shape[-1]
    batch = block.shape[:-1]
    thresh = np.broadcast_to(np.asarray(thresh, dtype=np.float64), batch)
    onset_idx = np.full(batch, np.nan)
    if n_time < window_days:
        return onset_idx

    lead_zero = np.zeros(batch + (1,))

    def prefix(x):
        # prefix[..., i] = sum of x[..., :i]; range sums in O(1)
        return np.concatenate([lead_zero, np.cumsum(x, axis=-1)], axis=-1)

    nan = np.isnan(block)
    csum = prefix(np.where(nan, 0.0, block))
    pre_nan = prefix(nan)
    pre_not_wet = prefix(~(block >= wet_day_thresh))

    if veto == "consecutive_dry":
        dry = ~nan & (block < dry_day_thresh)
        run = np.zeros(batch)
        starts = np.zeros(block.shape, dtype=bool)
        for i in range(n_time):
            run = (run + 1) * dry[..., i]
            if i >= dry_spell_days - 1:
                # mark a run's start the first time it reaches the length
                starts[..., i - dry_spell_days + 1] |= run == dry_spell_days
        pre_veto = prefix(starts)
    else:  # window_sum
        sw = sum_window_days
        n_win = max(0, n_time - sw + 1)
        win_sum = csum[..., sw : sw + n_win] - csum[..., :n_win]
        win_nan = (pre_nan[..., sw : sw + n_win] - pre_nan[..., :n_win]) > 0
        pre_veto = prefix(~win_nan & (win_sum < sum_thresh))

    found = np.zeros(batch, dtype=bool)
    valid_thresh = ~np.isnan(thresh)
    for t in range(max(0, start_idx), n_time - window_days + 1):
        trig_end = t + window_days
        if reject_short_followup and trig_end + follow_days > n_time:
            break  # every later candidate's follow-up runs even further past the end
        follow_end = min(n_time, trig_end + follow_days)

        all_wet = (pre_not_wet[..., trig_end] - pre_not_wet[..., t]) == 0
        trig = all_wet & ((csum[..., trig_end] - csum[..., t]) > thresh)

        if veto == "consecutive_dry":
            hi = follow_end
        else:
            hi = min(follow_end - sum_window_days + 1, n_time - sum_window_days + 1)
        if hi > trig_end:
            vetoed = (pre_veto[..., hi] - pre_veto[..., trig_end]) > 0
        else:
            vetoed = np.zeros(batch, dtype=bool)

        has_nan = (pre_nan[..., follow_end] - pre_nan[..., t]) > 0

        qualifies = trig & ~vetoed & ~has_nan & valid_thresh & ~found
        onset_idx = np.where(qualifies, t, onset_idx)
        found = found | qualifies

    return onset_idx


def _search_start_index(time_coord, search_start):
    """0-based index of the first time step on or after ``--mr-search-start``
    (``MM-DD``) in the year of the series' first time step; 0 when that
    date precedes the series. Requires an absolute (datetime/cftime) time
    coordinate."""
    import datetime

    import numpy as np

    m = re.fullmatch(r"(\d{2})-(\d{2})", search_start)
    try:
        if m is None:
            raise ValueError
        month, day = int(m.group(1)), int(m.group(2))
        datetime.date(2000, month, day)  # leap year, so 02-29 is accepted
    except ValueError:
        raise UsageError(f"--mr-search-start '{search_start}' is not a valid MM-DD date.") from None

    try:
        years = np.asarray(time_coord.dt.year.values)
        months = np.asarray(time_coord.dt.month.values)
        days = np.asarray(time_coord.dt.day.values)
    except (AttributeError, TypeError):
        raise UsageError(
            f"--mr-search-start needs absolute dates on time dim '{time_coord.name}', "
            f"but it holds {time_coord.dtype} values. Run step-to-time upstream to "
            "turn a lead-time axis into dates."
        ) from None

    key = years * 10000 + months * 100 + days
    target = int(years[0]) * 10000 + month * 100 + day
    after = np.nonzero(key >= target)[0]
    return int(after[0]) if after.size else len(key)


def _threshold_field(tds, var_name, da, dim):
    """The per-cell Moron_Robertson threshold from ``--mr-thresh-field``,
    checked against (and re-coordinated onto) the rainfall variable ``da``."""
    import numpy as np

    data_vars = list(tds.data_vars)
    if var_name is None:
        if len(data_vars) != 1:
            raise UsageError(
                f"--mr-thresh-field holds data variables {data_vars}; name the "
                "threshold with --mr-thresh-field-var."
            )
        var_name = data_vars[0]
    elif var_name not in tds.data_vars:
        raise UsageError(
            f"--mr-thresh-field-var '{var_name}' not in --mr-thresh-field "
            f"data variables {data_vars}."
        )
    thr = tds[var_name]
    if getattr(thr, "pint", None) is not None and thr.pint.units is not None:
        thr = thr.pint.dequantify()

    if dim in thr.dims:
        raise UsageError(
            f"--mr-thresh-field variable '{var_name}' carries the time dim '{dim}'; "
            "it must be one threshold per cell."
        )
    extra = [d for d in thr.dims if d not in da.dims]
    if extra:
        raise UsageError(
            f"--mr-thresh-field variable '{var_name}' has dim(s) {extra} that "
            f"'{da.name}' does not (its dims: {list(da.dims)}); the threshold "
            "field must be on the same grid."
        )
    for d in thr.dims:
        if thr.sizes[d] != da.sizes[d]:
            raise UsageError(
                f"--mr-thresh-field grid mismatch on '{d}': {thr.sizes[d]} "
                f"values vs {da.sizes[d]} in the input."
            )
        if d in thr.coords and d in da.coords:
            a, b = np.asarray(thr[d].values), np.asarray(da[d].values)
            numeric = np.issubdtype(a.dtype, np.number) and np.issubdtype(b.dtype, np.number)
            same = np.allclose(a, b, atol=1e-6, rtol=0) if numeric else np.array_equal(a, b)
            if not same:
                raise UsageError(
                    f"--mr-thresh-field grid mismatch: '{d}' coordinate values "
                    "differ from the input's. Regrid the threshold field first."
                )

    thr_units, da_units = thr.attrs.get("units"), da.attrs.get("units")
    if thr_units and da_units and not units_equal(thr_units, da_units):
        raise UsageError(
            f"--mr-thresh-field units '{thr_units}' differ from '{da.name}' units "
            f"'{da_units}'; use unit-convert on one of them first."
        )

    thr = thr.drop_vars([c for c in thr.coords if c not in thr.dims])
    thr = thr.assign_coords({d: da[d].values for d in thr.dims if d in da.coords})
    return thr.astype(np.float64).load(), var_name


def _onset_idx_to_date(onset_idx, time_values):
    """Turn a float index-into-time-dim (NaN where no onset found) into
    actual onset values, using the time dim's own coordinate values (a
    duration for a lead-time dim like ``step``, an absolute date for
    ``time``)."""
    import numpy as np
    import xarray as xr

    idx = onset_idx.values
    valid = ~np.isnan(idx)
    idx_int = np.where(valid, idx, 0).astype(int)
    nat = np.array("NaT", dtype=time_values.dtype)
    onset_values = np.where(valid, time_values[idx_int], nat)
    return xr.DataArray(
        onset_values, dims=onset_idx.dims, coords=onset_idx.coords, name="onset_date"
    )


def _resolve_time_dim(ds, override):
    """Explicit --time-dim wins; else the ontology's time dim; else a
    lead-time dim (e.g. `step`, aliased to `prediction_timedelta`)."""
    if override:
        if override not in ds.dims:
            raise UsageError(f"--time-dim '{override}' not in dims {list(ds.dims)}")
        return override
    try:
        return detect_time_dim(ds)
    except UsageError:
        pass
    lead = next((d for d in ds.dims if ALIASES.get(d) == PREDICTION_TIMEDELTA), None)
    if lead is not None:
        print(
            f"Note: no time dim found; computing onset date over lead-time "
            f"dim '{lead}' instead. Pass --time-dim to override.",
            file=sys.stderr,
        )
        return lead
    raise UsageError(
        f"no time/lead-time dim identified in {list(ds.dims)}. Pass --time-dim to override."
    )


@weather_skill(
    name="onset-date",
    version=_SKILL_VERSION,
)
@weather_skill.argument("-i", "--input", type=Dataset("any"), required=True)
@weather_skill.argument(
    "--variable",
    "-v",
    action="append",
    help="Restrict the computation to this data variable. Repeatable. "
    "Each selected variable must carry the time dim. Default (unset): "
    "every data variable carrying the time dim.",
)
@weather_skill.argument(
    "--definition",
    required=True,
    choices=["ICPAC", "CHC_start_grow_season", "Moron_Robertson"],
    help="Onset criterion. 'ICPAC': wet-spell-then-no-dry-spell (see "
    "--wet-spell-* / --dry-spell-* / --search-days). "
    "'CHC_start_grow_season': two-window cumulative rainfall check (see "
    "--period1-* / --period2-*). 'Moron_Robertson': all-wet window over a "
    "per-cell threshold, then no dry spell (see --mr-*; needs --mr-thresh "
    "or --mr-thresh-field).",
)
@weather_skill.argument(
    "--wet-spell-thresh",
    type=float,
    default=20.0,
    help="[ICPAC] Total rainfall a wet spell must exceed, in the variable's own units.",
)
@weather_skill.argument(
    "--wet-spell-days",
    type=int,
    default=3,
    help="[ICPAC] Number of consecutive days summed for the wet-spell check.",
)
@weather_skill.argument(
    "--dry-spell-thresh",
    type=float,
    default=1.0,
    help="[ICPAC] A day below this rainfall (in the variable's own units) counts as dry.",
)
@weather_skill.argument(
    "--dry-spell-days",
    type=int,
    default=7,
    help="[ICPAC] A dry run of this many consecutive days or more disqualifies the onset.",
)
@weather_skill.argument(
    "--search-days",
    type=int,
    default=21,
    help="[ICPAC] Window (from the wet spell's first day) searched for a disqualifying dry spell.",
)
@weather_skill.argument(
    "--period1-days",
    type=int,
    default=10,
    help="[CHC_start_grow_season] Length of the first accumulation window, in days.",
)
@weather_skill.argument(
    "--period1-thresh",
    type=float,
    default=20.0,
    help="[CHC_start_grow_season] The first window must accumulate at least this much rainfall.",
)
@weather_skill.argument(
    "--period2-days",
    type=int,
    default=20,
    help="[CHC_start_grow_season] Length of the second (confirmation) accumulation window, in days.",
)
@weather_skill.argument(
    "--period2-thresh",
    type=float,
    default=20.0,
    help="[CHC_start_grow_season] The second window must accumulate more than this much rainfall.",
)
@weather_skill.argument(
    "--mr-thresh",
    type=float,
    default=None,
    help="[Moron_Robertson] One trigger threshold for every cell, in the variable's own "
    "units: the trigger window's total must exceed it. Canonically the local "
    "climatological wet-spell amount, which varies by cell -- prefer --mr-thresh-field. "
    "Exactly one of --mr-thresh / --mr-thresh-field is required; there is no default.",
)
@weather_skill.argument(
    "--mr-thresh-field",
    type=Dataset("any"),
    default=None,
    help="[Moron_Robertson] Zarr holding a per-cell trigger threshold on the input's grid "
    "(dims a subset of the variable's non-time dims, identical coordinates), in the "
    "variable's own units.",
)
@weather_skill.argument(
    "--mr-thresh-field-var",
    default=None,
    help="[Moron_Robertson] Data variable in --mr-thresh-field to use. Default: its only "
    "data variable.",
)
@weather_skill.argument(
    "--mr-window-days",
    type=int,
    default=5,
    help="[Moron_Robertson] Trigger window length, in days; every day in it must be wet.",
)
@weather_skill.argument(
    "--mr-wet-day-thresh",
    type=float,
    default=1.0,
    help="[Moron_Robertson] A day with at least this rainfall counts as wet.",
)
@weather_skill.argument(
    "--mr-follow-days",
    type=int,
    default=30,
    help="[Moron_Robertson] Days after the trigger window searched for a dry spell. "
    "0 disables the veto.",
)
@weather_skill.argument(
    "--mr-veto",
    choices=["window_sum", "consecutive_dry"],
    default="window_sum",
    help="[Moron_Robertson] Dry-spell test. 'window_sum' (the original definition): a "
    "--mr-sum-window-days window inside the follow-up totals less than --mr-sum-thresh. "
    "'consecutive_dry': a run of --mr-dry-spell-days days below --mr-dry-day-thresh "
    "starts inside the follow-up.",
)
@weather_skill.argument(
    "--mr-sum-window-days",
    type=int,
    default=10,
    help="[Moron_Robertson, window_sum] Length of the dry-spell window, in days.",
)
@weather_skill.argument(
    "--mr-sum-thresh",
    type=float,
    default=5.0,
    help="[Moron_Robertson, window_sum] A window totaling less than this is a dry spell.",
)
@weather_skill.argument(
    "--mr-dry-spell-days",
    type=int,
    default=7,
    help="[Moron_Robertson, consecutive_dry] A dry run this many days or longer is a dry spell.",
)
@weather_skill.argument(
    "--mr-dry-day-thresh",
    type=float,
    default=None,
    help="[Moron_Robertson, consecutive_dry] A day below this rainfall counts as dry. "
    "Default: --mr-wet-day-thresh.",
)
@weather_skill.argument(
    "--mr-search-start",
    default=None,
    metavar="MM-DD",
    help="[Moron_Robertson] Ignore candidate onset days before this calendar date (in the "
    "year of the first time step; the whole series if it starts later), e.g. 06-02 for "
    "a climatological onset date. Needs an absolute time dim.",
)
@weather_skill.argument(
    "--mr-reject-short-followup",
    action="store_true",
    help="[Moron_Robertson] Reject a candidate whose follow-up period runs past the end "
    "of the series. Default: check the veto over the days that are available.",
)
@weather_skill.argument(
    "--time-dim",
    default=None,
    help="Name of the time-like dim when not auto-detectable.",
)
def onset_date(
    ds,
    variable,
    definition,
    wet_spell_thresh,
    wet_spell_days,
    dry_spell_thresh,
    dry_spell_days,
    search_days,
    period1_days,
    period1_thresh,
    period2_days,
    period2_thresh,
    mr_thresh,
    mr_thresh_field,
    mr_thresh_field_var,
    mr_window_days,
    mr_wet_day_thresh,
    mr_follow_days,
    mr_veto,
    mr_sum_window_days,
    mr_sum_thresh,
    mr_dry_spell_days,
    mr_dry_day_thresh,
    mr_search_start,
    mr_reject_short_followup,
    time_dim,
    **kwargs,
):
    """Rainy season onset date along a time-like dim, via a chosen --definition."""
    import numpy as np
    import xarray as xr

    is_mr = definition == "Moron_Robertson"
    if is_mr:
        if (mr_thresh is None) == (mr_thresh_field is None):
            raise UsageError(
                "--definition Moron_Robertson needs exactly one of --mr-thresh (one value "
                "for every cell) or --mr-thresh-field (a per-cell threshold Zarr). The "
                "canonical threshold is the local climatological wet-spell amount; this "
                "skill does not invent one."
            )
        if mr_thresh_field_var is not None and mr_thresh_field is None:
            raise UsageError("--mr-thresh-field-var only valid with --mr-thresh-field")
        for flag, value, low in (
            ("--mr-window-days", mr_window_days, 1),
            ("--mr-follow-days", mr_follow_days, 0),
            ("--mr-sum-window-days", mr_sum_window_days, 1),
            ("--mr-dry-spell-days", mr_dry_spell_days, 1),
        ):
            if value < low:
                raise UsageError(f"{flag} must be >= {low}, got {value}")
        if mr_dry_day_thresh is None:
            mr_dry_day_thresh = mr_wet_day_thresh
    else:
        given = [
            flag
            for flag, value in (
                ("--mr-thresh", mr_thresh),
                ("--mr-thresh-field", mr_thresh_field),
                ("--mr-thresh-field-var", mr_thresh_field_var),
                ("--mr-search-start", mr_search_start),
            )
            if value is not None
        ]
        if given:
            raise UsageError(f"{given} only valid with --definition Moron_Robertson")

    dim = _resolve_time_dim(ds, time_dim)

    # Variable selection, mirroring `spell-length`: explicit --variable names
    # must be data variables and must each carry the time dim. Default
    # selection takes every data variable carrying it; the rest pass through
    # untouched.
    if variable is not None:
        data_vars = list(ds.data_vars)
        invalid = [v for v in variable if v not in ds.data_vars]
        if invalid:
            raise UsageError(
                f"--variable {invalid} not data variable(s) of the input. "
                f"Valid data variables: {data_vars}"
            )
        selected = list(dict.fromkeys(variable))
        missing = [v for v in selected if dim not in ds[v].dims]
        if missing:
            raise UsageError(f"variable(s) {missing} do not carry time dim '{dim}'.")
    else:
        selected = [v for v in ds.data_vars if dim in ds[v].dims]
        if not selected:
            raise UsageError(f"no data variable carries time dim '{dim}'.")

    passthrough = [v for v in ds.data_vars if v not in selected]
    if passthrough:
        print(
            f"Note: passing through unreduced data variable(s) {passthrough}.",
            file=sys.stderr,
        )

    if definition == "ICPAC":
        print(
            f"Computing onset date dim={dim} definition=ICPAC "
            f"wet_spell_thresh={wet_spell_thresh} wet_spell_days={wet_spell_days} "
            f"dry_spell_thresh={dry_spell_thresh} dry_spell_days={dry_spell_days} "
            f"search_days={search_days} variables={selected}",
            file=sys.stderr,
        )
    elif is_mr:
        veto_params = (
            f"sum_window_days={mr_sum_window_days} sum_thresh={mr_sum_thresh}"
            if mr_veto == "window_sum"
            else f"dry_spell_days={mr_dry_spell_days} dry_day_thresh={mr_dry_day_thresh}"
        )
        print(
            f"Computing onset date dim={dim} definition=Moron_Robertson "
            f"thresh={mr_thresh if mr_thresh is not None else 'per-cell field'} "
            f"window_days={mr_window_days} wet_day_thresh={mr_wet_day_thresh} "
            f"follow_days={mr_follow_days} veto={mr_veto} {veto_params} "
            f"search_start={mr_search_start} "
            f"reject_short_followup={mr_reject_short_followup} variables={selected}",
            file=sys.stderr,
        )
    else:
        print(
            f"Computing onset date dim={dim} definition=CHC_start_grow_season "
            f"period1_days={period1_days} period1_thresh={period1_thresh} "
            f"period2_days={period2_days} period2_thresh={period2_thresh} "
            f"variables={selected}",
            file=sys.stderr,
        )

    time_values = ds[dim].values
    start_idx = 0
    if is_mr and mr_search_start is not None:
        start_idx = _search_start_index(ds[dim], mr_search_start)
        if start_idx < len(time_values):
            print(
                f"Note: onset search starts at {dim}={time_values[start_idx]} (index {start_idx}).",
                file=sys.stderr,
            )
        else:
            print(
                f"Note: --mr-search-start {mr_search_start} falls after the last {dim} "
                "value; every onset will be NaT.",
                file=sys.stderr,
            )
    out_ds = ds.copy()
    for var in selected:
        da = ds[var]
        # The decorator opens data-variable inputs as pint quantities; bare
        # float thresholds can't compare against one inside apply_ufunc, so
        # drop back to a plain array (this also restores the string `units`
        # attr for the label).
        if getattr(da, "pint", None) is not None and da.pint.units is not None:
            da = da.pint.dequantify()

        if definition == "ICPAC":
            onset_idx = xr.apply_ufunc(
                _rainfall_onset_nd,
                da,
                input_core_dims=[[dim]],
                kwargs=dict(
                    wet_thresh=wet_spell_thresh,
                    wet_days=wet_spell_days,
                    dry_thresh=dry_spell_thresh,
                    dry_days=dry_spell_days,
                    search_days=search_days,
                ),
                dask="parallelized",
                dask_gufunc_kwargs={"allow_rechunk": True},
                output_dtypes=[np.float64],
            )
        elif is_mr:
            if mr_thresh_field is not None:
                thresh, thresh_var = _threshold_field(mr_thresh_field, mr_thresh_field_var, da, dim)
            else:
                thresh, thresh_var = mr_thresh, None
            onset_idx = xr.apply_ufunc(
                _moron_robertson_onset_nd,
                da,
                thresh,
                input_core_dims=[[dim], []],
                kwargs=dict(
                    window_days=mr_window_days,
                    wet_day_thresh=mr_wet_day_thresh,
                    follow_days=mr_follow_days,
                    veto=mr_veto,
                    dry_spell_days=mr_dry_spell_days,
                    dry_day_thresh=mr_dry_day_thresh,
                    sum_window_days=mr_sum_window_days,
                    sum_thresh=mr_sum_thresh,
                    start_idx=start_idx,
                    reject_short_followup=mr_reject_short_followup,
                ),
                dask="parallelized",
                dask_gufunc_kwargs={"allow_rechunk": True},
                output_dtypes=[np.float64],
            )
            # A threshold field that lacks some of the variable's non-time dims
            # broadcasts over them; keep the variable's own dim order.
            onset_idx = onset_idx.transpose(*[d for d in da.dims if d != dim])
        else:
            onset_idx = xr.apply_ufunc(
                _rainfall_onset_accum_nd,
                da,
                input_core_dims=[[dim]],
                kwargs=dict(
                    period1_days=period1_days,
                    period1_thresh=period1_thresh,
                    period2_days=period2_days,
                    period2_thresh=period2_thresh,
                ),
                dask="parallelized",
                dask_gufunc_kwargs={"allow_rechunk": True},
                output_dtypes=[np.float64],
            )

        result = _onset_idx_to_date(onset_idx, time_values)

        src_units = da.attrs.get("units", "")
        # units_equal compares pint-equivalence rather than exact spelling,
        # so a dimensionless "1" and "dimensionless" are both recognized as
        # "no real unit" (see spell-length for the same reasoning).
        is_dimensionless = bool(src_units) and units_equal(src_units, "1")
        unit_suffix = f" {src_units}" if src_units and not is_dimensionless else ""

        if definition == "ICPAC":
            label = (
                f"{var} onset date (ICPAC: {wet_spell_thresh}{unit_suffix}/"
                f"{wet_spell_days}d, dry<{dry_spell_thresh}{unit_suffix} for "
                f"{dry_spell_days}d in {search_days}d)"
            )
            description = (
                f"first day of a {wet_spell_days}-day wet spell with "
                f">{wet_spell_thresh}{unit_suffix} total rainfall, with no dry spell of "
                f">={dry_spell_days} consecutive days (<{dry_spell_thresh}{unit_suffix}/day) "
                f"in the following {search_days} days"
            )
        elif is_mr:
            thresh_text = (
                f">{mr_thresh}{unit_suffix}"
                if thresh_var is None
                else f">per-cell '{thresh_var}'{unit_suffix}"
            )
            if mr_veto == "window_sum":
                veto_short = f"no {mr_sum_window_days}d<{mr_sum_thresh}{unit_suffix}"
                veto_text = (
                    f"no {mr_sum_window_days}-day window totaling <{mr_sum_thresh}{unit_suffix}"
                )
            else:
                veto_short = f"no {mr_dry_spell_days}d dry<{mr_dry_day_thresh}{unit_suffix}"
                veto_text = (
                    f"no run of >={mr_dry_spell_days} consecutive days "
                    f"<{mr_dry_day_thresh}{unit_suffix}/day starting"
                )
            start_text = f", from {mr_search_start}" if mr_search_start else ""
            label = (
                f"{var} onset date (Moron_Robertson: {mr_window_days}d wet{thresh_text}, "
                f"{veto_short} in {mr_follow_days}d{start_text})"
            )
            description = (
                f"first day of a {mr_window_days}-day run of wet days "
                f"(>={mr_wet_day_thresh}{unit_suffix}/day) totaling {thresh_text}, with "
                f"{veto_text} within the following {mr_follow_days} days"
                + (f"; days before {mr_search_start} not considered" if mr_search_start else "")
                + (
                    "; candidates whose follow-up runs past the series end rejected"
                    if mr_reject_short_followup
                    else ""
                )
            )
        else:
            label = (
                f"{var} onset date (CHC_start_grow_season: "
                f"{period1_days}d>={period1_thresh}{unit_suffix}, "
                f"{period2_days}d>{period2_thresh}{unit_suffix})"
            )
            description = (
                f"first day where the following {period1_days} days accumulate "
                f">={period1_thresh}{unit_suffix} rainfall, and the {period2_days} days "
                f"after that accumulate >{period2_thresh}{unit_suffix}"
            )

        # Attrs are rebuilt from scratch, NOT carried over from the source
        # variable: the source's standard_name/long_name/units describe the
        # input rainfall quantity, not this derived date/duration.
        # standard_name is explicitly None (CF has no entry for "rainy
        # season onset date" regardless). No `units` attr is set at all —
        # unlike spell-length's dimensionless-count output, this result is
        # genuinely timedelta64/datetime64-typed, and xarray's CF time coder
        # insists on owning that dtype's `units` attr itself; a manually-set
        # (or attrs-healed) `units` string on a time-like variable raises at
        # write time regardless of its value. The result is written under a
        # new variable name (replacing `var`, not reusing its name like
        # spell-length does) specifically so the decorator's same-name
        # attrs-healing from the source variable never applies here and
        # re-introduces `units` (see the naming note just below).
        result.attrs = {
            "GRIB_name": label,
            "long_name": label,
            "description": description,
            "standard_name": None,
        }
        del out_ds[var]
        # Sandwiched, not `{var}_onset_date` / `onset_date_{var}`:
        # weather_skills_core classifies a variable's physical kind (and
        # whether it then requires a `units` attr) by whether its name
        # starts or ends with a short hint like "tp", "pr", "t2m", "tas" —
        # exactly the source variable names this skill is typically run on.
        # A prefix or suffix placement would carry that hint to either end
        # of the new name and misclassify this date output as precip/temp
        # (which *does* require units), making it unreadable as `--input` to
        # any other skill. Sandwiching `var` between fixed, non-hint text
        # keeps it out of both boundary positions regardless of what `var`
        # is named.
        out_ds[f"onset_{var}_date"] = result

    # The collapsed dim disappears from the output (with its coordinates)
    # once no data variable carries it; a dim still carried by a
    # pass-through variable stays.
    if dim in out_ds.dims and all(dim not in out_ds[v].dims for v in out_ds.data_vars):
        out_ds = out_ds.drop_dims(dim)

    return out_ds


if __name__ == "__main__":
    onset_date()
