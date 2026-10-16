"""Host-side proof that the Kong key never reached a container or an artifact. Prints counts only.

The secret's VALUE is supplied to this process alone, through the environment of the workflow
step that runs it (``KONG_ANTHROPIC_KEY``); it is never printed, written, hashed into output or
placed on a command line. The script streams ``docker export <container>`` (the container's
whole filesystem, after it exited, without docker exec) and ``docker inspect <container>``
(which carries the container's environment and command) and counts occurrences of the value in
plain, base64 and URL-encoded forms; then it does the same over every file under each
``--scan-dir`` (the artifacts that will be uploaded). It also lists credential-like environment
variable NAMES from the inspect record.

usage: python -I container_harness/kong_key_scan.py --container NAME [--scan-dir DIR ...]
                                                    --out key-scan.json
Exit 0 when every count is zero and the scans actually ran; 1 otherwise.
"""

import argparse
import base64
import json
import os
import subprocess
import sys
import urllib.parse
from pathlib import Path

KEY_ENV = "KONG_ANTHROPIC_KEY"
CHUNK = 1 << 20
NAME_MARKERS = ("KONG", "ANTHROPIC", "OPENAI", "API_KEY", "SECRET")
ALLOWED_NAMES = {
    "SIMKIT_RELAY_URL",
    "SIMKIT_RELAY_TOKEN",
    "SIMKIT_RELAY_RUN_ID",
    "SIMKIT_RELAY_MODEL",
}
MIN_KEY_LENGTH = 16


def needles(key):
    raw = key.encode()
    forms = {
        raw,
        base64.b64encode(raw),
        base64.b64encode(raw).rstrip(b"="),
        base64.urlsafe_b64encode(raw).rstrip(b"="),
        urllib.parse.quote(key, safe="").encode(),
    }
    return sorted(forms, key=len)


def count_stream(stream, patterns):
    """Count occurrences of each pattern across a byte stream (overlap-safe at chunk edges)."""
    keep = max(len(p) for p in patterns) - 1
    tail = b""
    total = 0
    scanned = 0
    while True:
        chunk = stream.read(CHUNK)
        if not chunk:
            break
        scanned += len(chunk)
        window = tail + chunk
        for pattern in patterns:
            # Count matches that end inside the new chunk only (avoid double counting the tail).
            start = 0
            while (i := window.find(pattern, start)) != -1:
                if i + len(pattern) > len(tail):
                    total += 1
                start = i + 1
        tail = window[-keep:] if keep else b""
    return total, scanned


def scan_container(name, patterns):
    proc = subprocess.Popen(["docker", "export", name], stdout=subprocess.PIPE)
    matches, scanned = count_stream(proc.stdout, patterns)
    rc = proc.wait()
    inspect = subprocess.run(["docker", "inspect", name], capture_output=True)
    info = json.loads(inspect.stdout or b"[{}]")[0] if inspect.returncode == 0 else {}
    env = (info.get("Config") or {}).get("Env") or []
    names = sorted(e.split("=", 1)[0] for e in env)
    from io import BytesIO

    inspect_matches, _ = count_stream(BytesIO(inspect.stdout), patterns)
    return {
        "docker_export_rc": rc,
        "filesystem_bytes_scanned": scanned,
        "filesystem_matches": matches,
        "inspect_rc": inspect.returncode,
        "inspect_matches": inspect_matches,
        "env_names": names,
        "credential_like_env_names": [
            n for n in names if any(m in n.upper() for m in NAME_MARKERS) and n not in ALLOWED_NAMES
        ],
    }


def scan_dirs(dirs, patterns):
    files = 0
    matches = 0
    flagged = []
    for root in dirs:
        for path in sorted(Path(root).rglob("*")):
            if not path.is_file():
                continue
            files += 1
            with path.open("rb") as handle:
                found, _ = count_stream(handle, patterns)
            if found:
                matches += found
                flagged.append(str(path.relative_to(root)))
    return {"files_scanned": files, "matches": matches, "files_with_matches": flagged}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--container")
    ap.add_argument("--scan-dir", action="append", default=[])
    ap.add_argument("--out", required=True)
    args = ap.parse_args(argv)
    key = os.environ.get(KEY_ENV) or ""
    result = {
        "schema": "simkit.kong-key-scan/1",
        "key_supplied_to_scanner": bool(key),
        "key_length_ok": len(key) >= MIN_KEY_LENGTH,
        "forms_searched": ["raw", "base64", "base64-unpadded", "base64url-unpadded", "urlencoded"],
    }
    ok = result["key_supplied_to_scanner"] and result["key_length_ok"]
    if ok:
        patterns = needles(key)
        if args.container:
            result["container"] = scan_container(args.container, patterns)
            c = result["container"]
            ok = ok and c["docker_export_rc"] == 0 and c["filesystem_bytes_scanned"] > 0
            ok = ok and c["inspect_rc"] == 0 and c["filesystem_matches"] == 0
            ok = ok and c["inspect_matches"] == 0 and not c["credential_like_env_names"]
        if args.scan_dir:
            result["artifacts"] = scan_dirs(args.scan_dir, patterns)
            ok = ok and result["artifacts"]["matches"] == 0
        del patterns
    del key
    result["key_absent"] = ok
    Path(args.out).write_text(json.dumps(result, indent=1) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=1))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
