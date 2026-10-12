# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core @ git+https://github.com/rhiza-research/weather-skills-core@dev",
#   "cftime",
#   "xarray",
#   "xarray-regrid",
#   "numpy",
# ]
# ///
"""Downscale onto a finer grid (linear or empirical q-q)."""

import math

from weather_skills_core import Dataset, UsageError, weather_skill
from weather_skills_core.standard_dataset import detect_spatial_dims, detect_time_dim
from weather_skills_core.standard_utils import ensure_normalized_longitude, grid_spacing
from weather_skills_core.units import dequantify_dataset, units_equal, variable_units

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.0.2"


def _spacing(ds, dim):
    return grid_spacing(ds[dim].values)


def _target_coord(coord, new_spacing):
    import numpy as np

    vmin, vmax = float(np.min(coord)), float(np.max(coord))
    target = vmin + new_spacing * np.arange(int(np.floor((vmax - vmin) / new_spacing)) + 1)
    return target[::-1] if coord[0] > coord[-1] else target


def _qmap_1d(model, ref):
    import numpy as np

    out = np.full(model.shape, np.nan, dtype=float)
    m_ok, r_ok = ~np.isnan(model), ~np.isnan(ref)
    if not m_ok.any() or not r_ok.any():
        return out
    sorted_m, sorted_r = np.sort(model[m_ok]), np.sort(ref[r_ok])
    ranks = np.searchsorted(sorted_m, model[m_ok], side="right")
    quants = np.clip((ranks - 0.5) / sorted_m.size, 0.0, 1.0)
    out[m_ok] = np.interp(quants, (np.arange(sorted_r.size) + 0.5) / sorted_r.size, sorted_r)
    return out


@weather_skill(
    name="downscale",
    version=_SKILL_VERSION,
)
@weather_skill.argument("-i", "--input", type=Dataset("spatial"), required=True)
@weather_skill.argument("--variable", "-v")
@weather_skill.argument(
    "--algorithm",
    required=True,
    choices=["linear-interpolation", "q-q"],
)
@weather_skill.argument("-f", "--factor", type=int, default=None)
@weather_skill.argument("--target-resolution", type=float, default=None)
@weather_skill.argument("--reference-grid", default=None)
@weather_skill.argument("--qq-reference", default=None)
@weather_skill.argument("--time-dim", default="time")
def downscale(
    ds,
    variable,
    algorithm,
    factor,
    target_resolution,
    reference_grid,
    qq_reference,
    time_dim,
    **kwargs,
):
    """Downscale onto a finer grid (linear or empirical q-q)."""
    import numpy as np
    import xarray as xr
    import xarray_regrid  # noqa: F401

    n = sum(x is not None for x in (factor, target_resolution, reference_grid))
    if n != 1:
        raise UsageError("exactly one of --factor, --target-resolution, --reference-grid")
    if algorithm == "q-q" and not qq_reference:
        raise UsageError("--algorithm q-q requires --qq-reference")
    if qq_reference and algorithm != "q-q":
        raise UsageError("--qq-reference only valid with --algorithm q-q")

    lat_dim, lon_dim = detect_spatial_dims(ds)
    if variable:
        ds = ds[[variable]]
    ds = ensure_normalized_longitude(ds, lon_dim)
    in_lat, in_lon = _spacing(ds, lat_dim), _spacing(ds, lon_dim)

    if reference_grid is not None:
        ref = xr.open_zarr(reference_grid, consolidated=False)
        new_lat, new_lon = np.asarray(ref[lat_dim].values), np.asarray(ref[lon_dim].values)
        ref_lat, ref_lon = _spacing(ref, lat_dim), _spacing(ref, lon_dim)
        # 1% rel_tol: float noise / half-cell shift is not a resolution change.
        if (in_lat < ref_lat and not math.isclose(in_lat, ref_lat, rel_tol=1e-2)) or (
            in_lon < ref_lon and not math.isclose(in_lon, ref_lon, rel_tol=1e-2)
        ):
            raise UsageError("--reference-grid is coarser than input; use coarsen")
    else:
        if factor is not None:
            if factor < 1:
                raise UsageError("--factor must be >= 1")
            lat_sp, lon_sp = in_lat / factor, in_lon / factor
        else:
            too_coarse = (
                in_lat < target_resolution
                and not math.isclose(in_lat, target_resolution, rel_tol=1e-2)
            ) or (
                in_lon < target_resolution
                and not math.isclose(in_lon, target_resolution, rel_tol=1e-2)
            )
            if target_resolution <= 0 or too_coarse:
                raise UsageError("--target-resolution must be finer-or-equal to input")
            lat_sp = lon_sp = target_resolution
        if factor == 1:
            # Identity: keep the input coords exactly. Regenerating them from
            # vmin + spacing * arange drifts on real float32 grids (CHIRPS).
            new_lat, new_lon = ds[lat_dim].values, ds[lon_dim].values
        else:
            new_lat = _target_coord(ds[lat_dim].values, lat_sp)
            new_lon = _target_coord(ds[lon_dim].values, lon_sp)

    target = xr.Dataset(
        coords={
            lat_dim: (lat_dim, new_lat, dict(ds[lat_dim].attrs)),
            lon_dim: (lon_dim, new_lon, dict(ds[lon_dim].attrs)),
        }
    )
    # Regridding drops the pint quantity, so read the input units first.
    in_units = {v: variable_units(ds[v]) for v in ds.data_vars}
    if np.array_equal(new_lat, ds[lat_dim].values) and np.array_equal(new_lon, ds[lon_dim].values):
        # Grids already match: geometry is a no-op (SKILL.md), not a re-interpolation.
        out = dequantify_dataset(ds)
    else:
        out = ds.regrid.linear(target)

    if algorithm == "q-q":
        time_dim = detect_time_dim(out, time_dim)
        ref = xr.open_zarr(qq_reference, consolidated=False)
        if time_dim not in ref.dims:
            raise UsageError(
                f"--qq-reference has no '{time_dim}' dimension (have {list(ref.dims)}); "
                "q-q needs it as the sample axis (--time-dim)"
            )
        for d in (lat_dim, lon_dim):
            if (
                d not in ref.coords
                or ref[d].shape != out[d].shape
                or not np.allclose(out[d].values, ref[d].values, atol=1e-6, rtol=0)
            ):
                raise UsageError(f"--qq-reference '{d}' must match post-downscale grid")
        ref = ref.assign_coords({lat_dim: out[lat_dim], lon_dim: out[lon_dim]})
        shared = [v for v in out.data_vars if v in ref.data_vars]
        # Mapped values take the reference's numbers, so its units must match
        # the input's or the output would carry the wrong units label.
        for v in shared:
            src_u, ref_u = in_units.get(v), variable_units(ref[v])
            if (src_u is None) != (ref_u is None) or (
                src_u is not None and not units_equal(src_u, ref_u)
            ):
                raise UsageError(
                    f"--qq-reference variable '{v}' units {ref_u!r} differ from input "
                    f"units {src_u!r}; convert one with unit-convert first"
                )
        for v in shared:
            mapped = xr.apply_ufunc(
                _qmap_1d,
                out[v],
                ref[v].rename({time_dim: "_qq_ref_time"}),
                input_core_dims=[[time_dim], ["_qq_ref_time"]],
                output_core_dims=[[time_dim]],
                vectorize=True,
                output_dtypes=[float],
            )
            mapped.attrs = dict(out[v].attrs)
            out[v] = mapped
    return out


if __name__ == "__main__":
    downscale()
