"""Exercise a local catalog store without the optional Dask backend."""

from unittest.mock import patch

import numpy as np
import xarray as xr
from conftest import load_skill, run_skill


def test_catalog_fetch_uses_direct_subset_reads(tmp_path):
    values = np.arange(2 * 3 * 2 * 2, dtype=float).reshape(2, 3, 2, 2)
    source = xr.Dataset(
        {
            "precipitation_surface": (
                ("init_time", "lead_time", "latitude", "longitude"),
                values,
                {"units": "mm day-1"},
            )
        },
        coords={
            "init_time": np.array(["2020-01-01", "2020-01-02"], dtype="datetime64[ns]"),
            "lead_time": np.arange(1, 4).astype("timedelta64[D]"),
            "latitude": [1.0, 2.0],
            "longitude": [10.0, 11.0],
        },
    )
    store = tmp_path / "catalog.zarr"
    source.to_zarr(store, consolidated=True)
    opened = []

    def direct_open(dataset, **kwargs):
        assert kwargs.get("chunks", "auto") is None, "fetch must not require Dask"
        ds = xr.open_zarr(store, **kwargs)
        opened.append(ds)
        return ds

    try:
        with (
            patch("dynamical_catalog.list", return_value=["test-forecast"]),
            patch("dynamical_catalog.open", side_effect=direct_open),
        ):
            run_skill(
                load_skill("dynamical-fetch", "fetch").fetch,
                "--dataset",
                "test-forecast",
                "--date",
                "2020-01-02",
                "--bbox",
                "2/10/1/11",
                "-v",
                "precipitation_surface",
                "-o",
                str(tmp_path / "result.zarr"),
            )
        with xr.open_zarr(tmp_path / "result.zarr", chunks=None) as result:
            np.testing.assert_allclose(result.precipitation_surface, values[1])
            assert result.sizes == {"step": 3, "latitude": 2, "longitude": 2}
            assert result.time.values == np.datetime64("2020-01-02")
    finally:
        for ds in opened:
            ds.close()
