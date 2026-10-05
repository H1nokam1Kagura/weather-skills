"""Build evals/intake/requests_sample.jsonl from the population the clm owner CLEARED.

Cleared (clm-weather-skills session, 2026-10-04): runs/d4_distill/train.jsonl -- 1,380
GLM-5.1-written requests already used as D4 TRAINING data (messages[1] = request, meta.truth =
goal) -- with clarity taken from the fidelity audit runs/o0_audit/d4/databricks-gpt-oss-120b/
records.jsonl (`faithful`, keyed by line index; index alignment is asserted on `cluster`).

NEVER source this sample from results/h7_v2_2w/ (frozen TEST set), runs/f1_holdout/, results/f1/,
runs/o1_devset/, results/o0*/, runs/s1/, results/*/preds.jsonl or results/d4_serve/. A first build
did use h7_v2_2w; it was removed and the exposure recorded as clm D57.

Because these requests are D4's training data, a score on them says nothing about the D4 adapter;
it does measure THIS agent's intake (a different prompt and ask policy), which never trained on them.

    uv run python evals/intake/build_sample.py --clm C:/Users/neilha/wt/clm-weather-skills
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
from pathlib import Path

N_CLEAR = 48  # stratified over task x variable
SEED = 20261004
# Frozen test / held-out locations in the clm repo. Nothing this script reads may sit under one.
FORBIDDEN = (
    "results/h7_v2_2w/",
    "runs/f1_holdout/",
    "results/f1/",
    "runs/o1_devset/",
    "results/o0",
    "runs/s1/",
    "results/d4_serve/",
    "/preds.jsonl",
)


def _guard(clm: Path, p: Path) -> Path:
    rel = p.resolve().relative_to(clm.resolve()).as_posix()
    if any(f in rel or rel.startswith(f) for f in FORBIDDEN):
        raise SystemExit(f"refusing to read {rel}: a frozen test / held-out location")
    return p


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--clm", required=True, help="path to the clm-weather-skills checkout")
    ap.add_argument("--out", default=str(Path(__file__).with_name("requests_sample.jsonl")))
    a = ap.parse_args()
    clm = Path(a.clm)
    train_p = _guard(clm, clm / "runs" / "d4_distill" / "train.jsonl")
    audit_p = _guard(
        clm, clm / "runs" / "o0_audit" / "d4" / "databricks-gpt-oss-120b" / "records.jsonl"
    )
    train = [json.loads(x) for x in train_p.read_text(encoding="utf-8").splitlines() if x.strip()]
    audit = {
        r["unit"]: r
        for r in (
            json.loads(x) for x in audit_p.read_text(encoding="utf-8").splitlines() if x.strip()
        )
    }
    for i, t in enumerate(train):
        if i not in audit or audit[i]["cluster"] != t["meta"]["cluster"]:
            raise SystemExit(f"train line {i} does not align with the audit record; refusing")

    unclear = [i for i in range(len(train)) if audit[i]["faithful"] is False]
    clear = [i for i in range(len(train)) if audit[i]["faithful"] is True]
    rng = random.Random(SEED)
    strata: dict[tuple, list[int]] = {}
    for i in clear:
        tr = train[i]["meta"]["truth"]
        strata.setdefault((tr["task"], tr["variable"]), []).append(i)
    for k in strata:
        rng.shuffle(strata[k])
    keys = sorted(strata, key=str)
    picked: list[int] = []
    k = 0
    while len(picked) < N_CLEAR and any(strata.values()):
        s = strata[keys[k % len(keys)]]
        if s:
            picked.append(s.pop())
        k += 1
    units = sorted(picked) + sorted(unclear)

    out = []
    for i in units:
        t = train[i]
        out.append(
            {
                "id": f"d4train-{i:04d}",
                "source_unit": i,
                "cluster": t["meta"]["cluster"],
                "style": t["meta"]["style"],
                "writer": t["meta"]["writer"],
                "clarity": "clear" if audit[i]["faithful"] else "underdetermined",
                "request": t["messages"][1]["content"],
                "truth": t["meta"]["truth"],
            }
        )
    body = "".join(json.dumps(x, ensure_ascii=False) + "\n" for x in out).encode("utf-8")
    Path(a.out).write_bytes(body)
    manifest = {
        "sample_sha256": hashlib.sha256(body).hexdigest(),
        "n_units": len(out),
        "n_clear": len(picked),
        "n_underdetermined": len(unclear),
        "seed": SEED,
        "train_path": "runs/d4_distill/train.jsonl",
        "train_sha256": hashlib.sha256(train_p.read_bytes()).hexdigest(),
        "audit_path": "runs/o0_audit/d4/databricks-gpt-oss-120b/records.jsonl",
        "audit_sha256": hashlib.sha256(audit_p.read_bytes()).hexdigest(),
        "build_sample_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    }
    Path(a.out).with_suffix(".manifest.json").write_text(
        json.dumps(manifest, indent=1) + "\n", encoding="utf-8"
    )
    print(
        f"{len(out)} units ({len(picked)} clear, {len(unclear)} underdetermined) -> {a.out}\n"
        + "\n".join(f"{k} {v}" for k, v in manifest.items() if k.endswith("sha256"))
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
