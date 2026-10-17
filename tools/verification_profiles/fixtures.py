"""Honest, deterministic synthetic inputs; never impersonate downloaded weather data."""

import numpy as np
import xarray as xr

from .contracts import ROLES, request_hash


def authorize(request):
    request["authorization"] = {"by": "operator", "request_hash": request_hash(request)}
    return request


def request_for(profile="harvest-verification/v1", days=20, windows=None):
    return authorize(
        {
            "schema": "verification.request/1",
            "profile": profile,
            "products": [
                {"id": "init-20260101", "initialization": "2026-01-01", "valid_start": "2026-01-02"}
            ],
            "windows": windows or [2, 5, 10],
            "metrics": ["mae"],
            "threshold": None,
            "comparator": None,
            "bbox": [1, 10, 0, 11],
            "days": days,
            "members": 3,
            "uncertainty": {"block_windows": 1, "resamples": 20, "seed": 17},
            "limits": {"max_cells": 4, "max_bytes": 8 * 1024**2, "max_seconds": 600},
        }
    )


def make_sources(root, request, mutate=None):
    sources = {}
    for product in request["products"]:
        sources[product["id"]] = {}
        times = np.datetime64(product["valid_start"], "ns") + np.arange(
            request["days"]
        ) * np.timedelta64(1, "D")
        for role in ROLES:
            coords = {"time": times, "latitude": [1.0, 0.0], "longitude": [10.0, 11.0]}
            dims = ["time", "latitude", "longitude"]
            shape = [request["days"], 2, 2]
            if role != "reference":
                coords["number"] = np.arange(request["members"])
                dims.append("number")
                shape.append(request["members"])
            data = xr.Dataset(
                {"precip": (dims, np.full(shape, 2.5 if role == "forecast" else 2.0))},
                coords=coords,
            )
            data.precip.attrs.update(units="mm day-1", aggregation_period="1 day")
            data["time_bounds"] = (
                ("time", "bnds"),
                np.column_stack([times, times + np.timedelta64(1, "D")]),
            )
            data.time.attrs["bounds"] = "time_bounds"
            for variable in ("time", "time_bounds"):
                data[variable].encoding["units"] = "days since 2026-01-01"
            data.attrs.update(
                synthetic="true",
                source_lineage="synthetic:verification-fixture/1",
                initialization=product["initialization"],
            )
            if mutate:
                mutate(role, data)
            path = root / product["id"] / f"{role}.zarr"
            path.parent.mkdir(parents=True, exist_ok=True)
            data.to_zarr(path, mode="w", consolidated=True)
            sources[product["id"]][role] = path
    return sources
