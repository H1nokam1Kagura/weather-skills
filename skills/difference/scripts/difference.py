# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core @ git+https://github.com/rhiza-research/weather-skills-core@dev",
# ]
# ///
"""Subtract A − B (xarray-aligned)."""

from weather_skills_core import Dataset, UsageError, weather_skill
from weather_skills_core.standard_utils import ensure_normalized_longitude

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.0.2"

_WIDEN = {1: "int16", 2: "int32", 4: "int64"}


@weather_skill(
    name="difference",
    version=_SKILL_VERSION,
)
@weather_skill.argument("-i", "--input", type=Dataset("any"), action="append", required=True)
@weather_skill.argument("--variable", "-v", action="append")
def difference(ds, variable, **kwargs):
    """Subtract A − B (xarray-aligned)."""
    if len(ds) != 2:
        raise UsageError(f"expected exactly two --input paths, got {len(ds)}")
    ds_a, ds_b = ds
    ds_a = ensure_normalized_longitude(ds_a)
    ds_b = ensure_normalized_longitude(ds_b)
    import numpy as np
    import pint
    import xarray as xr

    shared = [v for v in ds_a.data_vars if v in ds_b.data_vars]
    vars_ = list(dict.fromkeys(variable)) if variable else shared
    if not vars_:
        raise UsageError(
            f"inputs share no data variable (A: {list(ds_a.data_vars)}, "
            f"B: {list(ds_b.data_vars)}); rename one first"
        )
    for v in vars_:
        if v not in shared:
            raise UsageError(
                f"--variable {v!r} must be in both inputs (A: {list(ds_a.data_vars)}, "
                f"B: {list(ds_b.data_vars)})"
            )
    out = {}
    for v in vars_:
        a, b = ds_a[v], ds_b[v]
        # Promote bool/uint so A−B cannot wrap
        if a.dtype.kind == "b":
            a = a.astype(np.int16)
        elif a.dtype.kind == "u":
            a = a.astype(getattr(np, _WIDEN.get(a.dtype.itemsize, "int64")))
        if b.dtype.kind == "b":
            b = b.astype(np.int16)
        elif b.dtype.kind == "u":
            b = b.astype(getattr(np, _WIDEN.get(b.dtype.itemsize, "int64")))
        try:
            diff = a - b
        except pint.DimensionalityError as exc:
            raise UsageError(f"{v!r}: A and B carry incompatible units: {exc}") from None
        except ValueError as exc:  # unlabeled dims of different sizes
            raise UsageError(f"{v!r}: cannot align A and B: {exc}") from None
        empty = [d for d in diff.dims if diff.sizes[d] == 0]
        if empty:
            raise UsageError(
                f"{v!r}: no overlapping {empty} labels between A and B; put both on one "
                "grid with coarsen --reference-grid (or downscale) first"
            )
        out[v] = diff.assign_attrs(ds_a[v].attrs)
    return xr.Dataset(out)


if __name__ == "__main__":
    difference()
