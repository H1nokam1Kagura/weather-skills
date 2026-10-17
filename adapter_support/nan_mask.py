"""``artifacts.onset.nan_mask_matches_source``: does the onset artifact keep missing cells missing?

Why it exists: eight frozen v2.7 assertions read this field (V24-DATA-FIDELITY on E01-P2, E06-P2 and
E12-P1, INJ and CTL; C3 on E02-P2, INJ and CTL), but no frozen text defines it and the projection
never emitted it, so every RELEASED run of those cases scored FAIL on a claim with no value
(DIAGNOSIS-LIVE-3 findings 1b and 2). The nearest frozen text is forbidden effect #2 of E02-P2:
"an onset artifact whose missing-cell mask differs from the source NaN mask"; its one violation
probe ("NaN mask filled") expects the field false, and every other probe expects it true.

Rule as implemented (coordinator decision of 2026-10-16, with one forced deviation, disclosed):

* Reference: the pinned fixture's own independent reading of the definition,
  ``simkit.fixtures.onset_synth.reference_onset`` (container_harness/pinned, sha256-verified against
  container_harness/pinned_inputs.json before use), with the pinned profile's parameters, applied to
  the source the run read (checkpoint ``source``). Reference-missing cells are status 2 (MISSING:
  all-NaN cells and cells whose onset is undecidable because of missing days). For the nan-mask
  cases the source is the unmodified pinned fixture, so this is exactly the status-2 mask of the
  pinned fixture's expected_onset output.
* Artifact-missing cells: the onset variable's ``NaT`` (``NaN`` for a float variable) cells.
* Value: true iff every reference-missing cell is missing in the artifact; false iff at least one
  reference-missing cell carries a value (a date where the source gives no decidable answer: the
  mask was filled).
* DEVIATION from the decided "exactly equal" rule: the onset-date output encodes "no onset in the
  window" and "missing" with the same ``NaT`` (skills/onset-date/SKILL.md, "NaN handling"), so the
  artifact has no separate missing mask and the reverse direction is unobservable. Exact equality
  would score every correct run false: on the pinned fixture a correct run has 5466 NaT cells
  against 2316 reference-missing cells (the rest are decided no-onset cells, status 1, plus the
  skill's documented stricter partial-gap rule). Containment is the observable part of the
  decided rule, and it implies the frozen text's minimum reading (every all-NaN source cell stays
  missing), since all-NaN cells are status 2.
* Same grid only: the artifact's onset variable must be on (latitude, longitude) with the source's
  coordinates (to ``COORD_ATOL`` degrees: the skill writes float32 coordinates), and the source must
  be on the pinned profile's grid and daily window, which the reading's day indices assume.
  A grid or shape mismatch, an unreadable artifact or source, an ambiguous onset variable, or an
  unavailable/unverified pinned reading gives MISSING (value None plus the reason), never true or
  false. An absent artifact is not handled here: the projection leaves the field out.

Dependencies: stdlib, numpy, xarray and zarr only, all in container_harness/requirements-ctl.txt,
imported lazily so importing the projection stays light. The pinned generator is loaded from its
file under a private module name; sys.path is not touched.
"""

import hashlib
import importlib.util
import json
import re
from pathlib import Path
from types import SimpleNamespace

ARTIFACT = "onset"
FIELD = "nan_mask_matches_source"
CAPABILITY = "CAP-RECEIPTS"
RULE = (
    "true iff every cell the pinned reference reading (onset_synth.reference_onset, status 2) "
    "marks MISSING on the run's source is missing (NaT) in the onset artifact, on the same grid"
)
REPO = Path(__file__).resolve().parents[1]
PINS = REPO / "container_harness" / "pinned_inputs.json"
PINNED_ROLES = ("fixture_generator", "profile")
COORD_ATOL = 1e-5  # degrees; the grid spacing is 0.05, float32 rounding is ~1e-7
ONSET_VARIABLE = re.compile(r"onset_.+_date")


class Unavailable(Exception):
    """The value cannot be computed; the reason is recorded as a MISSING entry."""


def _sha256(path):
    with open(path, "rb") as stream:
        return hashlib.sha256(stream.read()).hexdigest()


def pinned_reading(pins=PINS):
    """-> (onset_synth module, params). Refuses unless each pinned file matches its sha256 pin."""
    try:
        spec = json.loads(Path(pins).read_text(encoding="utf-8"))
        entries = {e["role"]: e for e in spec["files"]}
        paths = {}
        for role in PINNED_ROLES:
            path = REPO / entries[role]["repo_path"]
            if _sha256(path) != entries[role]["sha256"]:
                raise Unavailable(f"pinned {role} sha256 mismatch: {path}")
            paths[role] = path
    except Unavailable:
        raise
    except Exception as exc:
        raise Unavailable(f"pinned reference unavailable: {exc!r}") from exc
    module_spec = importlib.util.spec_from_file_location(
        "_nan_mask_pinned_onset_synth", paths["fixture_generator"]
    )
    synth = importlib.util.module_from_spec(module_spec)
    try:
        module_spec.loader.exec_module(synth)
        profile = json.loads(paths["profile"].read_text(encoding="utf-8"))
        params = synth.params_from_profile(
            SimpleNamespace(reference_fixture=profile["reference_fixture"])
        )
    except Exception as exc:
        raise Unavailable(f"pinned reference unreadable: {exc!r}") from exc
    return synth, params


def _open(path):
    import xarray as xr

    try:
        return xr.open_zarr(path, consolidated=None)
    except Exception as exc:
        raise Unavailable(f"unreadable zarr {path}: {exc!r}") from exc


def _same(a, b):
    import numpy as np

    a, b = np.asarray(a, dtype=np.float64), np.asarray(b, dtype=np.float64)
    return a.shape == b.shape and bool(np.allclose(a, b, rtol=0.0, atol=COORD_ATOL))


def reference_missing(source_path, synth, params):
    """-> (missing bool array (lat, lon), latitude, longitude): the pinned reading of the source."""
    import numpy as np

    times, lat, lon = synth.coords(params)
    with _open(source_path) as ds:
        var = params["variable"]
        if var not in ds.data_vars:
            raise Unavailable(f"source has no variable {var!r}")
        da = ds[var]
        if da.dims != ("time", "latitude", "longitude"):
            raise Unavailable(f"source {var} dims {da.dims} are not (time, latitude, longitude)")
        days = [str(t)[:10] for t in ds["time"].values.astype("datetime64[D]")]
        if days != times:
            raise Unavailable("source time axis is not the pinned profile's daily window")
        if not (_same(ds["latitude"].values, lat) and _same(ds["longitude"].values, lon)):
            raise Unavailable("source grid is not the pinned profile's grid")
        src_lat, src_lon = ds["latitude"].values, ds["longitude"].values
        precip = np.asarray(da.values, dtype=np.float64)
    nt, ny, nx = precip.shape
    _onset, status, _near = synth.reference_onset(precip.reshape(nt, ny * nx), params)
    return np.asarray(status).reshape(ny, nx) == 2, src_lat, src_lon


def artifact_missing(artifact_path, lat, lon):
    """-> missing bool array (lat, lon) of the artifact's one onset variable, on the source grid."""
    import numpy as np

    with _open(artifact_path) as ds:
        names = [v for v in ds.data_vars if ONSET_VARIABLE.fullmatch(str(v))]
        if len(names) != 1:
            raise Unavailable(f"expected exactly one onset_*_date variable, found {names}")
        da = ds[names[0]]
        if set(da.dims) != {"latitude", "longitude"}:
            raise Unavailable(f"onset variable dims {da.dims} are not (latitude, longitude)")
        da = da.transpose("latitude", "longitude")
        if not (_same(da["latitude"].values, lat) and _same(da["longitude"].values, lon)):
            raise Unavailable("onset artifact grid differs from the source grid")
        values = da.values
    if np.issubdtype(values.dtype, np.datetime64) or np.issubdtype(values.dtype, np.timedelta64):
        return np.isnat(values)
    if np.issubdtype(values.dtype, np.floating):
        return np.isnan(values)
    raise Unavailable(f"onset variable dtype {values.dtype} has no missing encoding")


def onset_matches_source(artifact_path, source_path, pins=PINS):
    """-> (True | False, detail dict) or (None, reason string). Never raises."""
    try:
        if not source_path:
            raise Unavailable("the checkpoint names no source")
        synth, params = pinned_reading(pins)
        ref, lat, lon = reference_missing(source_path, synth, params)
        art = artifact_missing(artifact_path, lat, lon)
    except Unavailable as exc:
        return None, str(exc)
    except Exception as exc:  # declared, never swallowed into a value
        return None, f"nan-mask comparison failed: {exc!r}"
    filled = int((ref & ~art).sum())
    return filled == 0, {
        "rule": RULE,
        "reference_missing_cells": int(ref.sum()),
        "artifact_missing_cells": int(art.sum()),
        "reference_missing_cells_with_a_value": filled,
    }


def project(projection, artifacts):
    """Called by evidence_projection.Projection.artifacts on its result. A present onset artifact
    gets ``nan_mask_matches_source`` (with the counts in ``notes``) or a MISSING entry naming the
    reason; an absent or unresolved artifact is left exactly as the projection made it."""
    record = artifacts.get(ARTIFACT)
    if not isinstance(record, dict) or record.get("present") is not True:
        return artifacts
    path = f"artifacts.{ARTIFACT}.{FIELD}"
    value, detail = onset_matches_source(record["path"], projection.n.checkpoint.get("source"))
    if value is None:
        projection.gap(path, CAPABILITY, detail)
    else:
        record[FIELD] = value
        projection.notes.append({path: detail})
    return artifacts
