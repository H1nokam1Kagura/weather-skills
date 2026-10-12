"""Correctness tests for unit-convert."""

from pathlib import Path

import pytest
import xarray as xr
from conftest import load_skill, make_gridded, run_skill, write_zarr
from weather_skills_core.provenance import load_history
from weather_skills_core.units import units_equal


@pytest.fixture(scope="module")
def unit_convert():
    return load_skill("unit-convert", "unit-convert").unit_convert


def test_unit_convert_to_standard(tmp_path, unit_convert):
    src = write_zarr(make_gridded(), tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"

    run_skill(unit_convert, "-i", str(src), "-o", str(out), "--to-standard")

    assert Path(out).exists()
    ds = xr.open_zarr(out, consolidated=True)
    assert units_equal(ds["precip"].attrs["units"], "mm day-1")
    assert load_history(out)[-1]["skill"] == "unit-convert"


def test_unit_convert_rejects_both_targets(tmp_path, unit_convert):
    src = write_zarr(make_gridded(), tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"

    with pytest.raises(SystemExit) as exc:
        run_skill(
            unit_convert,
            "-i",
            str(src),
            "-o",
            str(out),
            "--to-standard",
            "--to-units",
            "mm day-1",
        )
    assert exc.value.code == 2


def test_unit_convert_requires_target(tmp_path, unit_convert):
    src = write_zarr(make_gridded(), tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"

    with pytest.raises(SystemExit) as exc:
        run_skill(unit_convert, "-i", str(src), "-o", str(out))
    assert exc.value.code == 2


def _two_var(tmp_path):
    ds = make_gridded()
    ds["tmax"] = ds["precip"] * 0 + 300.0
    ds["tmax"].attrs = {"units": "K", "standard_name": "air_temperature"}
    return write_zarr(ds, tmp_path / "two.zarr")


def test_unit_convert_to_units_single_var(tmp_path, unit_convert):
    src = write_zarr(make_gridded(), tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"

    run_skill(unit_convert, "-i", str(src), "-o", str(out), "--to-units", "mm/hour")

    ds = xr.open_zarr(out, consolidated=True)
    assert units_equal(ds["precip"].attrs["units"], "mm/hour")
    assert float(ds["precip"].mean()) == pytest.approx(1.0 / 24.0)


def test_unit_convert_to_units_multi_var_requires_variable(tmp_path, unit_convert):
    src = _two_var(tmp_path)
    out = tmp_path / "out.zarr"
    with pytest.raises(SystemExit) as exc:
        run_skill(unit_convert, "-i", str(src), "-o", str(out), "--to-units", "degC")
    assert exc.value.code == 2
    assert not out.exists()


def test_unit_convert_to_units_multi_var_with_variable(tmp_path, unit_convert):
    src = _two_var(tmp_path)
    out = tmp_path / "out.zarr"

    run_skill(unit_convert, "-i", str(src), "-o", str(out), "-v", "tmax", "--to-units", "degC")

    ds = xr.open_zarr(out, consolidated=True)
    assert units_equal(ds["tmax"].attrs["units"], "degC")
    assert float(ds["tmax"].mean()) == pytest.approx(300.0 - 273.15)
    assert units_equal(ds["precip"].attrs["units"], "mm day-1")
    assert float(ds["precip"].mean()) == pytest.approx(1.0)


def test_unit_convert_rejects_unknown_variable(tmp_path, unit_convert):
    src = write_zarr(make_gridded(), tmp_path / "in.zarr")
    with pytest.raises(SystemExit) as exc:
        run_skill(
            unit_convert,
            "-i",
            str(src),
            "-o",
            str(tmp_path / "out.zarr"),
            "-v",
            "nope",
            "--to-units",
            "mm/hour",
        )
    assert exc.value.code == 2


def test_unit_convert_rejects_standard_name_with_to_standard(tmp_path, unit_convert):
    src = write_zarr(make_gridded(), tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"
    with pytest.raises(SystemExit) as exc:
        run_skill(
            unit_convert, "-i", str(src), "-o", str(out), "--to-standard", "--standard-name", "x"
        )
    assert exc.value.code == 2
    assert not out.exists()


def test_unit_convert_standard_name_with_to_units(tmp_path, unit_convert):
    src = write_zarr(make_gridded(), tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"

    run_skill(
        unit_convert,
        "-i",
        str(src),
        "-o",
        str(out),
        "--to-units",
        "mm/hour",
        "--standard-name",
        "custom_rate",
    )

    assert xr.open_zarr(out, consolidated=True)["precip"].attrs["standard_name"] == "custom_rate"


def test_unit_convert_rejects_unparseable_target_units(tmp_path, unit_convert):
    """Was an uncaught pint UndefinedUnitError (exit 1) once --to-units reached core."""
    src = write_zarr(make_gridded(), tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"
    with pytest.raises(SystemExit) as exc:
        run_skill(
            unit_convert, "-i", str(src), "-o", str(out), "--to-units", "definitely_not_a_unit"
        )
    assert exc.value.code == 2
    assert not out.exists()
