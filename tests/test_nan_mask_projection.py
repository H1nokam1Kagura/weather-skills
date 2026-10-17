"""artifacts.onset.nan_mask_matches_source (adapter_support/nan_mask.py, DIAGNOSIS-LIVE-3 1b/2).

The real path end to end: the pinned fixture (container_harness/pinned onset_synth, seed 20251001,
as case_driver.stage) is written and staged exactly as the harness stages it, and the real
onset-date skill runs on it with the argv the released E12-P1-CTL run recorded. Its cell counts
are the released run's gate observation (16534 of 22000 cells dated, 5466 without).
"""

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
import xarray as xr

from adapter_support import evidence_projection as ep
from adapter_support import nan_mask
from adapter_support.fixture_staging import stage_fixture

REPO = Path(__file__).resolve().parents[1]
SEED = 20251001  # container_harness/case_driver.SEED


@pytest.fixture(scope="module")
def pinned(tmp_path_factory):
    root = tmp_path_factory.mktemp("nanmask")
    synth, params = nan_mask.pinned_reading()
    profile = json.loads(
        (REPO / "container_harness/pinned/profiles/cached-onset/profile.json").read_text(
            encoding="utf-8"
        )
    )
    fixture = SimpleNamespace(reference_fixture=profile["reference_fixture"])
    generated = synth.generate(fixture, SEED)
    synth.write(generated, str(root / "gen/fixture-nc"), str(root / "gen/fixture-harness"))
    source = root / "case/source/source.zarr"
    stage_fixture(
        root / "gen/fixture-nc/precip.nc",
        source,
        root / "case/harness",
        generator_provenance={"synthetic": True, "generator": "simkit.fixtures.onset_synth"},
        presented_fetch_version="test",
    )
    artifact = root / "case/run/artifacts/onset.zarr"
    artifact.parent.mkdir(parents=True)
    argv = [
        sys.executable,
        str(REPO / "skills/onset-date/scripts/onset_date.py"),
        "--input",
        str(source),
        "--output",
        str(artifact),
        "--definition-ref",
        "agrhymet-sos-rolling",
        "--variable",
        "precip",
    ]
    done = subprocess.run(argv, capture_output=True, text=True, timeout=600)
    assert done.returncode == 0, done.stderr
    return SimpleNamespace(
        root=root, source=source, artifact=artifact, status=generated["status"], params=params
    )


def _rewrite(src, dst, change):
    with xr.open_zarr(src) as ds:
        ds = change(ds.load())
    for var in ds.variables.values():
        var.encoding = {}
    ds.to_zarr(dst, mode="w-")
    return dst


def test_correct_artifact_is_true_and_the_reference_is_the_pinned_status(pinned):
    value, detail = nan_mask.onset_matches_source(pinned.artifact, pinned.source)
    assert value is True, detail
    reference = int((pinned.status == 2).sum())
    assert detail["reference_missing_cells"] == reference == 2316
    assert detail["artifact_missing_cells"] == 5466  # released gate: 5466 of 22000 without a date
    assert detail["reference_missing_cells_with_a_value"] == 0
    # The reading over the staged source is the pinned fixture's own expected status-2 mask.
    synth, params = nan_mask.pinned_reading()
    ref, _, _ = nan_mask.reference_missing(pinned.source, synth, params)
    assert np.array_equal(ref, pinned.status == 2)


def test_exact_equality_would_fail_a_correct_run(pinned):
    """Why the rule is containment: NaT also means 'no onset', so the masks are never equal."""
    synth, params = nan_mask.pinned_reading()
    ref, lat, lon = nan_mask.reference_missing(pinned.source, synth, params)
    art = nan_mask.artifact_missing(pinned.artifact, lat, lon)
    assert not np.array_equal(ref, art) and bool(art[ref].all())


def test_a_filled_missing_cell_is_false(pinned, tmp_path):
    row, col = np.argwhere(pinned.status == 2)[0]

    def fill(ds):
        values = ds["onset_precip_date"].values.copy()
        values[row, col] = np.datetime64("2025-10-01", "ns")
        ds["onset_precip_date"] = (ds["onset_precip_date"].dims, values)
        return ds

    altered = _rewrite(pinned.artifact, tmp_path / "filled.zarr", fill)
    value, detail = nan_mask.onset_matches_source(altered, pinned.source)
    assert value is False
    assert detail["reference_missing_cells_with_a_value"] == 1


@pytest.mark.parametrize(
    "change, reason",
    [
        (lambda ds: ds.isel(latitude=slice(0, -1)), "grid differs"),
        (lambda ds: ds.assign_coords(longitude=ds.longitude + 0.05), "grid differs"),
        (lambda ds: ds.rename({"onset_precip_date": "something"}), "exactly one onset"),
    ],
)
def test_grid_mismatch_or_no_onset_variable_is_missing(pinned, tmp_path, change, reason):
    altered = _rewrite(pinned.artifact, tmp_path / "altered.zarr", change)
    value, detail = nan_mask.onset_matches_source(altered, pinned.source)
    assert value is None and reason in detail


def test_unreadable_artifact_source_or_pins_is_missing(pinned, tmp_path):
    junk = tmp_path / "junk.zarr"
    junk.mkdir()
    (junk / "values.bin").write_bytes(b"not a zarr store")
    assert nan_mask.onset_matches_source(junk, pinned.source)[0] is None
    assert nan_mask.onset_matches_source(pinned.artifact, tmp_path / "nowhere.zarr")[0] is None
    assert nan_mask.onset_matches_source(pinned.artifact, None)[0] is None
    pins = json.loads(nan_mask.PINS.read_text(encoding="utf-8"))
    for entry in pins["files"]:
        if entry["role"] == "fixture_generator":
            entry["sha256"] = "0" * 64
    bad = tmp_path / "pins.json"
    bad.write_text(json.dumps(pins), encoding="utf-8")
    value, reason = nan_mask.onset_matches_source(pinned.artifact, pinned.source, pins=bad)
    assert value is None and "sha256 mismatch" in reason


def _projection(source, artifact):
    native = SimpleNamespace(
        checkpoint={
            "source": str(source),
            "plan": {"final": ["onset"], "steps": [{"id": "onset", "output": "onset"}]},
        },
        context={
            "plan_validations": [
                {"parser_accepted": True, "argv": {"onset": ["--output", str(artifact)]}}
            ]
        },
    )
    return ep.Projection(native, {})


def test_projection_emits_the_field_or_a_gap_and_leaves_an_absent_artifact_alone(pinned, tmp_path):
    proj = _projection(pinned.source, pinned.artifact)
    onset = proj.artifacts()["onset"]
    assert onset["present"] is True and onset[nan_mask.FIELD] is True
    assert proj.missing == []
    assert any(f"artifacts.onset.{nan_mask.FIELD}" in note for note in proj.notes)

    cropped = _rewrite(
        pinned.artifact, tmp_path / "cropped.zarr", lambda ds: ds.isel(longitude=slice(1, None))
    )
    proj = _projection(pinned.source, cropped)
    onset = proj.artifacts()["onset"]
    assert onset["present"] is True and nan_mask.FIELD not in onset
    (gap,) = proj.missing
    assert gap["path"] == f"artifacts.onset.{nan_mask.FIELD}"
    assert gap["capability"] == nan_mask.CAPABILITY and "grid" in gap["reason"]

    proj = _projection(pinned.source, tmp_path / "absent.zarr")
    assert proj.artifacts() == {"onset": {"present": False, "path": str(tmp_path / "absent.zarr")}}
    assert proj.missing == []


def test_the_pinned_module_is_loaded_without_touching_sys_path(pinned):
    before = list(sys.path)
    nan_mask.pinned_reading()
    assert sys.path == before
