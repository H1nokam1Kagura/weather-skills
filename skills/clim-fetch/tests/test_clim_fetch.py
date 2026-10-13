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
