"""Correctness tests for rename."""

from pathlib import Path

import pytest
import xarray as xr
from conftest import load_skill, make_gridded, run_skill, write_zarr
from weather_skills_core.provenance import load_history


@pytest.fixture(scope="module")
def rename():
    return load_skill("rename", "rename").rename


def test_rename_default_variable_and_stamps_history(tmp_path, rename):
    src = write_zarr(make_gridded(), tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"

    run_skill(rename, "-i", str(src), "-o", str(out), "--to-name", "rain")

    assert Path(out).exists()
    ds = xr.open_zarr(out, consolidated=True)
    assert "rain" in ds.data_vars
    assert "precip" not in ds.data_vars
    assert load_history(out)[-1]["skill"] == "rename"


def test_rename_explicit_variable(tmp_path, rename):
    src = write_zarr(make_gridded(), tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"

    run_skill(rename, "-i", str(src), "-o", str(out), "-v", "precip", "--to-name", "rain")

    ds = xr.open_zarr(out, consolidated=True)
    assert "rain" in ds.data_vars


def test_rename_accepts_precip_totals(tmp_path, rename):
    ds = make_gridded()
    ds["precip"].attrs.update(
        units="mm",
        standard_name="lwe_thickness_of_precipitation_amount",
        cell_methods="time: sum",
    )
    src = write_zarr(ds, tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"

    run_skill(rename, "-i", str(src), "-o", str(out), "--to-name", "rain")

    result = xr.open_zarr(out, consolidated=True)
    assert "rain" in result.data_vars
    assert "sum" in result["rain"].attrs["cell_methods"]


def _two_vars(tmp_path):
    ds = make_gridded()
    ds["t2m"] = ds["precip"].copy(data=ds["precip"].values * 0 + 20.0)
    ds["t2m"].attrs = {"units": "degree_Celsius"}
    return write_zarr(ds, tmp_path / "in.zarr")


def test_rename_keeps_values_attrs_and_other_vars(tmp_path, rename):
    src = _two_vars(tmp_path)
    out = tmp_path / "out.zarr"

    run_skill(rename, "-i", str(src), "-o", str(out), "-v", "precip", "--to-name", "rain")

    before = xr.open_zarr(src, consolidated=True)
    after = xr.open_zarr(out, consolidated=True)
    assert set(after.data_vars) == {"rain", "t2m"}
    xr.testing.assert_identical(after["rain"].rename("precip"), before["precip"])
    xr.testing.assert_identical(after["t2m"], before["t2m"])


@pytest.mark.parametrize(
    "argv",
    [
        # Several data vars and no --variable: must not silently pick the first.
        ["--to-name", "rain"],
        # A coordinate is not a data variable: must not rename lat/lon/time.
        ["-v", "latitude", "--to-name", "y"],
        # Missing variable: clean error, not a ValueError traceback.
        ["-v", "nope", "--to-name", "y"],
        # Clashes with another data variable, a coordinate, or a dimension.
        ["-v", "precip", "--to-name", "t2m"],
        ["-v", "precip", "--to-name", "latitude"],
        ["-v", "precip", "--to-name", "time"],
        # Empty name, and '/' (Zarr writes it as a nested group; the var vanishes).
        ["-v", "precip", "--to-name", ""],
        ["-v", "precip", "--to-name", "a/b"],
    ],
)
def test_rename_usage_errors_exit_2(tmp_path, rename, argv):
    src = _two_vars(tmp_path)
    out = tmp_path / "out.zarr"

    with pytest.raises(SystemExit) as exc:
        run_skill(rename, "-i", str(src), "-o", str(out), *argv)

    assert exc.value.code == 2
    assert not out.exists()


def test_rename_no_data_vars_exits_2(tmp_path, rename):
    src = write_zarr(make_gridded()[[]], tmp_path / "in.zarr")

    with pytest.raises(SystemExit) as exc:
        run_skill(rename, "-i", str(src), "-o", str(tmp_path / "out.zarr"), "--to-name", "x")

    assert exc.value.code == 2


def test_rename_to_same_name_is_noop_with_history(tmp_path, rename):
    src = write_zarr(make_gridded(), tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"

    run_skill(rename, "-i", str(src), "-o", str(out), "-v", "precip", "--to-name", "precip")

    assert list(xr.open_zarr(out, consolidated=True).data_vars) == ["precip"]
    assert load_history(out)[-1]["skill"] == "rename"
