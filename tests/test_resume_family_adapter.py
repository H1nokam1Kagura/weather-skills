"""Wave 7, sub-wave C (adapter): the interrupt and resume family on the real controller.

The frozen cases E10-P1 (CTL/INJ), E11-P1 (CTL/INJ), E14-P1 (CTL/INJ) and E14-P2-INJ run
through WeatherAdapter.run_case with their FROZEN adapter_requirements, admitted by a
test-local copy of the producer file in which the nine wave-7 records are added and signed
(the committed file does not carry them: only the coordinator adds and signs them). Roles are
the substituted doubles the container case driver uses; tools are the stand-in scripts.
Frozen assertions are evaluated with the pinned frozen scorer.

Routes:

* E14 (automatic resumes, classified drops, notify-and-wait): the single in-process
  controller, exactly as before; runs everywhere.
* E10/E11 (harness_stop_controller): the forked route of adapter_support/resume_driver.py,
  in-process (no interrupt broker: the parent kills incarnation 1 itself). It needs os.fork,
  so those tests are skipped where fork is unavailable (Windows) and run on Linux CI.
* A THREAD DOUBLE of incarnation 1 (``ThreadIncarnation``, below) drives the same parent-side
  logic (stop packet, deferred entries, harness resume record, request_resume as incarnation
  2, projection, frozen scoring) on every platform. It kills nothing and runs both
  incarnations in one pid, so it is NEVER evidence of the real route; it only checks the
  harness logic around it.
"""

import copy
import json
import os
import queue
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_evidence_projection_v27 import MAP_GOAL, score
from test_role_substitution_adapter import PREFIX, frozen, lab  # noqa: F401 - shared fixtures

from adapter_support import resume_driver, resume_projection
from adapter_support.weather_adapter import AdapterRefusal, WeatherAdapter, load_producers
from tools.workflow.persistence import read_checkpoint

WAVE7 = (
    "CAP-INTERRUPT",
    "CAP-RESUME",
    "CAP-RESUME-ATTRIBUTION",
    "CAP-MIDPROCESS",
    "CAP-AUTO-RESUME",
    "CAP-DIAGNOSIS",
    "CAP-OPERATOR-NOTIFY",
    "CAP-MECHANICAL-REPAIR",
    "CAP-DENY-WRITE",
)
SIGN = {"reviewer": "test-local", "reviewed_at": "2026-10-15T00:00:00Z", "reviewed_commit": "0"}
FORK = pytest.mark.skipif(
    not resume_driver.fork_available(), reason="os.fork unavailable: the forked route runs on Linux"
)
E14 = ("E14-P1-CTL", "E14-P1-INJ", "E14-P2-INJ")
STOPPED = ("E10-P1-CTL", "E10-P1-INJ", "E11-P1-CTL", "E11-P1-INJ")


def signed(*caps):
    """The committed producer file plus test-local signed records for ``caps`` (default: all
    nine wave-7 records). Never written back: CAPABILITY-PRODUCERS.json is the coordinator's."""
    producers = load_producers()
    for cap in caps or WAVE7:
        producers["capabilities"][cap] = {
            "d4_gated": True,
            "status": "PRODUCER_MAPPED",
            **SIGN,
        }
    return producers


def execute(lab, case, producers=None):  # noqa: F811 - lab is the shared module fixture
    adapter = WeatherAdapter(enable_run_case=True, producers=producers or signed())
    profile = lab.profile(MAP_GOAL, case, stub=None)

    def factory(run_dir):
        return adapter.substituted_roles_factory(case, MAP_GOAL, run_dir)

    profile["roles_factory"] = factory
    evidence = adapter.run_case(case, profile)
    root = Path(profile["case_root"])
    report = json.loads((root / "harness/evidence-projection.json").read_text(encoding="utf-8"))
    return SimpleNamespace(
        evidence=evidence,
        report=report,
        route=report["harness"].get("resume_route"),
        adapter=adapter,
        run=root / "run",
        checkpoint=read_checkpoint(root / "run"),
    )


def verdicts(case, evidence, *ids):
    predicates = {a["id"]: a["predicate"] for a in case["assertions"]}
    return {i: score.evaluate(predicates[i], evidence) for i in ids}


def case_ids(case):
    return [a["id"] for a in case["assertions"] if not a["id"].startswith(("V2", "U", "RC", "$"))]


def missing(report, prefix):
    return [m for m in report["missing"] if m["path"].startswith(prefix)]


def notes(report, key):
    return [n[key] for n in report["notes"] if key in n]


@pytest.mark.parametrize("short", ["E14-P1-CTL", "E14-P1-INJ", "E14-P2-INJ"])
def test_probe(lab, frozen, short):  # noqa: F811
    case = copy.deepcopy(frozen[PREFIX + short])
    result = execute(lab, case)
    trace = result.evidence["controller_trace"]
    print(json.dumps(trace.get("terminal")), trace.get("final_state"))
    print(json.dumps(result.evidence["budget_ledger"].get("at_checkpoint")))
    print(json.dumps(result.report["missing"]))
    scored = score.score_case(case, {"status": "EXECUTED", "evidence": result.evidence}, allow_unratified=True)
    print("TERMINAL", scored["terminal"])
    for k, v in scored["assertions"].items():
        if v != "PASS" and (not isinstance(v, dict) or v.get("result") != "PASS"):
            print("ASSERT", k, json.dumps(v)[:600])
    assert 0
