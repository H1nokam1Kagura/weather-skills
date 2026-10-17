"""Closed, bounded contracts for the synthetic verification controller."""

import json
import math
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from tools.workflow.catalog import safe_path, tree_hash
from tools.workflow.contracts import Refused, digest, keys

from . import PROFILES

ROLES = ("forecast", "reference", "baseline")
METRICS = ("bias", "mae", "rmse", "crps", "brier")
FIELDS = {
    "schema",
    "profile",
    "products",
    "windows",
    "metrics",
    "threshold",
    "comparator",
    "bbox",
    "days",
    "members",
    "uncertainty",
    "limits",
    "authorization",
}


def require(condition, message):
    if not condition:
        raise Refused(message, gate="goal_check")


def request_hash(request):
    return digest({k: v for k, v in request.items() if k != "authorization"})


def validate(request):
    keys(request, FIELDS, "verification request", gate="goal_check")
    require(request["schema"] == "verification.request/1", "unknown request schema")
    require(request["profile"] in PROFILES, "unsupported verification profile")
    products = request["products"]
    require(isinstance(products, list) and 1 <= len(products) <= 64, "bounded products required")
    ids = set()
    for product in products:
        keys(product, {"id", "initialization", "valid_start"}, "product", gate="goal_check")
        name = product["id"]
        require(
            isinstance(name, str) and re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,39}", name),
            "invalid product id",
        )
        require(name not in ids, "duplicate product")
        ids.add(name)
        try:
            start = date.fromisoformat(product["valid_start"])
            init = date.fromisoformat(product["initialization"])
        except (ValueError, TypeError) as exc:
            raise Refused(
                "absolute initialization and valid_start required", gate="goal_check"
            ) from exc
        require(init <= start, "valid time precedes initialization")
    for field, allowed in (("windows", (2, 5, 10)), ("metrics", METRICS)):
        values = request[field]
        require(
            isinstance(values, list) and values and all(v in allowed for v in values),
            f"unsupported {field}",
        )
        require(len(set(values)) == len(values), f"duplicate {field}")
    require(all(type(w) is int for w in request["windows"]), "integer windows required")
    for field, upper in (("days", 370), ("members", 50)):
        require(type(request[field]) is int and 1 <= request[field] <= upper, f"invalid {field}")
    require(request["days"] >= max(request["windows"]), "no complete requested window")
    box = request["bbox"]
    require(
        isinstance(box, list)
        and len(box) == 4
        and all(type(x) in (float, int) and math.isfinite(x) for x in box),
        "finite bbox required",
    )
    n, w, s, e = box
    require(-90 <= s < n <= 90 and -180 <= w < e <= 180, "unsupported bbox")
    if "brier" in request["metrics"]:
        require(
            type(request["threshold"]) in (int, float)
            and math.isfinite(request["threshold"])
            and request["threshold"] >= 0
            and request["comparator"] == ">",
            "Brier requires an explicit nonnegative mm threshold and > comparator",
        )
    else:
        require(
            request["threshold"] is None and request["comparator"] is None,
            "unused threshold/comparator is unsupported",
        )
    uncertainty = request["uncertainty"]
    keys(uncertainty, {"block_windows", "resamples", "seed"}, "uncertainty", gate="goal_check")
    for field, lo, hi in (
        ("block_windows", 1, 100),
        ("resamples", 20, 1000),
        ("seed", 0, 2**32 - 1),
    ):
        require(
            type(uncertainty[field]) is int and lo <= uncertainty[field] <= hi,
            f"invalid uncertainty {field}",
        )
    limits = request["limits"]
    keys(limits, {"max_cells", "max_bytes", "max_seconds"}, "limits", gate="goal_check")
    for field, upper in (("max_cells", 2500), ("max_bytes", 128 * 1024**2), ("max_seconds", 600)):
        require(
            type(limits[field]) is int and 1 <= limits[field] <= upper, f"invalid limit {field}"
        )
    keys(request["authorization"], {"by", "request_hash"}, "authorization", gate="authorization")
    require(
        request["authorization"] == {"by": "operator", "request_hash": request_hash(request)},
        "authorization does not bind this exact request",
    )
    return request


@dataclass(frozen=True)
class SyntheticGrant:
    """Trusted harness registration; never accepted from JSON or the production CLI."""

    canonical: str

    @property
    def record(self):
        return json.loads(self.canonical)

    def verify(self):
        for product in self.record.values():
            if "unavailable" in product:
                continue
            for source in product.values():
                if tree_hash(source["path"]) != source["sha256"]:
                    raise Refused("registered source changed", gate="source_check")


def register_synthetic(sources):
    """Verify explicit synthetic lineage and bind each immutable source tree."""
    import xarray as xr

    require(isinstance(sources, dict) and sources, "source map required")
    records = {}
    for name, product in sources.items():
        if isinstance(product, dict) and set(product) == {"unavailable"}:
            require(
                isinstance(product["unavailable"], str) and product["unavailable"].strip(),
                "unavailability evidence required",
            )
            records[name] = dict(product)
            continue
        keys(product, ROLES, "source roles", gate="source_check")
        records[name] = {}
        for role, path in product.items():
            path = safe_path(path)
            with xr.open_zarr(path, chunks=None) as data:
                require(
                    data.attrs.get("synthetic") == "true"
                    and data.attrs.get("source_lineage") == "synthetic:verification-fixture/1"
                    and not data.attrs.get("weather_skills_source")
                    and not data.attrs.get("rhiza_history"),
                    "honest synthetic sources required",
                )
            records[name][role] = {"path": str(path), "sha256": tree_hash(path)}
    return SyntheticGrant(json.dumps(records, sort_keys=True))


def plan(request):
    return [
        {"product": p["id"], "window_days": w, "metrics": request["metrics"]}
        for p in request["products"]
        for w in request["windows"]
    ]


def build_identity(repo):
    paths = [
        Path(__file__).parent,
        repo / "skills/aggregate-temporal/scripts/aggregate.py",
        repo / "skills/convert-to-totals/scripts/convert_to_totals.py",
        repo / "skills/verify/scripts/verify.py",
        repo / "tools/workflow/catalog.py",
        repo / "tools/workflow/contracts.py",
        repo / "tools/workflow/persistence.py",
        repo / "adapter_support/verification_adapter.py",
    ]
    # Python bytecode is host-local and is not part of a reviewed implementation.
    files = [p for root in paths for p in (root.rglob("*.py") if root.is_dir() else [root])]
    import importlib.metadata

    core = importlib.metadata.distribution("weather-skills-core")
    return digest(
        {
            "files": {str(p.relative_to(repo)): tree_hash(p) for p in files},
            "core": core.read_text("direct_url.json"),
            "packages": {
                n: importlib.metadata.version(n)
                for n in ("numpy", "xarray", "zarr", "weather-skills-core")
            },
        }
    )
