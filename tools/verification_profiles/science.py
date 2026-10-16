"""Exact-support verification using the repository's real scientific skills."""

import importlib.util
import json
import time
from pathlib import Path

import numpy as np
import xarray as xr

from tools.workflow.catalog import tree_hash
from tools.workflow.contracts import Refused

from .contracts import ROLES, require


def inspect(request, product, records):
    """Reject before loading anything larger than the explicit limits."""
    datasets = {}
    started = time.perf_counter()
    total_bytes = 0
    decoded_bytes = 0
    try:
        for role in ROLES:
            path = Path(records[role]["path"])
            total_bytes += sum(p.stat().st_size for p in path.rglob("*") if p.is_file())
            require(total_bytes <= request["limits"]["max_bytes"], "source byte limit exceeded")
            ds = xr.open_zarr(path, chunks=None)
            datasets[role] = ds
            require(set(ds.data_vars) == {"precip", "time_bounds"}, "unexpected source variables")
            dims = {"time", "latitude", "longitude"} | (
                {"number"} if role != "reference" else set()
            )
            require(
                "precip" in ds and set(ds.precip.dims) == dims, "source variable/dimensions differ"
            )
            require(
                ds.sizes["latitude"] * ds.sizes["longitude"] <= request["limits"]["max_cells"],
                "spatial cell limit exceeded",
            )
            decoded_bytes += ds.nbytes
            require(
                decoded_bytes <= request["limits"]["max_bytes"],
                "decoded sources exceed memory bound",
            )
            require(
                ds.sizes["time"] == request["days"],
                "source does not cover the requested daily window",
            )
            if role != "reference":
                require(ds.sizes["number"] == request["members"], "member count differs")
                require(
                    ds.attrs.get("initialization") == product["initialization"],
                    "wrong historical initialization",
                )
                require(
                    len(set(ds.number.values.tolist())) == ds.sizes["number"], "duplicate members"
                )
            require(
                ds.precip.attrs.get("units") == "mm day-1", "explicit daily mm rate inputs required"
            )
            require(
                ds.precip.attrs.get("aggregation_period") == "1 day",
                "daily period metadata required",
            )
            wanted = np.datetime64(product["valid_start"], "ns") + np.arange(
                request["days"]
            ) * np.timedelta64(1, "D")
            require(
                np.array_equal(ds.time.values, wanted),
                "dates or cadence differ from the requested support",
            )
            require(
                ds.time.attrs.get("bounds") == "time_bounds"
                and "time_bounds" in ds
                and ds.time_bounds.dims == ("time", "bnds"),
                "explicit time bounds required",
            )
            bounds = np.column_stack([wanted, wanted + np.timedelta64(1, "D")])
            require(np.array_equal(ds.time_bounds.values, bounds), "daily interval bounds differ")
            box = [
                float(ds.latitude.max()),
                float(ds.longitude.min()),
                float(ds.latitude.min()),
                float(ds.longitude.max()),
            ]
            require(
                np.array_equal(box, request["bbox"]), "source extent differs from requested bbox"
            )
            for axis in ("latitude", "longitude"):
                values = ds[axis].values
                require(
                    np.isfinite(values).all() and len(np.unique(values)) == len(values),
                    "invalid spatial coordinates",
                )
            ds.load()
            require(not np.isinf(ds.precip.values).any(), "infinite rainfall is invalid")
            require(not (ds.precip.values < 0).any(), "negative rainfall is invalid")
        truth = datasets["reference"]
        for role in ("forecast", "baseline"):
            for axis in ("time", "latitude", "longitude"):
                require(
                    np.array_equal(datasets[role][axis], truth[axis]),
                    "inputs are not on identical coordinates",
                )
        mask = np.isfinite(truth.precip)
        for role in ("forecast", "baseline"):
            mask = mask & np.isfinite(datasets[role].precip).all("number")
        for ds in datasets.values():
            ds["precip"] = ds.precip.where(mask)
        return datasets, {
            "initialization": product["initialization"],
            "valid_start": product["valid_start"],
            "bytes": total_bytes,
            "seconds": time.perf_counter() - started,
            "members": request["members"],
            "days": request["days"],
            "cells": truth.sizes["latitude"] * truth.sizes["longitude"],
            "paired_daily_cells": int(mask.sum()),
            "backend": "cached-zarr-direct",
            "cache_state": "uncontrolled",
            "classification": "synthetic_probe_not_remote_benchmark",
        }
    finally:
        for ds in datasets.values():
            ds.close()


class Skills:
    """Closed in-process backend for bounded synthetic runs; no model-authored code."""

    SCRIPTS = {
        "aggregate-temporal": ("aggregate", "aggregate"),
        "convert-to-totals": ("convert_to_totals", "convert_to_totals"),
        "verify": ("verify", "verify"),
    }

    def __init__(self, repo):
        self.repo = Path(repo)
        self.loaded = {}
        self.receipts = []

    def call(self, skill, argv):
        stem, name = self.SCRIPTS[skill]
        if skill not in self.loaded:
            path = self.repo / "skills" / skill / "scripts" / (stem + ".py")
            spec = importlib.util.spec_from_file_location("verification_" + stem, path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            self.loaded[skill] = getattr(module, name)
        started = time.perf_counter()
        self.loaded[skill](argv)
        self.receipts.append(
            {"skill": skill, "argv": argv, "seconds": time.perf_counter() - started}
        )


def _mean(values, metric):
    finite = np.isfinite(values)
    if not finite.any():
        return None
    result = float(np.mean(values[finite]))
    return float(np.sqrt(result)) if metric == "rmse" else result


def paired_summary(forecast, baseline, metric, uncertainty):
    """Equal cell weights; circular time-block bootstrap of paired score differences."""
    require(forecast.shape == baseline.shape and forecast.ndim == 3, "score support differs")
    mask = np.isfinite(forecast) & np.isfinite(baseline)
    forecast, baseline = np.where(mask, forecast, np.nan), np.where(mask, baseline, np.nan)
    require(forecast.shape == baseline.shape and forecast.ndim == 3, "score support differs")
    count = int(mask.sum())
    fc, base = _mean(forecast, metric), _mean(baseline, metric)
    result = {
        "forecast": fc,
        "baseline": base,
        "paired_count": count,
        "difference": None if fc is None else fc - base,
        "sample_definition": "equal-weight finite window/latitude/longitude cells; all ensemble members required",
        "windows_with_samples": int(mask.any(axis=(1, 2)).sum()),
    }
    block = uncertainty["block_windows"]
    n = forecast.shape[0]
    result["uncertainty"] = {
        **uncertainty,
        "method": "paired circular time-block bootstrap",
        "confidence": 0.95,
        "status": "INSUFFICIENT_SAMPLES",
        "difference_interval": None,
    }
    if result["windows_with_samples"] >= 2 * block:
        rng = np.random.default_rng(uncertainty["seed"])
        values = []
        for _ in range(uncertainty["resamples"]):
            starts = rng.integers(0, n, size=(n + block - 1) // block)
            index = ((starts[:, None] + np.arange(block)) % n).ravel()[:n]
            a, b = _mean(forecast[index], metric), _mean(baseline[index], metric)
            if a is not None:
                values.append(a - b)
        if len(values) == uncertainty["resamples"]:
            result["uncertainty"].update(
                status="ESTIMATED", difference_interval=np.quantile(values, [0.025, 0.975]).tolist()
            )
    return result


def calculate(request, product, window, datasets, directory, backend, before_operation):
    """Run aggregate -> totals -> verify; masks and interval bounds are checked first."""
    directory = Path(directory)
    receipt_start = len(backend.receipts)
    directory.mkdir(exist_ok=False)
    count = request["days"] // window
    retained = count * window
    start = np.datetime64(product["valid_start"])
    end = str(start + np.timedelta64(retained, "D"))
    totals = {}
    # Reject incomplete temporal support even if every spatial cell lacks the same day.
    # The general aggregation skill can average available days; these profiles require all days.
    truth = (
        datasets["reference"]
        .precip.isel(time=slice(0, retained))
        .transpose("time", "latitude", "longitude")
    )
    complete = np.isfinite(truth.values).reshape(count, window, *truth.shape[1:]).all(axis=1)
    daily_complete = xr.DataArray(
        np.repeat(complete, window, axis=0), dims=truth.dims, coords=truth.coords
    )
    for role, data in datasets.items():
        before_operation()
        source, rate, total = [
            directory / f"{role}-{suffix}.zarr" for suffix in ("daily", "rate", "total")
        ]
        selected = data.isel(time=slice(0, retained)).copy()
        selected["precip"] = selected.precip.where(daily_complete)
        selected.to_zarr(source, mode="w", consolidated=True)
        backend.call(
            "aggregate-temporal",
            [
                "-i",
                str(source),
                "-o",
                str(rate),
                "--variable",
                "precip",
                "--period",
                f"{window} day",
                "--start-time",
                product["valid_start"],
                "--end-time",
                end,
            ],
        )
        before_operation()
        # Full-window masking already excludes incomplete samples. Keep all-NaN bins
        # in the coordinate ledger instead of the converter's default row removal.
        backend.call(
            "convert-to-totals", ["-i", str(rate), "-o", str(total), "--min-coverage", "0"]
        )
        totals[role] = total
    # Independent support/count check: no inner-join may silently discard mismatched bins.
    paired = None
    for path in totals.values():
        with xr.open_zarr(path, chunks=None) as ds:
            expected = start + np.arange(count) * np.timedelta64(window, "D")
            require(
                np.array_equal(ds.time.values, expected),
                "aggregation did not preserve exact requested bins",
            )
            require(ds.precip.attrs.get("units") == "mm", "totals are not rainfall amounts")
            finite = np.isfinite(ds.precip)
            if "number" in finite.dims:
                finite = finite.all("number")
            paired = finite if paired is None else paired & finite
    results = {}
    for metric in request["metrics"]:
        fields = []
        for role in ("forecast", "baseline"):
            before_operation()
            # RMSE is the root of the pooled mean squared bias, not the mean of per-cell RMSEs.
            native_metric = "bias" if metric == "rmse" else metric
            output = directory / f"{role}-{metric}.zarr"
            argv = [
                "--forecast",
                str(totals[role]),
                "--obs",
                str(totals["reference"]),
                "--metric",
                native_metric,
                "--variable",
                "precip",
                "-o",
                str(output),
            ]
            if metric == "brier":
                argv += ["--threshold", str(request["threshold"])]
            backend.call("verify", argv)
            with xr.open_zarr(output, chunks=None) as ds:
                name = "brier_score" if metric == "brier" else native_metric
                field = ds[name].where(paired).transpose("time", "latitude", "longitude").values
                fields.append(field**2 if metric == "rmse" else field.copy())
        results[metric] = paired_summary(*fields, metric, request["uncertainty"])
    if not any(r["paired_count"] for r in results.values()):
        raise Refused("no paired samples remain", gate="gate_evidence")
    report = {
        "product": product["id"],
        "initialization": product["initialization"],
        "window_days": window,
        "interval_start": str(start),
        "interval_end_exclusive": end,
        "complete_windows": count,
        "dropped_tail_days": request["days"] - retained,
        "metrics": results,
        "threshold_mm": request["threshold"],
        "comparator": request["comparator"],
        "crop_readiness": "NOT_ASSESSED: rainfall scores do not establish crop maturity or harvest safety",
        "classification": "synthetic_process_check_not_forecast_skill",
        "tool_receipts": backend.receipts[receipt_start:],
        "artifacts": {p.name: tree_hash(p) for p in directory.glob("*.zarr")},
    }
    (directory / "scores.json").write_text(
        json.dumps(report, indent=2, allow_nan=False), encoding="utf-8"
    )
    return report
