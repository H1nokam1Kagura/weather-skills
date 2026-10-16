"""Synthetic integration regressions for short harvest-weather windows.

These execute real skills; they do not establish crop maturity or forecast skill.
"""

import numpy as np
import pytest
import xarray as xr
from conftest import load_skill, make_gridded, run_skill, write_zarr


def _totals(tmp_path, label, days, offset, samples=20):
    ds = make_gridded(n_time=samples, fill=0.0)
    # A changing signal catches a wrong time bin as well as a wrong multiplier.
    ds.precip.values[:] = np.arange(samples)[:, None, None] + offset
    source = write_zarr(ds, tmp_path / f"{label}-daily.zarr")
    rate, total = (tmp_path / f"{label}-{suffix}.zarr" for suffix in ("rate", "total"))
    run_skill(
        load_skill("aggregate-temporal", "aggregate").aggregate,
        "-i",
        str(source),
        "-o",
        str(rate),
        "--period",
        f"{days} day",
    )
    run_skill(
        load_skill("convert-to-totals", "convert_to_totals").convert_to_totals,
        "-i",
        str(rate),
        "-o",
        str(total),
    )
    return total


@pytest.mark.parametrize("days", [2, 5, 10])
def test_complete_window_pipeline(tmp_path, days):
    forecast = _totals(tmp_path, "forecast", days, 0.5)
    obs = _totals(tmp_path, "obs", days, 0.0)
    out = tmp_path / "bias.zarr"
    run_skill(
        load_skill("verify", "verify").verify,
        "--forecast",
        str(forecast),
        "--obs",
        str(obs),
        "--metric",
        "bias",
        "-o",
        str(out),
    )
    with xr.open_zarr(obs, chunks=None) as truth, xr.open_zarr(out, chunks=None) as result:
        expected = np.arange(20).reshape(-1, days).sum(axis=1)
        np.testing.assert_allclose(truth.precip[:, 0, 0], expected)
        np.testing.assert_allclose(result.bias, days * 0.5)
        assert result.sizes["time"] == 20 // days
        assert truth.precip.attrs["units"] == "mm"


@pytest.mark.parametrize("days", [2, 5, 10])
def test_incomplete_tail_is_not_scored_as_full_window(tmp_path, days):
    total = _totals(tmp_path, "partial", days, 0, samples=19)
    with xr.open_zarr(total, chunks=None) as ds:
        assert ds.sizes["time"] == 19 // days
        expected = np.arange((19 // days) * days).reshape(-1, days).sum(axis=1)
        np.testing.assert_allclose(ds.precip[:, 0, 0], expected)


@pytest.mark.parametrize("other_period", ["5 day", None])
def test_verification_rejects_incompatible_window_metadata(tmp_path, other_period):
    forecast = make_gridded()
    obs = make_gridded()
    for ds in (forecast, obs):
        ds.precip.attrs["units"] = "mm"
    forecast.precip.attrs["aggregation_period"] = "2 day"
    if other_period is not None:
        obs.precip.attrs["aggregation_period"] = other_period
    fc = write_zarr(forecast, tmp_path / "forecast.zarr")
    truth = write_zarr(obs, tmp_path / "obs.zarr")
    with pytest.raises(SystemExit) as error:
        run_skill(
            load_skill("verify", "verify").verify,
            "--forecast",
            str(fc),
            "--obs",
            str(truth),
            "--metric",
            "bias",
            "-o",
            str(tmp_path / "invalid-score.zarr"),
        )
    assert error.value.code == 2
    assert not (tmp_path / "invalid-score.zarr").exists()


def test_verification_accepts_equivalent_window_units(tmp_path):
    forecast = make_gridded()
    obs = make_gridded()
    forecast.precip.attrs["aggregation_period"] = "2 day"
    obs.precip.attrs["aggregation_period"] = "48 hour"
    fc = write_zarr(forecast, tmp_path / "forecast.zarr")
    truth = write_zarr(obs, tmp_path / "obs.zarr")
    out = tmp_path / "score.zarr"
    run_skill(
        load_skill("verify", "verify").verify,
        "--forecast",
        str(fc),
        "--obs",
        str(truth),
        "--metric",
        "bias",
        "-o",
        str(out),
    )
    with xr.open_zarr(out, chunks=None) as result:
        np.testing.assert_allclose(result.bias, 0)
