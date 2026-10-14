"""Disposable storage and real pinned-parser probes, never scientific execution."""

import hashlib
import json

import numpy as np
import pytest
import xarray as xr

from adapter_support.fixture_staging import stage_fixture


@pytest.fixture
def source(tmp_path):
    values = np.arange(30 * 2 * 2, dtype="float32").reshape(30, 2, 2) / 10
    values[2, 0, 1] = np.nan
    values[:, 1, 1] = np.nan
    ds = xr.Dataset(
        {"precip": (("time", "latitude", "longitude"), values, {"units": "mm day-1"})},
        coords={
            "time": np.arange("2026-01-01", "2026-01-31", dtype="datetime64[D]").astype(
                "datetime64[ns]"
            ),
            "latitude": np.array([1.05, 1.0]),
            "longitude": np.array([30.0, 30.05]),
        },
        attrs={
            "dataset": "chirps",
            "source_lineage": "chirps-fetch",
            "title": "CHIRPS daily precipitation (cached subset)",
        },
    )
    path = tmp_path / "fixture.nc"
    ds.to_netcdf(path, engine="scipy")
    return path


def stage(source, tmp_path, **overrides):
    args = dict(
        source=source,
        destination=tmp_path / "staged.zarr",
        harness_directory=tmp_path / "harness",
        generator_provenance={"synthetic": True, "generator": "unit-test-grid/1", "seed": 17},
        presented_fetch_version="0.0.2",
    )
    args.update(overrides)
    return stage_fixture(**args)


def test_values_coordinates_nan_units_and_source_preserved(source, tmp_path):
    before = source.read_bytes()
    manifest = stage(source, tmp_path)
    with xr.open_dataset(source) as original, xr.open_zarr(tmp_path / "staged.zarr") as result:
        for name in original.variables:
            xr.testing.assert_identical(original[name], result[name])
        assert result.precip.dtype == np.dtype("float32")
        np.testing.assert_array_equal(np.isnan(original.precip), np.isnan(result.precip))
        assert result.precip.attrs["units"] == "mm day-1"
        assert "synthetic" not in result.attrs
        assert "generator" not in result.attrs
        assert "seed" not in result.attrs
        assert result.attrs["weather_skills_source"] == "chirps"
    assert source.read_bytes() == before
    assert manifest["source"]["sha256"] == hashlib.sha256(before).hexdigest()
    assert manifest["tag"] == manifest["output"]["tag"] == "synthetic"
    assert manifest["actual_fetch_executed"] is False
    assert manifest["generator_provenance"]["generator"] == "unit-test-grid/1"
    assert json.loads((tmp_path / "harness/manifest.json").read_text()) == manifest
    assert not (tmp_path / "staged.zarr/manifest.json").exists()


@pytest.mark.parametrize("existing", ["staged.zarr", "harness"])
def test_existing_destinations_refused_without_changes(source, tmp_path, existing):
    occupied = tmp_path / existing
    occupied.mkdir()
    marker = occupied / "keep.txt"
    marker.write_text("unchanged")
    with pytest.raises(FileExistsError):
        stage(source, tmp_path)
    assert marker.read_text() == "unchanged"
    other = "harness" if existing == "staged.zarr" else "staged.zarr"
    assert not (tmp_path / other).exists()


def test_repeat_staging_refuses_and_preserves_output(source, tmp_path):
    first = stage(source, tmp_path)
    before = {str(p): p.read_bytes() for p in (tmp_path / "staged.zarr").rglob("*") if p.is_file()}
    with pytest.raises(FileExistsError):
        stage(source, tmp_path)
    assert before == {
        str(p): p.read_bytes() for p in (tmp_path / "staged.zarr").rglob("*") if p.is_file()
    }
    assert json.loads((tmp_path / "harness/manifest.json").read_text()) == first


@pytest.mark.parametrize("reverse", [False, True])
def test_nested_harness_namespace_refused(source, tmp_path, reverse):
    outer, inner = tmp_path / "new", tmp_path / "new/inside"
    destination, harness = (inner, outer) if reverse else (outer, inner)
    with pytest.raises(ValueError, match="disjoint"):
        stage(source, tmp_path, destination=destination, harness_directory=harness)
    assert not outer.exists()


def test_requires_explicit_truthful_generator_provenance(source, tmp_path):
    with pytest.raises(ValueError, match="synthetic generator"):
        stage(source, tmp_path, generator_provenance={"synthetic": False})
    assert not (tmp_path / "staged.zarr").exists()


def test_units_not_silently_converted(source, tmp_path):
    with xr.open_dataset(source) as ds:
        changed = ds.load()
    changed.precip.attrs["units"] = "mm"
    changed.to_netcdf(source, engine="scipy", mode="w")
    stage(source, tmp_path)
    with xr.open_zarr(tmp_path / "staged.zarr") as result:
        assert result.precip.attrs["units"] == "mm"
        np.testing.assert_equal(result.precip.values, changed.precip.values)


def test_real_pinned_history_loader_and_dataset_open(source, tmp_path):
    import importlib.metadata

    from weather_skills_core import Dataset
    from weather_skills_core.decorator import _open_zarr
    from weather_skills_core.provenance import load_history

    distribution = importlib.metadata.distribution("weather-skills-core")
    provenance = json.loads(distribution.read_text("direct_url.json"))
    assert provenance["vcs_info"]["commit_id"] == "a4110e30c8637ea99d79f752499d00e4cd65fafb"
    stage(source, tmp_path)
    path = tmp_path / "staged.zarr"
    history = load_history(path)
    assert len(history) == 1
    assert history[0]["skill"] == "chirps-fetch"
    assert history[0]["input"] is None
    for requirement in ("any", "spatial"):
        dataset_type = Dataset(requirement)
        parsed = dataset_type(str(path))
        assert parsed == path
        opened = _open_zarr(parsed, dataset_type.io_spec)
        try:
            assert dict(opened.sizes) == {"time": 30, "latitude": 2, "longitude": 2}
        finally:
            opened.close()


def test_real_source_inspector_accepts_staged_and_refuses_plain_conversion(source, tmp_path):
    from tools.workflow.contracts import Refused
    from tools.workflow.runner import Controller

    stage(source, tmp_path)
    controller = object.__new__(Controller)
    controller.source = tmp_path / "staged.zarr"
    controller.admission = None
    controller.goal = {
        "start_date": "2026-01-01",
        "end_date": "2026-01-30",
        "bbox": [1.05, 30.0, 1.0, 30.05],
    }
    metadata = controller.inspect_source()
    assert metadata["source"] == "chirps"
    assert metadata["units"] == "mm day-1"
    plain = tmp_path / "plain.zarr"
    with xr.open_dataset(source) as ds:
        ds.load().to_zarr(plain, zarr_format=2, consolidated=True)
    controller.source = plain
    with pytest.raises(Refused, match="production admission"):
        controller.inspect_source()


def test_real_source_inspector_refuses_changed_request_and_wrong_lineage(source, tmp_path):
    import zarr

    from tools.workflow.contracts import Refused
    from tools.workflow.runner import Controller

    stage(source, tmp_path)
    controller = object.__new__(Controller)
    controller.source = tmp_path / "staged.zarr"
    controller.admission = None
    controller.goal = {
        "start_date": "2026-01-02",
        "end_date": "2026-01-30",
        "bbox": [1.05, 30.0, 1.0, 30.05],
    }
    with pytest.raises(Refused, match="dates/cadence"):
        controller.inspect_source()
    controller.goal["start_date"] = "2026-01-01"
    group = zarr.open_group(controller.source, mode="a")
    group.attrs["weather_skills_history"] = json.dumps([{"skill": "other"}])
    zarr.consolidate_metadata(controller.source)
    with pytest.raises(Refused, match="direct cached CHIRPS"):
        controller.inspect_source()
