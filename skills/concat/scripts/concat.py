# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core @ git+https://github.com/rhiza-research/weather-skills-core@dev",
#   "cftime",
#   "xarray",
# ]
# ///
"""Concatenate Zarr stores along a named dim."""

from weather_skills_core import Dataset, UsageError, weather_skill
from weather_skills_core.units import dequantify_dataset, units_equal, variable_units

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.0.2"


def _check_compatible(dss, dim):
    """Refuse inputs that do not share variables, other dims, and units."""
    first = dss[0]
    first_vars = set(first.data_vars)
    for n, item in enumerate(dss[1:], start=2):
        item_vars = set(item.data_vars)
        if item_vars != first_vars:
            raise UsageError(
                f"input 1 and input {n} have different data variables "
                f"({sorted(first_vars)} vs {sorted(item_vars)}); concat needs the same set"
            )
        other_dims = (set(first.dims) | set(item.dims)) - {dim}
        for d in sorted(other_dims):
            if d not in first.dims or d not in item.dims:
                raise UsageError(f"dim '{d}' is on only one of input 1 and input {n}")
            if first.sizes[d] != item.sizes[d]:
                raise UsageError(
                    f"dim '{d}' has size {first.sizes[d]} on input 1 but "
                    f"{item.sizes[d]} on input {n}"
                )
            in_first, in_item = d in first.coords, d in item.coords
            if in_first != in_item or (in_first and not first[d].variable.equals(item[d].variable)):
                raise UsageError(f"coordinate '{d}' differs between input 1 and input {n}")

    calendars = [(n, _calendar(item, dim)) for n, item in enumerate(dss, start=1)]
    known = [(n, c) for n, c in calendars if c is not None]
    if any(c != known[0][1] for _, c in known[1:]):
        listing = ", ".join(f"input {n}: {c}" for n, c in known)
        raise UsageError(
            f"'{dim}' coordinate calendars differ across inputs ({listing}); "
            "convert them to one calendar first (convert-calendar)"
        )

    units = {}
    for name in sorted(first_vars):
        seen = [(n, variable_units(item[name])) for n, item in enumerate(dss, start=1)]
        present = [(n, u) for n, u in seen if u is not None]
        if any(not units_equal(present[0][1], u) for _, u in present[1:]):
            listing = ", ".join(f"input {n}: {u!r}" for n, u in present)
            raise UsageError(
                f"variable '{name}' has differing units across inputs ({listing}); "
                "convert them to one unit first (unit-convert)"
            )
        if present:
            units[name] = present[0][1]
    return units


def _calendar(item, dim):
    """Calendar of a datetime-like ``dim`` coordinate, else None."""
    if dim not in item.coords:
        return None
    try:
        return item[dim].dt.calendar
    except (AttributeError, TypeError):
        return None


def _coerce(values):
    out = []
    for v in values:
        try:
            out.append(int(v))
        except ValueError:
            try:
                out.append(float(v))
            except ValueError:
                out.append(v)
    return out


@weather_skill(
    name="concat",
    version=_SKILL_VERSION,
)
@weather_skill.argument("-i", "--input", type=Dataset("any"), action="append", required=True)
@weather_skill.argument("--dim", required=True)
@weather_skill.argument("--coords", help="Comma-separated coord values for the new dim")
def concat(ds, dim, coords, **kwargs):
    """Concatenate Zarr stores along a named dim."""
    import xarray as xr

    # Repeat -i/--input per store. nargs="+" would treat a second -i as a
    # replacement, so only the last input would be written.
    if not isinstance(ds, (list, tuple)) or len(ds) < 2:
        n = 1 if not isinstance(ds, (list, tuple)) else len(ds)
        raise UsageError(f"concat requires at least two --input paths (repeat -i), got {n}")
    dss = list(ds)
    units = _check_compatible(dss, dim)
    # Units are equal wherever present (checked above), so concatenate plain
    # magnitudes: pint cannot join a unitless input with a unit-bearing one.
    dss = [dequantify_dataset(d) for d in dss]

    if dim not in dss[0].dims or not all(dim in item.dims for item in dss):
        if coords:
            vals = _coerce([c.strip() for c in coords.split(",")])
            if len(vals) != len(dss):
                raise UsageError(f"--coords len {len(vals)} != inputs {len(dss)}")
            dss = [d.expand_dims({dim: [v]}) for d, v in zip(dss, vals, strict=True)]
        else:
            dss = [d.expand_dims(dim) for d in dss]
    out = xr.concat(dss, dim=dim)
    for name, unit in units.items():
        out[name].attrs["units"] = unit
    return out


if __name__ == "__main__":
    concat()
