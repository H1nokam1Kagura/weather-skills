"""Correctness tests for day-of-year."""

import numpy as np
import pytest
import xarray as xr
from conftest import load_skill, run_skill, write_zarr


@pytest.fixture(scope="module")
def day_of_year():
    return load_skill("day-of-year", "day_of_year").day_of_year


def _dated_ds(dates, name="onset_date", extra_var=None):
    """A single-gridpoint dataset carrying `dates` (datetime64, NaT allowed)
    as a data variable, for exercising the dayofyear extraction directly."""
    data = np.array(dates, dtype="datetime64[ns]").reshape(1, 1)
    coords = {"latitude": [1.0], "longitude": [10.0]}
    data_vars = {name: (["latitude", "longitude"], data)}
    if extra_var is not None:
        data_vars["mask"] = (["latitude", "longitude"], np.array([[1.0]]))
    ds = xr.Dataset(data_vars, coords=coords)
    ds["latitude"].attrs.update(standard_name="latitude", units="degrees_north", axis="Y")
    ds["longitude"].attrs.update(standard_name="longitude", units="degrees_east", axis="X")
    return ds


def test_dayofyear_extraction(tmp_path, day_of_year):
    src = write_zarr(_dated_ds(["2026-02-15"]), tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"

    run_skill(day_of_year, "-i", str(src), "-o", str(out))

    ds = xr.open_zarr(out, consolidated=True)
    assert "onset_date_dayofyear" in ds.data_vars
    assert "onset_date" not in ds.data_vars
    doy = ds["onset_date_dayofyear"].isel(latitude=0, longitude=0).values
    assert int(doy) == 46  # 2026 is not a leap year; Feb 15 is day 46
    assert ds["onset_date_dayofyear"].attrs["units"] == "1"
    assert ds["onset_date_dayofyear"].attrs["standard_name"] is None


def test_nat_gives_nan(tmp_path, day_of_year):
    src = write_zarr(_dated_ds([np.datetime64("NaT", "ns")]), tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"

    run_skill(day_of_year, "-i", str(src), "-o", str(out))

    ds = xr.open_zarr(out, consolidated=True)
    doy = ds["onset_date_dayofyear"].isel(latitude=0, longitude=0).values
    assert np.isnan(doy)


def test_rejects_non_datetime_variable(tmp_path, day_of_year):
    src = write_zarr(_dated_ds(["2026-02-15"], extra_var=True), tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"

    with pytest.raises(SystemExit) as exc:
        run_skill(day_of_year, "-i", str(src), "-o", str(out), "--variable", "mask")
    assert exc.value.code != 0


def test_variable_selection_and_passthrough(tmp_path, day_of_year):
    src = write_zarr(_dated_ds(["2026-02-15"], extra_var=True), tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"

    run_skill(day_of_year, "-i", str(src), "-o", str(out), "--variable", "onset_date")

    ds = xr.open_zarr(out, consolidated=True)
    assert "mask" in ds.data_vars
    assert ds["mask"].values[0, 0] == 1.0  # untouched passthrough
    assert "onset_date_dayofyear" in ds.data_vars


def test_no_datetime_variable_errors(tmp_path, day_of_year):
    ds = xr.Dataset(
        {"mask": (["latitude", "longitude"], np.array([[1.0]]))},
        coords={"latitude": [1.0], "longitude": [10.0]},
    )
    src = write_zarr(ds, tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"

    with pytest.raises(SystemExit) as exc:
        run_skill(day_of_year, "-i", str(src), "-o", str(out))
    assert exc.value.code != 0


def _member_ds(dates):
    data = np.array(dates, dtype="datetime64[ns]").reshape(len(dates), 1, 1)
    ds = xr.Dataset(
        {"onset_date": (["number", "latitude", "longitude"], data)},
        coords={"number": list(range(len(dates))), "latitude": [1.0], "longitude": [10.0]},
    )
    ds["latitude"].attrs.update(standard_name="latitude", units="degrees_north", axis="Y")
    ds["longitude"].attrs.update(standard_name="longitude", units="degrees_east", axis="X")
    return ds


def test_new_year_wrap_refused_without_since(tmp_path, day_of_year):
    src = write_zarr(_member_ds(["2024-12-28", "2025-01-03"]), tmp_path / "in.zarr")
    with pytest.raises(SystemExit) as exc:
        run_skill(day_of_year, "-i", str(src), "-o", str(tmp_path / "o.zarr"))
    assert exc.value.code != 0


def test_since_gives_wrap_safe_offset(tmp_path, day_of_year):
    src = write_zarr(_member_ds(["2024-12-28", "2025-01-03"]), tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"
    run_skill(day_of_year, "-i", str(src), "-o", str(out), "--since", "2024-12-01")
    v = xr.open_zarr(out)["onset_date_days_since"].values[:, 0, 0]
    assert list(v) == [27.0, 33.0]
    assert float(np.mean(v)) == 30.0  # 31 Dec, not "day 183"


def test_multi_year_climatology_allowed(tmp_path, day_of_year):
    src = write_zarr(_member_ds(["2001-03-10", "2002-03-20", "2003-03-15"]), tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"
    run_skill(day_of_year, "-i", str(src), "-o", str(out))
    assert list(xr.open_zarr(out)["onset_date_dayofyear"].values[:, 0, 0]) == [69, 79, 74]
