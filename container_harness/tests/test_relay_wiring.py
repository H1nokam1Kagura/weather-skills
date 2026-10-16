"""Relay wiring for GitHub runners, offline (no docker, no network, no secret).

Pins: the workflow exposes the Kong secret ONLY to the relay-start and key-scan steps; the
launcher passes relay settings as ``-e NAME`` (values in the docker client env, never argv) and
strips Kong variables from that env; the key scanner counts across chunk edges and in encoded
forms; the smoke report fails closed.
"""

import base64
import io
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(HERE.parent))

import kong_key_scan  # noqa: E402
import launch  # noqa: E402
import relay_smoke_report  # noqa: E402

WORKFLOW = REPO / ".github" / "workflows" / "simkit-cases.yml"
FAKE = "fake-kong-key-0123456789abcdef"  # synthetic test value, not a credential


# ------------------------------------------------------------------ workflow secret scoping


def steps(text):
    """Split the workflow into (job, step-name, step-text) by '      - ' step markers."""
    out, job = [], None
    for block in re.split(r"\n(?=  [a-z][a-z-]*:\n|      - )", text):
        m = re.match(r"  ([a-z][a-z-]*):\n", block)
        if m:
            job = m.group(1)
            continue
        if block.startswith("      - "):
            name = re.search(r"name: (.+)", block)
            out.append((job, name.group(1) if name else block.split("\n")[0], block))
    return out


def test_kong_secret_is_scoped_to_relay_start_and_key_scan_steps_only():
    text = WORKFLOW.read_text(encoding="utf-8")
    holders = [(job, name) for job, name, body in steps(text) if "secrets." in body]
    assert holders and all(
        ("relay" in name.lower() and "start" in name.lower()) or "key-absence" in name.lower()
        for _, name in holders
    ), holders
    assert {job for job, _ in holders} == {"relay-smoke", "case"}
    for _, _, body in steps(text):
        for line in body.splitlines():
            if "secrets." in line:
                assert line.strip() == "KONG_ANTHROPIC_KEY: ${{ secrets.KONG_ANTHROPIC_KEY }}"
    # never at job or workflow level, never inside a run: script
    head = text.split("\njobs:\n", 1)[0]
    assert "secrets." not in head
    assert not re.search(r"run: .*secrets\.", text)


def test_relay_smoke_is_dispatch_only_and_admission_skips_it():
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "github.event.inputs.selector == 'relay-smoke'" in text
    assert "github.event.inputs.selector != 'relay-smoke'" in text


# ------------------------------------------------------------------ workflow (parsed YAML)


def workflow():
    import yaml  # simkit-cases.yml installs pyyaml for these tests: a missing one fails, never skips

    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def named(job, prefix):
    (hit,) = [
        (i, s) for i, s in enumerate(job["steps"]) if (s.get("name") or "").startswith(prefix)
    ]
    return hit


def test_workflow_declares_the_live_inputs():
    wf = workflow()
    inputs = (wf.get("on") or wf[True])["workflow_dispatch"]["inputs"]
    for name in ("live_max_cases", "rerun_of_run_id"):
        assert inputs[name]["required"] is False and inputs[name]["default"] == ""
    assert "live" in inputs["live_max_cases"]["description"].lower()
    assert inputs["max_parallel"]["default"] == "8"


def test_harness_tests_install_pyyaml_for_the_wiring_tests():
    _, step = named(workflow()["jobs"]["admission"], "Harness unit tests")
    assert "pyyaml==" in step["run"]


def test_live_spend_gate_runs_in_admission_after_the_upload():
    adm = workflow()["jobs"]["admission"]
    assert adm["env"]["LIVE_MAX_CASES"] == "${{ github.event.inputs.live_max_cases }}"
    i_up, _ = named(adm, "Upload admission")
    i_gate, gate = named(adm, "Live spend gate")
    assert i_gate > i_up and "if" not in gate  # always enforced, never skipped
    assert "container_harness/relay_usage.py live-gate" in gate["run"]
    assert '--live-max-cases "$LIVE_MAX_CASES"' in gate["run"]
    assert "|| true" not in gate["run"]
    # the recorded copy (uploaded) is written in the matrix step, before the upload
    i_matrix, matrix = named(adm, "Case matrix")
    assert i_matrix < i_up and "LIVE-GATE.json" in matrix["run"]
    assert "relay_usage.max_parallel(" in matrix["run"] and "live_count=" in matrix["run"]
    assert adm["outputs"]["max_parallel"] == "${{ steps.matrix.outputs.max_parallel }}"
    assert adm["outputs"]["live_count"] == "${{ steps.matrix.outputs.live_count }}"
    # a failed admission job skips every case job (no Kong call)
    assert workflow()["jobs"]["case"]["needs"] == "admission"


def test_case_max_parallel_comes_from_admission():
    case = workflow()["jobs"]["case"]
    assert case["strategy"]["max-parallel"] == (
        "${{ fromJSON(needs.admission.outputs.max_parallel || '8') }}"
    )
    import relay_usage

    assert relay_usage.LIVE_MAX_PARALLEL == 3 and relay_usage.max_parallel("", 0) == 8


def test_live_case_relay_caps_come_from_the_matrix_row():
    case = workflow()["jobs"]["case"]
    i_caps, caps = named(case, "Relay caps from the admission matrix row")
    i_start, start = named(case, "Start host relay")
    assert i_caps < i_start
    assert caps["id"] == "caps" and caps["if"] == "matrix.role_mode == 'live'"
    assert caps["env"] == {"MATRIX_ROW": "${{ toJSON(matrix) }}"}
    assert "relay_usage.py caps" in caps["run"] and '>> "$GITHUB_OUTPUT"' in caps["run"]
    env = start["env"]
    assert env["RELAY_ROLES"] == "${{ steps.caps.outputs.roles }}"
    assert env["RELAY_MAX_REQUESTS"] == "${{ steps.caps.outputs.max_requests }}"
    assert env["RELAY_MAX_TOTAL_TOKENS"] == "${{ steps.caps.outputs.max_total_tokens }}"
    assert env["RELAY_BUDGET"] == "${{ steps.caps.outputs.budget_usd }}"
    assert env["KONG_ANTHROPIC_KEY"] == "${{ secrets.KONG_ANTHROPIC_KEY }}"
    _, teardown = named(case, "Tear down relay")
    assert '"$RUNNER_TEMP/relay-caps.json" "$CASE_OUT/relay/"' in teardown["run"]


def test_live_case_keeps_the_container_scans_its_export_then_always_removes_it():
    case = workflow()["jobs"]["case"]
    i_run, run = named(case, "Run case in the traced container")
    assert '--relay-dir "$RELAY_DIR" --keep-container' in run["run"]
    assert "if" not in run  # offline cases run the same step without the flag
    i_down, _ = named(case, "Tear down relay")
    i_scan, scan = named(case, "Key-absence scan")
    i_rm, rm = named(case, "Remove the kept case container")
    i_up, _ = named(case, "Upload case result")
    assert i_run < i_down < i_scan < i_rm < i_up
    live_always = "always() && matrix.role_mode == 'live'"
    assert scan["if"] == live_always and rm["if"] == live_always
    for flag in ('--container "$c"', '--scan-dir "$CASE_OUT"', '--scan-dir "$RELAY_DIR"'):
        assert flag in scan["run"]
    assert 'cat "$CASE_OUT/container.txt"' in scan["run"] and 'exit "$rc"' in scan["run"]
    assert 'docker rm -f "$c"' in rm["run"] and "env" not in rm


def test_kong_secret_steps_by_parsed_yaml():
    holders = []
    for job_name, job in workflow()["jobs"].items():
        assert "secrets." not in json.dumps(job.get("env") or {})
        for step in job["steps"]:
            if "secrets." in json.dumps(step):
                assert set(step.get("env") or {}) >= {"KONG_ANTHROPIC_KEY"}
                assert "secrets." not in step.get("run", "")
                holders.append((job_name, step["name"]))
    assert sorted(holders) == sorted(
        [
            (
                "relay-smoke",
                "Start host relay (the ONLY step besides the key scan that sees the Kong key)",
            ),
            (
                "relay-smoke",
                "Key-absence proof (host-side scan of the exported container + artifacts; counts only)",
            ),
            ("case", "Start host relay (live-role case; the only step that sees the Kong key)"),
            (
                "case",
                "Key-absence scan of the exported container + case artifacts + relay dir (live-role case; counts only)",
            ),
        ]
    )


def test_report_reads_the_original_run_for_a_rerun():
    rep = workflow()["jobs"]["report"]
    assert rep["permissions"] == {"contents": "read", "actions": "read"}
    assert rep["env"]["RERUN_OF_RUN_ID"] == "${{ github.event.inputs.rerun_of_run_id }}"
    i_prior, prior = named(rep, "Original run's results")
    i_agg, agg = named(rep, "Aggregate RESULTS.json")
    assert i_prior < i_agg and "continue-on-error" not in prior
    assert prior["if"] == "github.event.inputs.rerun_of_run_id != ''"
    assert prior["with"]["name"] == "simkit-results"
    assert prior["with"]["run-id"] == "${{ github.event.inputs.rerun_of_run_id }}"
    assert "--prior-results report-in/prior/RESULTS.json" in agg["run"]
    assert "github.token" in prior["with"]["github-token"]


def test_admission_refuses_a_rerun_before_any_case_job():
    wf = workflow()
    adm = wf["jobs"]["admission"]
    rerun_set = "github.event.inputs.rerun_of_run_id != ''"
    assert adm["permissions"] == {"contents": "read", "actions": "read"}
    assert adm["env"]["RERUN_OF_RUN_ID"] == "${{ github.event.inputs.rerun_of_run_id }}"
    i_adm, _ = named(adm, "Frozen-runner capability admission")
    i_dl, dl = named(adm, "Original run's results for the rerun gate")
    i_rec, rec = named(adm, "Rerun gate record")
    i_up, up = named(adm, "Upload admission")
    i_gate, gate = named(adm, "Rerun gate (rerun dispatch only")
    assert i_adm < i_dl < i_rec < i_up < i_gate
    # only when rerun_of_run_id is set; the download never tolerates a missing original
    assert dl["if"] == rerun_set and gate["if"] == rerun_set
    assert rec["if"] == f"always() && {rerun_set}"
    assert "continue-on-error" not in dl and "continue-on-error" not in gate
    assert dl["uses"].startswith("actions/download-artifact@")
    assert dl["with"]["name"] == "simkit-results" and dl["with"]["path"] == "rerun-prior"
    assert dl["with"]["run-id"] == "${{ github.event.inputs.rerun_of_run_id }}"
    assert dl["with"]["github-token"] == "${{ github.token }}"
    for step in (rec, gate):
        assert "container_harness/relay_usage.py rerun-gate" in step["run"]
        assert "--prior-results rerun-prior/RESULTS.json" in step["run"]
        assert '--rerun-of-run-id "$RERUN_OF_RUN_ID"' in step["run"]
    # the record lands in the uploaded admission evidence; the enforcing step never swallows
    assert '"$OUT/RERUN-GATE.json"' in rec["run"] and up["with"]["path"] == "admission-out"
    assert "|| true" not in gate["run"]
    # case jobs need admission, so a refused rerun starts no case (and makes no Kong call)
    assert wf["jobs"]["case"]["needs"] == "admission"
    # without rerun_of_run_id nothing else in admission changes: no other step reads it
    readers = [s.get("name") for s in adm["steps"] if "rerun" in json.dumps(s).lower()]
    assert sorted(readers) == sorted([dl["name"], rec["name"], gate["name"]])


# ------------------------------------------------------------------ launcher


class FakeDocker:
    """Records docker calls made by launch.run; no docker needed."""

    def __init__(self):
        self.calls = []

    def sh(self, cmd, **kw):
        self.calls.append(cmd)
        stdout = '{"ExitCode": 0, "OOMKilled": false}' if "inspect" in cmd else ""
        return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")

    def run(self, cmd, **kw):
        self.calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")


def launch_run(tmp_path, monkeypatch, *extra):
    fake = FakeDocker()
    monkeypatch.setattr(launch, "sh", fake.sh)
    monkeypatch.setattr(launch.subprocess, "run", fake.run)
    monkeypatch.setattr(launch, "conditions_from_inspect", lambda name, net=None: ({}, {"a": True}))
    out = tmp_path / "out"
    argv = ["x", "run", "case", "--case-id", "C1", "--image", "img", "--out", str(out), *extra]
    assert launch.main(argv) == 0
    rec = json.loads((out / "launch.json").read_text(encoding="utf-8"))
    return fake, out, rec


def test_run_removes_the_container_by_default(tmp_path, monkeypatch):
    fake, out, rec = launch_run(tmp_path, monkeypatch)
    assert any(c[:3] == ["docker", "rm", "-f"] for c in fake.calls)
    assert not (out / "container.txt").exists() and "container_kept" not in rec


def test_run_keep_container_leaves_it_named_for_the_key_scan(tmp_path, monkeypatch):
    fake, out, rec = launch_run(tmp_path, monkeypatch, "--keep-container")
    assert not any(c[:2] == ["docker", "rm"] for c in fake.calls)
    name = (out / "container.txt").read_text(encoding="utf-8").strip()
    assert name == rec["container"] and rec["container_kept"] is True
    assert ["docker", "cp", f"{name}:/harness-out", str(out / "harness-out")] in fake.calls


def relay_dir(tmp_path, internal=True, bind="172.30.99.1", port=18765):
    (tmp_path / "secret").mkdir(parents=True)
    (tmp_path / "secret" / "relay.token").write_text("tok-" + "x" * 40 + "\n")
    (tmp_path / "net.json").write_text(
        json.dumps(
            {
                "network": "simkit-relay-net",
                "gateway": "172.30.99.1",
                "relay_port": 18765,
                "internal": internal,
                "probe_targets": {"kong_ip_host_resolved": "203.0.113.7"},
            }
        )
    )
    (tmp_path / "relay-ready.json").write_text(
        json.dumps({"run_id": "relay-run-1", "port": port, "binds": [bind], "model": "m"})
    )
    return tmp_path


def test_relay_settings_pass_names_only_and_strip_kong_env(tmp_path, monkeypatch):
    monkeypatch.setenv("KONG_ANTHROPIC_KEY", FAKE)
    monkeypatch.setenv("ANTHROPIC_API_KEY", FAKE)
    args, env, net = launch.relay_settings(relay_dir(tmp_path), probe_targets=True)
    assert "none" not in args and args[args.index("--network") + 1] == "simkit-relay-net"
    names = [args[i + 1] for i, a in enumerate(args) if a == "-e"]
    assert names == list(launch.RELAY_ENV_NAMES)
    assert all("=" not in n for n in names)
    assert env["SIMKIT_RELAY_URL"] == "http://172.30.99.1:18765"
    assert env["SIMKIT_RELAY_TOKEN"] == "tok-" + "x" * 40
    assert "KONG_ANTHROPIC_KEY" not in env and "ANTHROPIC_API_KEY" not in env
    assert FAKE not in json.dumps(args) and FAKE not in json.dumps(env)
    for flag in ("--init", "SYS_PTRACE", "core=0"):
        assert flag in args
    launch.check_cmd(["docker", "create", *args, "img"])


def test_relay_settings_refuse_non_internal_or_misbound(tmp_path):
    with pytest.raises(SystemExit, match="internal"):
        launch.relay_settings(relay_dir(tmp_path / "a", internal=False))
    with pytest.raises(SystemExit, match="gateway"):
        launch.relay_settings(relay_dir(tmp_path / "b", bind="127.0.0.1"))


def test_check_cmd_refuses_values_on_argv_and_privilege():
    with pytest.raises(AssertionError):
        launch.check_cmd(["docker", "create", "-e", "SIMKIT_RELAY_TOKEN=abc", "img"])
    with pytest.raises(AssertionError):
        launch.check_cmd(["docker", "create", "--privileged", "img"])
    with pytest.raises(AssertionError):
        launch.check_cmd(["docker", "create", "-v", "/x:/y", "img"])


# ------------------------------------------------------------------ key scanner


def test_count_stream_finds_matches_across_chunk_edges(monkeypatch):
    monkeypatch.setattr(kong_key_scan, "CHUNK", 7)
    data = b"aa" + FAKE.encode() + b"bbb" + FAKE.encode() + b"c"
    found, scanned = kong_key_scan.count_stream(io.BytesIO(data), [FAKE.encode()])
    assert found == 2 and scanned == len(data)


def test_scan_dirs_counts_plain_and_encoded_forms(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("KONG_ANTHROPIC_KEY", FAKE)
    (tmp_path / "clean.txt").write_text("nothing here")
    out = tmp_path / "scan.json"
    assert kong_key_scan.main(["--scan-dir", str(tmp_path), "--out", str(out)]) == 0
    assert json.loads(out.read_text())["key_absent"] is True
    out.unlink()
    (tmp_path / "leak.b64").write_bytes(base64.b64encode(FAKE.encode()))
    assert kong_key_scan.main(["--scan-dir", str(tmp_path), "--out", str(out)]) == 1
    result = json.loads(out.read_text())
    assert result["artifacts"]["files_with_matches"] == ["leak.b64"]
    assert FAKE not in out.read_text() and FAKE not in capsys.readouterr().out


def test_scan_without_the_secret_is_not_a_pass(tmp_path, monkeypatch):
    monkeypatch.delenv("KONG_ANTHROPIC_KEY", raising=False)
    out = tmp_path / "scan.json"
    assert kong_key_scan.main(["--scan-dir", str(tmp_path), "--out", str(out)]) == 1
    assert json.loads(out.read_text())["key_absent"] is False


# ------------------------------------------------------------------ smoke report


def smoke_inputs(tmp_path, **changes):
    out, relay = tmp_path / "out", tmp_path / "relay"
    out.mkdir()
    relay.mkdir()
    conditions = {"never_privileged": True, "relay_only_network": True, "no_docker_exec": True}
    names = sorted(relay_smoke_report.EXPECTED_ENV_NAMES)
    launch_rec = {"run_conditions": conditions, "env_names_passed": names, "smoke_parsed": True}
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
    smoke = {
        "uid": 1000,
        "credential_like_env_names": [],
        "credential_like_files": [],
        "health": {"outcome": "OK"},
        "egress_probes": probes,
        "outcome": "SUCCESS",
        "receipt": {
            "model_observed": "claude-sonnet-4-6",
            "gateway_mock": False,
            "transport": "kong-native-anthropic",
        },
    }
    smoke.update(changes)
    (out / "launch.json").write_text(json.dumps(launch_rec))
    (out / "relay-smoke.container.json").write_text(json.dumps(smoke))
    (out / "key-scan.json").write_text(json.dumps({"key_absent": True}))
    (relay / "net.json").write_text(json.dumps({"internal": True}))
    audit = [{"event": "ask", "outcome": "SUCCESS"}, {"event": "relay_stop", "reason": "max"}]
    (relay / "relay-audit.jsonl").write_text("\n".join(json.dumps(a) for a in audit))
    return out, relay


def test_smoke_report_passes_on_complete_evidence(tmp_path, monkeypatch):
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    out, relay = smoke_inputs(tmp_path)
    assert relay_smoke_report.main(["x", str(out), str(relay)]) == 0


@pytest.mark.parametrize(
    "change",
    [
        {"outcome": "UNKNOWN"},
        {"receipt": {"model_observed": "", "gateway_mock": False}},
        {"credential_like_env_names": ["KONG_ANTHROPIC_KEY"]},
        {"egress_probes": []},
    ],
)
def test_smoke_report_fails_closed(tmp_path, monkeypatch, change):
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    out, relay = smoke_inputs(tmp_path, **change)
    assert relay_smoke_report.main(["x", str(out), str(relay)]) == 1


def test_smoke_report_fails_when_a_blocked_target_connects(tmp_path, monkeypatch):
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    out, relay = smoke_inputs(tmp_path)
    smoke = json.loads((out / "relay-smoke.container.json").read_text())
    smoke["egress_probes"][1].update(observed="connected", passed=False)
    (out / "relay-smoke.container.json").write_text(json.dumps(smoke))
    assert relay_smoke_report.main(["x", str(out), str(relay)]) == 1


def test_credential_file_heuristic_ignores_hongkong_system_files():
    import relay_smoke

    for benign in (
        "/etc/ssl/certs/Hongkong_Post_Root_CA_3.pem",
        "/usr/share/ca-certificates/mozilla/Hongkong_Post_Root_CA_3.crt",
        "/usr/share/zoneinfo/Asia/Hong_Kong",
    ):
        assert not relay_smoke.credential_like_name(benign)
    for bad in ("/home/x/.env", "/tmp/prod.env", "/tmp/kong_key.txt", "/root/KONG-TOKEN"):
        assert relay_smoke.credential_like_name(bad)
