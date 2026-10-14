"""Correctness tests for clim-fetch (mocked GCS)."""

import io
import json
from unittest.mock import patch

import pytest
from conftest import load_skill, run_skill


@pytest.fixture(scope="module")
def mod():
    return load_skill("clim-fetch", "fetch")


def test_missing_pair_lists_available_climatologies(tmp_path, mod, capsys):
    listing = {
        "prefixes": [
            "climatologies/era5_precip_1d.zarr/",
            "climatologies/gefs_tmp2m_1d.zarr/",
            "climatologies/imerg_final_precip_1d.zarr/",
        ]
    }

    def _urlopen(url, timeout=None):
        return io.BytesIO(json.dumps(listing).encode())

    with (
        patch.object(mod, "_open_zarr_or_none", return_value=None),
        patch("urllib.request.urlopen", _urlopen),
        pytest.raises(SystemExit) as exc,
    ):
        run_skill(
            mod.fetch,
            "--dataset",
            "era5",
            "--variable",
            "tmp2m",
            "--start-time",
            "2026-09-01",
            "--end-time",
            "2026-09-02",
            "-o",
            str(tmp_path / "out.zarr"),
        )
    assert exc.value.code != 0
    err = capsys.readouterr().err
    assert "no climatology for --dataset era5 --variable tmp2m" in err
    assert "--dataset gefs --variable tmp2m" in err
    assert "--dataset imerg_final --variable precip" in err


@pytest.mark.parametrize(
    "start, end",
    [("2021-02-27", "2021-03-02"), ("2020-02-28", "2020-03-01"), ("2021-12-30", "2022-01-01")],
)
def test_each_date_gets_its_own_calendar_days_climatology(mod, start, end):
    """Match by month/day in the 1904 cycle: a non-leap Mar 1 is not 1904's Feb 29."""
    import numpy as np
    import pandas as pd
    import xarray as xr

    cycle = pd.date_range("1904-01-01", "1904-12-31", freq="D")
    clim = xr.Dataset(
        {"precip_avg": ("time", (cycle.month * 100 + cycle.day).to_numpy(dtype=float))},
        coords={"time": cycle},
    )
    out = mod._expand_climatology(clim, pd.Timestamp(start), pd.Timestamp(end))
    dates = pd.DatetimeIndex(out["time"].values)
    assert out["precip_avg"].values.tolist() == (dates.month * 100 + dates.day).tolist()
    assert np.isfinite(out["precip_avg"].values).all()
