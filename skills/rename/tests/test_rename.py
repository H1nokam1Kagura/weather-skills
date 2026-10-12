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


def _two_var(tmp_path):
    ds = make_gridded()
    ds["tmax"] = ds["precip"] * 0 + 300.0
    ds["tmax"].attrs = {"units": "K", "standard_name": "air_temperature"}
    return write_zarr(ds, tmp_path / "two.zarr")


def test_rename_multi_var_requires_variable(tmp_path, rename):
    src = _two_var(tmp_path)
    out = tmp_path / "out.zarr"
    with pytest.raises(SystemExit) as exc:
        run_skill(rename, "-i", str(src), "-o", str(out), "--to-name", "rain")
    assert exc.value.code == 2
    assert not out.exists()


def test_rename_multi_var_with_variable(tmp_path, rename):
    src = _two_var(tmp_path)
    out = tmp_path / "out.zarr"
    run_skill(rename, "-i", str(src), "-o", str(out), "-v", "tmax", "--to-name", "t2m")
    ds = xr.open_zarr(out, consolidated=True)
    assert sorted(ds.data_vars) == ["precip", "t2m"]


@pytest.mark.parametrize("variable", ["latitude", "time", "nope"])
def test_rename_rejects_non_data_variable(tmp_path, rename, variable):
    src = write_zarr(make_gridded(), tmp_path / "in.zarr")
    with pytest.raises(SystemExit) as exc:
        run_skill(
            rename, "-i", str(src), "-o", str(tmp_path / "o.zarr"), "-v", variable, "--to-name", "x"
        )
    assert exc.value.code == 2


@pytest.mark.parametrize("to_name", ["tmax", "latitude"])
def test_rename_rejects_clashing_name(tmp_path, rename, to_name):
    src = _two_var(tmp_path)
    with pytest.raises(SystemExit) as exc:
        run_skill(
            rename,
            "-i",
            str(src),
            "-o",
            str(tmp_path / "o.zarr"),
            "-v",
            "precip",
            "--to-name",
            to_name,
        )
    assert exc.value.code == 2


def test_rename_same_name_is_noop(tmp_path, rename):
    src = write_zarr(make_gridded(), tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"
    run_skill(rename, "-i", str(src), "-o", str(out), "-v", "precip", "--to-name", "precip")
    assert "precip" in xr.open_zarr(out, consolidated=True).data_vars
