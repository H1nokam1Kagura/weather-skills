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


# ------------------------------------------------------------------ launcher


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
