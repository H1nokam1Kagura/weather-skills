# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core @ git+https://github.com/rhiza-research/weather-skills-core@a4110e30c8637ea99d79f752499d00e4cd65fafb",
#   "cftime",
#   "numpy",
#   "xarray",
#   "zarr",
#   "pillow",
# ]
# ///
"""Deterministic evaluation gate for a weather-skills artifact (writes only to a temp dir).

Reads the artifact's weather_skills_history, re-hashes every recorded input,
optionally replays the recorded chain into a temp dir and compares data
fingerprints, then prints a gate card: VERDICT PASS / BLOCK / UNVERIFIABLE
(exit 0 / 1 / 2). No model or network call is involved in deciding the verdict.
"""

import hashlib
import json
import re
import subprocess
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np
from weather_skills_core import weather_skill
from weather_skills_core.errors import SkillError
from weather_skills_core.provenance import (
    HISTORY_ATTR,
    hash_zarr,
    input_items,
    load_figure_history,
    parse_chain,
    validate_chain,
)

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.0.1"

PASS = "PASS"
BLOCK = "BLOCK"
UNVERIFIABLE = "UNVERIFIABLE"
INFO = "INFO"

EXIT_CODES = {PASS: 0, BLOCK: 1, UNVERIFIABLE: 2}
FIGURE_SUFFIXES = {".png", ".jpg", ".jpeg", ".html", ".htm"}
SKILLS_ROOT = Path(__file__).resolve().parents[2]
REPLAY_TIMEOUT_SECONDS = 900
_VERSION_RE = re.compile(r'^_SKILL_VERSION\s*=\s*"([^"]*)"', re.MULTILINE)


class GateBlock(SkillError):
    """Verdict BLOCK: a check positively failed. Exits 1."""

    exit_code = 1


class GateUnverifiable(SkillError):
    """Verdict UNVERIFIABLE: the evidence needed to decide is missing. Exits 2."""

    exit_code = 2


@dataclass
class Check:
    id: str
    kind: str
    subject: str
    status: str
    observed: str
    expected: str


class Gate:
    """Accumulates checks in order and derives the verdict from them."""

    def __init__(self):
        self.checks: list[Check] = []

    def add(self, kind, subject, status, observed, expected) -> Check:
        check = Check(f"C{len(self.checks) + 1}", kind, subject, status, observed, expected)
        self.checks.append(check)
        return check

    def verdict(self) -> tuple[str, str]:
        blocking = [c for c in self.checks if c.status == BLOCK]
        if blocking:
            ids = ", ".join(c.id for c in blocking)
            return BLOCK, f"check(s) {ids} failed"
        unverifiable = [c for c in self.checks if c.status == UNVERIFIABLE]
        if unverifiable:
            ids = ", ".join(c.id for c in unverifiable)
            return UNVERIFIABLE, f"check(s) {ids} could not be decided"
        substantive = [c for c in self.checks if c.kind != "provenance" and c.status == PASS]
        if not substantive:
            # Absence of evidence is never success.
            return UNVERIFIABLE, "no input or replay check could be run"
        return PASS, f"all {len(substantive)} input/replay check(s) passed"


# --------------------------------------------------------------------------- reading


def _read_histories(path: Path) -> tuple[str, dict, str | None]:
    """Return ``(kind, {label: raw_or_list}, error)`` for the artifact."""
    if not path.exists():
        return "missing", {}, f"{path} does not exist"
    if path.is_dir():
        import xarray as xr

        try:
            with xr.open_zarr(path, consolidated=None) as ds:
                raw = ds.attrs.get(HISTORY_ATTR)
        except Exception as exc:  # noqa: BLE001
            return "zarr", {}, f"could not open {path.name} as a zarr store: {exc}"
        return "zarr", ({path.name: raw} if raw else {}), None
    suffix = path.suffix.lower()
    if suffix == ".png":
        from PIL import Image

        try:
            with Image.open(path) as img:
                info = dict(img.info)
        except Exception as exc:  # noqa: BLE001
            return "figure", {}, f"could not open {path.name} as a PNG: {exc}"
        raws = {}
        for key in sorted(info):
            if key == HISTORY_ATTR and info[key]:
                raws[path.name] = info[key]
            elif key.startswith(f"{HISTORY_ATTR}_") and info[key]:
                raws[key[len(HISTORY_ATTR) + 1 :]] = info[key]
        return "figure", raws, None
    if suffix in FIGURE_SUFFIXES:
        chain = load_figure_history(path)
        return "figure", ({path.name: chain} if chain is not None else {}), None
    return "unknown", {}, f"{path.name} is neither a zarr directory nor a stamped figure"


def _parse(raw) -> tuple[list | None, list]:
    if isinstance(raw, list):
        chain = raw
    else:
        try:
            chain = parse_chain(raw)
        except ValueError as exc:
            return None, [str(exc)]
    violations, _notes = validate_chain(chain, HISTORY_ATTR)
    if not chain:
        violations.append("chain is empty")
    return chain, violations


def _short(digest: str | None) -> str:
    if not digest:
        return "-"
    return f"{digest[:12]}..{digest[-4:]}" if len(digest) > 20 else digest


def _hash_path(path: Path) -> str:
    if path.is_dir():
        return hash_zarr(path)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _resolve(basename: str, search_dirs: list[Path]) -> Path | None:
    for directory in search_dirs:
        candidate = directory / basename
        if candidate.exists():
            return candidate
    return None


# --------------------------------------------------------------------------- input hashes


def _check_inputs(gate: Gate, chain: list, search_dirs: list[Path], verified: dict, where=""):
    """Re-hash every recorded input (recursing into join subgraphs)."""
    for idx, step in enumerate(chain, start=1):
        if not isinstance(step, dict):
            continue
        skill = step.get("skill", "?")
        for item in input_items(step):
            basename = item.get("basename") or "?"
            recorded = item.get("hash")
            key = (basename, recorded)
            if key in verified:
                continue
            subject = f"{where}step {idx} {skill} <- {basename}"
            expected = f"sha256 {_short(recorded)} (recorded in history)"
            found = _resolve(basename, search_dirs)
            if found is None:
                dirs = ", ".join(str(d) for d in search_dirs)
                gate.add(
                    "input-hash",
                    subject,
                    UNVERIFIABLE,
                    f"input not found in: {dirs}",
                    expected,
                )
                verified[key] = None
            else:
                actual = _hash_path(found)
                status = PASS if actual == recorded else BLOCK
                note = "" if status == PASS else " -- changed since the step ran"
                gate.add(
                    "input-hash",
                    subject,
                    status,
                    f"sha256 {_short(actual)} at {found}{note}",
                    expected,
                )
                verified[key] = found if status == PASS else None
            nested = item.get("history")
            if isinstance(nested, list) and nested:
                _check_inputs(gate, nested, search_dirs, verified, where=f"{where}{basename} > ")


def _fetch_steps(chain: list) -> list[str]:
    out = []
    for step in chain:
        if not isinstance(step, dict):
            continue
        if step.get("input") is None:
            out.append(step.get("skill", "?"))
        for item in input_items(step):
            nested = item.get("history")
            if isinstance(nested, list):
                out += _fetch_steps(nested)
    return out


# --------------------------------------------------------------------------- fingerprints


def _canonical_bytes(values: np.ndarray) -> bytes:
    arr = np.asarray(values)
    if arr.dtype.kind in "mM":
        return np.ascontiguousarray(arr.view("int64")).tobytes()
    if arr.dtype.kind == "f":
        arr = np.array(arr, copy=True)
        arr[np.isnan(arr)] = np.nan  # one NaN bit pattern
        return np.ascontiguousarray(arr).tobytes()
    if arr.dtype.kind in "biuc":
        return np.ascontiguousarray(arr).tobytes()
    return "\x1f".join(str(v) for v in arr.ravel().tolist()).encode()


def _fingerprint(path: Path) -> dict:
    """Per-variable sha256 over dims, shape, dtype, units and values."""
    import xarray as xr

    with xr.open_zarr(path, consolidated=None) as ds:
        ds = ds.load()
        out = {}
        for name in sorted(ds.variables, key=str):
            var = ds[name].variable
            values = np.asarray(var.values)
            h = hashlib.sha256()
            header = {
                "dims": list(var.dims),
                "shape": list(values.shape),
                "dtype": str(values.dtype),
                "units": str(var.attrs.get("units", "")),
            }
            h.update(json.dumps(header, sort_keys=True).encode())
            h.update(_canonical_bytes(values))
            out[str(name)] = {"hash": h.hexdigest(), "values": values, **header}
    return out


def _overall(fp: dict) -> str:
    h = hashlib.sha256()
    for name in sorted(fp):
        h.update(f"{name}={fp[name]['hash']};".encode())
    return h.hexdigest()


def _compare(replayed: Path, reference: Path, rtol: float) -> tuple[str, str, str]:
    """Return ``(status, observed, expected)`` comparing two Zarr fingerprints."""
    got, want = _fingerprint(replayed), _fingerprint(reference)
    diffs, tolerated = [], []
    for name in sorted(set(got) | set(want)):
        if name not in got:
            diffs.append(f"{name}: missing from replay")
            continue
        if name not in want:
            diffs.append(f"{name}: not in reference")
            continue
        a, b = got[name], want[name]
        if a["hash"] == b["hash"]:
            continue
        if a["dims"] != b["dims"] or a["shape"] != b["shape"]:
            diffs.append(f"{name}: dims/shape {a['dims']}{a['shape']} vs {b['dims']}{b['shape']}")
            continue
        if a["units"] != b["units"]:
            diffs.append(f"{name}: units {a['units']!r} vs {b['units']!r}")
            continue
        va, vb = a["values"], b["values"]
        if va.dtype.kind in "iuf" and vb.dtype.kind in "iuf":
            fa, fb = va.astype("float64"), vb.astype("float64")
            both = np.isfinite(fa) & np.isfinite(fb)
            nan_mismatch = not np.array_equal(np.isnan(fa), np.isnan(fb))
            max_abs = float(np.max(np.abs(fa[both] - fb[both]))) if both.any() else 0.0
            if (
                not nan_mismatch
                and rtol > 0
                and np.allclose(fa, fb, rtol=rtol, atol=0.0, equal_nan=True)
            ):
                tolerated.append(f"{name} (max |diff| {max_abs:.3g})")
                continue
            extra = ", NaN pattern differs" if nan_mismatch else ""
            diffs.append(f"{name}: values differ, max |diff| {max_abs:.3g}{extra}")
        else:
            diffs.append(f"{name}: values differ")
    observed = f"replay fingerprint {_short(_overall(got))} ({len(got)} vars)"
    expected = f"fingerprint {_short(_overall(want))} of {reference.name}"
    if rtol > 0:
        expected += f" within rtol={rtol:g}"
    if diffs:
        return BLOCK, f"{observed}; differs -- " + "; ".join(diffs), expected
    if tolerated:
        return PASS, f"{observed}; equal within tolerance: " + ", ".join(tolerated), expected
    return PASS, f"{observed}; bit-identical values", expected


# --------------------------------------------------------------------------- replay


def _skill_script(skill: str) -> Path | None:
    scripts = sorted((SKILLS_ROOT / skill / "scripts").glob("*.py"))
    return scripts[0] if len(scripts) == 1 else None


def _local_version(script: Path) -> str | None:
    m = _VERSION_RE.search(script.read_text(encoding="utf-8"))
    return m.group(1) if m else None


def _runner(script: Path) -> list[str]:
    """Command prefix that runs a sibling skill script the way the agent does."""
    return ["uv", "run", "--quiet", "--script", str(script)]


def _cli_args(args: dict) -> list[str]:
    out = []
    for dest, value in sorted((args or {}).items()):
        flag = "--" + dest.replace("_", "-")
        if value is None or value is False:
            continue
        if value is True:
            out.append(flag)
        elif isinstance(value, list):
            out += [f"{flag}={item}" for item in value]
        elif isinstance(value, dict):
            out.append(f"{flag}={json.dumps(value, sort_keys=True)}")
        else:
            out.append(f"{flag}={value}")
    return out


def _verified_inputs(step: dict, verified: dict) -> list[Path] | None:
    items = input_items(step)
    if not items:
        return None
    paths = [verified.get((i.get("basename") or "?", i.get("hash"))) for i in items]
    return paths if all(paths) else None


def _replay(gate: Gate, chain: list, kind: str, artifact: Path, verified: dict, rtol: float):
    if kind == "figure":
        steps, last = chain[:-1], chain[-1]
        target = _verified_inputs(last, verified) if isinstance(last, dict) else None
        if not target or len(target) != 1:
            gate.add(
                "replay",
                f"data plotted by {last.get('skill', '?') if isinstance(last, dict) else '?'}",
                UNVERIFIABLE,
                "the plotted data is not a single hash-verified input on disk",
                "a verified upstream data artifact to regenerate and compare",
            )
            return
        target = target[0]
        figure_step = last.get("skill", "?")
    else:
        steps, target, figure_step = chain, artifact, None

    start = next(
        (k for k, s in enumerate(steps) if isinstance(s, dict) and _verified_inputs(s, verified)),
        None,
    )
    if start is None:
        if figure_step is not None:
            gate.add(
                "replay",
                f"data plotted by {figure_step}",
                INFO,
                f"nothing to replay: {target.name} is itself hash-verified and only remote "
                "fetch steps precede it",
                "a transform step between a verified input and the plot",
            )
            return
        gate.add(
            "replay",
            artifact.name,
            UNVERIFIABLE,
            "no hash-verified on-disk input to replay from (remote fetch steps are never re-run)",
            "a verified input upstream of at least one transform step",
        )
        return

    with tempfile.TemporaryDirectory(prefix="verify-run-") as tmp:
        prev: list[Path] = _verified_inputs(steps[start], verified)
        for k in range(start, len(steps)):
            step = steps[k]
            skill = step.get("skill", "?")
            subject = f"replay step {k + 1} {skill}"
            script = _skill_script(skill)
            if script is None:
                gate.add(
                    "replay",
                    subject,
                    UNVERIFIABLE,
                    f"skill {skill!r} is not installed next to verify-run",
                    f"skills/{skill}/scripts/<one>.py",
                )
                return
            local = _local_version(script)
            if local != step.get("version"):
                gate.add(
                    "skill-version",
                    subject,
                    INFO,
                    f"replayed with local v{local}",
                    f"recorded v{step.get('version')}",
                )
            out = Path(tmp) / f"replay_step{k + 1}.zarr"
            cmd = _runner(script) + _cli_args(step.get("args"))
            for path in prev:
                cmd.append(f"--input={path}")
            cmd.append(f"--output={out}")
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=False,
                timeout=REPLAY_TIMEOUT_SECONDS,
            )
            if proc.returncode != 0 or not out.exists():
                tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-3:]
                gate.add(
                    "replay",
                    subject,
                    UNVERIFIABLE,
                    f"replay command exited {proc.returncode}: " + " | ".join(tail),
                    "the recorded step re-runs cleanly",
                )
                return
            if k + 1 < len(steps):
                reference = _verified_inputs(steps[k + 1], verified)
                if reference and len(reference) == 1:
                    status, observed, expected = _compare(out, reference[0], rtol)
                    gate.add("replay-step", subject, status, observed, expected)
            prev = [out]
        status, observed, expected = _compare(prev[0], target, rtol)
        subject = f"replay of {target.name}"
        if figure_step is not None:
            subject += f" (data plotted by {figure_step})"
        gate.add("replay", subject, status, observed, expected)


# --------------------------------------------------------------------------- card


def _render(artifact: Path, kind: str, chains: dict, scope: str, gate: Gate) -> str:
    verdict, reason = gate.verdict()
    lines = ["=== verify-run gate card ===", f"artifact : {artifact} ({kind})"]
    for label, chain in chains.items():
        if isinstance(chain, list):
            names = " -> ".join(s.get("skill", "?") if isinstance(s, dict) else "?" for s in chain)
            prefix = f"chain[{label}]" if len(chains) > 1 else "chain"
            lines.append(f"{prefix:<9}: {len(chain)} step(s): {names}")
    lines += [
        f"scope    : {scope}",
        f"VERDICT  : {verdict}",
        f"reason   : {reason}",
        f"exit     : {EXIT_CODES[verdict]}",
        "",
        "checks:",
    ]
    for c in gate.checks:
        lines.append(f"  [{c.status}] {c.id} {c.kind}: {c.subject}")
        lines.append(f"      observed: {c.observed}")
        lines.append(f"      expected: {c.expected}")
    return "\n".join(lines)


def _emit(artifact, kind, chains, scope, gate, output_format):
    if output_format == "json":
        verdict, reason = gate.verdict()
        print(
            json.dumps(
                {
                    "schema": "verify-run.gate/1",
                    "artifact": str(artifact.resolve()),
                    "kind": kind,
                    "scope": scope,
                    "verdict": verdict,
                    "exit_code": EXIT_CODES[verdict],
                    "reason": reason,
                    "checks": [asdict(c) for c in gate.checks],
                }
            )
        )
    else:
        print(_render(artifact, kind, chains, scope, gate))


@weather_skill(
    name="verify-run",
    version=_SKILL_VERSION,
    output=False,
)
@weather_skill.argument(
    "-i",
    "--input",
    required=True,
    help="Artifact to verify: a zarr dir or a stamped figure (.png/.jpg/.html).",
)
@weather_skill.argument(
    "--replay",
    action="store_true",
    help="Also re-run the recorded transform steps in a temp dir and compare data fingerprints.",
)
@weather_skill.argument(
    "--require-replay",
    action="store_true",
    help="Final-result gate: implies --replay and requires a successful final data comparison.",
)
@weather_skill.argument(
    "--rtol",
    type=float,
    default=0.0,
    help="Relative tolerance for replay value comparison (default 0: bit-identical).",
)
@weather_skill.argument(
    "--search-dir",
    action="append",
    default=None,
    help="Extra directory to look for recorded inputs (repeatable; artifact's dir is first).",
)
@weather_skill.argument(
    "--format", dest="output_format", choices=["human", "json"], default="human"
)
def verify_run(
    input, replay, rtol, search_dir, require_replay=False, output_format="human", **kwargs
):
    """Deterministic evaluation gate for a weather-skills artifact (writes only to a temp dir)."""
    artifact = Path(input)
    gate = Gate()
    replay = replay or require_replay
    scope = "provenance + recorded-input sha256" + (
        f" + replay (rtol={rtol:g})" if replay else " (replay not requested)"
    )
    if require_replay:
        scope += " (final data comparison required)"
    if rtol < 0:
        gate.add("usage", "--rtol", UNVERIFIABLE, f"rtol={rtol:g}", "rtol >= 0")
        _emit(artifact, "?", {}, scope, gate, output_format)
        raise GateUnverifiable("verify-run: VERDICT UNVERIFIABLE", prefix=False)

    kind, raws, error = _read_histories(artifact)
    chains: dict = {}
    if error is not None or not raws:
        gate.add(
            "provenance",
            artifact.name,
            UNVERIFIABLE,
            error or f"no {HISTORY_ATTR} recorded",
            f"a non-empty, schema-valid {HISTORY_ATTR}",
        )
    else:
        for label, raw in raws.items():
            chain, violations = _parse(raw)
            chains[label] = chain
            subject = artifact.name if len(raws) == 1 else f"{artifact.name} [{label}]"
            if violations:
                gate.add(
                    "provenance",
                    subject,
                    UNVERIFIABLE,
                    "invalid history: " + "; ".join(violations[:3]),
                    f"a non-empty, schema-valid {HISTORY_ATTR}",
                )
            else:
                gate.add(
                    "provenance",
                    subject,
                    PASS,
                    f"{len(chain)}-step chain, schema valid",
                    f"a non-empty, schema-valid {HISTORY_ATTR}",
                )

    valid = {k: v for k, v in chains.items() if isinstance(v, list) and v}
    search_dirs = [artifact.parent] + [Path(d) for d in (search_dir or [])]
    verified: dict = {}
    for chain in valid.values():
        _check_inputs(gate, chain, search_dirs, verified)
        for step in chain:
            if isinstance(step, dict) and step.get("dirty") is True:
                gate.add(
                    "dirty",
                    f"{step.get('skill', '?')} @{str(step.get('commit', '?'))[:12]}",
                    INFO,
                    "ran from a working tree with uncommitted changes",
                    "a clean commit (the recorded commit may not match the code that ran)",
                )
        fetchers = _fetch_steps(chain)
        if fetchers:
            gate.add(
                "scope",
                ", ".join(sorted(set(fetchers))),
                INFO,
                "remote fetch step(s) are not re-executed; their outputs are checked by hash only",
                "-",
            )

    if replay and valid:
        if len(valid) > 1:
            gate.add(
                "replay",
                artifact.name,
                UNVERIFIABLE,
                f"{len(valid)} separately stamped branches; multi-branch figure replay "
                "is not supported",
                "a single-branch artifact",
            )
        else:
            _replay(gate, next(iter(valid.values())), kind, artifact, verified, rtol)

    if require_replay and not any(c.kind == "replay" and c.status == PASS for c in gate.checks):
        gate.add(
            "required-replay",
            artifact.name,
            UNVERIFIABLE,
            "no successful final data comparison; input hashes alone do not verify output values",
            "a PASS replay comparison of the final dataset or the figure's plotted data",
        )

    _emit(artifact, kind, chains, scope, gate, output_format)
    verdict, _reason = gate.verdict()
    if verdict == BLOCK:
        raise GateBlock("verify-run: VERDICT BLOCK", prefix=False)
    if verdict == UNVERIFIABLE:
        raise GateUnverifiable("verify-run: VERDICT UNVERIFIABLE", prefix=False)


if __name__ == "__main__":
    verify_run()
