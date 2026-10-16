# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core @ git+https://github.com/rhiza-research/weather-skills-core@a4110e30c8637ea99d79f752499d00e4cd65fafb",
#   "cftime>=1.6",
#   "numpy",
#   "xarray",
# ]
# ///
"""Extract day-of-year (1-366) from a datetime64 data variable.

For each selected data variable, replaces it with its calendar day of year
via xarray's ``.dt.dayofyear`` accessor, under a new ``VAR_dayofyear`` name.
Element-wise (no dim is reduced): the result keeps the source variable's
exact shape and dims. ``NaT`` entries become ``NaN``. Data variables that
aren't selected pass through untouched.

Only ``datetime64``-typed variables qualify — a lead-time/duration
(``timedelta64``) variable, e.g. the ``step`` axis or an onset-date result
still expressed as elapsed lead time, has no calendar day of year to read;
run ``step-to-time`` first to turn it into an absolute date.
"""

import sys

from weather_skills_core import Dataset, UsageError, weather_skill

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.1.0"


def _is_datetime64(da):
    import numpy as np

    return np.issubdtype(da.dtype, np.datetime64)


@weather_skill(
    name="day-of-year",
    version=_SKILL_VERSION,
)
@weather_skill.argument("-i", "--input", type=Dataset("any"), required=True)
@weather_skill.argument(
    "--variable",
    "-v",
    action="append",
    help="Restrict the computation to this data variable. Repeatable. Each "
    "selected variable must be datetime64-typed. Default (unset): every "
    "datetime64-typed data variable.",
)
@weather_skill.argument(
    "--since",
    default=None,
    metavar="YYYY-MM-DD",
    help="Instead of calendar day-of-year, write whole days since this date "
    "(VAR_days_since). Required when the dates span more than one calendar "
    "year: day-of-year wraps at 1 January, so a mean of 28 Dec (363) and 3 Jan "
    "(3) comes out as day 183.",
)
def day_of_year(ds, variable, since, **kwargs):
    """Extract day-of-year (1-366) from a datetime64 data variable."""
    # Variable selection, mirroring `spell-length`/`onset-date`: explicit
    # --variable names must be data variables and must each be
    # datetime64-typed. Default selection takes every datetime64-typed data
    # variable; the rest pass through untouched.
    if variable is not None:
        data_vars = list(ds.data_vars)
        invalid = [v for v in variable if v not in ds.data_vars]
        if invalid:
            raise UsageError(
                f"--variable {invalid} not data variable(s) of the input. "
                f"Valid data variables: {data_vars}"
            )
        selected = list(dict.fromkeys(variable))
        not_datetime = [v for v in selected if not _is_datetime64(ds[v])]
        if not_datetime:
            dtypes = [str(ds[v].dtype) for v in not_datetime]
            raise UsageError(
                f"variable(s) {not_datetime} are not datetime64-typed (dtypes: "
                f"{dtypes}). A lead-time/duration (timedelta64) variable has no "
                "calendar day of year; run step-to-time first to convert it to "
                "an absolute date."
            )
    else:
        selected = [v for v in ds.data_vars if _is_datetime64(ds[v])]
        if not selected:
            dtypes = {v: str(ds[v].dtype) for v in ds.data_vars}
            raise UsageError(f"no datetime64-typed data variable found (dtypes: {dtypes}).")

    passthrough = [v for v in ds.data_vars if v not in selected]
    if passthrough:
        print(
            f"Note: passing through untouched data variable(s) {passthrough}.",
            file=sys.stderr,
        )

    import numpy as np

    ref = None
    if since is not None:
        from weather_skills_core.standard_utils import parse_date

        ref = np.datetime64(parse_date(since), "ns")
    mode = f"days since {since}" if ref is not None else "day-of-year"
    print(f"Computing {mode} for variables={selected}", file=sys.stderr)

    out_ds = ds.copy()
    for var in selected:
        da = ds[var]
        if getattr(da, "pint", None) is not None and da.pint.units is not None:
            da = da.pint.dequantify()

        if ref is not None:
            result = ((da - ref) / np.timedelta64(1, "D")).astype("float64")
            result = np.floor(result)
            result.attrs = {
                "GRIB_name": f"{var} days since {since}",
                "long_name": f"{var} days since {since}",
                "description": f"whole days since {since} (NaT -> NaN) extracted from {var}",
                "standard_name": None,
                "units": "1",
            }
            del out_ds[var]
            out_ds[f"{var}_days_since"] = result
            continue

        valid = da.values[~np.isnat(da.values)]
        years = np.unique(da.dt.year.values[~np.isnat(da.values)])
        span_days = (valid.max() - valid.min()) / np.timedelta64(1, "D") if valid.size else 0
        # One season straddling 1 January is the hazard. A multi-year stack of
        # onsets (a climatology, span >= a year) is the intended day-of-year use.
        if years.size > 1 and span_days < 365:
            raise UsageError(
                f"'{var}' spans calendar years {years.tolist()}: day-of-year wraps at "
                "1 January, so averaging or thresholding it across the boundary is wrong "
                "(28 Dec and 3 Jan average to day 183). Pass --since YYYY-MM-DD (e.g. the "
                "forecast's first day) to get a wrap-safe day offset instead."
            )

        result = da.dt.dayofyear
        # Attrs are rebuilt from scratch, NOT carried over from the source
        # variable: the source describes a date, not this derived integer
        # day-of-year. standard_name is explicitly None (CF has no entry for
        # this derived quantity); units is the CF convention "1" for a
        # dimensionless count (same reasoning as spell-length).
        result.attrs = {
            "GRIB_name": f"{var} day of year",
            "long_name": f"{var} day of year",
            "description": f"day of year (1-366; NaT -> NaN) extracted from {var}",
            "standard_name": None,
            "units": "1",
        }
        del out_ds[var]
        out_ds[f"{var}_dayofyear"] = result

    return out_ds


if __name__ == "__main__":
    day_of_year()
