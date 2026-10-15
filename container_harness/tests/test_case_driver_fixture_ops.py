"""CAP-FIXTURE-OP on the container route, offline (no docker): ``case_driver prepare`` stages the
disposable repository copy untraced and the traced ``run`` profile carries it as
profile["prepared_repo"]; the scored in-container projection merges the fixture-operations
report (project_in_container.INPUT_KEYS), so HARNESS_HASH:onset_after_mutation reaches it.
"""

import json
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path[:0] = [str(REPO), str(REPO / "tests")]

from frozen_v27 import frozen_root  # noqa: E402

SIMKIT_ROOT = frozen_root()  # verified against FINAL-PINS by tests/frozen_v27.py
sys.path.insert(0, str(SIMKIT_ROOT))

from adapter_support import evidence_projection  # noqa: E402
from adapter_support import fixture_rereview_projection as frx  # noqa: E402
from adapter_support.weather_adapter import AdapterRefusal  # noqa: E402
from container_harness import case_driver as cd  # noqa: E402
from container_harness import project_in_container as pic  # noqa: E402
from tools.workflow import catalog  # noqa: E402

PREFIX = "DEV-E2E-V2_7-"
DEV_ONLY = "casesets/cached-onset-e2e-v2_7/frozen/agentic-e2e-injections.DEV-ONLY.json"


@pytest.fixture(scope="module")
def frozen():
    data = json.loads((SIMKIT_ROOT / DEV_ONLY).read_text(encoding="utf-8"))
    return {c["id"]: c for c in data["cases"]}


def stand_in_repo(root, runtime_manifest=True):
    """The files catalog.build_manifest hashes that a repository copy needs, copied from the
    real repository (its shipped runtime manifest included unless ``runtime_manifest`` is off)."""
    root.mkdir(parents=True)
    target = root / frx.DEFINITIONS_FILE
    target.parent.mkdir(parents=True)
    shutil.copyfile(REPO / frx.DEFINITIONS_FILE, target)
    shutil.copyfile(REPO / "pyproject.toml", root / "pyproject.toml")
    if runtime_manifest:
        shutil.copyfile(REPO / catalog.RUNTIME_MANIFEST, root / catalog.RUNTIME_MANIFEST)
    return root


def run_profile(case, work, prepared_repo):
    """case_driver.case_profile as the traced ``run`` builds it (adapter and documents are
    stand-ins; only the keys this test reads matter)."""
    adapter = SimpleNamespace(select_profile=lambda case, document: {"goal": {}})
    return cd.case_profile(
        case,
        adapter,
        {"reference_fixture": {"source_ledger": {}}},
        work,
        spec={"supplement_id": "test-only"},
        pins={"fixture_generator": {"sha256": "0" * 64}},
        version="0.0.2",
        live=False,
        prepared={},
        inputs_channel=None,
        processes_factory=None,
        prepared_repo=prepared_repo,
    )


def test_prepare_stages_the_repo_copy_untraced_and_the_run_profile_adopts_it(frozen, tmp_path):
    repo = stand_in_repo(tmp_path / "ctl")
    work = tmp_path / "work"
    (work / "case").mkdir(parents=True)
    case = frozen[PREFIX + "E15-P1-CTL"]
    record = cd.prepare_repo_copy(case, work, repo=repo)
    assert Path(record["repo"]) == (work / "case" / "repo").resolve()
    assert record["copy_manifest_hash"] == record["source_manifest_hash"]
    assert json.loads((work / cd.PREPARED_REPO_NAME).read_text("utf-8")) == record
    assert (work / "case" / "repo" / catalog.RUNTIME_MANIFEST).is_file()
    # The traced run reads the record back (JSON round trip) and passes it on.
    staged = json.loads((work / cd.PREPARED_REPO_NAME).read_text("utf-8"))
    profile = run_profile(case, work, staged)
    assert profile["prepared_repo"] == record and profile["prepared_fixture"] == {}
    # run_case's controller_repo adopts it in pre-staged mode instead of refusing.
    plan = frx.fixture_plan(case, repo)
    profile["repo"] = repo
    used, adopted = frx.controller_repo(plan, profile, {"case_root": str(work / "case")})
    assert used == Path(record["repo"]) and adopted == record
    # Without the staged record, pre-staged mode still refuses (nothing created in run_case).
    with pytest.raises(AdapterRefusal, match="outside HARNESS_RUN_DIR"):
        frx.controller_repo(
            plan, run_profile(case, work, None) | {"repo": repo}, {"case_root": str(work)}
        )


def test_prepare_stages_nothing_without_a_repository_operation_or_when_refused(frozen, tmp_path):
    repo = stand_in_repo(tmp_path / "ctl")
    bare = stand_in_repo(tmp_path / "bare", runtime_manifest=False)
    for name, case, source in (
        ("no-repo-op", frozen[PREFIX + "E08-P1-CTL"], repo),  # stream operation only
        ("refused", frozen[PREFIX + "E15-P1-CTL"], bare),  # no runtime manifest to bump
    ):
        work = tmp_path / name
        (work / "case").mkdir(parents=True)
        assert cd.prepare_repo_copy(case, work, repo=source) is None
        assert not (work / cd.PREPARED_REPO_NAME).exists()
        assert not (work / "case" / "repo").exists()
        assert run_profile(case, work, None)["prepared_repo"] is None


def onset_shift_report():
    plan = {
        "entries": [
            {
                "index": 0,
                "fixture_operation": "FX-ONSET-CELL-SHIFT",
                "hook": "after_transition",
                "target": frx.FIXTURE_TARGETS["FX-ONSET-CELL-SHIFT"],
                "creates_outside_run_dir": None,
            }
        ]
    }
    applied = {"index": 0, "status": "APPLIED", "before_hash": "b", "after_hash": "onset-after"}
    hooks = SimpleNamespace(injection=SimpleNamespace(records=[applied], dispositions={}))
    return frx.fixture_report(plan, hooks, "pre")


def test_scored_projection_merges_the_fixture_operations_report(tmp_path, monkeypatch):
    assert "fixture_operations" in pic.INPUT_KEYS
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    report = onset_shift_report()
    inputs = {
        "schema": pic.INPUTS_SCHEMA,
        "case_id": "test",
        "run_dir": str(run_dir),
        "fixture_operations": report,
    }
    path = tmp_path / "inputs.json"
    path.write_text(json.dumps(inputs), encoding="utf-8")
    merged, status = pic.load_inputs(path, run_dir)
    assert merged["fixture_operations"] == report and "fixture_operations" in status["merged"]
    assert "fixture_operations" not in status["not_carried"]
    # Optional: a case without fixture operations carries none and it is never "not carried".
    path.write_text(json.dumps({k: v for k, v in inputs.items() if k != "fixture_operations"}))
    assert "fixture_operations" not in pic.load_inputs(path, run_dir)[1]["not_carried"]
    path.write_text(json.dumps(inputs), encoding="utf-8")
    # The scored route hands it to project_run, whose symbols read it (project_fixture_symbols).
    seen = {}

    def project_run(run, harness):
        seen.update(harness)
        out, gaps = {}, []
        frx.project_fixture_symbols(
            SimpleNamespace(h=harness, notes=[], gap=lambda *a: gaps.append(a)), out
        )
        return {"symbols": out, "gaps": gaps}

    monkeypatch.setattr(evidence_projection, "project_run", project_run)
    out = tmp_path / "projection.json"
    argv = ["project_in_container.py", str(run_dir), "obs.json", str(out), "--inputs", str(path)]
    assert pic.main(argv) == 0
    record = json.loads(out.read_text(encoding="utf-8"))
    assert record["status"] == "OK" and seen["fixture_operations"] == report
    assert record["projection"]["symbols"] == {"HARNESS_HASH:onset_after_mutation": "onset-after"}
    assert record["projection"]["gaps"] == []
