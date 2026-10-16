"""Relay ROLE smoke: report, launcher and workflow wiring, offline (stdlib + pyyaml only).

The controller-dependent half (payloads captured from the real controller, the full offline
pass through real RelayRoles -> host Relay -> KongRoles with a fake worker, and every
judgement made by the controller's own acceptance) is
tools/workflow/tests/test_relay_role_smoke_controller.py: it needs the controller stack, which
this slim step (simkit-cases.yml admission / relay-role-smoke) does not install.

Pins: (1) the report passes only on complete evidence where EVERY live role's answer was
accepted by the controller, and fails closed otherwise (a refused answer, the run-38906458002
mixed reviewer shape among them, a missing call, missing files, a relay that did not stop
itself); (2) the job is wired exactly like relay-smoke / relay-dedup-smoke (secret scoping,
isolation, key-absence scan, teardown) with the relay caps the smoke module declares, and runs
only for its selector; admission skips it; (3) the launcher runs the role smoke script.
"""

import copy
import json
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
for _path in (str(HERE.parent), str(REPO)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import launch  # noqa: E402
import relay_role_smoke as smoke  # noqa: E402
import relay_role_smoke_report as report  # noqa: E402
import relay_smoke_report  # noqa: E402

WORKFLOW = REPO / ".github" / "workflows" / "simkit-cases.yml"
MIXED_REVIEWER = {  # run 38906458002: legacy keys + the frozen-only "reason"
    "verdict": "APPROVE",
    "binding_hash": "b" * 64,
    "findings": [],
    "reason": "The plan matches the registered profile.",
}
REFUSAL = "review: expected exactly ['binding_hash', 'findings', 'verdict']"


# ------------------------------------------------------------------ the plan of calls


def test_report_expects_exactly_the_smokes_calls():
    assert [(c["call"], c["role"]) for c in smoke.CALLS] == list(report.EXPECTED_CALLS)
    assert report.ROLES == smoke.ROLES
    assert smoke.ROLES == [
        "compiler",
        "evidence_checker",
        "planner",
        "repairer",
        "reviewer",
        "shadow_policy",
        "verifier",
    ]
    assert "router" not in smoke.ROLES and "rubric_grader" not in smoke.ROLES
    assert float(smoke.RELAY_CAPS["RELAY_BUDGET"]) == report.BUDGET_USD == smoke.BUDGET_USD
    for call in smoke.CALLS:
        assert call["variant"] and call["why"], call["call"]


# ------------------------------------------------------------------ synthetic evidence


def call_record(call, role, n):
    """One good container call record and its relay audit ask line."""
    tool = f"submit_{role}_answer"
    choice = {"type": "tool", "name": tool}
    relay_id = f"r-{n}"
    sent = {
        "model": "claude-sonnet-4-6",
        "max_tokens": 2048,
        "extra_body": {"temperature": 0.0},
        "tools": [{"name": tool}],
        "tool_choice": choice,
    }
    sent_keys = ["extra_body", "max_tokens", "messages", "model", "system", "tool_choice"]
    sent_keys.append("tools")
    usage = {"input_tokens": 1000 * n, "output_tokens": 100}
    cost = (usage["input_tokens"] * 3 + usage["output_tokens"] * 15) / 1e6
    prediction = {
        "transport_encoding": None,
        "request_body_bytes": 5000 + n,
        "model_facing_hash": f"sha256:m{n}",
        "system_prompt_hash": f"sha256:s{n}",
        "structured_output": "forced_tool",
        "answer_tool": tool,
        "answer_tool_hash": f"sha256:t{n}",
        "tool_choice": choice,
    }
    record = {
        "call": call,
        "role": role,
        "variant": "v",
        "why": "w",
        "variant_keys": [],
        "prediction": prediction,
        "outcome": "SUCCESS",
        "answer": {"k": n},
        "receipt": {
            "relay_id": relay_id,
            "role": role,
            "cost_usd": cost,
            "cost_basis": "Anthropic list-price estimate; Kong billing not observed",
            "model_observed": "claude-sonnet-4-6",
            "usage": usage,
            "stop_reason": "tool_use",
            "gateway_mock": False,
            "transport": "kong-native-anthropic",
            "settings": {
                "structured_output": "forced_tool",
                "answer_source": "tool_use.input",
                "answer_tool": tool,
                "answer_tool_hash": f"sha256:t{n}",
                "system_prompt_hash": f"sha256:s{n}",
                "max_tokens": 2048,
            },
            "settings_sent": sent,
            "sent_keys": sent_keys,
            "transport_encoding": None,
            "transport_bytes": {
                "request_body_bytes": 5000 + n,
                "model_facing_hash": f"sha256:m{n}",
            },
        },
        "gateway_body_bytes": 7000,
        "schema_errors": [],
        "controller": {
            "controller_accepts": True,
            "controller_outcome": "ACCEPTED",
            "controller_refusal": None,
            "payload_identical": True,
            "payload_source": "controller_capture",
            "replay_stop": "next",
            "judgment": None,
        },
    }
    ask = {
        "event": "ask",
        "relay_id": relay_id,
        "role": role,
        "outcome": "SUCCESS",
        "sent_to_gateway": True,
        "temperature_requested": 0,
        "settings_sent": sent,
        "sent_keys": sent_keys,
        "structured_output": "forced_tool",
        "answer_source": "tool_use.input",
        "usage": usage,
        "cost_usd": cost,
        "charge": {"basis": "observed"},
    }
    return record, ask


def evidence(tmp_path, change=None, audit_change=None):
    """Complete good evidence for every planned call, optionally edited, written to disk."""
    calls, audit = [], []
    for n, (call, role) in enumerate(report.EXPECTED_CALLS, 1):
        record, ask = call_record(call, role, n)
        calls.append(record)
        audit.append(ask)
    audit += [
        {"event": "spend_cap_reached", "cap": "max_requests"},
        {
            "event": "ask",
            "relay_id": "r-cap",
            "role": "verifier",
            "outcome": "REFUSED",
            "sent_to_gateway": False,
            "reason_code": "relay_spend_cap",
        },
        {"event": "relay_stop", "reason": "spend cap reached: max_requests"},
    ]
    probes = [
        {
            "probe": p,
            "observed": "connected" if p == "relay_port_tcp" else "timeout",
            "expected": "reachable" if p == "relay_port_tcp" else "blocked",
            "passed": True,
            "target": "t",
            "port": 1,
        }
        for p in relay_smoke_report.REQUIRED_PROBES
    ]
    container = {
        "schema": smoke.SCHEMA,
        "uid": 1000,
        "credential_like_env_names": [],
        "credential_like_files": [],
        "health": {"outcome": "OK", "allowed_roles": list(smoke.ROLES)},
        "egress_probes": probes,
        "planned_calls": [c for c, _ in report.EXPECTED_CALLS],
        "calls": calls,
        "cap_probe": {
            "outcome": "REFUSED",
            "gate": "budget",
            "reason": "verifier relay spend cap (r-cap): spend cap max_requests reached",
            "requests": 8,
            "max_requests": 8,
        },
    }
    if change is not None:
        change(container)
    if audit_change is not None:
        audit_change(audit)
    out, relay = tmp_path / "out", tmp_path / "relay"
    out.mkdir()
    relay.mkdir()
    launch_rec = {
        "client": "relay_role_smoke",
        "run_conditions": {"never_privileged": True, "relay_only_network": True},
        "env_names_passed": sorted(relay_smoke_report.EXPECTED_ENV_NAMES),
        "smoke_parsed": True,
    }
    (out / "launch.json").write_text(json.dumps(launch_rec))
    (out / report.CONTAINER_JSON).write_text(json.dumps(container))
    (out / "key-scan.json").write_text(json.dumps({"key_absent": True}))
    (relay / "net.json").write_text(json.dumps({"internal": True}))
    (relay / "relay-audit.jsonl").write_text("\n".join(json.dumps(a) for a in audit) + "\n")
    return out, relay


def run(out, relay):
    code = report.main(["x", str(out), str(relay)])
    result = json.loads((out / "RELAY-ROLE-SMOKE.json").read_text(encoding="utf-8"))
    return code, result, sorted(n for n, c in result["checks"].items() if not c["passed"])


@pytest.fixture(autouse=True)
def _no_summary(monkeypatch):
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)


def test_report_passes_on_complete_accepted_evidence(tmp_path):
    code, result, failed = run(*evidence(tmp_path))
    assert failed == [] and code == 0 and result["passed"] is True
    assert [s["call"] for s in result["calls"]] == [c for c, _ in report.EXPECTED_CALLS]
    assert all(s["controller_accepts"] is True for s in result["calls"])
    assert result["estimated_usd_total"] == pytest.approx(
        sum(s["estimated_usd"] for s in result["calls"])
    )
    for name in report.PER_CALL_CHECKS:
        assert result["checks"][f"planner:{name}"]["passed"] is True, name


def _call(name, fn):
    def change(container):
        (record,) = [r for r in container["calls"] if r["call"] == name]
        fn(record)

    return change


def mixed_reviewer(record):
    record["answer"] = dict(MIXED_REVIEWER)
    record["controller"].update(
        controller_accepts=False,
        controller_outcome="REFUSED",
        controller_refusal={"error_type": "Refused", "text": REFUSAL, "gate": None},
    )


def test_report_fails_when_the_controller_refused_the_mixed_reviewer_shape(tmp_path):
    """Run 38906458002's shape: the relay said SUCCESS, the controller refuses."""
    code, result, failed = run(*evidence(tmp_path, _call("reviewer", mixed_reviewer)))
    assert code == 1 and failed == ["reviewer:controller_accepts"]
    (s,) = [s for s in result["calls"] if s["call"] == "reviewer"]
    assert s["outcome"] == "SUCCESS" and s["controller_refusal"]["text"] == REFUSAL
    assert set(s["answer"]) == set(MIXED_REVIEWER)
    md = (tmp_path / "out" / "RELAY-ROLE-SMOKE.md").read_text()
    assert "Relay role smoke: FAIL" in md and "review: expected exactly" in md


@pytest.mark.parametrize("name", [c for c, _ in report.EXPECTED_CALLS])
def test_any_refused_role_fails_the_smoke(tmp_path, name):
    def refuse(record):
        record["controller"].update(
            controller_accepts=False,
            controller_outcome="REFUSED",
            controller_refusal={"text": "x", "gate": "g"},
        )

    code, _, failed = run(*evidence(tmp_path, _call(name, refuse)))
    assert code == 1 and failed == [f"{name}:controller_accepts"]


@pytest.mark.parametrize(
    "check, fn",
    [
        ("controller_accepts", lambda r: r.pop("controller")),
        ("controller_accepts", lambda r: r["controller"].update(controller_accepts=None)),
        ("controller_judged_the_sent_payload", lambda r: r["controller"].pop("payload_identical")),
        (
            "controller_judged_the_sent_payload",
            lambda r: r["controller"].update(payload_identical=False),
        ),
        ("role_call_success", lambda r: r.update(outcome="UNKNOWN")),
        ("model_observed_sonnet_4_6", lambda r: r["receipt"].update(model_observed="other")),
        ("receipt_live_gateway", lambda r: r["receipt"].update(gateway_mock=True)),
        ("receipt_live_gateway", lambda r: r["receipt"].update(role="verifier")),
        (
            "temperature_zero_sent",
            lambda r: r["receipt"].update(settings_sent={"extra_body": {"temperature": 1}}),
        ),
        (
            "structured_output_forced",
            lambda r: r["receipt"]["settings"].update(answer_source="text"),
        ),
        ("structured_output_forced", lambda r: r["receipt"].update(stop_reason="end_turn")),
        (
            "structured_output_forced",
            lambda r: r["receipt"]["settings"].update(answer_tool_hash="sha256:x"),
        ),
        (
            "model_facing_input_as_predicted",
            lambda r: r["prediction"].update(model_facing_hash="sha256:x"),
        ),
        ("transport_as_predicted", lambda r: r["prediction"].update(request_body_bytes=1)),
        (
            "body_under_cap",
            lambda r: r["receipt"]["transport_bytes"].update(request_body_bytes=88_000),
        ),
        ("gateway_body_under_known_good", lambda r: r.update(gateway_body_bytes=97_601)),
    ],
)
def test_report_fails_closed_on_call_evidence(tmp_path, check, fn):
    code, _, failed = run(*evidence(tmp_path, _call("planner", fn)))
    assert code == 1 and f"planner:{check}" in failed


@pytest.mark.parametrize(
    "check, fn",
    [
        ("audit_temperature_zero", lambda a: a[1].update(temperature_requested=None)),
        ("audit_priced_usage", lambda a: a[1].update(cost_usd=None)),
        ("audit_one_forwarded_successful_ask", lambda a: a[1].update(outcome="REFUSED")),
        ("audit_one_forwarded_successful_ask", lambda a: a.append(dict(a[1]))),
        ("structured_output_forced", lambda a: a[1].update(structured_output=None)),
    ],
)
def test_report_fails_closed_on_the_calls_audit_line(tmp_path, check, fn):
    code, _, failed = run(*evidence(tmp_path, audit_change=fn))
    assert code == 1 and f"planner:{check}" in failed


def test_a_planned_call_without_a_record_fails(tmp_path):
    def drop(container):
        container["calls"] = [r for r in container["calls"] if r["call"] != "repairer"]

    code, _, failed = run(*evidence(tmp_path, drop))
    assert code == 1
    assert "every_planned_call_recorded" in failed and "repairer:role_call_success" in failed


@pytest.mark.parametrize(
    "check, change, audit_change",
    [
        (
            "relay_allows_exactly_the_smoke_roles",
            lambda c: c["health"].update(allowed_roles=["reviewer"]),
            None,
        ),
        ("request_cap_refused_before_forwarding", lambda c: c.pop("cap_probe"), None),
        (
            "request_cap_refused_before_forwarding",
            lambda c: c.update(cap_probe={"outcome": "SKIPPED", "reason": "request cap not full"}),
            None,
        ),
        ("relay_stopped_itself", None, lambda a: a.pop()),
        (
            "relay_stopped_itself",
            None,
            lambda a: a[-1].update(reason="run_seconds elapsed"),
        ),
        ("audit_paid_asks_are_the_planned_calls", None, lambda a: a.insert(0, dict(a[0]))),
        ("audit_redacted_fields_only", None, lambda a: a[0].update(payload={"x": 1})),
        (
            "audit_answer_shape_has_no_excerpt",
            None,
            lambda a: a[0].update(answer_shape={"head": "x"}),
        ),
        (
            "spend_within_budget",
            lambda c: c["calls"][0]["receipt"].update(cost_usd=2.5),
            None,
        ),
        ("probe:relay_port_tcp", lambda c: c.update(egress_probes=[]), None),
        (
            "no_credential_like_env_names",
            lambda c: c.update(credential_like_env_names=["KONG"]),
            None,
        ),
        ("ran_as_uid_1000", lambda c: c.update(uid=0), None),
    ],
)
def test_report_fails_closed_on_run_evidence(tmp_path, check, change, audit_change):
    code, _, failed = run(*evidence(tmp_path, change, audit_change))
    assert code == 1 and check in failed


def test_report_fails_without_container_output_or_key_scan(tmp_path):
    out, relay = evidence(tmp_path)
    (out / report.CONTAINER_JSON).unlink()
    (out / "key-scan.json").unlink()
    code, result, failed = run(out, relay)
    assert code == 1 and result["calls"] == []
    assert {"container_output_parsed", "key_absent_container_and_artifacts"} <= set(failed)
    assert "every_planned_call_recorded" in failed
    assert all(f"{c}:role_call_success" in failed for c, _ in report.EXPECTED_CALLS)


def test_a_capture_fault_spends_nothing_and_fails(tmp_path):
    """The container records a capture fault and makes no call; the report fails closed."""

    def fault(container):
        container["capture_error"] = {"error_type": "RuntimeError", "text": "no review"}
        container["calls"] = []

    code, _, failed = run(*evidence(tmp_path, fault))
    assert code == 1
    assert {"payloads_built_by_the_controller", "every_planned_call_recorded"} <= set(failed)


def test_a_judge_fault_never_reads_as_acceptance(tmp_path):
    def judge_error(record):
        record["controller"] = {
            "controller_accepts": None,
            "controller_outcome": "JUDGE_ERROR",
            "controller_refusal": {"error_type": "KeyError", "text": "'x'", "gate": None},
        }

    code, _, failed = run(*evidence(tmp_path, _call("verifier", judge_error)))
    assert code == 1 and "verifier:controller_accepts" in failed


def test_needs_info_and_lenient_schema_are_reported_not_gated(tmp_path):
    def needs_info(record):
        record["controller"].update(controller_outcome="NEEDS_INFO", needs_info_question="q")

    def schema(record):
        record["schema_errors"] = ["$: unexpected key x"]

    def both(container):
        _call("planner", needs_info)(container)
        _call("verifier", schema)(container)

    code, result, failed = run(*evidence(tmp_path, both))
    assert code == 0 and failed == []
    assert len(result["warnings"]) == 2
    assert "planner: NEEDS_INFO" in result["warnings"][0]
    md = (tmp_path / "out" / "RELAY-ROLE-SMOKE.md").read_text()
    assert "WARNING planner: NEEDS_INFO" in md


# ------------------------------------------------------------------ launcher


def test_launch_relay_smoke_client_runs_the_role_script(tmp_path, monkeypatch):
    calls = []
    line = json.dumps({"schema": smoke.SCHEMA, "calls": []})

    def sh(cmd, **kw):
        calls.append(cmd)
        stdout = '{"ExitCode": 0}' if "inspect" in cmd else ""
        return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")

    def run_(cmd, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout=line + "\n", stderr="")

    net = {"network": "simkit-relay-net", "internal": True}
    monkeypatch.setattr(launch, "sh", sh)
    monkeypatch.setattr(launch.subprocess, "run", run_)
    monkeypatch.setattr(launch, "relay_settings", lambda d, probe_targets=False: ([], {}, net))
    monkeypatch.setattr(launch, "conditions_from_inspect", lambda n, r=None: ({}, {"a": True}))
    out = tmp_path / "out"
    argv = ["x", "relay-smoke", "--client", "relay_role_smoke", "--image", "img"]
    assert launch.main([*argv, "--out", str(out), "--relay-dir", str(tmp_path)]) == 0
    create = calls[0]
    assert create[-1] == "/opt/ctl/container_harness/relay_role_smoke.py"
    assert (REPO / "container_harness" / "relay_role_smoke.py").is_file()
    rec = json.loads((out / "launch.json").read_text())
    assert rec["client"] == report.CLIENT
    assert rec["container"].startswith("simkit-relay-role-smoke-")
    assert json.loads((out / report.CONTAINER_JSON).read_text())["schema"] == smoke.SCHEMA


# ------------------------------------------------------------------ workflow (parsed YAML)


def workflow():
    import yaml  # installed by the workflow for these tests: a missing one fails, never skips

    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def step(job, prefix):
    (hit,) = [s for s in job["steps"] if (s.get("name") or "").startswith(prefix)]
    return hit


def secret_steps(job):
    return [s for s in job["steps"] if "secrets." in json.dumps(s)]


def test_role_job_runs_only_for_its_selector_and_admission_skips_it():
    jobs = workflow()["jobs"]
    job = jobs["relay-role-smoke"]
    assert job["if"] == (
        "github.event_name == 'workflow_dispatch' && "
        "github.event.inputs.selector == 'relay-role-smoke'"
    )
    for selector in ("relay-smoke", "relay-dedup-smoke", "relay-role-smoke"):
        assert f"github.event.inputs.selector != '{selector}'" in jobs["admission"]["if"]
    assert "needs" not in job  # independent of admission/case/report
    selected = [n for n, j in jobs.items() if "== 'relay-role-smoke'" in j.get("if", "")]
    assert selected == ["relay-role-smoke"]
    inputs = (workflow().get("on") or workflow()[True])["workflow_dispatch"]["inputs"]
    assert "'relay-role-smoke'" in inputs["selector"]["description"]


def test_role_job_secret_scoping_is_identical_to_relay_smoke():
    jobs = workflow()["jobs"]
    base, dedup, job = jobs["relay-smoke"], jobs["relay-dedup-smoke"], jobs["relay-role-smoke"]
    assert "secrets." not in json.dumps(job.get("env") or {})
    base_secret, job_secret = secret_steps(base), secret_steps(job)
    assert [s["name"] for s in job_secret] == [s["name"] for s in base_secret]
    assert [s["name"] for s in job_secret] == [s["name"] for s in secret_steps(dedup)]
    for b, d in zip(base_secret, job_secret, strict=True):
        assert d["env"]["KONG_ANTHROPIC_KEY"] == "${{ secrets.KONG_ANTHROPIC_KEY }}"
        assert d.get("if") == b.get("if") and d["run"] == b["run"]
        assert "secrets." not in d["run"]
        assert [k for k, v in d["env"].items() if "secrets." in v] == ["KONG_ANTHROPIC_KEY"]


def test_role_job_relay_caps_are_the_smokes():
    job = workflow()["jobs"]["relay-role-smoke"]
    env = step(job, "Start host relay")["env"]
    caps = {k: v for k, v in env.items() if k in smoke.RELAY_CAPS}
    assert caps == smoke.RELAY_CAPS
    assert set(env) == {*smoke.RELAY_CAPS, "KONG_ANTHROPIC_KEY", "RELAY_RUN_SECONDS"}
    assert env["RELAY_MAX_REQUESTS"] == str(len(smoke.CALLS))
    assert set(env["RELAY_ROLES"].split()) == set(smoke.ROLES)
    # planner/repairer keep the live 4096 ceiling (no RELAY_ROLE_MAX_TOKENS override)
    assert "RELAY_ROLE_MAX_TOKENS" not in env
    # the relay outlives the container (else it stops on run_seconds, not the cap), and the
    # job outlives both
    run_ = step(job, "Role smoke container")["run"]
    container_timeout = int(run_.split("--timeout ")[1].split()[0])
    relay_seconds = int(env["RELAY_RUN_SECONDS"])
    assert smoke.CALL_TIMEOUT * len(smoke.CALLS) < container_timeout < relay_seconds
    assert relay_seconds < job["timeout-minutes"] * 60


def test_role_job_reuses_relay_smoke_isolation_and_teardown():
    jobs = workflow()["jobs"]
    base, job = jobs["relay-smoke"], jobs["relay-role-smoke"]
    assert job["env"]["RELAY_DIR"] == base["env"]["RELAY_DIR"]
    assert job["env"]["OUT"] == "${{ github.workspace }}/relay-role-smoke-out"
    for prefix in ("Build harness image", "Relay-only network", "Key-absence proof", "Tear down"):
        assert step(job, prefix)["run"] == step(base, prefix)["run"], prefix
    names = [s.get("name") or s.get("uses") for s in job["steps"]]
    order = [
        "Runner Kong client",
        "Controller-judge offline tests",
        "Build harness image",
        "Relay-only network",
        "Start host relay",
        "Role smoke container",
        "Key-absence proof",
        "Tear down",
        "Assert role smoke",
        "Upload relay role smoke evidence",
    ]
    idx = [next(i for i, n in enumerate(names) if n.startswith(p)) for p in order]
    assert idx == sorted(idx)
    run_ = step(job, "Role smoke container")["run"]
    assert "launch.py relay-smoke --client relay_role_smoke" in run_
    assert "env" not in step(job, "Role smoke container")
    assert "relay_role_smoke_report.py" in step(job, "Assert role smoke")["run"]
    up = step(job, "Upload relay role smoke evidence")
    assert up["if"] == "always()"
    assert up["with"]["name"] == "simkit-relay-role-smoke"
    assert up["with"]["path"] == "relay-role-smoke-out"
    # the offline tests (slim and controller-judge) run before anything is built or spent
    tests = step(job, "Runner Kong client")
    assert "container_harness/tests/test_relay_role_smoke.py" in tests["run"]
    assert "pyyaml==" in tests["run"] and "env" not in tests
    assert (
        tests["run"].split("\n")[0]
        == step(jobs["relay-dedup-smoke"], "Runner Kong")["run"].split("\n")[0]
    )  # the relay's own pinned SDK install, as the dedup job does it
    judge = step(job, "Controller-judge offline tests")
    assert "tools/workflow/tests/test_relay_role_smoke_controller.py" in judge["run"]
    assert "env" not in judge and "secrets." not in json.dumps(judge)


def test_no_other_job_mentions_the_role_selector():
    jobs = workflow()["jobs"]
    for name, job in jobs.items():
        if name in ("relay-role-smoke", "admission"):
            continue
        assert "relay-role-smoke" not in json.dumps(job), name
    # deep copy so a failure message never shows a mutated structure
    assert copy.deepcopy(jobs["relay-role-smoke"])["runs-on"] == "ubuntu-latest"
