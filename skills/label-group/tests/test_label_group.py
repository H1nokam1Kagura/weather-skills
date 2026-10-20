"""Correctness tests for label-group."""

import json

import numpy as np
import pytest
import xarray as xr
from conftest import load_skill, make_gridded, make_point_obs, run_skill, write_zarr
from weather_skills_core import region as region_mod
from weather_skills_core.provenance import load_history


@pytest.fixture(scope="module")
def label_group():
    return load_skill("label-group", "label_group").label_group


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    def offline(iso3, level):
        raise region_mod._AdminMissing("offline test")

    region_mod._admin_collection.cache_clear()
    monkeypatch.setattr("weather_skills_core.region._load_admin_geojson", offline)


def _box(name, west, south, east, north):
    ring = [[west, south], [east, south], [east, north], [west, north], [west, south]]
    return {
        "type": "Feature",
        "properties": {"basin": name},
        "geometry": {"type": "Polygon", "coordinates": [ring]},
    }


def _geojson(tmp_path, *features):
    path = tmp_path / "zones.geojson"
    path.write_text(json.dumps({"type": "FeatureCollection", "features": list(features)}))
    return str(path)


def test_geojson_labels_grid_by_cell_centre(tmp_path, label_group):
    src = write_zarr(make_gridded(), tmp_path / "in.zarr")
    zones = _geojson(
        tmp_path, _box("west", 9.5, 0.5, 11.5, 3.5), _box("east", 11.5, 0.5, 12.5, 2.5)
    )
    out = tmp_path / "out.zarr"

    run_skill(
        label_group, "-i", str(src), "-o", str(out), "--geojson", zones, "--property", "basin"
    )

    ds = xr.open_zarr(out, consolidated=True)
    assert ds["basin"].dims == ("latitude", "longitude")
    # Rows are lat 1, 2, 3; columns lon 10..13.
    assert ds["basin"].values.tolist() == [
        ["west", "west", "east", ""],
        ["west", "west", "east", ""],
        ["west", "west", "", ""],
    ]
    assert ds["precip"].values.tolist() == make_gridded()["precip"].values.tolist()
    assert load_history(out)[-1]["skill"] == "label-group"


def test_geojson_labels_stations(tmp_path, label_group):
    src = write_zarr(make_point_obs(n_points=2), tmp_path / "in.zarr")
    zones = _geojson(tmp_path, _box("north", 10.5, 1.5, 11.5, 2.5))
    out = tmp_path / "out.zarr"

    run_skill(
        label_group, "-i", str(src), "-o", str(out), "--geojson", zones, "--property", "basin"
    )

    ds = xr.open_zarr(out, consolidated=True)
    assert ds["basin"].dims == ("point_id",)
    assert ds["basin"].values.tolist() == ["", "north"]


def test_level_subregion_uses_resolve_region_keys(tmp_path, label_group):
    # 0.0/38.0 is in Kenya; 10.0/8.0 is in Nigeria; 0.0/-30.0 is open Atlantic.
    ds = make_gridded(lats=(0.0, 10.0), lons=(-30.0, 8.0, 38.0))
    src = write_zarr(ds, tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"

    run_skill(label_group, "-i", str(src), "-o", str(out), "--level", "subregion")

    labels = xr.open_zarr(out, consolidated=True)["subregion"]
    assert labels.sel(latitude=0.0, longitude=38.0).item() == "eastern_africa"
    assert labels.sel(latitude=10.0, longitude=8.0).item() == "western_africa"
    assert labels.sel(latitude=0.0, longitude=-30.0).item() == ""
    names = xr.open_zarr(out, consolidated=True)["subregion_name"]
    assert names.sel(latitude=0.0, longitude=38.0).item() == "Eastern Africa"


def test_time_parts_and_month_groups(tmp_path, label_group):
    ds = make_gridded(n_time=365, start="2023-07-01")
    src = write_zarr(ds, tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"

    run_skill(
        label_group,
        "-i",
        str(src),
        "-o",
        str(out),
        "--time",
        "month",
        "--time",
        "year",
        "--time",
        "DJF,MAM",
    )

    ds = xr.open_zarr(out, consolidated=True)
    at = ds.sel(time=["2023-07-01", "2023-12-15", "2024-01-15", "2024-02-29", "2024-04-01"])
    assert at["month"].values.tolist() == [7, 12, 1, 2, 4]
    assert at["month_group"].values.tolist() == ["", "DJF", "DJF", "DJF", "MAM"]
    # DJF takes the year it begins.
    assert at["year"].values.tolist() == [2023, 2023, 2023, 2023, 2024]


@pytest.mark.parametrize(
    ("spec", "message"),
    [("J", "more than one"), ("MAX", "no run"), ("MAM,AMJ", "share a month")],
)
def test_bad_month_groups(tmp_path, label_group, spec, message, capsys):
    src = write_zarr(make_gridded(), tmp_path / "in.zarr")
    with pytest.raises(SystemExit):
        run_skill(label_group, "-i", str(src), "-o", str(tmp_path / "o.zarr"), "--time", spec)
    assert message in capsys.readouterr().err


def test_level_and_geojson_are_exclusive(tmp_path, label_group, capsys):
    src = write_zarr(make_gridded(), tmp_path / "in.zarr")
    zones = _geojson(tmp_path, _box("a", 0, 0, 1, 1))
    with pytest.raises(SystemExit):
        run_skill(
            label_group,
            "-i",
            str(src),
            "-o",
            str(tmp_path / "o.zarr"),
            "--level",
            "country",
            "--geojson",
            zones,
            "--property",
            "basin",
        )
    assert "not both" in capsys.readouterr().err


def test_unit_without_cells_is_reported(tmp_path, label_group, capsys):
    src = write_zarr(make_gridded(), tmp_path / "in.zarr")
    zones = _geojson(tmp_path, _box("tiny", 10.1, 1.1, 10.2, 1.2), _box("all", 9, 0, 14, 4))
    out = tmp_path / "out.zarr"

    run_skill(
        label_group, "-i", str(src), "-o", str(out), "--geojson", zones, "--property", "basin"
    )

    assert "tiny" in capsys.readouterr().err
    assert set(np.unique(xr.open_zarr(out, consolidated=True)["basin"].values)) == {"all"}
