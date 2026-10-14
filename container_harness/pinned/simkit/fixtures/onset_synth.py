"""Deterministic SYNTHETIC rainfall fixture for the cached-onset profile, presented to the system AS CHIRPS (UC-12).

Why it exists: the brief permits 'disposable copied or synthetic data' (adapter_contract.safe_execution)
and forbids new weather-data fetching, so scored runs need not depend on a real CHIRPS cache. Strict
replay validates calculated data, not observational truth (scope_limits), so synthetic data is a
complete substitute for the replay and control-plane cases.

UC-12 (owner ruling, 2026-10-08): "i would want synthetic data for CHIRPS to run through as CHIRPS and the run
itself e2e to be metadata tagged to synthetic data for review and analysis purposes only". So:
  * the DATA the system reads (precip.nc) is CHIRPS-identical in everything the system reads, including its
    source lineage (chirps-fetch): no synthetic attribute, generator, seed or hash in it, so the real, unmodified
    source check is what gets tested;
  * synthetic provenance lives ONLY in a harness-side manifest (manifest.json, written to a separate harness
    directory the system is never given), next to the known-answer file (expected_onset.nc);
  * the harness tags the run and every collected output synthetic (run_provenance / tag_output), in its own records.
It is NOT observational data, and no result produced on it is a scientific finding.

Everything dimensional comes from the profile's reference_fixture.source_metadata and definition; the
definition's numbers are parsed from its text with strict checks, so a changed brief fails loudly.

Known-answer layer: an INDEPENDENT reference reading of the definition text computes the expected onset
for every cell. UC-13 (owner ruling): the reading uses the EXACT BOUNDED RULE for missing days (rain is
non-negative, so a missing day can only add): a threshold is certainly met if the observed sum (missing = 0)
reaches it, certainly not met only if the window is fully observed and its sum stays below it (no assumed cap
on daily rain); a candidate is TRUE if both thresholds are certainly met, FALSE if either is certainly not met,
else UNKNOWN; the onset is reported only if some candidate is TRUE and every earlier eligible candidate is
FALSE, 'no onset' only if all are FALSE, otherwise the cell is MISSING (indeterminate), never a guessed date.
No zero-fill, no skip-window, no assumed cap. It is a self-test aid for the adapter session, not a scored
oracle: the brief scores replay agreement, not agreement with this reading.
"""
import datetime
import hashlib
import json
import re

import numpy as np

CLASS_ORDER = ("no_data", "boundary_last_eligible", "boundary_after_last", "boundary_first_day",
               "threshold_equality", "false_start_then_onset", "no_onset_dry", "general_onset",
               # UC-13: partial-gap cells with known answers under the exact bounded rule
               "partial_gap_decided_onset", "partial_gap_missing", "partial_gap_decided_no_onset")
STATUS = {0: "onset_found", 1: "no_onset_found", 2: "missing"}  # 2 = MISSING (indeterminate or no data), never a date
PARTIAL_GAP = {"onset_day": 45, "gap_day_decided": 65, "gap_day_missing": 50, "gap_day_no_onset": 60}

# What the system reads: CHIRPS-identical attributes (UC-12). The synthetic tag never appears here.
PRESENTED_ATTRS = {"dataset": "chirps", "source_lineage": "chirps-fetch", "title": "CHIRPS daily precipitation (cached subset)"}


def params_from_profile(profile):
    sm = profile.reference_fixture["source_metadata"]
    d = profile.reference_fixture["definition"]
    trig = re.search(r"At least (\d+(?:\.\d+)?) mm accumulated across the first (\d+) consecutive days", d["trigger"])
    conf = re.search(r"At least (\d+(?:\.\d+)?) mm across the next (\d+) consecutive days, beginning (\d+) days after", d["confirmation"])
    if not trig or not conf:
        raise ValueError("definition text changed; refusing to guess its parameters")
    p = {
        "trigger_mm": float(trig.group(1)), "trigger_days": int(trig.group(2)),
        "confirm_mm": float(conf.group(1)), "confirm_days": int(conf.group(2)), "confirm_offset_days": int(conf.group(3)),
        "window_days": int(d["required_complete_window_days"]),
        "first_day": sm["first_day"], "last_day": sm["last_day"],
        "first_eligible": d["first_eligible_onset_date"], "last_eligible": d["last_eligible_onset_date"],
        "dims": sm["dimensions"], "cell_center_bbox": sm["cell_center_bbox"], "spacing": sm["grid_spacing_degrees"],
        "nan_fraction": sm["nan_fraction"], "units": sm["units"], "variable": sm["variable"],
        "dataset": sm["dataset"], "source_lineage": list(sm["source_lineage"]),
    }
    if p["confirm_offset_days"] + p["confirm_days"] != p["window_days"] or p["trigger_days"] != p["confirm_offset_days"]:
        raise ValueError("definition windows are not contiguous as the reference reading assumes")
    return p


def _day_index(p, iso):
    return (datetime.date.fromisoformat(iso) - datetime.date.fromisoformat(p["first_day"])).days


def coords(p):
    n, w, s, e = p["cell_center_bbox"]
    nt, ny, nx = p["dims"]["time"], p["dims"]["latitude"], p["dims"]["longitude"]
    lat = np.round(np.linspace(n, s, ny), 6)
    lon = np.round(np.linspace(w, e, nx), 6)
    if not (np.allclose(np.diff(lat), -p["spacing"]) and np.allclose(np.diff(lon), p["spacing"])):
        raise ValueError("bbox, dimensions and spacing are inconsistent")
    t0 = datetime.date.fromisoformat(p["first_day"])
    times = [(t0 + datetime.timedelta(days=i)).isoformat() for i in range(nt)]
    if times[-1] != p["last_day"]:
        raise ValueError("time dimension does not end on last_day")
    return times, lat, lon


def candidate_states(precip, p):
    """Per eligible candidate and cell, the exact bounded rule (UC-13): returns (true, false) boolean arrays of shape
    (candidates, cells). Rain is non-negative, so missing days can only raise a window sum: a threshold is certainly met
    if the observed sum (missing = 0) reaches it, and certainly not met only if the window is fully observed (no cap)."""
    x = precip.astype(np.float64)
    miss = np.isnan(x)
    S = np.concatenate([np.zeros((1, x.shape[1])), np.cumsum(np.nan_to_num(x), axis=0)], axis=0)
    M = np.concatenate([np.zeros((1, x.shape[1])), np.cumsum(miss.astype(np.int64), axis=0)], axis=0)
    t_lo, t_hi = _day_index(p, p["first_eligible"]), _day_index(p, p["last_eligible"])
    ts = np.arange(t_lo, t_hi + 1)
    td, off, cd = p["trigger_days"], p["confirm_offset_days"], p["confirm_days"]
    Lt, Mt = S[ts + td] - S[ts], M[ts + td] - M[ts]
    Lc, Mc = S[ts + off + cd] - S[ts + off], M[ts + off + cd] - M[ts + off]
    true = (Lt >= p["trigger_mm"]) & (Lc >= p["confirm_mm"])
    false = ((Mt == 0) & (Lt < p["trigger_mm"])) | ((Mc == 0) & (Lc < p["confirm_mm"]))
    near = (((Mt == 0) & (np.abs(Lt - p["trigger_mm"]) < 0.01)) | ((Mc == 0) & (np.abs(Lc - p["confirm_mm"]) < 0.01))).any(axis=0)
    return ts, true, false, near


def reference_onset(precip, p):
    """Independent reading of the definition. precip: (time, cells) float array. Returns (onset_idx, status, near).

    status 0 = onset found (decided), 1 = no onset found (decided: every eligible candidate certainly fails),
    2 = MISSING (some candidate is undecidable before any decided onset; all-NaN cells are MISSING too). onset_idx is
    -1 unless status is 0. Reading:
      candidate t is eligible iff first_eligible <= t <= last_eligible (so t..t+window-1 is inside the input);
      trigger:      sum(precip[t : t+trigger_days]) >= trigger_mm
      confirmation: sum(precip[t+offset : t+offset+confirm_days]) >= confirm_mm
      onset = the first eligible t satisfying both, decided by the exact bounded rule (candidate_states) when days are
      missing: reported only if that candidate is TRUE and every earlier candidate is FALSE.
    """
    nt = precip.shape[0]
    t_hi = _day_index(p, p["last_eligible"])
    if t_hi + p["window_days"] - 1 > nt - 1:
        raise ValueError("last eligible onset leaves an incomplete window")
    ts, true, false, near = candidate_states(precip, p)
    ncell = precip.shape[1]
    onset = np.full(ncell, -1, dtype=np.int64)
    status = np.full(ncell, 1, dtype=np.int8)
    decided = np.zeros(ncell, dtype=bool)
    for i in range(ts.size):
        hit = ~decided & true[i]
        onset[hit] = ts[i]
        status[hit] = 0
        decided |= hit
        unk = ~decided & ~true[i] & ~false[i]
        status[unk] = 2
        decided |= unk
    near = near & (status != 2)
    return onset.astype(np.int16), status, near


def generate(profile, seed=20251001):
    p = params_from_profile(profile)
    times, lat, lon = coords(p)
    nt, ny, nx = len(times), len(lat), len(lon)
    ncell = ny * nx
    rng = np.random.default_rng(seed)
    n_nan = int(round(p["nan_fraction"] * ncell))
    if abs(n_nan / ncell - p["nan_fraction"]) > 1e-12:
        raise ValueError("nan_fraction is not exactly representable on this grid")
    t_last, t_first = _day_index(p, p["last_eligible"]), _day_index(p, p["first_eligible"])
    counts = {"no_data": n_nan, "boundary_last_eligible": 50, "boundary_after_last": 50, "boundary_first_day": 50,
              "threshold_equality": 50, "false_start_then_onset": 2000, "no_onset_dry": 3000,
              "partial_gap_decided_onset": 50, "partial_gap_missing": 50, "partial_gap_decided_no_onset": 50}
    counts["general_onset"] = ncell - sum(counts.values())
    labels = np.concatenate([np.full(counts[k], i, dtype=np.int8) for i, k in enumerate(CLASS_ORDER)])
    labels = labels[rng.permutation(ncell)]
    X = rng.uniform(0.0, 0.8, size=(nt, ncell)).astype(np.float32)  # background: 10-day max 8 mm < trigger
    td, cd, off = p["trigger_days"], p["confirm_days"], p["confirm_offset_days"]

    def exact_onset(cols, d):
        X[:, cols] = 0.0
        X[d, cols] = p["trigger_mm"]                                 # exactly the trigger, on day d only
        X[d + off:d + off + cd, cols] = p["confirm_mm"] / cd          # exactly the confirmation total
    exact_onset(np.where(labels == CLASS_ORDER.index("boundary_last_eligible"))[0], t_last)
    # one day later: day t_last+1 rain falls inside t_last's trigger window, but t_last's confirmation
    # window then holds only cd-1 wet days -> no eligible onset exists
    cols = np.where(labels == CLASS_ORDER.index("boundary_after_last"))[0]
    X[:, cols] = 0.0
    X[t_last + 1, cols] = p["trigger_mm"]
    X[t_last + 1 + off:, cols] = p["confirm_mm"] / cd
    exact_onset(np.where(labels == CLASS_ORDER.index("boundary_first_day"))[0], t_first)
    exact_onset(np.where(labels == CLASS_ORDER.index("threshold_equality"))[0], 45)
    for k in ("general_onset", "false_start_then_onset"):
        cols = np.where(labels == CLASS_ORDER.index(k))[0]
        d2 = rng.integers(t_first + (35 if k == "false_start_then_onset" else 0), t_last - 5, size=cols.size)
        if k == "false_start_then_onset":
            d1 = d2 - 32
            for i in range(td):
                X[d1 + i, cols] = rng.uniform(3.0, 5.0, size=cols.size)  # trigger met, then a dry spell
        for i in range(td):
            X[d2 + i, cols] = rng.uniform(3.0, 5.0, size=cols.size)
        for i in range(cd):
            idx = np.minimum(d2 + off + i, nt - 1)
            X[idx, cols] = rng.uniform(1.0, 2.0, size=cols.size)
    # UC-13 partial-gap classes (exact bounded rule known answers; see partial_gap_answers)
    d = PARTIAL_GAP["onset_day"]
    for k, gap in (("partial_gap_decided_onset", PARTIAL_GAP["gap_day_decided"]), ("partial_gap_missing", PARTIAL_GAP["gap_day_missing"])):
        cols = np.where(labels == CLASS_ORDER.index(k))[0]
        X[:, cols] = 0.0
        X[d + td - 1, cols] = p["trigger_mm"]   # the whole trigger on the LAST day of d's trigger window
        X[d + off, cols] = p["confirm_mm"]      # the whole confirmation on the FIRST day of d's confirmation window
        X[gap, cols] = np.nan                   # one missing day
    cols = np.where(labels == CLASS_ORDER.index("partial_gap_decided_no_onset"))[0]
    X[:, cols] = 0.0
    X[PARTIAL_GAP["gap_day_no_onset"], cols] = np.nan
    X[:, labels == CLASS_ORDER.index("no_data")] = np.nan
    onset, status, near = reference_onset(X, p)
    cube = X.reshape(nt, ny, nx)
    return {"params": p, "seed": seed, "times": times, "lat": lat, "lon": lon, "precip": cube,
            "labels": labels.reshape(ny, nx), "onset_idx": onset.reshape(ny, nx), "status": status.reshape(ny, nx),
            "near_threshold": near.reshape(ny, nx), "counts": counts}


def partial_gap_answers(p):
    """Known answers for the UC-13 partial-gap classes (derivation, not computation):
      decided onset  - trigger on day d+9 and confirmation on day d+10 put d's windows over both thresholds with the
                       observed days alone; the gap (d+20) lies only in windows of candidates whose fully observed
                       trigger window is 0 mm, so every earlier candidate is certainly FALSE -> onset d;
      MISSING        - the gap (d+5) lies in the trigger window of candidates d-4..d-1, whose confirmation is already met
                       (it contains d+9 and d+10): those candidates are UNKNOWN before d -> MISSING (zero-fill would
                       report d, a guessed date);
      decided no onset - an all-zero series with one gap: each candidate has the gap in at most one window and the other,
                       fully observed window is 0 mm -> every candidate is certainly FALSE -> no onset (not MISSING)."""
    d = PARTIAL_GAP["onset_day"]
    return {"partial_gap_decided_onset": {"status": 0, "onset_idx": d},
            "partial_gap_missing": {"status": 2, "onset_idx": -1},
            "partial_gap_decided_no_onset": {"status": 1, "onset_idx": -1}}


def content_hash(g):
    """Hash of canonical content (float32 LE C-order, canonical NaN) + coords: independent of file encoding."""
    h = hashlib.sha256()
    arr = np.ascontiguousarray(g["precip"], dtype="<f4").copy()
    arr[np.isnan(arr)] = np.float32("nan")
    h.update(arr.tobytes())
    h.update(json.dumps({"times": g["times"], "lat": g["lat"].tolist(), "lon": g["lon"].tolist()}).encode())
    return h.hexdigest()


def manifest(g):
    """The HARNESS-SIDE manifest (UC-12): the only place synthetic provenance is recorded. Never given to the system."""
    p = g["params"]
    st, lab = g["status"], g["labels"]
    by_class = {}
    for i, k in enumerate(CLASS_ORDER):
        m = lab == i
        by_class[k] = {"cells": int(m.sum()), "onset_found": int((st[m] == 0).sum()), "no_onset": int((st[m] == 1).sum()),
                       "missing": int((st[m] == 2).sum())}
    t0 = datetime.date.fromisoformat(p["first_day"])
    onset_dates = g["onset_idx"][st == 0]
    return {
        "schema_version": "simkit-synthetic-fixture/2",
        "harness_side_only": True,
        "synthetic": True,
        "run_tag": "synthetic",
        "visible_to_system": False,
        "true_provenance": "synthetic:simkit.fixtures.onset_synth/2",
        "presented_to_system": {"attributes": dict(PRESENTED_ATTRS, variable=p["variable"], units=p["units"]),
                                "dataset": p["dataset"], "source_lineage": list(p["source_lineage"])},
        "emulates": "cached daily CHIRPS precipitation, CHIRPS-identical in everything the system reads (UC-12)",
        "purpose": "review and analysis of the system only; never a scientific finding",
        "seed": g["seed"],
        "content_sha256": content_hash(g),
        "dimensions": {"time": len(g["times"]), "latitude": len(g["lat"]), "longitude": len(g["lon"])},
        "first_day": g["times"][0], "last_day": g["times"][-1],
        "nan_fraction": float(np.isnan(g["precip"]).all(axis=0).mean()),
        "units": p["units"], "variable": p["variable"],
        "definition_parameters_parsed": {k: p[k] for k in ("trigger_mm", "trigger_days", "confirm_mm", "confirm_days", "confirm_offset_days", "window_days", "first_eligible", "last_eligible")},
        "classes": by_class,
        "status_codes": {str(k): v for k, v in STATUS.items()},
        "partial_gap_known_answers": partial_gap_answers(p),
        "onset_date_range": [(t0 + datetime.timedelta(days=int(onset_dates.min()))).isoformat(), (t0 + datetime.timedelta(days=int(onset_dates.max()))).isoformat()] if onset_dates.size else None,
        "near_threshold_cells": int(g["near_threshold"].sum()),
        "reference_reading": [
            "candidate t eligible iff first_eligible <= t <= last_eligible",
            "trigger: sum over t..t+trigger_days-1 >= trigger_mm (inclusive)",
            "confirmation: sum over t+offset..t+offset+confirm_days-1 >= confirm_mm (inclusive)",
            "UC-13 exact bounded rule: a threshold is certainly met if the observed sum (missing = 0) reaches it, certainly not met only if its window is fully observed and the sum is below it (no assumed cap); candidate TRUE if both certainly met, FALSE if either certainly not met, else UNKNOWN",
            "onset = the first TRUE candidate when every earlier eligible candidate is FALSE; no onset if every candidate is FALSE; otherwise MISSING (status 2), never a guessed date; an all-NaN series is MISSING",
            "no zero-fill, no skip-window, no assumed maximum daily rainfall",
        ],
        "not_a_scored_oracle": "The expected onset is a self-test aid. Scored cases test strict replay and the control plane, never agreement with this reading.",
        "rulings": ["UC-12 RULED 2026-10-08: CHIRPS-identical data, synthetic provenance harness-side only, run and outputs tagged by the harness",
                    "UC-13 RULED 2026-10-08: exact bounded rule; partial-gap cells with known answers"],
    }


def run_provenance(m, collected_outputs=()):
    """Harness evidence for a run on this fixture (UC-12): harness_observations.run_provenance, plus one tag record per
    collected output. Lives in harness records only; nothing here is written into anything the system reads."""
    h = hashlib.sha256(json.dumps(m, sort_keys=True).encode()).hexdigest()
    tags = [tag_output(path, m) for path in collected_outputs]
    return {"tag": m["run_tag"], "manifest_hash": h, "outputs_tagged": all(t["tag"] == "synthetic" for t in tags),
            "visible_to_system": False, "collected_outputs": tags}


def tag_output(path, m):
    return {"path": path, "tag": m["run_tag"], "true_provenance": m["true_provenance"], "purpose": m["purpose"]}


def write(g, out_dir, harness_dir=None):
    """Write the system-visible data (precip.nc only) into a NEW directory out_dir, and the harness-side files
    (manifest.json, expected_onset.nc) into a NEW directory harness_dir (default: out_dir + '.harness'). Never
    overwrites. The data carries CHIRPS-identical attributes and no synthetic marker (UC-12)."""
    import os

    import xarray as xr
    harness_dir = harness_dir or (str(out_dir).rstrip("/\\") + ".harness")
    if os.path.abspath(harness_dir).startswith(os.path.abspath(out_dir) + os.sep) or os.path.abspath(harness_dir) == os.path.abspath(out_dir):
        raise ValueError("the harness directory must not be inside the data directory the system reads")
    os.makedirs(out_dir, exist_ok=False)
    os.makedirs(harness_dir, exist_ok=False)
    m = manifest(g)
    t = np.array(g["times"], dtype="datetime64[ns]")
    ds = xr.Dataset({g["params"]["variable"]: (("time", "latitude", "longitude"), g["precip"], {"units": g["params"]["units"]})},
                    coords={"time": t, "latitude": g["lat"], "longitude": g["lon"]},
                    attrs=dict(PRESENTED_ATTRS))
    ds.to_netcdf(os.path.join(out_dir, "precip.nc"))
    ex = xr.Dataset({"onset_day_index": (("latitude", "longitude"), g["onset_idx"]),
                     "onset_status": (("latitude", "longitude"), g["status"], {"flag_values": "0 1 2", "flag_meanings": "onset_found no_onset_found missing"}),
                     "near_threshold": (("latitude", "longitude"), g["near_threshold"].astype(np.int8))},
                    coords={"latitude": g["lat"], "longitude": g["lon"]},
                    attrs={"reference_day0": g["times"][0], "not_a_scored_oracle": "true", "harness_side_only": "true"})
    ex.to_netcdf(os.path.join(harness_dir, "expected_onset.nc"))
    with open(os.path.join(harness_dir, "manifest.json"), "w", encoding="utf-8", newline="\n") as f:
        json.dump(m, f, indent=2, ensure_ascii=False, allow_nan=False)
        f.write("\n")
    m = dict(m, data_dir=str(out_dir).replace("\\", "/"), harness_dir=str(harness_dir).replace("\\", "/"))
    return m
