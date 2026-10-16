"""CAP-FIXTURE-OP and CAP-REREVIEW on the real controller, through WeatherAdapter.run_case.

The frozen cases run verbatim (id, profile, budget, agent_roles, injection_schedule and
adapter_requirements): the two records are signed in a test-local copy of the producer file;
the committed records are signed too (D4 review at 0f264d2) and the gate test strips them.
CAP-FIXTURE-OP only: E03-P1 (CTL/INJ), E05-P1 (CTL/INJ), E08-P1-CTL. With CAP-REREVIEW:
E15-P1 (CTL/INJ). Roles are the substituted doubles (CAP-ROLE-SUBSTITUTE); tools are the
stand-in scripts; the build manifest is the controller's REAL catalog.build_manifest over a
stand-in repository carrying the real definition registry and the real runtime manifest, so
the definition edit and the runtime-manifest bumps reach the controller's binding.

In-process route: the container-only harness observations (whole-tree counts, RAM sampler,
created_paths) are MISSING here, so assertions that read them do not pass; they are excluded
by name below, never by result.
"""

import copy
import hashlib
import json
import shutil
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_evidence_projection_v27 import (
    FROZEN_CASES,
    FROZEN_CASES_SHA256,
    MAP_GOAL,
    Lab,
    native_checkpoint,
    resigned,
    rewrite_run,
    score,
    simkit_run,
)

from adapter_support import fixture_rereview_projection as frx
from adapter_support.evidence_projection import EvidenceMappingError
from adapter_support.weather_adapter import AdapterRefusal, WeatherAdapter
from tools.workflow import agents, catalog, runner
from tools.workflow.contracts import digest
from tools.workflow.persistence import read_checkpoint

PREFIX = "DEV-E2E-V2_7-"
REPO = Path(__file__).resolve().parents[1]
SIGN = {
    "reviewer": "test-local",
    "reviewed_at": "2026-10-14T00:00:00Z",
    "reviewed_commit": "0" * 40,
}
FIXTURE_ONLY = ("E03-P1-CTL", "E03-P1-INJ", "E05-P1-CTL", "E05-P1-INJ", "E08-P1-CTL")
WITH_REREVIEW = ("E15-P1-CTL", "E15-P1-INJ")
# Evidence only the container harness supplies (CAP-HARNESS-OBS / CAP-EXHAUSTIVE-WRITES).
CONTAINER_ONLY = (
    "harness_observations.max_concurrent_children",
    "harness_observations.shell_launches",
    "harness_observations.data_fetch_attempts",
    "harness_observations.min_free_ram_while_running_gib",
    "filesystem_snapshot.created_paths",
)
PROFILE_STATES = list(runner.TRANSITIONS) + ["RELEASED"]


def signed_producers():
    """resigned() (the committed file, rereview flags cleared test-locally) with the two
    records signed test-locally. Never written back."""
    return resigned(**{"CAP-FIXTURE-OP": SIGN, "CAP-REREVIEW": SIGN})


@pytest.fixture(scope="module")
def frozen():
    raw = FROZEN_CASES.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == FROZEN_CASES_SHA256
    return {c["id"]: c for c in json.loads(raw)["cases"]}


class FixtureLab(Lab):
    """The stand-in repository plus what the fixture operations edit: the real definition
    registry (catalog.build_manifest checks it), pyproject.toml and the shipped runtime
    manifest."""

    def __init__(self, root):
        super().__init__(root)
        target = self.repo / frx.DEFINITIONS_FILE
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(REPO / frx.DEFINITIONS_FILE, target)
        for name in ("pyproject.toml", catalog.RUNTIME_MANIFEST):
            shutil.copyfile(REPO / name, self.repo / name)


@pytest.fixture(scope="module")
def lab(tmp_path_factory):
    with pytest.MonkeyPatch.context() as mp:
        # The stand-in tools have no argparse contract; build_manifest stays the real one.
        mp.setattr(runner, "preflight", lambda *args: ({}, {}))
        mp.setattr(agents.shutil, "which", lambda _: "never-launched-claude")
        yield FixtureLab(tmp_path_factory.mktemp("fixture-op"))


def execute(lab, case):
    adapter = WeatherAdapter(enable_run_case=True, producers=signed_producers())
    profile = lab.profile(MAP_GOAL, case, stub=None)
    built = []

    def factory(run_dir):
        built.append(adapter.substituted_roles_factory(case, MAP_GOAL, run_dir))
        return built[-1]

    profile["roles_factory"] = factory
    evidence = adapter.run_case(case, profile)
    root = Path(profile["case_root"])
    report = json.loads((root / "harness/evidence-projection.json").read_text(encoding="utf-8"))
    return SimpleNamespace(
        adapter=adapter,
        case=case,
        evidence=evidence,
        report=report,
        run=root / "run",
        root=root,
        checkpoint=read_checkpoint(root / "run"),
    )


def scored(result, case=None):
    record = {"status": "EXECUTED", "evidence": result.evidence}
    return score.score_case(case or result.case, record, allow_unratified=True)


def container_only(case, assertion_id):
    (assertion,) = [a for a in case["assertions"] if a["id"] == assertion_id]
    text = json.dumps(assertion["predicate"])
    return any(path in text for path in CONTAINER_ONLY)


def unexplained_fails(result, case=None):
    case = case or result.case
    out = scored(result, case)
    return sorted(
        k for k, v in out["assertions"].items() if v == "FAIL" and not container_only(case, k)
    )


def fixture_record(result):
    return result.report["harness"]["fixture_operations"]


def dispatch_events(run):
    return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(run.glob("dispatch-event-*"))]


def producing(result):
    return [r for r in result.evidence["process_receipts"] if r.get("producing")]


@pytest.fixture(scope="module")
def runs(lab, frozen):
    return {s: execute(lab, copy.deepcopy(frozen[PREFIX + s])) for s in FIXTURE_ONLY}


@pytest.fixture(scope="module")
def rereviews(lab, frozen):
    return {s: execute(lab, copy.deepcopy(frozen[PREFIX + s])) for s in WITH_REREVIEW}


def bump_after_authorization(frozen):
    """LOCAL variant (not a frozen case): E15-P1-CTL with its one schedule entry being a
    runtime-manifest bump at after_transition AUTHORIZED occurrence 1, so the return to review
    withdraws an AUTHORIZED transition (the frozen bumps all land before AUTHORIZED)."""
    case = copy.deepcopy(frozen[PREFIX + "E15-P1-CTL"])
    case["id"] += "-LOCAL-BUMP-AFTER-AUTHORIZED"
    bump = copy.deepcopy(case["injection_schedule"][0])
    assert bump["payload"]["fixture_operation"] == "FX-RUNTIME-MANIFEST-BUMP"
    bump.update(hook="after_transition", requires=[])
    bump["selector"] = {"role_or_tool_or_stage": "AUTHORIZED", "occurrence": 1}
    case["injection_schedule"] = [bump]
    return case


@pytest.fixture(scope="module")
def reauthorized(lab, frozen):
    return execute(lab, bump_after_authorization(frozen))


# --------------------------------------------------------------------------- CAP-FIXTURE-OP


def test_e03_p1_ledger_append_is_delivered_and_the_widened_goal_is_stopped(runs, frozen):
    for kind in ("CTL", "INJ"):
        result = runs[f"E03-P1-{kind}"]
        frozen_args = frozen[PREFIX + f"E03-P1-{kind}"]["injection_schedule"][0]["payload"]["args"]
        (entry,) = fixture_record(result)["entries"]
        assert entry["fixture_operation"] == "FX-LEDGER-APPEND" and entry["status"] == "APPLIED"
        assert entry["target"] == "request" and entry["creates_outside_run_dir"] is None
        assert entry["delivered_target"] == "request.messages"
        messages = result.checkpoint["request"]["messages"]
        anchor = [m["id"] for m in messages].index(
            result.checkpoint["request"]["authorization"]["message_id"]
        )
        appended = messages[anchor + 1]
        assert appended["speaker"] == "assistant" and appended["text"] == frozen_args["text"]
        assert entry["after_hash"] == digest(result.checkpoint["request"])
        # The harness's seeded ledger is the pre-append record.
        seeded = json.loads((result.root / "harness/seeded-ledger.json").read_text("utf-8"))
        assert len(seeded["request"]["messages"]) == len(messages) - 1
        assert unexplained_fails(result) == []
    ctl, inj = runs["E03-P1-CTL"], runs["E03-P1-INJ"]
    assert ctl.evidence["controller_trace"]["final_state"] == "RELEASED"
    assert inj.evidence["controller_trace"]["terminal"]["failed_gate"] == "goal_check"
    assert "bbox" in inj.evidence["controller_trace"]["goal_check"]["failed_fields"]
    assert not producing(inj) and inj.evidence["controller_trace"]["authorizations"] == []


def test_e05_p1_definition_edit_blocks_and_touch_releases_on_the_repo_copy(runs, lab):
    lab_definition = (lab.repo / frx.DEFINITIONS_FILE).read_bytes()
    inj, ctl = runs["E05-P1-INJ"], runs["E05-P1-CTL"]
    for result in (inj, ctl):
        copy_record = result.report["harness"]["repo_copy"]
        assert Path(copy_record["repo"]) == result.root / "repo"
        assert copy_record["copy_manifest_hash"] == copy_record["source_manifest_hash"]
        assert Path(result.checkpoint["repo"]) == (result.root / "repo").resolve()
        assert result.evidence["controller_trace"]["material_changes"] == []
        assert unexplained_fails(result) == []
        # The reviewer occurrence-2 rejection is never reached under either outcome.
        assert result.report["harness"]["case_schedule"]["coverage"]["unreached"] == [1]
    # INJ: 25 -> 24 mm on the copy; build_manifest's definition check refuses at review_binding.
    (edit,) = fixture_record(inj)["entries"]
    assert edit["status"] == "APPLIED" and edit["before_hash"] != edit["after_hash"]
    assert edit["adapter_mapping"].endswith("trigger.total_mm")
    edited = tomllib.loads((inj.root / "repo" / frx.DEFINITIONS_FILE).read_text("utf-8"))
    assert edited["definitions"]["agrhymet-sos-rolling"]["trigger"]["total_mm"] == 24
    assert (lab.repo / frx.DEFINITIONS_FILE).read_bytes() == lab_definition  # source untouched
    assert inj.evidence["controller_trace"]["terminal"] == {
        "outcome": "BLOCKED",
        "reason": "review_binding_failed",
        "failed_gate": "review_binding",
    }
    assert inj.evidence["controller_trace"]["final_state"] == "REVIEW_APPROVED"
    assert not producing(inj)
    # The edited copy is refused by build_manifest at projection time too: the current
    # manifest symbol is a CAP-SYMBOLS gap with that refusal, never the adopted build_hash.
    manifest_path = "symbols.HARNESS_MANIFEST_HASH:current"
    assert "HARNESS_MANIFEST_HASH:current" not in inj.evidence["symbols"]
    (gap,) = [m for m in inj.report["missing"] if m["path"] == manifest_path]
    assert gap["capability"] == "CAP-SYMBOLS"
    assert gap["reason"].startswith("build manifest unobservable at run end: ")
    assert "definition changed; cached-onset/v1 must be reviewed" in gap["reason"]
    # CTL: mtime only, content hash unchanged, the binding stays valid and the run releases.
    (touch,) = fixture_record(ctl)["entries"]
    assert touch["status"] == "APPLIED" and touch["before_hash"] == touch["after_hash"]
    assert touch["mtime_delta_seconds"] >= 3600
    assert ctl.evidence["controller_trace"]["final_state"] == "RELEASED"
    assert ctl.evidence["symbols"]["HARNESS_MANIFEST_HASH:current"]
    assert not [m for m in ctl.report["missing"] if m["path"] == manifest_path]
    assert scored(ctl)["terminal"] == scored(inj)["terminal"] == "PASS"


def test_e08_p1_ctl_reserialized_report_releases(runs):
    result = runs["E08-P1-CTL"]
    (entry,) = fixture_record(result)["entries"]
    assert entry["fixture_operation"] == "FX-REPORT-RESERIALIZE" and entry["status"] == "APPLIED"
    assert entry["json_value_equal"] is True and entry["bytes_differ"] is True
    assert entry["target"] == "process_stream"
    assert result.evidence["controller_trace"]["final_state"] == "RELEASED"
    assert "verify-run(map)" in result.evidence["gate_reports"]
    assert "repo_copy" in result.report["harness"] and result.report["harness"]["repo_copy"] is None
    assert unexplained_fails(result) == [] and scored(result)["terminal"] == "PASS"


def test_fixture_cases_resolve_source_expected_after_and_pass_u13(runs, rereviews):
    for short, result in {**runs, **rereviews}.items():
        symbols = result.evidence["symbols"]
        assert symbols["HARNESS_HASH:source_expected_after"] == symbols["HARNESS_HASH:source_pre"]
        assert not any(
            m["path"] == "symbols.HARNESS_HASH:source_expected_after"
            for m in result.report["missing"]
        ), short
        assert fixture_record(result)["outside_run_dir"] == [], short
        u13 = scored(result)["assertions"].get("U13")
        assert u13 in (None, "PASS"), short
    assert scored(runs["E03-P1-CTL"])["assertions"]["U13"] == "PASS"


def test_fixture_plan_refuses_incomplete_or_unreachable_operations_before_side_effects(
    frozen, lab, tmp_path
):
    adapter = WeatherAdapter(enable_run_case=True, producers=signed_producers())
    base = frozen[PREFIX + "E08-P1-CTL"]

    def variant(**args):
        case = copy.deepcopy(base)
        case["id"] += "-LOCAL-BAD-" + "-".join(sorted(args))
        case["injection_schedule"][0]["payload"]["args"].update(args)
        return case

    for case, message in (
        (variant(indent=4), "reverse-order, indent-2"),
        (variant(report="verify-run(onset)"), "reverse-order, indent-2"),
        (variant(extra=1), "complete frozen set"),
    ):
        profile = lab.profile(MAP_GOAL, case, stub=None)
        profile["case_root"] = tmp_path / "never"
        with pytest.raises(AdapterRefusal, match=message):
            adapter.run_case(case, profile)
        assert not profile["case_root"].exists()
    incomplete = copy.deepcopy(frozen[PREFIX + "E05-P1-INJ"])
    del incomplete["injection_schedule"][0]["payload"]["args"]["from"]
    with pytest.raises(AdapterRefusal, match="complete frozen set"):
        frx.fixture_plan(incomplete, lab.repo)
    unknown = copy.deepcopy(frozen[PREFIX + "E05-P1-INJ"])
    unknown["injection_schedule"][0]["payload"]["fixture_operation"] = "FX-DECOY-SPAWN"
    with pytest.raises(AdapterRefusal, match="unavailable"):
        frx.fixture_plan(unknown, lab.repo)
    # A runtime-manifest bump on a repository that carries none: refused, nothing seeded.
    bare = tmp_path / "bare-repo"
    bare.mkdir()
    with pytest.raises(AdapterRefusal, match="no runtime-manifest.json"):
        frx.fixture_plan(frozen[PREFIX + "E15-P1-CTL"], bare)
    # The real repository ships one (owner ruling B): build-1, below every frozen bump value.
    assert catalog.runtime_build_id(REPO / catalog.RUNTIME_MANIFEST) == "build-1"
    for short in ("E15-P1-CTL", "E15-P1-INJ"):
        entries = frx.fixture_plan(frozen[PREFIX + short], REPO)["entries"]
        assert {e["fixture_operation"] for e in entries} == {"FX-RUNTIME-MANIFEST-BUMP"}
    plan = frx.fixture_plan(frozen[PREFIX + "E10-P2-INJ"], lab.repo)
    (shift,) = plan["entries"]
    assert shift["fixture_operation"] == "FX-SRC-CELL-INCREMENT"
    assert "outside HARNESS_RUN_DIR" in shift["creates_outside_run_dir"]


def hooks_with(records, dispositions=None):
    return SimpleNamespace(
        injection=SimpleNamespace(records=records, dispositions=dispositions or {})
    )


def plan_of(*entries):
    return {
        "entries": [
            {
                "index": i,
                "fixture_operation": op,
                "hook": hook,
                "target": frx.FIXTURE_TARGETS[op],
                "creates_outside_run_dir": frx.CREATES_OUTSIDE_RUN_DIR.get(op),
            }
            for i, (op, hook) in enumerate(entries)
        ]
    }


def test_unapplied_operations_keep_their_symbols_missing():
    plan = plan_of(("FX-SRC-CELL-INCREMENT", "after_transition"))
    report = frx.fixture_report(plan, hooks_with([]), "pre")
    assert "HARNESS_HASH:source_expected_after" not in report["symbols"]
    assert "0:UNREACHED" in report["missing"][0]["reason"]
    inputs = frx.harness_inputs_from(report, {"source_expected_after": "pre"})
    assert "source_expected_after" not in inputs  # never left at source_pre
    assert "CAP-FIXTURE-OP" in inputs["source_expected_after_reason"]
    applied = {"index": 0, "status": "APPLIED", "before_hash": "pre", "after_hash": "post"}
    report = frx.fixture_report(plan, hooks_with([applied]), "pre")
    assert report["symbols"]["HARNESS_HASH:source_expected_after"] == "post"
    assert report["outside_run_dir"][0]["operation"] == "FX-SRC-CELL-INCREMENT"
    plan = plan_of(("FX-SRC-META-ATTR", "before_run"), ("FX-ONSET-CELL-SHIFT", "after_transition"))
    meta = {"index": 0, "status": "APPLIED", "after_hash": "meta"}
    report = frx.fixture_report(plan, hooks_with([meta]), "pre")
    assert report["symbols"]["HARNESS_HASH:source_pre"] == "meta"
    assert report["symbols"]["HARNESS_HASH:source_expected_after"] == "meta"
    assert [m["symbol"] for m in report["missing"]] == ["HARNESS_HASH:onset_after_mutation"]
    gaps = []
    projection = SimpleNamespace(
        h={"fixture_operations": report},
        gap=lambda path, cap, reason: gaps.append((path, cap)),
        notes=[],
    )
    out = {}
    frx.project_fixture_symbols(projection, out)
    assert "HARNESS_HASH:onset_after_mutation" not in out
    assert gaps == [("symbols.HARNESS_HASH:onset_after_mutation", "CAP-FIXTURE-OP")]
    shift = {"index": 1, "status": "APPLIED", "after_hash": "onset-after"}
    report = frx.fixture_report(plan, hooks_with([meta, shift]), "pre")
    frx.project_fixture_symbols(SimpleNamespace(h={"fixture_operations": report}, notes=[]), out)
    assert out["HARNESS_HASH:onset_after_mutation"] == "onset-after"


def test_repo_copy_is_refused_in_prestaged_mode_and_reverified_when_adopted(lab, tmp_path):
    plan = {"needs_repo_copy": True}
    prepared = {"case_root": str(tmp_path / "case")}
    (tmp_path / "case").mkdir()
    with pytest.raises(AdapterRefusal, match="outside HARNESS_RUN_DIR"):
        frx.controller_repo(plan, {"repo": lab.repo, "prepared_fixture": {}}, prepared)
    assert not (tmp_path / "case" / "repo").exists()
    record = frx.stage_repo_copy(lab.repo, tmp_path / "case")
    assert frx.adopt_repo_copy(json.loads(json.dumps(record)), tmp_path / "case") == record
    repo, adopted = frx.controller_repo(
        plan, {"repo": lab.repo, "prepared_fixture": {}, "prepared_repo": record}, prepared
    )
    assert repo == Path(record["repo"]) and adopted == record
    with pytest.raises(AdapterRefusal, match="reuse is refused"):
        frx.stage_repo_copy(lab.repo, tmp_path / "case")
    (Path(record["repo"]) / catalog.RUNTIME_MANIFEST).write_text('{"build_id": "drift"}')
    with pytest.raises(AdapterRefusal, match="changed since staging"):
        frx.adopt_repo_copy(record, tmp_path / "case")
    assert frx.controller_repo({"needs_repo_copy": False}, {"repo": "r"}, prepared) == ("r", None)


def test_unsigned_records_keep_the_cases_not_implemented(frozen):
    # The committed records are signed (D4 review at 0f264d2) but flagged rereview_required
    # by wave 7, so the committed file leaves them undeclared; the test-local re-signed copy
    # declares them, and stripping the signatures from THAT copy must make them undeclared
    # again and the cases NOT_IMPLEMENTED (the signature alone, not the flag, is the cause).
    unsign = {"reviewer": None, "reviewed_at": None, "reviewed_commit": None}
    stripped = resigned(**{"CAP-FIXTURE-OP": unsign, "CAP-REREVIEW": unsign})
    for capability in ("CAP-FIXTURE-OP", "CAP-REREVIEW"):
        record = WeatherAdapter().producers["capabilities"][capability]
        assert record["d4_gated"] is True and record["status"] == "PRODUCER_MAPPED"
        assert record["reviewer"] and record["reviewed_at"] and record["reviewed_commit"]
        assert record["rereview_required"].strip()
        assert capability not in WeatherAdapter().implemented_capabilities()
        assert capability in WeatherAdapter(producers=resigned()).implemented_capabilities()
        assert capability not in WeatherAdapter(producers=stripped).implemented_capabilities()
        assert capability in WeatherAdapter(producers=signed_producers()).implemented_capabilities()
    cases = [frozen[PREFIX + s] for s in FIXTURE_ONLY + WITH_REREVIEW]
    records = simkit_run(
        cases, WeatherAdapter(enable_run_case=True, producers=stripped), profile={}
    )
    assert {r["status"] for r in records} == {"NOT_IMPLEMENTED"}
    for record in records:
        assert set(record["missing_capabilities"]) <= {"CAP-FIXTURE-OP", "CAP-REREVIEW"}


# --------------------------------------------------------------------------- CAP-REREVIEW


def test_x3_rereview_run_projects_with_the_surviving_history(rereviews):
    """X3: return_to_review pops the withdrawn transitions from the checkpoint but not their
    dispatch events, so the old check (transition events == checkpoint history) refused every
    re-review run. The history is rebuilt from the controller's return_to_review record."""
    result = rereviews["E15-P1-CTL"]
    events = dispatch_events(result.run)
    raw = [e["transition"] for e in events if e["kind"] == "transition"]
    assert raw != result.checkpoint["events"]  # the pre-fix projection refused here
    assert [t["to"] for t in raw].count("REVIEW_APPROVED") == 3  # approval + two re-reviews
    trace = result.evidence["controller_trace"]
    assert trace["states"] == PROFILE_STATES and trace["final_state"] == "RELEASED"
    withdrawals = next(n for n in result.report["notes"] if "withdrawn_transitions" in n)[
        "withdrawn_transitions"
    ]
    assert [[w["state"] for w in r["withdrawn"]] for r in withdrawals] == [["REVIEW_APPROVED"]] * 2
    surviving = [e for e in events if e["kind"] == "transition"][-8]
    assert surviving["transition"]["to"] == "REVIEW_APPROVED"
    assert (
        result.evidence["event_seq"]["controller_trace.states[REVIEW_APPROVED]"]
        == (surviving["sequence"])
    )
    assert all(r["withdrawn"][0]["sequence"] < surviving["sequence"] for r in withdrawals)
    found = score.run_evidence_findings(
        next(
            a["predicate"]["operand"]
            for a in result.case["assertions"]
            if a.get("predicate", {}).get("operator") == "run_evidence"
        ),
        result.evidence,
    )
    assert not [m for s, m in found if s == "FAIL" and "state" in m]


def assert_rereview_records(result, bumps):
    trace = result.evidence["controller_trace"]
    changes = trace["material_changes"]
    assert [c["object"] for c in changes] == ["manifest"] * bumps
    assert [c["index"] for c in changes] == list(range(bumps))
    for change in changes:
        assert (
            result.evidence["event_seq"][
                f"controller_trace.material_changes[index={change['index']}]"
            ]
            == change["sequence"]
        )
    assert trace["source_rechecks"] == []  # no source change
    report = result.adapter.record_material_changes(result.run, None)
    assert report["material_changes"] == changes and report["missing"] == []
    return trace, report


def test_e15_p1_ctl_two_rereviews_release_under_the_replacing_approval(rereviews, frozen):
    frozen_ctl = frozen[PREFIX + "E15-P1-CTL"]
    # Frozen schedule, verbatim: plan_scope "initial" is the first accepted PLAN identity
    # (schedule_fixes.py), and a manifest bump re-binds the same plan, so bump-2 (occurrence 2,
    # plan_scope initial) is delivered: two re-reviews of v1, each a replacing approval; release.
    local = rereviews["E15-P1-CTL"]
    entries = local.report["harness"]["case_schedule"]["coverage"]["dependencies"]["entries"]
    assert [e["disposition"] for e in entries] == ["DELIVERED", "DELIVERED"]
    trace, report = assert_rereview_records(local, 2)
    approval = trace["active_approval"]
    superseded = report["rereview"]["superseded_approvals"]
    assert [s["invalidated_by"] for s in superseded] == [[0], [1]]
    assert approval["replaces"] == superseded[-1]["approval_id"]
    assert approval["manifest_hash"] == local.evidence["symbols"]["HARNESS_MANIFEST_HASH:current"]
    assert approval["reviewer_occurrence"] == 3
    assert (
        local.evidence["event_seq"]["controller_trace.active_approval"]
        == (local.checkpoint["context"]["active_approval"]["sequence"])
    )
    v1 = local.evidence["symbols"]["HARNESS_PLAN_HASH:v1"]
    presented = [
        c
        for c in trace["claim_checks"]
        if c["plan_hash"] == v1 and c["decision"] == "PRESENT_FOR_REVIEW"
    ]
    assert len(presented) == 3
    assert presented[-1]["manifest_hash"] == approval["manifest_hash"]
    assert trace["states"] == PROFILE_STATES
    out = scored(local, frozen_ctl)
    assert out["assertions"]["C1"] == "PASS" and out["terminal"] == "PASS"
    assert unexplained_fails(local, frozen_ctl) == []


def test_e15_p1_inj_third_change_stops_at_the_rereview_cap(rereviews, frozen):
    frozen_inj = frozen[PREFIX + "E15-P1-INJ"]
    local = rereviews["E15-P1-INJ"]  # frozen schedule, verbatim: all three bumps delivered
    entries = local.report["harness"]["case_schedule"]["coverage"]["dependencies"]["entries"]
    assert [e["disposition"] for e in entries] == ["DELIVERED"] * 3
    trace, report = assert_rereview_records(local, 3)
    # Two re-reviews used; the third change is recorded, then the cap refuses at the binding
    # check, still approved and with nothing produced (the frozen acceptable alternative).
    assert trace["terminal"] == {
        "outcome": "BLOCKED",
        "reason": "approval_invalidated",
        "failed_gate": "review_binding",
    }
    assert trace["final_state"] == "REVIEW_APPROVED" and not producing(local)
    assert trace["states"] == PROFILE_STATES[: PROFILE_STATES.index("REVIEW_APPROVED") + 1]
    assert len(report["rereview"]["rereviews"]) == 2
    assert trace["active_approval"]["reviewer_occurrence"] == 3
    # The current manifest is the third (unadopted) bump, observed by the harness at run end,
    # not the last build the controller adopted (run-4 E15-P1-INJ projection defect).
    symbols = local.evidence["symbols"]
    assert symbols["HARNESS_MANIFEST_HASH:current"] == trace["material_changes"][-1]["to_hash"]
    assert symbols["HARNESS_MANIFEST_HASH:current"] != local.checkpoint["build_hash"]
    out = scored(local, frozen_inj)
    assert out["assertions"]["TS"] == "PASS" and out["terminal"] == "PASS"
    assert unexplained_fails(local, frozen_inj) == []


def test_rereview_lists_are_empty_only_for_a_finished_run(runs, rereviews, tmp_path):
    finished = runs["E03-P1-CTL"].evidence["controller_trace"]
    assert finished["material_changes"] == [] and finished["source_rechecks"] == []

    def unfinish(run, payload):
        terminal = sorted(run.glob("dispatch-event-*.json"))[-1]
        assert json.loads(terminal.read_text())["kind"] == "terminal"
        payload["context"]["dispatch_evidence"]["sequence"] -= 1
        payload["evidence"].pop(terminal.name)
        payload["context"].pop("terminal")
        terminal.unlink()
        (run / "result.json").unlink()
        return []

    for name, source in (("e03", runs["E03-P1-CTL"]), ("e15", rereviews["E15-P1-CTL"])):
        run = rewrite_run(source.run, tmp_path / name, unfinish)
        projection = WeatherAdapter().capture_evidence(run)
        trace = projection["evidence"]["controller_trace"]
        owners = {m["path"]: m["capability"] for m in projection["missing"]}
        if name == "e03":
            assert "material_changes" not in trace and "source_rechecks" not in trace
            assert owners["controller_trace.material_changes"] == "CAP-REREVIEW"
            assert owners["controller_trace.source_rechecks"] == "CAP-REREVIEW"
        else:  # the controller's lists exist: projected verbatim even before the terminal
            assert len(trace["material_changes"]) == 2
            assert owners["controller_trace.source_rechecks"] == "CAP-REREVIEW"


def test_tampered_rereview_records_refuse_the_projection(rereviews, tmp_path):
    source = rereviews["E15-P1-CTL"].run

    def change_file(run, payload):
        path = run / "material-change-000.json"
        record = json.loads(path.read_text())
        record["to_hash"] = "0" * 64
        path.write_text(json.dumps(record))
        return [path.name]

    def withdraw_other(run, payload):
        payload["context"]["rereviews"][0]["withdrawn_states"] = ["AUTHORIZED"]
        return []

    def superseded_file(run, payload):
        path = run / "active-approval-000.json"
        record = json.loads(path.read_text())
        record["plan_hash"] = "0" * 64
        path.write_text(json.dumps(record))
        return [path.name]

    def unlink_replacement(run, payload):
        payload["context"]["active_approval"]["replaces"] = "another-approval"
        return []

    for name, edit, message in (
        ("file", change_file, "material_changes\\[0\\] differs"),
        ("withdrawn", withdraw_other, "return_to_review event differs"),
        ("superseded", superseded_file, "superseded approval 0 differs"),
        ("replaces", unlink_replacement, "native active approval differs|does not replace"),
    ):
        run = rewrite_run(source, tmp_path / name, edit)
        with pytest.raises(EvidenceMappingError, match=message):
            WeatherAdapter().capture_evidence(run)
    assert native_checkpoint(source)["context"]["rereviews"][0]["withdrawn_states"] == [
        "REVIEW_APPROVED"
    ]


def test_withdrawn_authorization_is_not_an_authorization(reauthorized):
    """A return to review that withdraws an AUTHORIZED transition leaves one authorization:
    controller_trace.authorizations counts the surviving history, not every transition event."""
    result = reauthorized
    (entry,) = fixture_record(result)["entries"]
    assert entry["status"] == "APPLIED" and entry["build_id_to"] == "build-2"
    events = dispatch_events(result.run)
    authorized = [
        e for e in events if e["kind"] == "transition" and e["transition"]["to"] == "AUTHORIZED"
    ]
    assert len(authorized) == 2  # the withdrawn one and the re-authorization
    (rereview,) = result.checkpoint["context"]["rereviews"]
    assert "AUTHORIZED" in rereview["withdrawn_states"]
    trace = result.evidence["controller_trace"]
    assert trace["final_state"] == "RELEASED" and trace["states"] == PROFILE_STATES
    (authorization,) = trace["authorizations"]
    assert authorization["authorized_at"] == authorized[-1]["sequence"]
    assert authorization["source"] == "operator_ledger" and authorization["speaker"] == "user"
