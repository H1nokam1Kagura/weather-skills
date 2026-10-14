"""Audited tool catalogue and parse-only adapter, without invoking skill wrappers."""

import contextlib
import importlib.metadata
import importlib.util
import json
import os
import stat
import sys
from pathlib import Path

from .contracts import Refused, digest

CATALOG = {
    "onset-date": ("onset_date.py", "onset_date"),
    "plot-onset": ("plot_onset.py", "plot_onset"),
    "check-artifact": ("check_artifact.py", "check_artifact"),
    "verify-run": ("verify_run.py", "verify_run"),
}
# The runtime manifest an approval binds carries a build identity (FX-RUNTIME-MANIFEST-BUMP,
# field build_id). Read from this repo-relative file when present; absent stays absent.
RUNTIME_MANIFEST = "runtime-manifest.json"


def safe_path(path):
    path = Path(path).absolute()
    if str(path).startswith(("\\\\", "//")):
        raise Refused("network/device paths are outside this profile")
    for part in (path, *path.parents):
        if part.exists() or part.is_symlink():
            info = part.lstat()
            if part.is_symlink() or getattr(info, "st_file_attributes", 0) & 0x400:
                raise Refused(f"links/reparse points are not permitted: {part}")
    return path.resolve()


def tree_hash(path):
    import hashlib

    path = safe_path(path)
    if not path.exists():
        raise Refused(f"missing artifact: {path}")
    files = [path] if path.is_file() else sorted(path.rglob("*"))
    entries = []
    for file in files:
        safe_path(file)
        if not file.is_file():
            continue
        if not stat.S_ISREG(file.stat().st_mode):
            raise Refused("only regular artifact files are supported")
        h = hashlib.sha256()
        with file.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                h.update(chunk)
        entries.append(
            (file.name if path.is_file() else file.relative_to(path).as_posix(), h.hexdigest())
        )
    if not entries:
        raise Refused("empty artifact")
    return digest(entries)


def script(repo, skill):
    if skill not in CATALOG:
        raise Refused(f"tool is not allowlisted: {skill}")
    return safe_path(Path(repo) / "skills" / skill / "scripts" / CATALOG[skill][0])


def load(repo, skill):
    path = script(repo, skill)
    name = "workflow_" + skill.replace("-", "_")
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return getattr(module, CATALOG[skill][1])


def preflight(repo, skill, argv):
    from weather_skills_core import standard_args
    from weather_skills_core.decorator import ARGS_ATTR
    from weather_skills_core.errors import SkillError

    if not isinstance(argv, list) or not all(isinstance(x, str) for x in argv):
        raise Refused("tool arguments must be a string array")
    if any(x in {"-h", "--help", "--version"} or "${" in x or "\0" in x for x in argv):
        raise Refused("non-execution flag or unresolved placeholder")
    fn = load(repo, skill)
    # Argparse abbreviations can silently reinterpret a misspelled plan flag.
    fn.parser.allow_abbrev = False
    try:
        parsed = fn.parser.parse_args(standard_args.rewrite_bbox_argv(argv))
        converted = standard_args.convert_standard_args(parsed, getattr(fn, ARGS_ATTR))
    except (SystemExit, ValueError, SkillError) as exc:
        raise Refused(f"invalid CLI contract for {skill}") from exc

    # The real parser returns pathlib objects for outputs. Receipts must be JSON,
    # while the converted values remain available for type-contract checks.
    def encode(value):
        if isinstance(value, Path):
            return str(value)
        if hasattr(value, "isoformat"):
            return value.isoformat()
        raise TypeError(f"unsupported parser receipt type: {type(value).__name__}")

    return json.loads(json.dumps(vars(parsed), default=encode)), converted


def build_manifest(repo):
    repo = Path(repo)
    paths = sorted(
        set(
            list((repo / "skills").rglob("*.py"))
            + list((repo / "skills").rglob("*.toml"))
            + list((repo / "skills").rglob("SKILL.md"))
            + list((repo / "registry").glob("*.toml"))
            + list((repo / "agents").glob("*.md"))
            + list((repo / "tools" / "workflow").glob("*.py"))
            + [repo / "pyproject.toml"]
        )
    )
    manifest = {p.relative_to(repo).as_posix(): tree_hash(p) for p in paths}
    core = importlib.metadata.distribution("weather-skills-core")
    origin = json.loads(core.read_text("direct_url.json") or "{}")
    if origin.get("vcs_info", {}).get("commit_id") != "a4110e30c8637ea99d79f752499d00e4cd65fafb":
        raise Refused("runtime core revision differs from the admitted profile")
    manifest["@runtime"] = {
        "python": sys.version,
        "packages": {d.metadata["Name"]: d.version for d in importlib.metadata.distributions()},
        "core_origin": origin,
    }
    runtime_file = repo / RUNTIME_MANIFEST
    if runtime_file.exists():
        manifest["@runtime"]["build_id"] = runtime_build_id(runtime_file)
    import tomllib

    definition_file = repo / "skills/onset-date/references/onset_definitions.toml"
    definition = tomllib.loads(definition_file.read_text(encoding="utf-8"))["definitions"][
        "agrhymet-sos-rolling"
    ]
    if (
        definition["time_basis"] != "rolling_daily"
        or definition["trigger"] != {"window_days": 10, "total_mm": 25.0, "total_op": ">="}
        or definition["confirm"]
        != {"window_days": 20, "total_mm": 20.0, "total_op": ">=", "after_days": 10}
        or definition["veto"] != {"mode": "none"}
    ):
        raise Refused("definition changed; cached-onset/v1 must be reviewed")
    return manifest


def runtime_build_id(path):
    """The build_id of a runtime manifest file: a JSON object with a nonempty string."""
    try:
        value = json.loads(safe_path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise Refused("runtime manifest unreadable", gate="review_binding") from exc
    build_id = value.get("build_id") if isinstance(value, dict) else None
    if not isinstance(build_id, str) or not build_id:
        raise Refused("runtime manifest lacks a build_id", gate="review_binding")
    return build_id


def child_environment(scratch=None):
    env = os.environ.copy()
    for key in list(env):
        if key.startswith("CLAUDE_CODE_") or key in {
            "CLAUDECODE",
            "CLAUDE_PID",
            "WS_DECISION_SHADOW",
        }:
            env.pop(key, None)
    env.update(
        PYTHONUTF8="1",
        PYTHONIOENCODING="utf-8",
        MPLBACKEND="Agg",
        OMP_NUM_THREADS="1",
        OPENBLAS_NUM_THREADS="1",
        MKL_NUM_THREADS="1",
        # The controller runs every skill with its own interpreter (no network, no uv), so
        # verify-run replays recorded steps the same way instead of `uv run --script`.
        WEATHER_SKILLS_REPLAY_RUNNER="python",
    )
    if scratch is not None:
        scratch = safe_path(scratch)
        scratch.mkdir(parents=True, exist_ok=True)
        env.update(scratch_environment(scratch))
    return env


def scratch_environment(scratch):
    """HOME-derived cache/config/temp locations, all under the run's scratch directory.

    Applied to every child (child_environment) and, for the duration of Controller.run, to
    the controller process itself (confined_process), so neither writes ~/.config/matplotlib,
    ~/.cache, ~/.local/share/cartopy or /tmp: filesystem_snapshot.created_paths must stay
    under HARNESS_RUN_DIR (frozen v2.7 all_under, every case). HOME itself is not changed
    (role transports read their own credentials from it). The read-only pre-seeded
    CARTOPY_DATA_DIR is left as is: reads create nothing.
    """
    scratch = Path(scratch)
    return {
        "TMP": str(scratch),
        "TEMP": str(scratch),
        "TMPDIR": str(scratch),
        "MPLCONFIGDIR": str(scratch / "matplotlib"),
        "XDG_CACHE_HOME": str(scratch / "xdg-cache"),
        "XDG_CONFIG_HOME": str(scratch / "xdg-config"),
        # cartopy's writable data_dir (where a missing shapefile download lands)
        "XDG_DATA_HOME": str(scratch / "xdg-data"),
    }


@contextlib.contextmanager
def confined_process(scratch):
    """Controller-native confinement of THIS process's HOME-derived caches and temp files:
    scratch_environment in os.environ, tempfile.tempdir, and an already-imported cartopy's
    writable dirs. Everything is restored on exit. matplotlib reads MPLCONFIGDIR when first
    imported, which for the controller happens inside the run (in-process skill preflight)."""
    import tempfile

    scratch = safe_path(scratch)
    scratch.mkdir(parents=True, exist_ok=True)
    values = scratch_environment(scratch)
    saved_env = {key: os.environ.get(key) for key in values}
    saved_tempdir = tempfile.tempdir
    cartopy = sys.modules.get("cartopy")
    saved_cartopy = None
    if cartopy is not None and isinstance(getattr(cartopy, "config", None), dict):
        saved_cartopy = {k: cartopy.config.get(k) for k in ("cache_dir", "data_dir")}
        cartopy.config["cache_dir"] = scratch / "cartopy_cache_dir"
        cartopy.config["data_dir"] = Path(values["XDG_DATA_HOME"]) / "cartopy"
    os.environ.update(values)
    tempfile.tempdir = str(scratch)
    try:
        yield values
    finally:
        tempfile.tempdir = saved_tempdir
        for key, value in saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        if saved_cartopy is not None:
            cartopy.config.update(saved_cartopy)
