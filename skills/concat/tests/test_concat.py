"""Correctness tests for concat."""

from pathlib import Path

import numpy as np
import pytest
import xarray as xr
from conftest import load_skill, make_gridded, run_skill, write_zarr
from weather_skills_core.provenance import load_history


@pytest.fixture(scope="module")
def concat():
    return load_skill("concat", "concat").concat


def test_concat_along_new_dim_with_coords(tmp_path, concat):
    d1 = write_zarr(make_gridded(fill=1.0), tmp_path / "d1.zarr")
    d2 = write_zarr(make_gridded(fill=2.0), tmp_path / "d2.zarr")
    out = tmp_path / "out.zarr"

    run_skill(
        concat,
        "-i",
        str(d1),
        "-i",
        str(d2),
        "-o",
        str(out),
        "--dim",
        "number",
        "--coords",
        "0,1",
    )

    assert Path(out).exists()
    ds = xr.open_zarr(out, consolidated=True)
    assert ds.sizes["number"] == 2
    assert list(ds["number"].values) == [0, 1]
    assert float(ds["precip"].isel(number=0).mean()) == pytest.approx(1.0)
    assert float(ds["precip"].isel(number=1).mean()) == pytest.approx(2.0)
    assert load_history(out)[-1]["skill"] == "concat"


def test_concat_along_time(tmp_path, concat):
    d1 = write_zarr(make_gridded(n_time=2, start="2026-01-01"), tmp_path / "d1.zarr")
    d2 = write_zarr(make_gridded(n_time=2, start="2026-01-03"), tmp_path / "d2.zarr")
    out = tmp_path / "out.zarr"

    run_skill(concat, "-i", str(d1), "-i", str(d2), "-o", str(out), "--dim", "time")

    ds = xr.open_zarr(out, consolidated=True)
    assert ds.sizes["time"] == 4
    times = ds["time"].values.astype("datetime64[D]")
    assert times[0] == np.datetime64("2026-01-01")
    assert times[-1] == np.datetime64("2026-01-04")


def test_concat_rejects_single_input(tmp_path, concat):
    src = write_zarr(make_gridded(), tmp_path / "only.zarr")
    with pytest.raises(SystemExit) as exc:
        run_skill(concat, "-i", str(src), "-o", str(tmp_path / "out.zarr"), "--dim", "time")
    assert exc.value.code == 2


def _concat_time(concat, tmp_path, a, b):
    pa = write_zarr(a, tmp_path / "a.zarr")
    pb = write_zarr(b, tmp_path / "b.zarr")
    out = tmp_path / "out.zarr"
    run_skill(concat, "-i", str(pa), "-i", str(pb), "-o", str(out), "--dim", "time")
    return out


def test_concat_rejects_mismatched_units(tmp_path, concat):
    b = make_gridded(fill=2.0, start="2026-01-03")
    b["precip"].attrs["units"] = "m day-1"
    with pytest.raises(SystemExit) as exc:
        _concat_time(concat, tmp_path, make_gridded(), b)
    assert exc.value.code == 2
    assert not (tmp_path / "out.zarr").exists()


def test_concat_accepts_equivalent_unit_spellings(tmp_path, concat):
    b = make_gridded(fill=2.0, start="2026-01-03")
    b["precip"].attrs["units"] = "mm/day"
    out = _concat_time(concat, tmp_path, make_gridded(), b)
    ds = xr.open_zarr(out, consolidated=True)
    assert ds.sizes["time"] == 4
    assert float(ds["precip"].isel(time=-1).mean()) == pytest.approx(2.0)


def test_concat_rejects_different_data_variables(tmp_path, concat):
    b = make_gridded(fill=2.0, start="2026-01-03")
    b["extra"] = b["precip"] * 1
    with pytest.raises(SystemExit) as exc:
        _concat_time(concat, tmp_path, make_gridded(), b)
    assert exc.value.code == 2
    assert not (tmp_path / "out.zarr").exists()


def test_concat_rejects_mismatched_other_dim_coords(tmp_path, concat):
    b = make_gridded(fill=2.0, start="2026-01-03", lats=(1.0, 2.0, 4.0))
    with pytest.raises(SystemExit) as exc:
        _concat_time(concat, tmp_path, make_gridded(), b)
    assert exc.value.code == 2


def test_concat_rejects_mismatched_other_dim_size(tmp_path, concat):
    b = make_gridded(fill=2.0, start="2026-01-03", lons=(10.0, 11.0))
    with pytest.raises(SystemExit) as exc:
        _concat_time(concat, tmp_path, make_gridded(), b)
    assert exc.value.code == 2
