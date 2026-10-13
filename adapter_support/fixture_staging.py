"""Stage an owner-authorized synthetic CHIRPS presentation; never run a fetch.

The caller must keep the separate harness directory outside system-visible inputs.
This is format/provenance adaptation, not admission or scientific validation.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Mapping

import numpy as np
import xarray as xr


def _sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _tree_hash(path: Path) -> str:
    digest = hashlib.sha256()
    for child in sorted(path.rglob("*")):
        if child.is_file():
            digest.update(child.relative_to(path).as_posix().encode())
            digest.update(bytes.fromhex(_sha256(child)))
    return digest.hexdigest()


def _local_path(value: str | Path) -> Path:
    path = Path(value).absolute()
    if str(path).startswith(("\\\\", "//")):
        raise ValueError("network paths are not disposable local staging inputs")
    for ancestor in (path, *path.parents):
        if ancestor.is_symlink() or ancestor.is_junction():
            raise ValueError("symlink/junction paths are not accepted")
    return path.resolve()


def stage_fixture(
    source: str | Path,
    destination: str | Path,
    harness_directory: str | Path,
    *,
    generator_provenance: Mapping,
    presented_fetch_version: str,
) -> dict:
    """Copy decoded NetCDF values to new Zarr v2 and write a separate manifest.

    The presented fetch entry is a scenario label authorized by UC-12, not a
    record that this helper downloaded observations. Provenance must describe
    the generator truthfully. Existing destinations are always refused. Failed
    writes remain incomplete and are never retried in place. Units are retained
    exactly; no physical-unit conversion, resampling or NaN filling occurs.
    """
    source, destination, harness = map(
        _local_path, (source, destination, harness_directory)
    )
    if not source.is_file():
        raise ValueError("source must be a local NetCDF file")
    if destination.exists() or harness.exists():
        raise FileExistsError("staging and harness destinations must both be new")
    paths = (source, destination, harness)
    for index, first in enumerate(paths):
        for second in paths[index + 1 :]:
            if first == second or first.is_relative_to(second) or second.is_relative_to(first):
                raise ValueError("source, staging and harness paths must be disjoint")
    provenance = json.loads(json.dumps(dict(generator_provenance), allow_nan=False))
    if provenance.get("synthetic") is not True or not provenance.get("generator"):
        raise ValueError("truthful synthetic generator provenance is required")
    if not isinstance(presented_fetch_version, str) or not presented_fetch_version.strip():
        raise ValueError("an explicit presented fetch version is required")

    source_hash = _sha256(source)
    with xr.open_dataset(source) as opened:
        original = opened.load()
    if set(original.data_vars) != {"precip"}:
        raise ValueError("fixture must contain only precip")
    if set(original.sizes) != {"time", "latitude", "longitude"}:
        raise ValueError("fixture dimensions must be time, latitude and longitude")
    if original.precip.dims != ("time", "latitude", "longitude"):
        raise ValueError("fixture precip dimensions must retain time/latitude/longitude order")
    for name in ("time", "latitude", "longitude"):
        if name not in original.coords or original[name].dims != (name,):
            raise ValueError(f"fixture requires a one-dimensional {name} coordinate")
        if not original.sizes[name]:
            raise ValueError("empty fixture coordinates are not supported")
    if not np.issubdtype(original.time.dtype, np.datetime64):
        raise ValueError("fixture requires datetime64 time")
    if not np.issubdtype(original.precip.dtype, np.floating):
        raise ValueError("fixture precipitation must be floating point")
    if not original.precip.attrs.get("units"):
        raise ValueError("fixture precipitation units must be explicit")
    if original.attrs.get("dataset") != "chirps":
        raise ValueError("fixture must already declare the authorized CHIRPS presentation")
    if original.attrs.get("source_lineage") != "chirps-fetch":
        raise ValueError("fixture must already declare chirps-fetch presentation lineage")
    forbidden = {"synthetic", "simulation_fixture", "generator", "seed", "weather_skills_history"}
    for variable in [original, *original.variables.values()]:
        if forbidden.intersection(variable.attrs):
            raise ValueError("unexpected provenance in system-visible fixture attributes")
    if original.attrs.get("weather_skills_source", "chirps") != "chirps":
        raise ValueError("conflicting source presentation")

    dates = original.time.values.astype("datetime64[D]")
    bounds = [
        float(original.latitude.max()),
        float(original.longitude.min()),
        float(original.latitude.min()),
        float(original.longitude.max()),
    ]
    history = [{
        "skill": "chirps-fetch",
        "version": presented_fetch_version,
        "args": {"start_time": str(dates[0]), "end_time": str(dates[-1]), "bbox": bounds},
        "input": None,
    }]
    staged = original.copy(deep=True)
    staged.attrs.update(
        weather_skills_source="chirps",
        weather_skills_history=json.dumps(history, sort_keys=True, allow_nan=False),
    )
    for variable in staged.variables.values():
        variable.encoding = {}
    # NetCDF encoding/backend hints cannot be copied into Zarr's encoding.
    destination.mkdir(parents=True, exist_ok=False)
    harness.mkdir(parents=True, exist_ok=False)
    staged.to_zarr(destination, mode="w-", zarr_format=2, consolidated=True)
    with xr.open_zarr(destination, consolidated=True) as reopened:
        recovered = reopened.load()
    for name in original.variables:
        if recovered[name].dtype != original[name].dtype:
            raise ValueError(f"decoded dtype changed for {name}")
        xr.testing.assert_identical(original[name], recovered[name])
    if _sha256(source) != source_hash:
        raise ValueError("source changed during staging")
    manifest = {
        "schema": "adapter-fixture-staging/1",
        "tag": "synthetic",
        "synthetic": True,
        "generator_provenance": provenance,
        "actual_fetch_executed": False,
        "source": {"path": str(source), "sha256": source_hash, "format": "NetCDF"},
        "output": {"path": str(destination), "sha256": _tree_hash(destination),
                   "format": "Zarr v2", "tag": "synthetic"},
        "conversion": "Decoded values, dtypes, coordinates, NaNs and units retained exactly",
        "presentation_only": {"owner_ruling": "UC-12", "added_attributes": [
            "weather_skills_source", "weather_skills_history"], "history": history},
        "validation": "storage preservation only; no admission or scientific correctness claim",
    }
    with (harness / "manifest.json").open("x", encoding="utf-8") as stream:
        json.dump(manifest, stream, indent=2, allow_nan=False)
        stream.write("\n")
    return manifest
