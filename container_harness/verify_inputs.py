"""Verify every pinned harness input inside the image against inputs.json (build AND run time).

usage: python -I verify_inputs.py /opt/inputs.json [--out result.json]
Exit 0 only if every listed file exists and its raw-byte sha256 matches. Stdlib only.
"""

import hashlib
import json
import sys


def verify(spec_path):
    with open(spec_path, encoding="utf-8") as stream:
        spec = json.load(stream)
    results = []
    for entry in spec["files"]:
        try:
            with open(entry["path"], "rb") as stream:
                actual = hashlib.sha256(stream.read()).hexdigest()
        except OSError as exc:
            actual = "unreadable:" + type(exc).__name__
        results.append(
            {
                "path": entry["path"],
                "role": entry["role"],
                "expected_sha256": entry["sha256"],
                "actual_sha256": actual,
                "ok": actual == entry["sha256"],
            }
        )
    return {
        "schema": "simkit.harness-input-verification/1",
        "supplement_id": spec.get("supplement_id"),
        "controller_commit": spec.get("controller_commit"),
        "ok": bool(results) and all(r["ok"] for r in results),
        "files": results,
    }


def main(argv):
    result = verify(argv[1])
    text = json.dumps(result, indent=1)
    if "--out" in argv:
        with open(argv[argv.index("--out") + 1], "w", encoding="utf-8") as stream:
            stream.write(text + "\n")
    print(text)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
