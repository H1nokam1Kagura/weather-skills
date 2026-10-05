"""Trusted source policies. Synthetic grants are harness inputs, never agent outputs."""

import json
from dataclasses import dataclass

from .catalog import safe_path, tree_hash
from .contracts import Refused, digest

PRODUCTION = {"policy": "production-chirps/v1", "classification": "production"}
GENERATORS = {"simkit.fixtures.onset_synth/1", "workflow.synthetic-rainfall/1"}


@dataclass(frozen=True)
class SyntheticAdmission:
    """Immutable registration issued by trusted Python harness code.

    This is a capability boundary for tool-free agents, not a security boundary
    against arbitrary Python code running as the same OS account.
    """

    canonical_record: str

    @property
    def record(self):
        return json.loads(self.canonical_record)

    def verify(self, source):
        record = self.record
        if record.get("policy") != "synthetic-evaluation/v1":
            raise Refused("invalid synthetic admission policy")
        if str(safe_path(source)) != record["source"] or tree_hash(source) != record["source_hash"]:
            raise Refused("registered synthetic source changed")
        for name in ("manifest", "generator"):
            if tree_hash(record[name]) != record[name + "_hash"]:
                raise Refused("registered synthetic " + name + " changed")


def register_synthetic(source, manifest, generator):
    """Register a disposable, honestly labelled fixture before candidate execution."""
    import hashlib

    import numpy as np
    import xarray as xr
    from weather_skills_core.provenance import load_history

    source, manifest, generator = map(safe_path, (source, manifest, generator))
    provenance = json.loads(manifest.read_text(encoding="utf-8"))
    with xr.open_zarr(source, consolidated=None) as ds:
        identity = ds.attrs.get("generator")
        if (
            ds.attrs.get("synthetic") != "true"
            or identity not in GENERATORS
            or ds.attrs.get("source_lineage") != "synthetic:" + identity
            or ds.attrs.get("weather_skills_source") == "chirps"
            or load_history(source)
        ):
            raise Refused("synthetic fixture must have honest generator lineage, not fetch history")
        if provenance.get("synthetic") is not True or provenance.get("seed") != ds.attrs.get(
            "seed"
        ):
            raise Refused("synthetic fixture manifest disagrees with source")
        if provenance.get("source_lineage") != ["synthetic:" + identity]:
            raise Refused("synthetic fixture manifest lineage differs")
        if type(provenance.get("seed")) is not int:
            raise Refused("synthetic fixture manifest requires an integer seed")
        if "source_hash" in provenance and provenance["source_hash"] != tree_hash(source):
            raise Refused("synthetic fixture manifest source hash differs")
        if "content_sha256" in provenance:
            values = np.ascontiguousarray(ds.precip.values, dtype="<f4").copy()
            values[np.isnan(values)] = np.float32("nan")
            fingerprint = hashlib.sha256(values.tobytes())
            fingerprint.update(
                json.dumps(
                    {
                        "times": ds.time.values.astype("datetime64[D]").astype(str).tolist(),
                        "lat": ds.latitude.values.tolist(),
                        "lon": ds.longitude.values.tolist(),
                    }
                ).encode()
            )
            if (
                fingerprint.hexdigest() != provenance["content_sha256"]
                or ds.attrs.get("content_sha256") != provenance["content_sha256"]
            ):
                raise Refused("synthetic fixture manifest content hash differs")
    record = {
        "policy": "synthetic-evaluation/v1",
        "classification": "synthetic-evaluation",
        "source": str(source),
        "source_hash": tree_hash(source),
        "manifest": str(manifest),
        "manifest_hash": tree_hash(manifest),
        "generator": str(generator),
        "generator_hash": tree_hash(generator),
        "generator_id": identity,
        "seed": provenance["seed"],
    }
    return SyntheticAdmission(json.dumps(record, sort_keys=True, allow_nan=False))


def record_for(grant, source):
    if grant is None:
        return dict(PRODUCTION)
    if type(grant) is not SyntheticAdmission:
        raise Refused("synthetic admission requires a trusted harness registration")
    grant.verify(source)
    return grant.record


def identity(record):
    return digest(record)
