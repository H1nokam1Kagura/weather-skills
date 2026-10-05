# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core @ git+https://github.com/rhiza-research/weather-skills-core@dev",
#   "cftime>=1.6",
#   "numpy",
#   "xarray",
# ]
# ///
"""Check a Zarr artifact against fixed physical and structural invariants (exit 0/1/2)."""

import json
import math
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import xarray as xr
from weather_skills_core import DataError, UsageError, weather_skill
from weather_skills_core.units import (
    AGGREGATION_COVERAGE_COORD,
    AGGREGATION_PERIOD_ATTR,
    DATA_INTERVAL_ATTR,
    convert_values,
    units_convertible,
    units_equal,
    units_match,
    ureg,
)

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.0.1"

HISTORY_ATTR = "weather_skills_history"

# ---------------------------------------------------------------------------
# Thresholds. Every number here is documented, with its source, in SKILL.md
# ("Checks and thresholds"); change both together.
# ---------------------------------------------------------------------------

# Units allow-lists per variable family (compared with pint, so spelling
# variants such as "mm/day", "mm d-1" and "mm day-1" are the same unit).
PRECIP_RATE_UNITS = (
    "mm day-1",
    "mm h-1",
    "mm s-1",
    "m s-1",
    "m day-1",
    "kg m-2 s-1",
    "kg m-2 h-1",
    "kg m-2 day-1",
)
PRECIP_AMOUNT_UNITS = ("mm", "m", "kg m-2")
TEMPERATURE_UNITS = ("K", "degree_Celsius")

# Precipitation, in mm/day equivalent.
PRECIP_NEG_TOLERANCE = 1e-3  # below -this is FAIL; (-this, 0) is WARN
PRECIP_DAILY_WARN = 1000.0  # user-facing review ceiling for >= 1-day intervals
PRECIP_DAILY_FAIL = 1825.0  # 24-h world record (Foc-Foc, La Reunion, 1966)
PRECIP_SUBDAILY_FAIL = 500.0 * 24  # 500 mm/h, above every reported 1-h record

# Temperature, degC.
TEMP_MIN_C = -90.0  # lowest measured surface air temperature: -89.2 (Vostok, 1983)
TEMP_MAX_C = 60.0  # highest recognised surface air temperature: 56.7 (Death Valley, 1913)

DEFAULT_MAX_NAN_FRAC = 0.5

# Event-date variables (e.g. onset-date output): share of detected events that fall on the
# FIRST day of the search window. Such an "event" was already under way when the window opened
# (left-censored), so its date is the window start, not an onset. Measured on Kenya CHIRPS
# OND 2025 (agrhymet-sos-rolling): good windows 1.4% and 2.5%; a window opened inside the
# season 17.7% and 41.8%.
CENSORED_WARN_FRAC = 0.05
CENSORED_FAIL_FRAC = 0.25

PRECIP_NAMES = {"tp", "pr", "prcp", "precip", "rain", "rainfall", "precipitation"}
TEMP_NAMES = {"t2m", "tas", "tasmax", "tasmin", "tmax", "tmin", "tavg", "temp", "sst", "skt"}
TEMP_NAMES |= {"d2m", "t", "2m_temperature"}
ANOMALY_SKILLS = {"difference", "standardize-anomaly", "verify"}
ANOMALY_HINTS = ("anom", "diff", "change", "bias", "error")


@dataclass
class Check:
    check_id: str
    subject: str
    status: str  # PASS / FAIL / WARN
    observed: str
    expected: str
    basis: str


def _fmt(value):
    if isinstance(value, float):
        if math.isnan(value):
            return "nan"
        return f"{value:.6g}"
    return str(value)


def _open(path: Path):
    if not path.exists():
        raise UsageError(f"cannot read artifact: {path} does not exist")
    if not path.is_dir():
        raise UsageError(f"cannot read artifact: {path} is not a Zarr directory")
    try:
        return xr.open_zarr(path, consolidated=None)
    except Exception as exc:  # noqa: BLE001 — any open failure is "unreadable"
        raise UsageError(f"cannot read artifact {path}: {type(exc).__name__}: {exc}") from None


def _load_history(ds):
    raw = ds.attrs.get(HISTORY_ATTR)
    if raw is None:
        return None, "absent"
    try:
        chain = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return None, "malformed (not JSON)"
    if not isinstance(chain, list):
        return None, "malformed (not a JSON array)"
    if not chain:
        return None, "empty array"
    return chain, "ok"


def _history_skills(chain):
    """Every skill name in the chain, including nested join parents."""
    names = set()
    stack = list(chain or [])
    while stack:
        entry = stack.pop()
        if not isinstance(entry, dict):
            continue
        if isinstance(entry.get("skill"), str):
            names.add(entry["skill"])
        inputs = entry.get("input")
        for ref in inputs if isinstance(inputs, list) else [inputs]:
            if isinstance(ref, dict) and isinstance(ref.get("history"), list):
                stack.extend(ref["history"])
    return names


def _dimensionless(units):
    try:
        return ureg.Unit(units).dimensionless
    except Exception:  # noqa: BLE001
        return False


def _family(name, da):
    """'precip', 'temperature' or None, from standard_name then the variable name.

    Units are deliberately NOT used to pick the family, so a precip variable
    carrying temperature units is still checked as precip (and fails units).
    A dimensionless variable (an index, a probability) has no physical family.
    """
    units = da.attrs.get("units")
    if isinstance(units, str) and units.strip() and _dimensionless(units.strip()):
        return None
    sn = str(da.attrs.get("standard_name") or "").lower()
    if "precipitation" in sn or "rainfall" in sn:
        return "precip"
    if "temperature" in sn:
        return "temperature"
    key = str(name).lower()
    if key in PRECIP_NAMES or "precip" in key or "rain" in key:
        return "precip"
    if key in TEMP_NAMES or "temperature" in key:
        return "temperature"
    return None


def _is_anomaly(name, da, history_skills):
    text = " ".join(
        str(x).lower() for x in (name, da.attrs.get("long_name"), da.attrs.get("standard_name"))
    )
    if any(h in text for h in ANOMALY_HINTS):
        return f"name/attrs mention one of {ANOMALY_HINTS}"
    hit = sorted(history_skills & ANOMALY_SKILLS)
    if hit:
        return f"history contains {', '.join(hit)}"
    return None


def _duration_days(text):
    try:
        return float(ureg.Quantity(str(text).strip()).to("day").magnitude)
    except Exception:  # noqa: BLE001
        return None


def _time_axis(ds):
    """(label, values as np.ndarray, kind) for the artifact's time-like axis, or None.

    kind is 'valid' (absolute datetimes) or 'lead' (timedeltas, no init).
    A classic forecast (step dim + scalar time init) is realized as init + step.
    """
    if "time" in ds.dims:
        return "time", ds["time"].values, "valid"
    if "step" in ds.dims:
        step = ds["step"].values
        if "time" in ds.coords and ds["time"].ndim == 0:
            init = ds["time"].values
            try:
                return "time + step (valid time)", init + step, "valid"
            except TypeError:
                pass
        return "step", step, "lead"
    return None


def _as_ns(values):
    """int64 nanoseconds for datetime64/timedelta64/cftime arrays."""
    values = np.asarray(values)
    if np.issubdtype(values.dtype, np.datetime64):
        return values.astype("datetime64[ns]").astype(np.int64)
    if np.issubdtype(values.dtype, np.timedelta64):
        return values.astype("timedelta64[ns]").astype(np.int64)
    index = xr.CFTimeIndex(values)  # cftime objects
    return index.asi8 * 1000  # asi8 is microseconds


def _date_to_ns(date, sample):
    sample = np.asarray(sample)
    if np.issubdtype(sample.dtype, np.datetime64):
        return np.datetime64(date.isoformat(), "ns").astype(np.int64)
    first = sample.reshape(-1)[0]
    stamp = type(first)(date.year, date.month, date.day, calendar=first.calendar)
    return int(xr.CFTimeIndex([stamp]).asi8[0]) * 1000


def _show_ns(ns, sample):
    sample = np.asarray(sample).reshape(-1)
    if np.issubdtype(sample.dtype, np.timedelta64):
        return f"{ns / 86400e9:g} day"
    if np.issubdtype(sample.dtype, np.datetime64):
        return str(np.datetime64(int(ns), "ns").astype("datetime64[s]"))
    # cftime: show the closest actual value rather than re-encoding a calendar date.
    return str(sample[int(np.argmin(np.abs(_as_ns(sample) - ns)))])


def _coord(ds, names, standard_name):
    for name in ds.coords:
        if str(ds[name].attrs.get("standard_name", "")).lower() == standard_name:
            return name
    for name in names:
        if name in ds.coords or name in ds.dims:
            return name
    return None


def _spacing(values):
    vals = np.unique(np.asarray(values, dtype=float).reshape(-1))
    vals = vals[np.isfinite(vals)]
    if vals.size < 2:
        return 0.0
    return float(np.median(np.diff(vals)))


def _wrap(lon):
    return ((np.asarray(lon, dtype=float) + 180.0) % 360.0) - 180.0


def _numeric_stats(da):
    """(n, n_nan, min, max) without loading the array twice into memory."""
    n = int(da.size)
    n_nan = int(da.isnull().sum().compute())
    if n_nan == n:
        return n, n_nan, float("nan"), float("nan")
    vmin = float(da.min(skipna=True).compute())
    vmax = float(da.max(skipna=True).compute())
    return n, n_nan, vmin, vmax


def _check_variable(name, da, ds, history_skills, expect_units, max_nan_frac, checks):
    units = da.attrs.get("units")
    units = units.strip() if isinstance(units, str) and units.strip() else None
    family = _family(name, da)

    # units-present
    if units is not None:
        checks.append(Check("units-present", name, "PASS", repr(units), "a units attribute", "CF"))
    elif family is not None:
        checks.append(
            Check(
                "units-present",
                name,
                "FAIL",
                "no units attribute",
                f"units required for a {family} variable",
                "weather-skills-core: precip and temperature are units_required kinds",
            )
        )
    else:
        checks.append(
            Check(
                "units-present",
                name,
                "WARN",
                "no units attribute",
                "a units attribute",
                "unrecognised family: values cannot be range-checked",
            )
        )

    # expect-units
    if expect_units is not None:
        ok = units is not None and units_equal(units, expect_units)
        checks.append(
            Check(
                "expect-units",
                name,
                "PASS" if ok else "FAIL",
                repr(units),
                repr(expect_units),
                "--expect-units (pint equivalence, spelling-independent)",
            )
        )

    # units-allowed (+ which precip sub-kind)
    precip_kind = None
    if family is not None and units is not None:
        if family == "precip":
            if units_match(units, PRECIP_RATE_UNITS):
                precip_kind = "rate"
            elif units_match(units, PRECIP_AMOUNT_UNITS):
                precip_kind = "amount"
            allowed = PRECIP_RATE_UNITS + PRECIP_AMOUNT_UNITS
            ok = precip_kind is not None
        else:
            allowed = TEMPERATURE_UNITS
            ok = units_match(units, TEMPERATURE_UNITS)
        checks.append(
            Check(
                "units-allowed",
                name,
                "PASS" if ok else "FAIL",
                repr(units) + (f" ({precip_kind})" if precip_kind else ""),
                f"{family} units in {list(allowed)}",
                "check-artifact allow-list (SKILL.md: Units allow-list)",
            )
        )

    n, n_nan, vmin, vmax = _numeric_stats(da)
    frac = n_nan / n if n else 1.0

    # nan-fraction
    if n == 0:
        status, basis = "FAIL", "variable has no cells"
    elif n_nan == n:
        status, basis = "FAIL", "all values missing: nothing downstream can use this artifact"
    else:
        status = "PASS" if frac <= max_nan_frac else "FAIL"
        basis = "--max-nan-frac (default 0.5; SKILL.md: NaN fraction)"
    checks.append(
        Check(
            "nan-fraction",
            name,
            status,
            f"{frac:.3f} ({n_nan}/{n} missing)",
            f"<= {max_nan_frac:g}",
            basis,
        )
    )

    # empty-slices: whole time/step slices that are all-NaN
    tdim = "time" if "time" in da.dims else "step" if "step" in da.dims else None
    if tdim is not None and 0 < n_nan < n and da.sizes[tdim] > 1:
        other = [d for d in da.dims if d != tdim]
        empty = da.isnull().all(dim=other) if other else da.isnull()
        n_empty = int(empty.sum().compute())
        if n_empty:
            checks.append(
                Check(
                    "empty-slices",
                    name,
                    "WARN",
                    f"{n_empty} of {da.sizes[tdim]} {tdim} slices are entirely missing",
                    "no all-missing slices",
                    "unpublished leads or a fetch gap; aggregate-temporal stamps them coverage 0",
                )
            )

    # range
    if family is None or units is None or n_nan == n:
        return
    anomaly = _is_anomaly(name, da, history_skills)
    if anomaly:
        checks.append(
            Check(
                "range",
                name,
                "WARN",
                f"min {_fmt(vmin)}, max {_fmt(vmax)} {units}",
                "not checked",
                f"field looks like a difference/anomaly ({anomaly}); "
                "absolute physical bounds do not apply",
            )
        )
        return
    if family == "temperature":
        if not units_match(units, TEMPERATURE_UNITS):
            return  # already failed units-allowed; a range in unknown units means nothing
        lo, hi = convert_values(np.array([vmin, vmax]), units, "degree_Celsius")[0]
        ok = TEMP_MIN_C <= lo and hi <= TEMP_MAX_C
        checks.append(
            Check(
                "range",
                name,
                "PASS" if ok else "FAIL",
                f"min {_fmt(float(lo))}, max {_fmt(float(hi))} degC",
                f"within [{TEMP_MIN_C:g}, {TEMP_MAX_C:g}] degC",
                "surface air temperature records (-89.2 degC Vostok; 56.7 degC Death Valley)",
            )
        )
        return
    if precip_kind is None:
        return
    _check_precip_range(name, da, ds, units, precip_kind, vmin, vmax, checks)


def _check_precip_range(name, da, ds, units, precip_kind, vmin, vmax, checks):
    interval_days = None
    for attr in (DATA_INTERVAL_ATTR, AGGREGATION_PERIOD_ATTR):
        if da.attrs.get(attr):
            interval_days = _duration_days(da.attrs[attr])
            break
    if interval_days is None:
        axis = _time_axis(ds)
        if axis is not None and np.asarray(axis[1]).size >= 2:
            diffs = np.diff(np.unique(_as_ns(axis[1])))
            if diffs.size:
                interval_days = float(np.median(diffs)) / 86400e9
    subdaily = interval_days is not None and interval_days < 1.0

    if precip_kind == "rate":
        lo, hi = convert_values(np.array([vmin, vmax]), units, "mm day-1")[0]
        scale, shown = 1.0, "mm/day"
    else:
        lo, hi = convert_values(np.array([vmin, vmax]), units, "mm")[0]
        period = da.attrs.get(AGGREGATION_PERIOD_ATTR)
        scale = _duration_days(period) if period else None
        shown = "mm"
    lo, hi = float(lo), float(hi)

    # Lower bound.
    if lo < -PRECIP_NEG_TOLERANCE:
        status = "FAIL"
    elif lo < 0:
        status = "WARN"
    else:
        status = "PASS"
    checks.append(
        Check(
            "range-min",
            name,
            status,
            f"min {_fmt(lo)} {shown}",
            f">= 0 (FAIL below -{PRECIP_NEG_TOLERANCE:g}; WARN in between)",
            "precipitation cannot be negative; small negatives are float noise from deaccumulation",
        )
    )

    # Upper bound.
    if precip_kind == "amount" and scale is None:
        checks.append(
            Check(
                "range-max",
                name,
                "WARN",
                f"max {_fmt(hi)} {shown}",
                "not checked",
                "amount with no aggregation_period: the period it totals is unknown",
            )
        )
        return
    per_day = hi / scale if precip_kind == "amount" else hi
    if subdaily:
        fail_at, warn_at = PRECIP_SUBDAILY_FAIL, None
        basis = "sub-daily sampling: 500 mm/h, above every reported 1-hour rainfall record"
    else:
        fail_at, warn_at = PRECIP_DAILY_FAIL, PRECIP_DAILY_WARN
        basis = (
            "FAIL above the 24-h world record (1825 mm); WARN above 1000 mm/day, which only a "
            "handful of station records have ever exceeded"
        )
    if per_day > fail_at:
        status = "FAIL"
    elif warn_at is not None and per_day > warn_at:
        status = "WARN"
    else:
        status = "PASS"
    expected = f"<= {fail_at:g} mm/day equivalent"
    if warn_at is not None:
        expected += f" (WARN above {warn_at:g})"
    observed = f"max {_fmt(hi)} {shown}"
    if precip_kind == "amount":
        observed += f" over {scale:g} day = {_fmt(per_day)} mm/day"
    checks.append(Check("range-max", name, status, observed, expected, basis))


def _check_coverage(ds, checks):
    if AGGREGATION_COVERAGE_COORD not in ds.coords:
        return
    cov = np.asarray(ds[AGGREGATION_COVERAGE_COORD].values, dtype=float).reshape(-1)
    low = int(np.sum(~(cov >= 1.0)))
    status = "PASS" if low == 0 else "WARN"
    checks.append(
        Check(
            "aggregation-coverage",
            AGGREGATION_COVERAGE_COORD,
            status,
            f"{low} of {cov.size} intervals below 1.0 (min {_fmt(float(np.nanmin(cov)))})",
            "every interval complete (1.0)",
            "aggregate-temporal keeps incomplete bins; only convert-to-totals --min-coverage "
            "drops them, and only while the time/step axis is still there",
        )
    )


def _check_time(ds, start_time, end_time, checks):
    axis = _time_axis(ds)
    if axis is None:
        if start_time is not None or end_time is not None:
            checks.append(
                Check(
                    "time-range",
                    "time",
                    "FAIL",
                    "no time or step dimension",
                    "a time axis to compare with --start-time/--end-time",
                    "requested expectation cannot be verified",
                )
            )
        return
    label, values, kind = axis
    values = np.asarray(values).reshape(-1)
    try:
        ns = _as_ns(values)
    except Exception as exc:  # noqa: BLE001
        checks.append(Check("time-monotonic", label, "FAIL", f"unreadable values ({exc})", "", ""))
        return
    diffs = np.diff(ns)
    n_dup = int(np.sum(diffs == 0))
    n_back = int(np.sum(diffs < 0))
    ok = n_dup == 0 and n_back == 0
    checks.append(
        Check(
            "time-monotonic",
            label,
            "PASS" if ok else "FAIL",
            f"{values.size} values, {n_dup} duplicates, {n_back} decreasing steps",
            "strictly increasing, no duplicates",
            "time-ordered skills (aggregate, onset, convert-to-totals) assume it",
        )
    )

    if ok and diffs.size >= 2:
        freq = float(np.median(diffs))
        gaps = np.nonzero(diffs > 1.5 * freq)[0]
        if gaps.size:
            first = gaps[0]
            observed = (
                f"{gaps.size} gap(s) wider than 1.5x the inferred spacing "
                f"({freq / 86400e9:g} day); first after {_show_ns(ns[first], values)}"
            )
            checks.append(
                Check(
                    "time-gaps",
                    label,
                    "WARN",
                    observed,
                    "regular spacing",
                    "spacing inferred as the median step; monthly calendars are tolerated",
                )
            )
        else:
            checks.append(
                Check(
                    "time-gaps",
                    label,
                    "PASS",
                    f"regular, spacing {freq / 86400e9:g} day",
                    "no gap wider than 1.5x the inferred spacing",
                    "median step",
                )
            )

    if start_time is None and end_time is None:
        return
    if kind != "valid":
        checks.append(
            Check(
                "time-range",
                label,
                "FAIL",
                "lead-time axis with no init date",
                "absolute valid times",
                "run step-to-time first, or check a store that carries its init",
            )
        )
        return
    tol = float(np.median(diffs)) if diffs.size else 0.0
    first, last = int(ns.min()), int(ns.max())
    problems = []
    if start_time is not None:
        s = _date_to_ns(start_time, values)
        if first > s + tol:
            problems.append("starts after --start-time")
        if first < s - tol:
            problems.append("starts before --start-time")
    if end_time is not None:
        e = _date_to_ns(end_time, values)
        if last < e - tol:
            problems.append("ends before --end-time")
        if last > e + tol:
            problems.append("ends after --end-time")
    expected = f"{start_time or '...'} .. {end_time or '...'} (+/- one step)"
    checks.append(
        Check(
            "time-range",
            label,
            "FAIL" if problems else "PASS",
            f"{_show_ns(first, values)} .. {_show_ns(last, values)}"
            + (f" ({'; '.join(problems)})" if problems else ""),
            expected,
            "--start-time/--end-time; tolerance one inferred step (left-labelled bins)",
        )
    )


def _check_bbox(ds, bbox, checks):
    north, west, south, east = bbox
    lat = _coord(ds, ("latitude", "lat"), "latitude")
    lon = _coord(ds, ("longitude", "lon"), "longitude")
    expected = f"inside {north:g}/{west:g}/{south:g}/{east:g} (N/W/S/E) +/- one grid cell"
    if lat is None or lon is None:
        checks.append(
            Check(
                "bbox",
                "latitude/longitude",
                "FAIL",
                "no latitude/longitude coordinate",
                expected,
                "requested expectation cannot be verified",
            )
        )
        return
    lats = np.asarray(ds[lat].values, dtype=float).reshape(-1)
    lons = _wrap(ds[lon].values).reshape(-1)
    dlat, dlon = abs(_spacing(lats)), abs(_spacing(lons))
    w, e = float(_wrap(west)), float(_wrap(east))
    eps = 1e-6
    lat_ok = lats.min() >= south - dlat - eps and lats.max() <= north + dlat + eps
    if w <= e:
        lon_in = (lons >= w - dlon - eps) & (lons <= e + dlon + eps)
    else:  # antimeridian box
        lon_in = (lons >= w - dlon - eps) | (lons <= e + dlon + eps)
    lon_ok = bool(np.all(lon_in))
    observed = (
        f"lat {lats.min():g}..{lats.max():g}, lon {lons.min():g}..{lons.max():g} "
        f"(cell {dlat:g} x {dlon:g} deg)"
    )
    status = "PASS" if lat_ok and lon_ok else "FAIL"
    basis = "--bbox; one-cell tolerance for cell-centre vs edge clipping"
    if status == "FAIL" and not lon_ok:
        basis += "; a W/E swap selects the complement of the box (antimeridian rule)"
    checks.append(Check("bbox", f"{lat}/{lon}", status, observed, expected, basis))

    if status == "PASS":
        short = []
        if lats.max() < north - dlat - eps or lats.min() > south + dlat + eps:
            short.append("latitude")
        if w <= e and (lons.min() > w + dlon + eps or lons.max() < e - dlon - eps):
            short.append("longitude")
        if short:
            checks.append(
                Check(
                    "bbox-coverage",
                    f"{lat}/{lon}",
                    "WARN",
                    observed,
                    "reaches every edge of the box within one cell",
                    f"{' and '.join(short)} extent stops short of the box "
                    "(over-clipped, or a source grid smaller than the box)",
                )
            )


def _history_window(chain):
    """(start, end) as datetime.date from the first history step that records start_time/end_time
    (the fetch that bounded the series), or (None, None)."""
    import datetime as _dt

    for entry in chain or []:
        args = entry.get("args") if isinstance(entry, dict) else None
        if not isinstance(args, dict):
            continue
        s, e = args.get("start_time"), args.get("end_time")
        if s or e:
            try:
                return (
                    _dt.date.fromisoformat(str(s)[:10]) if s else None,
                    _dt.date.fromisoformat(str(e)[:10]) if e else None,
                )
            except ValueError:
                return None, None
    return None, None


def _check_dates(name, da, start, end, source, checks):
    """Checks for an event-date variable (datetime64): missing share, window, left-censoring."""
    vals = np.asarray(da.values).astype("datetime64[D]").reshape(-1)
    n = vals.size
    hit = vals[~np.isnat(vals)]
    checks.append(
        Check(
            "event-found",
            name,
            "WARN" if hit.size == 0 else "PASS",
            f"{hit.size} of {n} cells have a date ({n - hit.size} without: no event, or no data)",
            "at least one detected event",
            "NaT means no event in the window (a valid result, unlike missing data); "
            "none at all usually means the window missed the season",
        )
    )
    if hit.size == 0:
        return
    if start is None and end is None:
        checks.append(
            Check(
                "date-window",
                name,
                "WARN",
                f"{hit.min()} .. {hit.max()}",
                "a known search window",
                "no --start-time/--end-time and none recorded in the history: "
                "window and left-censoring not checked",
            )
        )
        return
    s = np.datetime64(start.isoformat(), "D") if start else None
    e = np.datetime64(end.isoformat(), "D") if end else None
    outside = 0
    if s is not None:
        outside += int(np.sum(hit < s))
    if e is not None:
        outside += int(np.sum(hit > e))
    checks.append(
        Check(
            "date-window",
            name,
            "FAIL" if outside else "PASS",
            f"{hit.min()} .. {hit.max()} ({outside} outside)",
            f"inside {start or '...'} .. {end or '...'}",
            f"an event date cannot fall outside the series it was detected in (window from {source})",
        )
    )
    if s is None:
        return
    frac = float(np.mean(hit == s))
    if frac > CENSORED_FAIL_FRAC:
        status = "FAIL"
    elif frac > CENSORED_WARN_FRAC:
        status = "WARN"
    else:
        status = "PASS"
    checks.append(
        Check(
            "date-censored",
            name,
            status,
            f"{frac:.1%} of events on the window's first day ({start})",
            f"<= {CENSORED_WARN_FRAC:.0%} (WARN above; FAIL above {CENSORED_FAIL_FRAC:.0%})",
            "an event on day one was already under way when the window opened, so its date is "
            "the window start, not an onset; start the series earlier or restrict the area",
        )
    )


def _check_provenance(chain, state, checks):
    if chain:
        last = chain[-1].get("skill") if isinstance(chain[-1], dict) else None
        checks.append(
            Check(
                "provenance",
                HISTORY_ATTR,
                "PASS",
                f"{len(chain)} step(s); last: {last}",
                "non-empty history",
                "every catalog skill stamps it",
            )
        )
    else:
        checks.append(
            Check(
                "provenance",
                HISTORY_ATTR,
                "WARN",
                state,
                "non-empty history",
                "not written by a catalog skill, or written by hand-made code: lineage unknown",
            )
        )


def _render(path, variables, skipped, checks):
    lines = [f"check-artifact: {path}", f"variables checked: {', '.join(variables)}"]
    if skipped:
        lines.append(f"not checked: {', '.join(skipped)}")
    lines.append("")
    for c in checks:
        lines.append(f"[{c.status}] {c.check_id} ({c.subject})")
        lines.append(f"    observed: {c.observed}")
        if c.expected:
            lines.append(f"    expected: {c.expected}")
        if c.basis:
            lines.append(f"    basis:    {c.basis}")
    counts = {s: sum(c.status == s for c in checks) for s in ("FAIL", "WARN", "PASS")}
    verdict = "FAIL" if counts["FAIL"] else "PASS"
    lines.append("")
    lines.append(
        f"RESULT: {verdict} - {counts['FAIL']} FAIL, {counts['WARN']} WARN, "
        f"{counts['PASS']} PASS (exit {1 if counts['FAIL'] else 0})"
    )
    return "\n".join(lines), counts


@weather_skill(
    name="check-artifact",
    version=_SKILL_VERSION,
    output=False,
)
@weather_skill.argument(
    "-i",
    "--input",
    required=True,
    help="Zarr artifact to check (opened read-only; unreadable exits 2).",
)
@weather_skill.argument(
    "--variable",
    "-v",
    action="append",
    help="Data variable to check (repeatable). Default: every numeric data variable.",
)
@weather_skill.argument(
    "--bbox",
    default=None,
    help="Expected extent: every lat/lon must lie inside it, within one grid cell.",
)
@weather_skill.argument(
    "--start-time",
    default=None,
    help="Expected first valid time (within one step).",
)
@weather_skill.argument(
    "--end-time",
    default=None,
    help="Expected last valid time (within one step).",
)
@weather_skill.argument(
    "--expect-units",
    default=None,
    help="Units every checked variable must carry (pint-equivalent spelling accepted).",
)
@weather_skill.argument(
    "--max-nan-frac",
    type=float,
    default=DEFAULT_MAX_NAN_FRAC,
    help="Maximum fraction of missing cells per variable (0-1). Default 0.5.",
)
def check_artifact(
    input, variable, bbox, start_time, end_time, expect_units, max_nan_frac, **kwargs
):
    """Check a Zarr artifact against fixed physical and structural invariants."""
    if not 0.0 <= max_nan_frac <= 1.0:
        raise UsageError("--max-nan-frac must be between 0 and 1")
    if bbox is not None and bbox[0] < bbox[2]:
        raise UsageError(
            f"--bbox north {bbox[0]:g} is south of south {bbox[2]:g}; the order is N/W/S/E"
        )
    if expect_units is not None and not units_convertible(expect_units, expect_units):
        raise UsageError(f"--expect-units {expect_units!r} is not a recognised unit")

    path = Path(input)
    ds = _open(path)

    numeric, dated, skipped = [], [], []
    for name, da in ds.data_vars.items():
        if np.issubdtype(da.dtype, np.number) and not np.issubdtype(da.dtype, np.timedelta64):
            numeric.append(name)
        elif np.issubdtype(da.dtype, np.datetime64):
            dated.append(name)
        else:
            skipped.append(f"{name} ({da.dtype})")
    if variable:
        missing = [v for v in variable if v not in ds.data_vars]
        if missing:
            raise UsageError(
                f"--variable {', '.join(missing)} not in artifact (have {list(ds.data_vars)})"
            )
        uncheckable = [v for v in variable if v not in numeric and v not in dated]
        if uncheckable:
            raise UsageError(f"no checkable values in {', '.join(uncheckable)} (not numeric or dates)")
        selected = [v for v in dict.fromkeys(variable) if v in numeric]
        selected_dates = [v for v in dict.fromkeys(variable) if v in dated]
    else:
        selected, selected_dates = numeric, dated
    if not selected and not selected_dates:
        raise UsageError(
            f"no checkable variable in {path}: no numeric or date data variables"
            + (f" (found {', '.join(skipped)})" if skipped else "")
            + "; absence of a check is not a pass"
        )

    chain, state = _load_history(ds)
    history_skills = _history_skills(chain)
    checks: list[Check] = []
    for name in selected:
        _check_variable(name, ds[name], ds, history_skills, expect_units, max_nan_frac, checks)
    if selected_dates:
        if start_time is not None or end_time is not None:
            win, source = (start_time, end_time), "--start-time/--end-time"
        else:
            win, source = _history_window(chain), "the fetch step in the history"
        for name in selected_dates:
            _check_dates(name, ds[name], win[0], win[1], source, checks)
    _check_coverage(ds, checks)
    if _time_axis(ds) is not None or not selected_dates:
        # An event-date artifact has no time axis; its window was checked on the dates above.
        _check_time(ds, start_time, end_time, checks)
    if bbox is not None:
        _check_bbox(ds, bbox, checks)
    _check_provenance(chain, state, checks)

    text, counts = _render(path, selected + selected_dates, skipped, checks)
    print(text)
    sys.stdout.flush()
    if counts["FAIL"]:
        raise DataError(f"check-artifact: {counts['FAIL']} check(s) failed on {path}", prefix=False)


if __name__ == "__main__":
    check_artifact()
