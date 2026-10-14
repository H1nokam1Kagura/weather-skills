"""Frozen SIMKIT v2.7 case driver for the traced container (workflow simkit-cases.yml).

usage (inside the harness image, cwd /opt/ctl):
    python -m container_harness.case_driver admission --selector S
        Capability admission of all 66 frozen cases with the FROZEN runner (simkit.runner.run):
        missing capability => NOT_IMPLEMENTED, never attempted. Prints ADMISSION.json to stdout
        (no bind mounts: the host captures stdout). Never executes a case.
    python -m container_harness.case_driver stage CASE_ID /work      (untraced, uid 1000)
        Generates the synthetic fixture with the pinned generator into /work/gen.
    python -m container_harness.case_driver prepare CASE_ID /work    (untraced, uid 1000)
        The adapter's prepare_fixture stages the case root (source/, harness/manifest.json,
        empty run/) before the trace; record in /work/prepared-fixture.json.
    python -m container_harness.case_driver run CASE_ID /work        (traced root, uid 1000)
        THE traced controller process: the frozen runner dispatches the one case to
        WeatherAdapter(enable_run_case=True).run_case, which re-checks admission, re-verifies
        the pre-staged fixture, runs the real Controller and projects what it recorded; it
        creates nothing outside the run directory. The runner record
        goes to fd 3 and run_case's projection inputs to fd 4 (both opened by the root entry
        shell onto /harness-out; never a traced path, so neither is a created path).
    python -m container_harness.case_driver score CASE_ID RECORD EVIDENCE PROJECTION OUT
        Untraced, after the traced tree exited, same container and path namespace: the pinned
        frozen guarded scorer over the record whose evidence is the in-container projection
        (container harness observations merged). allow_unratified: oracles are PROPOSED, so the
        score is diagnostic and flagged as such, never a ratified result.

Honesty rules carried here: a case is EXECUTED only if the controller actually ran (a
checkpoint in its run dir); a projection that is UNAVAILABLE/ERROR is never replaced by the
in-process evidence for scoring; missing evidence stays missing.
"""

import hashlib
import json
import os
import re
import sys
import traceback
from pathlib import Path

CTL = Path(os.environ.get("SIMKIT_CTL", "/opt/ctl"))
SIMKIT_PINNED = Path(os.environ.get("SIMKIT_PINNED", "/opt/simkit"))
INPUTS = Path(os.environ.get("SIMKIT_INPUTS", "/opt/inputs.json"))
PROFILE_PATH = SIMKIT_PINNED / "profiles" / "cached-onset" / "profile.json"
DATASET_REL = "casesets/cached-onset-e2e-v2_7/frozen/agentic-e2e-injections.DEV-ONLY.json"
AMENDMENTS_REL = "casesets/cached-onset-e2e-v2_7/amendments.json"
SEED = 20251001
LIVE = "CAP-LIVE-ROLE"
SUBST = "CAP-ROLE-SUBSTITUTE"
# Matrix dispositions for cases the frozen runner ADMITS but this workflow does not run yet.
BLOCKED_LIVE = "BLOCKED_LIVE_ROLE_TRANSPORT"
BLOCKED_SUBST = "BLOCKED_SUBSTITUTED_ROLE_WIRING"
SUBST_FACTORY = "substituted_roles_factory"  # adapter hook the driver needs: (case, goal, run_dir)


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def frozen_root():
    """The verified frozen package (tests/frozen_v27.py: FINAL-PINS + every pinned file)."""
    sys.path.insert(0, str(CTL / "tests"))
    import frozen_v27

    root = frozen_v27.frozen_root()
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    return root, frozen_v27.FINAL_PINS_SHA256


def load_cases(root):
    """Cases exactly as the frozen CLI's dry-run sees them: effective_cases over an empty ledger
    (the frozen package ships no ratification ledger, so nothing is ratified)."""
    from simkit.core import strict_load
    from simkit.ratify import effective_cases

    ds = strict_load(str(root / DATASET_REL))
    amendments = strict_load(str(root / AMENDMENTS_REL))["amendments"]
    cases, ratified = effective_cases(ds["cases"], {"entries": []}, amendments)
    return cases, ratified


def role_mode(case):
    reqs = set(case["adapter_requirements"])
    if LIVE in reqs:
        return "live"
    return "substituted" if SUBST in reqs else "fixture"


def pair_id(case):
    return case.get("pair_id") or case["id"].rsplit("-", 1)[0]


# ------------------------------------------------------------------------------- admission


def admission(selector, smoke_ids=None):
    root, pins_sha = frozen_root()
    from simkit.runner import run as frozen_run

    from adapter_support.weather_adapter import WeatherAdapter

    cases, ratified = load_cases(root)
    real = WeatherAdapter(enable_run_case=True)
    implemented = real.implemented_capabilities()

    class _Admitted(Exception):
        pass

    class AdmissionProbe(WeatherAdapter):
        """The frozen runner's own admission, without executing anything: an admitted case
        reaches run_case, which raises instead of running. can_run mirrors the real adapter."""

        def run_case(self, case, profile):
            raise _Admitted(case["id"])

        def can_run(self):
            return real.can_run()

    probe = AdmissionProbe(enable_run_case=True)
    assert probe.implemented_capabilities() == implemented
    records = frozen_run(cases, probe, profile=None)
    by_id = {c["id"]: c for c in cases}
    rows = []
    for rec in records:
        case = by_id[rec["case_id"]]
        admitted = rec["status"] == "HARNESS_ERROR" and rec["error"].startswith("_Admitted:")
        if rec["status"] not in ("NOT_IMPLEMENTED", "HARNESS_ERROR") or (
            rec["status"] == "HARNESS_ERROR" and not admitted
        ):
            raise SystemExit(f"admission probe produced an unexpected record: {rec}")
        mode = role_mode(case)
        has_factory = callable(getattr(real, SUBST_FACTORY, None))
        if not admitted:
            disposition = "NOT_IMPLEMENTED"
        elif SUBST in case["adapter_requirements"] and not has_factory:
            disposition = BLOCKED_SUBST
        elif mode == "live" and SUBST in case["adapter_requirements"]:
            # Live + substituted roles in one run need a combining adapter hook (not built).
            disposition = BLOCKED_LIVE
        else:
            # Live-only cases run with RelayRoles through the host relay (simkit-cases.yml).
            disposition = "RUNNABLE"
        rows.append(
            {
                "case_id": case["id"],
                "pair_id": pair_id(case),
                "kind": case.get("kind"),
                "family_id": case.get("family_id"),
                "execution_mode": case.get("execution_mode"),
                "role_mode": mode,
                "admitted": admitted,
                "missing_capabilities": rec.get("missing_capabilities", []),
                "frozen_runner_status": "ADMITTED" if admitted else rec["status"],
                "disposition": disposition,
                "oracle_status": (case.get("oracle") or {}).get("status"),
                "required_capabilities": sorted(case["adapter_requirements"]),
            }
        )
    runnable = sorted(r["case_id"] for r in rows if r["disposition"] == "RUNNABLE")
    # Live-role cases make paid Kong calls: only an explicit 'live' selector or explicit ids
    # select them; 'substituted' and the push smoke never do.
    live = {r["case_id"] for r in rows if r["role_mode"] == "live"}
    offline = [i for i in runnable if i not in live]
    known = {r["case_id"] for r in rows}
    if selector in ("admission", "relay-smoke"):
        selected, unknown = [], []
    elif selector == "substituted":
        selected, unknown = offline, []
    elif selector == "live":
        selected, unknown = [i for i in runnable if i in live], []
    elif selector == "smoke":
        wanted = [i for i in (smoke_ids or []) if i in offline]
        selected, unknown = (wanted or offline)[:2], []
    else:
        wanted = [i.strip() for i in selector.split(",") if i.strip()]
        unknown = [i for i in wanted if i not in known]
        selected = [i for i in wanted if i in runnable]
    for r in rows:
        r["selected"] = r["case_id"] in selected
    missing_counts = {}
    for r in rows:
        for cap in r["missing_capabilities"]:
            missing_counts[cap] = missing_counts.get(cap, 0) + 1
    from simkit.adapter import CAPABILITY_METHODS, Adapter

    def why_missing(cap):
        """Diagnostic only (admission itself is the frozen runner's): why a cap is undeclared."""
        method = CAPABILITY_METHODS.get(cap) or WeatherAdapter.extension_methods.get(cap)
        own = getattr(WeatherAdapter, method, None) if method else None
        has_method = callable(own) and own is not getattr(Adapter, method, None)
        record = real.producers["capabilities"].get(cap) or {}
        if not has_method:
            return "NO_ADAPTER_METHOD"
        if record.get("status") == "PRODUCER_MAPPED" and not record.get("reviewer"):
            return "D4_REVIEWER_PENDING (method + PRODUCER_MAPPED, reviewer null)"
        return f"D4_{record.get('status', 'NO_RECORD')}"

    disp = {}
    for r in rows:
        disp[r["disposition"]] = disp.get(r["disposition"], 0) + 1
    return {
        "schema": "simkit.gha-admission/1",
        "frozen_final_pins_sha256": pins_sha,
        "frozen_root": str(root),
        "frozen_runner": "simkit.runner.run (frozen v2.7, FINAL-PINS)",
        "adapter": real.name,
        "adapter_can_run": real.can_run(),
        "implemented_capabilities": sorted(implemented),
        "ratified_case_ids": sorted(ratified),
        "selector": selector,
        "unknown_selected_ids": unknown,
        "selected": selected,
        "counts": {
            "total": len(rows),
            "pairs": len({r["pair_id"] for r in rows}),
            "admitted": sum(r["admitted"] for r in rows),
            "not_implemented": sum(not r["admitted"] for r in rows),
            "dispositions": disp,
            "selected": len(selected),
        },
        "missing_capability_counts": dict(
            sorted(missing_counts.items(), key=lambda kv: (-kv[1], kv[0]))
        ),
        "missing_capability_reasons": {cap: why_missing(cap) for cap in missing_counts},
        "cases": rows,
        "matrix": [{"case_id": i, "role_mode": role_mode(by_id[i])} for i in selected],
    }


# ------------------------------------------------------------------------------- stage / run


def _pins():
    spec = json.loads(INPUTS.read_text(encoding="utf-8"))
    return spec, {entry["role"]: entry for entry in spec["files"]}


def stage(case_id, work):
    work = Path(work)
    spec, pins = _pins()
    generator = Path(pins["fixture_generator"]["path"])
    if sha256(generator) != pins["fixture_generator"]["sha256"]:
        raise SystemExit("pinned generator sha256 mismatch at staging")
    sys.path[:0] = [str(SIMKIT_PINNED)]
    from simkit.fixtures import onset_synth
    from simkit.profile import Profile

    if Path(onset_synth.__file__).resolve() != generator.resolve():
        raise SystemExit("imported generator is not the pinned file")
    profile = Profile.load(pins["profile"]["path"])
    gen = work / "gen"
    gen.mkdir(parents=True, exist_ok=False)
    fixture = onset_synth.write(
        onset_synth.generate(profile, seed=SEED),
        str(gen / "fixture-nc"),
        str(gen / "fixture-harness"),
    )
    record = {
        "schema": "simkit.gha-case-stage/1",
        "case_id": case_id,
        "supplement_id": spec["supplement_id"],
        "generator_sha256": pins["fixture_generator"]["sha256"],
        "profile_sha256": pins["profile"]["sha256"],
        "seed": SEED,
        "fixture_content_sha256": fixture["content_sha256"],
        "fixture": str(gen / "fixture-nc" / "precip.nc"),
        "case_root": str(work / "case"),
        "run_dir": str(work / "case" / "run"),
    }
    print(json.dumps(record, indent=1))
    return 0


PREPARED_NAME = "prepared-fixture.json"


def _generator_provenance(spec, pins):
    return {
        "synthetic": True,
        "generator": "simkit.fixtures.onset_synth",
        "generator_sha256": pins["fixture_generator"]["sha256"],
        "supplement_id": spec["supplement_id"],
        "seed": SEED,
    }


def _fetch_version():
    skill = (CTL / "skills/chirps-fetch/SKILL.md").read_text(encoding="utf-8")
    return re.search(r'^\s*version:\s*"([^"]+)"', skill, re.M).group(1)


def prepare(case_id, work):
    """Untraced (uid 1000, after ``stage``): the adapter's own prepare_fixture stages the case
    root (source/source.zarr, harness/manifest.json, empty run/) BEFORE the trace starts, so
    none of it is a created path of the traced run (frozen all_under HARNESS_RUN_DIR). The
    record goes to /work/prepared-fixture.json; the traced ``run`` passes it to run_case,
    which re-verifies it (adopt_prepared_fixture) and stages nothing."""
    work = Path(work)
    root, _ = frozen_root()
    from adapter_support.weather_adapter import WeatherAdapter

    cases, _ratified = load_cases(root)
    case = next(c for c in cases if c["id"] == case_id)
    spec, pins = _pins()
    prepared = WeatherAdapter(enable_run_case=True).prepare_fixture(
        case,
        work / "case",
        source=work / "gen" / "fixture-nc" / "precip.nc",
        generator_provenance=_generator_provenance(spec, pins),
        presented_fetch_version=_fetch_version(),
    )
    (work / PREPARED_NAME).write_text(json.dumps(prepared, indent=1) + "\n", encoding="utf-8")
    print(json.dumps({k: prepared[k] for k in ("case_id", "case_root", "run", "source_pre")}))
    return 0


def _record_channel():
    try:
        return os.fdopen(3, "w", encoding="utf-8")
    except OSError:
        return sys.stdout


def _inputs_channel():
    """fd 4, opened by the root entry shell onto /harness-out/projection_inputs.json (never a
    traced path): run_case writes its in-process harness inputs there for the post-trace
    projection. Used only when fd 4 is verifiably that file; otherwise None (run_case then
    writes harness/evidence-projection-inputs.json, the in-process route)."""
    try:
        target = os.readlink("/proc/self/fd/4")
    except OSError:
        return None
    if not target.endswith("/projection_inputs.json"):
        return None
    return os.fdopen(4, "w", encoding="utf-8")


def _relay_roles():
    from tools.workflow.relay_roles import RelayRoles

    return RelayRoles.from_env()


DEFINITION_PATH = Path(__file__).resolve().parent / "pinned" / "definitions"
DEFINITION_PATH = DEFINITION_PATH / "agrhymet-sos-rolling.txt"


def reference_definition(path=DEFINITION_PATH):
    """Ruling D1: the controller binds the reference definition text under
    'definition:agrhymet-sos-rolling'. The text is a pinned, byte-exact input (LF-only by
    .gitattributes); its hash is checked against the frozen v2.7 source hash here, and a
    mismatch refuses the run instead of binding a different text."""
    from adapter_support.role_substitution import FROZEN_DEFINITION_SOURCE_HASH
    from tools.workflow.review_evidence import text_hash

    path = Path(path)
    observed = text_hash(path.read_bytes().decode("utf-8"))
    if observed != FROZEN_DEFINITION_SOURCE_HASH:
        raise RuntimeError(
            f"pinned reference definition {path} hashes to {observed}, "
            f"not the frozen {FROZEN_DEFINITION_SOURCE_HASH}"
        )
    return path


def case_profile(
    case,
    adapter,
    document,
    work,
    *,
    spec,
    pins,
    version,
    live,
    prepared,
    inputs_channel,
    processes_factory,
    definition=DEFINITION_PATH,
):
    """The run_case profile the traced ``run`` passes (one builder, also driven by tests)."""
    work = Path(work)
    goal = adapter.select_profile(case, document)["goal"]
    factory = getattr(adapter, SUBST_FACTORY, None)
    return {
        "profile_document": document,
        "source_ledger": document["reference_fixture"]["source_ledger"],
        "case_root": work / "case",
        "source": work / "gen" / "fixture-nc" / "precip.nc",
        "generator_provenance": _generator_provenance(spec, pins),
        "presented_fetch_version": version,
        "prepared_fixture": prepared,
        # D1: the pinned reference definition, hash-verified (a path: Controller binds it via
        # claim_gates.bind_definition_source, reading bytes).
        "definition_source": reference_definition(definition),
        "repo": CTL,
        "roles_factory": (
            # Key-free client of the host Kong relay (owner ruling: the key never enters
            # this container; only SIMKIT_RELAY_URL/TOKEN/RUN_ID/MODEL are present).
            (lambda run_dir: _relay_roles())
            if live
            else (lambda run_dir: factory(case, goal, run_dir))
        ),
        # Real memory reader: UC-07 reading is the container VM's figure (owner ruling).
        "processes_factory": processes_factory,
        "input_supplement": {
            "supplement_id": spec["supplement_id"],
            "generator_sha256": pins["fixture_generator"]["sha256"],
        },
        # run_case's harness bookkeeping leaves through fd 4, not harness/ (outside the frozen
        # created_paths allow-list HARNESS_RUN_DIR).
        "projection_inputs_channel": inputs_channel,
    }


def run(case_id, work):
    work = Path(work)
    out = _record_channel()
    inputs_channel = _inputs_channel()
    record = {"case_id": case_id, "status": "HARNESS_ERROR", "evidence": None}
    try:
        root, _ = frozen_root()
        from simkit.runner import run as frozen_run

        from adapter_support.weather_adapter import WeatherAdapter
        from tools.workflow.processes import Processes

        cases, _ratified = load_cases(root)
        case = next(c for c in cases if c["id"] == case_id)
        spec, pins = _pins()
        document = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
        adapter = WeatherAdapter(enable_run_case=True)
        factory = getattr(adapter, SUBST_FACTORY, None)
        live = role_mode(case) == "live"
        if live and SUBST in case["adapter_requirements"]:
            raise RuntimeError(f"{BLOCKED_LIVE}: live + substituted role wiring is not built")
        if live and not os.environ.get("SIMKIT_RELAY_URL"):
            raise RuntimeError(f"{BLOCKED_LIVE}: no host relay configured for this container")
        if not live and not callable(factory):
            raise RuntimeError(
                f"{BLOCKED_SUBST}: adapter has no {SUBST_FACTORY}(case, goal, run_dir)"
            )
        prepared_path = work / PREPARED_NAME
        prepared = (
            json.loads(prepared_path.read_text(encoding="utf-8"))
            if prepared_path.is_file()
            else None
        )
        if prepared is None and inputs_channel is not None:
            # In the traced container the fixture must be staged before the trace; staging
            # here would put source/ and harness/ in created_paths, outside HARNESS_RUN_DIR.
            raise RuntimeError("no pre-staged fixture (case_driver prepare did not run)")
        profile = case_profile(
            case,
            adapter,
            document,
            work,
            spec=spec,
            pins=pins,
            version=_fetch_version(),
            live=live,
            prepared=prepared,
            inputs_channel=inputs_channel,
            processes_factory=lambda run_dir: Processes(Path(run_dir) / "processes"),
        )
        (record,) = frozen_run([case], adapter, profile, only={case_id})
    except Exception as exc:  # noqa: BLE001 - recorded as the driver's own HARNESS_ERROR
        record = {
            "case_id": case_id,
            "status": "HARNESS_ERROR",
            "error": f"case_driver: {type(exc).__name__}: {exc}",
            "traceback": traceback.format_exc(limit=8),
            "evidence": None,
            "harness_error_source": "case_driver",
        }
    run_dir = work / "case" / "run"
    record["controller_ran"] = (run_dir / "checkpoint.json").is_file()
    record["fixture_prestaged"] = (work / PREPARED_NAME).is_file()
    out.write(json.dumps(record, default=str))
    out.flush()
    return 0


# ------------------------------------------------------------------------------- score


def load_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


_ERROR_TYPE = re.compile(r"^([A-Za-z_][\w.]*): ")


def runner_error_source(record, record_path):
    """Where a runner-record HARNESS_ERROR came from, never left unspecified.

    ``harness_error_source`` is the record's own (case_driver) or ``runner:<ExceptionType>``
    parsed from the frozen runner's "<Type>: <message>" error. The ROOT cause can sit
    underneath it: an injection hook abort leaves the controller's result.json (copied to
    run-copy/ beside the record) with terminal_mapping.error_type and its error, while the
    runner reports only the secondary refusal that followed. Both are recorded.
    """
    error = record.get("error")
    out = {}
    match = _ERROR_TYPE.match(error) if isinstance(error, str) else None
    out["harness_error_source"] = record.get("harness_error_source") or (
        f"runner:{match.group(1)}" if match else "runner"
    )
    result = load_json(Path(record_path).parent / "run-copy" / "result.json")
    if isinstance(result, dict):
        mapping = (
            result.get("terminal_mapping")
            if isinstance(result.get("terminal_mapping"), dict)
            else {}
        )
        if isinstance(mapping.get("error_type"), str):
            out["root_error_type"] = mapping["error_type"]
        if isinstance(result.get("error"), str):
            out["root_error"] = result["error"]
    return out


def score(case_id, record_path, evidence_path, projection_path, out_path):
    result = {
        "schema": "simkit.gha-case-score/1",
        "case_id": case_id,
        "oracles": "PROPOSED (unratified): diagnostic score, not a ratified result",
    }
    try:
        root, pins_sha = frozen_root()
        from simkit.cli import profile_refs
        from simkit.guarded import score_case_guarded
        from simkit.profile import Profile

        result["frozen_final_pins_sha256"] = pins_sha
        result["scorer_sha256"] = sha256(root / "simkit" / "score.py")
        cases, ratified = load_cases(root)
        case = next(c for c in cases if c["id"] == case_id)
        record = load_json(record_path)
        projection = load_json(projection_path) or {}
        harness_evidence = load_json(evidence_path)
        if not isinstance(record, dict):
            record = {
                "case_id": case_id,
                "status": "HARNESS_ERROR",
                "evidence": None,
                "error": "no runner record came out of the traced run",
            }
            result["harness_error_source"] = "no_runner_record"
        status = record.get("status")
        if status == "HARNESS_ERROR" and "harness_error_source" not in result:
            result.update(runner_error_source(record, record_path))
        if status == "EXECUTED" and not record.get("controller_ran"):
            status = "NOT_EXECUTED"
            result["demoted"] = "frozen runner returned but the controller wrote no checkpoint"
        evidence = None
        if status == "EXECUTED":
            if projection.get("status") == "OK":
                evidence = projection["projection"].get("evidence")
                result["evidence_source"] = "in-container projection with harness observations"
            else:
                status = "HARNESS_ERROR"
                result["harness_error_source"] = f"projection_{projection.get('status', 'ABSENT')}"
        result["run_status"] = status
        result["tracer_status"] = ((harness_evidence or {}).get("harness_observations") or {}).get(
            "tracer_status"
        )
        scored_record = {"case_id": case_id, "status": status, "evidence": evidence}
        refs = profile_refs(Profile.load(str(PROFILE_PATH)), cases)
        result["scored"] = score_case_guarded(
            case, scored_record, refs=refs, ratified=ratified, allow_unratified=True
        )
        result["record_error"] = record.get("error")
    except Exception as exc:  # noqa: BLE001
        result.update(
            run_status="HARNESS_ERROR",
            harness_error_source="scorer_driver",
            error=f"{type(exc).__name__}: {exc}",
            traceback=traceback.format_exc(limit=8),
        )
    Path(out_path).write_text(json.dumps(result, indent=1, default=str) + "\n", encoding="utf-8")
    print(json.dumps({k: result.get(k) for k in ("case_id", "run_status", "harness_error_source")}))
    return 0


def main(argv):
    cmd = argv[1] if len(argv) > 1 else ""
    if cmd == "admission":
        selector = argv[argv.index("--selector") + 1] if "--selector" in argv else "admission"
        smoke = argv[argv.index("--smoke-ids") + 1].split(",") if "--smoke-ids" in argv else None
        print(json.dumps(admission(selector, smoke), indent=1))
        return 0
    if cmd == "stage":
        return stage(argv[2], argv[3])
    if cmd == "prepare":
        return prepare(argv[2], argv[3])
    if cmd == "run":
        return run(argv[2], argv[3])
    if cmd == "score":
        return score(*argv[2:7])
    sys.exit(__doc__)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
