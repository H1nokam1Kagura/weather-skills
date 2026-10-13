"""Correctness tests for summarize-dim."""

from pathlib import Path

import numpy as np
import pytest
import xarray as xr
from conftest import load_skill, make_gridded, run_skill, write_zarr
from weather_skills_core.provenance import load_history


@pytest.fixture(scope="module")
def summarize_dim():
    return load_skill("summarize-dim", "summarize_dim").summarize_dim


def test_mean_collapses_time(tmp_path, summarize_dim):
    src = write_zarr(make_gridded(n_time=3, fill=2.0), tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"

    run_skill(summarize_dim, "-i", str(src), "-o", str(out), "--dim", "time", "--method", "mean")

    assert Path(out).exists()
    ds = xr.open_zarr(out, consolidated=True)
    assert "time" not in ds.dims
    assert ds["precip"].values == pytest.approx(2.0)
    assert load_history(out)[-1]["skill"] == "summarize-dim"


def test_sum_collapses_longitude(tmp_path, summarize_dim):
    src = write_zarr(make_gridded(), tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"

    run_skill(
        summarize_dim, "-i", str(src), "-o", str(out), "--dim", "longitude", "--method", "sum"
    )

    ds = xr.open_zarr(out, consolidated=True)
    assert "longitude" not in ds.dims
    assert ds["precip"].sizes["latitude"] == 3


def test_accepts_precip_totals(tmp_path, summarize_dim):
    ds = make_gridded()
    ds["precip"].attrs.update(
        units="mm",
        standard_name="lwe_thickness_of_precipitation_amount",
        cell_methods="time: sum",
    )
    src = write_zarr(ds, tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"

    run_skill(summarize_dim, "-i", str(src), "-o", str(out), "--dim", "time", "--method", "mean")

    result = xr.open_zarr(out, consolidated=True)
    assert "time" not in result.dims
    assert "sum" in result["precip"].attrs["cell_methods"]


@pytest.mark.parametrize("flag", [["--dim", "tme"], ["--dim", "time", "-v", "precp"]])
def test_rejects_unknown_dim_or_variable(tmp_path, summarize_dim, flag):
    src = write_zarr(make_gridded(), tmp_path / "in.zarr")
    with pytest.raises(SystemExit) as exc:
        run_skill(
            summarize_dim,
            "-i",
            str(src),
            "-o",
            str(tmp_path / "out.zarr"),
            "--method",
            "mean",
            *flag,
        )
    assert exc.value.code == 2


def test_nan_conventions(tmp_path, summarize_dim):
    """std over one sample is NaN (ddof=1); an all-NaN sum is NaN, not 0."""
    one = write_zarr(make_gridded(n_time=1), tmp_path / "one.zarr")
    run_skill(
        summarize_dim,
        "-i",
        str(one),
        "-o",
        str(tmp_path / "std.zarr"),
        "--dim",
        "time",
        "--method",
        "std",
    )
    assert xr.open_zarr(tmp_path / "std.zarr")["precip"].isnull().all()

    empty = make_gridded()
    empty["precip"].values[:] = float("nan")
    src = write_zarr(empty, tmp_path / "nan.zarr")
    run_skill(
        summarize_dim,
        "-i",
        str(src),
        "-o",
        str(tmp_path / "sum.zarr"),
        "--dim",
        "time",
        "--method",
        "sum",
    )
    assert xr.open_zarr(tmp_path / "sum.zarr")["precip"].isnull().all()


def test_median_and_lat_weighted_mean(tmp_path, summarize_dim):
    ds = make_gridded(n_time=4)
    ds["precip"].values[:] = np.arange(4)[:, None, None]
    src = write_zarr(ds, tmp_path / "in.zarr")
    run_skill(
        summarize_dim,
        "-i",
        str(src),
        "-o",
        str(tmp_path / "med.zarr"),
        "--dim",
        "time",
        "--method",
        "median",
    )
    assert xr.open_zarr(tmp_path / "med.zarr")["precip"].values == pytest.approx(1.5)

    # cos(0) = 1, cos(60) = 0.5 -> (0 * 1 + 1 * 0.5) / 1.5
    ds = make_gridded(n_time=1, lats=(0.0, 60.0))
    ds["precip"].values[0, 0, :] = 0.0
    ds["precip"].values[0, 1, :] = 1.0
    src = write_zarr(ds, tmp_path / "lat.zarr")
    run_skill(
        summarize_dim,
        "-i",
        str(src),
        "-o",
        str(tmp_path / "latw.zarr"),
        "--dim",
        "latitude",
        "--method",
        "mean",
        "--lat-weighted",
    )
    assert xr.open_zarr(tmp_path / "latw.zarr")["precip"].values == pytest.approx(1 / 3)


def test_std_relabels_spread(tmp_path, summarize_dim):
    """A spread of precip totals is not labelled 'Total precipitation'."""
    ds = make_gridded(n_time=3, fill=2.0)
    ds["precip"].attrs.update(
        units="mm",
        long_name="Total precipitation",
        standard_name="lwe_thickness_of_precipitation_amount",
        cell_methods="time: sum",
    )
    src = write_zarr(ds, tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"

    run_skill(summarize_dim, "-i", str(src), "-o", str(out), "--dim", "time", "--method", "std")

    attrs = xr.open_zarr(out)["precip"].attrs
    assert attrs["long_name"] == "Standard deviation of Total precipitation across time"
    assert attrs["standard_name"] == "lwe_thickness_of_precipitation_amount"
    assert attrs["units"] == "mm"
    assert attrs["cell_methods"] == "time: sum time: standard_deviation"


def test_mean_keeps_names_and_appends_cell_methods(tmp_path, summarize_dim):
    src = write_zarr(make_gridded(n_time=3), tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"
    run_skill(
        summarize_dim,
        "-i",
        str(src),
        "-o",
        str(out),
        "--dim",
        "latitude",
        "--dim",
        "longitude",
        "--method",
        "mean",
    )
    attrs = xr.open_zarr(out)["precip"].attrs
    assert attrs["standard_name"] == "lwe_precipitation_rate"
    assert attrs["cell_methods"] == "latitude: longitude: mean"
