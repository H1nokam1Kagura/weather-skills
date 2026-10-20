# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core @ git+https://github.com/weather-skills/weather-skills-core@dev",
# ]
# ///
"""Collapse named dimensions of a Zarr, or each group of a label coord, with a statistic."""

from weather_skills_core import DataError, Dataset, UsageError, weather_skill
from weather_skills_core.standard_dataset import detect_spatial_dims
from weather_skills_core.standard_utils import latitude_weights

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.0.3"


@weather_skill(
    name="summarize-dim",
    version=_SKILL_VERSION,
)
@weather_skill.argument("-i", "--input", type=Dataset("any"), required=True)
@weather_skill.argument("--variable", "-v", action="append")
@weather_skill.argument(
    "--dim",
    action="append",
    help="Dim to summarize (repeatable).",
)
@weather_skill.argument(
    "--by",
    action="append",
    help="Label coord to group by; its dims collapse to one value per label (repeatable).",
)
@weather_skill.argument(
    "--method",
    required=True,
    choices=["mean", "std", "min", "max", "sum", "median"],
)
@weather_skill.argument(
    "--lat-weighted",
    action="store_true",
    help="cos(lat) weights for --method mean over latitude.",
)
def summarize_dim(ds, variable, dim, by, method, lat_weighted, **kwargs):
    """Collapse named dimensions of a Zarr, or each group of a label coord, with a statistic."""
    if not dim and not by:
        raise UsageError("pass --dim, --by, or both")
    dims = list(dict.fromkeys(dim or []))
    for d in dims:
        if d not in ds.dims:
            raise UsageError(f"--dim {d!r} not in dataset (have {list(ds.dims)})")
    by = list(dict.fromkeys(by or []))
    for b in by:
        if b not in ds.coords or b in ds.dims:
            labels = [c for c in ds.coords if c not in ds.dims]
            raise UsageError(f"--by {b!r} is not a label coord (have {labels})")
    if by and lat_weighted:
        raise UsageError("--lat-weighted cannot be combined with --by")
    # The dims a label spans collapse into one entry per label.
    group_dims = list(dict.fromkeys(d for b in by for d in ds[b].dims))
    dims = list(dict.fromkeys([*group_dims, *dims]))
    # Other label coords on the grouped dims (admin_1_name, ...) cannot survive the collapse.
    ds = ds.drop_vars(
        [c for c in ds.coords if c not in (*by, *ds.dims) and set(ds[c].dims) & set(group_dims)]
    )
    lat_dim = None
    if lat_weighted:
        if method != "mean":
            raise UsageError("--lat-weighted requires --method mean")
        lat_dim, _ = detect_spatial_dims(ds)
        if lat_dim not in dims:
            raise UsageError(f"--lat-weighted needs --dim {lat_dim}")

    # No --variable: whole dataset (vars without the dim are skipped by rdims).
    selected = list(dict.fromkeys(variable)) if variable else list(ds.data_vars)
    for var in selected:
        if var not in ds.data_vars:
            raise UsageError(f"--variable {var!r} not in dataset (have {list(ds.data_vars)})")
    out = ds.drop_vars(by)
    for var in selected:
        da = ds[var]
        rdims = [d for d in dims if d in da.dims]
        # With --by, only variables carrying every label dim are summarized.
        if not rdims or not set(group_dims) <= set(da.dims):
            continue
        if by:
            # min_count=1 / ddof=1 as below; the "" label (outside every group) is dropped.
            kw = {"min_count": 1} if method == "sum" else {"ddof": 1} if method == "std" else {}
            # Stack every collapsed dim into one so labels on different dims group together.
            stacked = da.stack(_cells=rdims)
            grouped = getattr(stacked.groupby(by), method)(dim="_cells", keep_attrs=True, **kw)
            for b in by:
                if grouped[b].dtype.kind in "UO" and "" in grouped[b].values:
                    grouped = grouped.drop_sel({b: ""})
                if grouped.sizes[b] == 0:
                    raise DataError(
                        f'--by {b!r}: every value is the empty label ""; nothing to group'
                    )
            out[var] = grouped
        elif method == "sum":
            # min_count=1: all-NaN → NaN, not 0
            out[var] = da.sum(dim=rdims, keep_attrs=True, min_count=1)
        elif method == "std":
            out[var] = da.std(dim=rdims, keep_attrs=True, ddof=1)
        elif method == "mean" and lat_weighted and lat_dim in rdims:
            out[var] = (
                da.pint.dequantify()
                .weighted(latitude_weights(ds[lat_dim]))
                .mean(dim=rdims, keep_attrs=True)
            )
        else:
            out[var] = getattr(da, method)(dim=rdims, keep_attrs=True)
        # Record the statistic (CF cell_methods). A spread or a sum is not the
        # input quantity, so its long_name must not read "Total precipitation".
        attrs = dict(out[var].attrs)
        cf_method = {"std": "standard_deviation", "min": "minimum", "max": "maximum"}
        step = f"{': '.join(rdims)}: {cf_method.get(method, method)}"
        if by:
            step += f" (grouped by {', '.join(by)})"
        attrs["cell_methods"] = (
            f"{attrs['cell_methods']} {step}" if attrs.get("cell_methods") else step
        )
        if method in ("std", "sum"):
            label = "Standard deviation" if method == "std" else "Sum"
            what = attrs.get("long_name") or attrs.get("standard_name") or var
            attrs["long_name"] = f"{label} of {what} across {', '.join(rdims)}"
            # standard_name stays: CF encodes a spread as the quantity's
            # standard_name plus a cell_methods statistic.
            for key in ("GRIB_name", "short_name", "comment"):
                attrs.pop(key, None)
        out[var].attrs = attrs

    for d in dims:
        if d in out.dims and all(d not in out[v].dims for v in out.data_vars):
            out = out.drop_dims(d)
    return out


if __name__ == "__main__":
    summarize_dim()
