"""Real CPU smoke: one decision per decision point through the pinned Laya checkpoint.

    uv run --no-project --with laya==0.3.26 python shadow/decision-reviewer/eval/smoke_laya.py

Synthetic states only (no user text). Writes eval/smoke_laya_<date>.json with each answer,
the one-time load time, and per-decision latency (cold first call, then 5 warm repeats).
These are NOT evaluation results: n=3, no outcomes. They show the backend runs.
"""
import json
import platform
import statistics
import sys
import time
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from decision_shadow import backends  # noqa: E402

CASES = [
    ("escalate",
     "User request: 'show me rainfall for the farm, the usual period'. No location, no dates and no "
     "product were given, and the last session used two different regions.", None),
    ("review_verdict",
     "Request: seasonal rainfall totals for Kenya, March to May 2026, as a map.\n"
     "Proposed pipeline: chirps-fetch -> clip-region -> aggregate-temporal --period season -> plot.\n"
     "Skill rules: fetchers write precip as rates (mm day-1); run convert-to-totals before any plot "
     "so figures are period mm, not rates. Skip deaccumulate after fetch.", None),
    ("next_skill",
     "Goal: map seasonal rainfall totals for Kenya, March to May 2026.\n"
     "Done so far: chirps-fetch (rates, mm day-1) -> clip-region -> aggregate-temporal --period season.",
     {"convert-to-totals": "convert rate fields to period totals before plotting",
      "plot": "draw a map or figure from a Zarr",
      "deaccumulate": "turn cumulative-since-init fields into per-step amounts"}),
]


def main() -> int:
    out = {"date": date.today().isoformat(), "host": f"{platform.system()} {platform.release()} "
           f"{platform.machine()}", "python": platform.python_version(), "results": []}
    try:
        import laya
        import torch
        out["laya_version"] = getattr(laya, "__version__", "?")
        out["torch"] = torch.__version__
        out["torch_threads"] = torch.get_num_threads()
    except Exception as e:  # noqa: BLE001
        out["import_error"] = f"{type(e).__name__}: {e}"
    for point, state, options in CASES:
        rec = {"decision_point": point}
        try:
            first = backends.score(point, state, options, backend="laya")
            warm = []
            for _ in range(5):
                t0 = time.perf_counter()
                backends.score(point, state, options, backend="laya")
                warm.append((time.perf_counter() - t0) * 1000)
            rec.update(first)
            rec["warm_latency_ms_median"] = round(statistics.median(warm), 1)
            rec["warm_latency_ms_all"] = [round(w, 1) for w in warm]
        except Exception as e:  # noqa: BLE001
            rec["error"] = f"{type(e).__name__}: {e}"
        out["results"].append(rec)
        print(json.dumps(rec), flush=True)
    path = HERE / f"smoke_laya_{out['date']}.json"
    path.write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {path}")
    return 0 if all("error" not in r for r in out["results"]) else 1


if __name__ == "__main__":
    sys.exit(main())
