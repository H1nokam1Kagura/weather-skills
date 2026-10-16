"""Build the harness image and run one traced container scenario (stdlib only; host side).

usage:
    python container_harness/launch.py build --tag simkit-harness-ctl:<sha> [--context DIR]
    python container_harness/launch.py run MODE --image TAG --out DIR [--case-id ID]
                                       [--no-trace] [--kill-tracer-after S] [--timeout S]
                                       [--relay-dir RDIR] [--keep-container]
    python container_harness/launch.py admission --image TAG --out ADMISSION.json
                                       [--selector S] [--smoke-ids A,B]
    python container_harness/launch.py relay-smoke --image TAG --out DIR --relay-dir RDIR
                                       [--client relay_smoke|relay_dedup_smoke]

Live roles (owner ruling container_route_ruling_2026-10-13.live_roles): with ``--relay-dir``
(written by ``relay_ci.sh net-up`` + ``relay-start``) the container joins the docker
``--internal`` relay network instead of ``--network none`` and receives exactly
SIMKIT_RELAY_URL / _TOKEN / _RUN_ID / _MODEL (+ SIMKIT_PROBE_TARGETS for the smoke) as
``-e NAME`` with the values in the docker client's environment, never on its command line.
The Kong key is never among them. ``relay-smoke`` runs container_harness/relay_smoke.py as the
container's own command and leaves the exited container for the host key scan.

``admission`` runs the frozen runner's capability admission (case_driver.py) in one throwaway
container under the same run conditions (no network, no mounts) and writes its stdout JSON.

MODE is an entry.sh mode (canary, tree, kill-tracer). ``run`` creates ONE fresh container under
the owner-ruled run conditions (container_route_ruling_2026-10-13), starts it attached, waits,
copies /harness-out out with ``docker cp`` after exit (never ``docker exec``), records
``docker diff`` as an independent cross-check, removes the container (``--keep-container``
leaves it, named in DIR/container.txt, for the live case job's ``docker export`` key scan; the
workflow removes it in an always() step), then projects the trace
with analyze_trace.py into DIR/evidence.json. The conditions are re-read from ``docker inspect``
and recorded beside the evidence, so a run that drifted from them is visible in its own output.
"""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(HERE))

import analyze_trace  # noqa: E402

# Owner ruling run conditions. Default seccomp (no --security-opt seccomp=...), never
# --privileged, one added capability (SYS_PTRACE, so the root tracer can trace the uid-1000
# tree and the tree cannot trace or signal the tracer), no bind mounts or volumes at all, core
# dumps off, no network (fixture roles; no Kong), docker-init as PID 1.
RUN_ARGS = [
    "--init",
    "--cap-add",
    "SYS_PTRACE",
    "--ulimit",
    "core=0",
    "--network",
    "none",
]
FORBIDDEN = ("--privileged", "-v", "--volume", "--mount", "seccomp=unconfined", "--pid")
RELAY_ENV_NAMES = (
    "SIMKIT_RELAY_URL",
    "SIMKIT_RELAY_TOKEN",
    "SIMKIT_RELAY_RUN_ID",
    "SIMKIT_RELAY_MODEL",
    "SIMKIT_PROBE_TARGETS",
)
FORBIDDEN_ENV_MARKERS = ("KONG", "ANTHROPIC", "OPENAI")
# relay-smoke in-container clients (container_harness/<client>.py), same run conditions.
SMOKE_CLIENTS = ("relay_smoke", "relay_dedup_smoke")


def relay_settings(relay_dir, probe_targets=False):
    """(docker args, client env, net) joining the relay network; values only in the client env."""
    relay_dir = Path(relay_dir)
    net = json.loads((relay_dir / "net.json").read_text(encoding="utf-8"))
    ready = json.loads((relay_dir / "relay-ready.json").read_text(encoding="utf-8"))
    if net["gateway"] not in ready["binds"] or ready["port"] != net["relay_port"]:
        raise SystemExit("relay is not bound to the relay network gateway/port")
    if not net["internal"]:
        raise SystemExit("relay network is not --internal")
    values = {
        "SIMKIT_RELAY_URL": f"http://{net['gateway']}:{ready['port']}",
        "SIMKIT_RELAY_TOKEN": (relay_dir / "secret" / "relay.token").read_text().strip(),
        "SIMKIT_RELAY_RUN_ID": ready["run_id"],
        "SIMKIT_RELAY_MODEL": ready["model"],
    }
    if probe_targets:
        values["SIMKIT_PROBE_TARGETS"] = json.dumps(net["probe_targets"])
    args = [a for a in RUN_ARGS if a not in ("--network", "none")]
    args += ["--network", net["network"]]
    for name in values:
        args += ["-e", name]  # NAME only: docker reads the value from its own environment
    # The docker client itself never holds a Kong credential either.
    env = {
        k: v
        for k, v in os.environ.items()
        if not any(m in k.upper() for m in FORBIDDEN_ENV_MARKERS)
    }
    env.update(values)
    return args, env, net


def check_cmd(cmd):
    assert not any(tok in FORBIDDEN or tok.startswith("--privileged") for tok in cmd), cmd
    for tok in cmd:
        assert tok.split("=", 1)[0] not in RELAY_ENV_NAMES or "=" not in tok, "env value on argv"


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def sh(cmd, **kw):
    return subprocess.run(cmd, check=True, text=True, **kw)


def build(args):
    pins = json.loads((HERE / "pinned_inputs.json").read_text(encoding="utf-8"))
    bad = [f["repo_path"] for f in pins["files"] if sha256(REPO / f["repo_path"]) != f["sha256"]]
    if bad:
        raise SystemExit(f"pinned input sha256 mismatch on the host: {bad}")
    commit = sh(["git", "-C", str(REPO), "rev-parse", "HEAD"], capture_output=True).stdout.strip()
    ctx = Path(args.context or tempfile.mkdtemp(prefix="simkit-harness-ctx-"))
    if ctx.exists():
        shutil.rmtree(ctx)
    ctx.mkdir(parents=True)
    archive = ctx / "ctl.tar"
    sh(["git", "-C", str(REPO), "archive", "--format=tar", "-o", str(archive), "HEAD"])
    with tarfile.open(archive) as tar:
        tar.extractall(ctx / "ctl", filter="data")
    archive.unlink()
    shutil.copytree(HERE / "pinned", ctx / "simkit")
    files = [
        {"role": f["role"], "path": f["image_path"], "sha256": f["sha256"]} for f in pins["files"]
    ]
    spec = {"supplement_id": pins["supplement_id"], "controller_commit": commit, "files": files}
    (ctx / "inputs.json").write_text(json.dumps(spec, indent=1) + "\n", encoding="utf-8")
    shutil.copy2(HERE / "Dockerfile", ctx / "Dockerfile")
    shutil.copy2(HERE / "requirements-ctl.txt", ctx / "requirements-ctl.txt")
    t0 = time.monotonic()
    sh(
        [
            "docker",
            "build",
            "--progress=plain",
            "-t",
            args.tag,
            "--build-arg",
            f"CONTROLLER_COMMIT={commit}",
            str(ctx),
        ]
    )
    image_id = sh(
        ["docker", "image", "inspect", "-f", "{{.Id}}", args.tag], capture_output=True
    ).stdout.strip()
    record = {
        "schema": "simkit.harness-build/1",
        "tag": args.tag,
        "image_id": image_id,
        "controller_commit": commit,
        "build_seconds": round(time.monotonic() - t0, 1),
        "inputs": spec,
    }
    print(json.dumps(record, indent=1))
    if args.record:
        Path(args.record).write_text(json.dumps(record, indent=1) + "\n", encoding="utf-8")
    return 0


def conditions_from_inspect(name, relay_net=None):
    raw = sh(["docker", "inspect", name], capture_output=True).stdout
    info = json.loads(raw)[0]
    hc = info.get("HostConfig", {})
    sec = hc.get("SecurityOpt") or []
    ulimits = {u.get("Name"): (u.get("Soft"), u.get("Hard")) for u in (hc.get("Ulimits") or [])}
    observed = {
        "privileged": hc.get("Privileged"),
        "cap_add": hc.get("CapAdd"),
        "security_opt": sec,
        "binds": hc.get("Binds"),
        "mounts": info.get("Mounts"),
        "network_mode": hc.get("NetworkMode"),
        "init": hc.get("Init"),
        "ulimit_core": ulimits.get("core"),
        "pid_mode": hc.get("PidMode"),
    }
    checks = {
        "never_privileged": observed["privileged"] is False,
        "only_sys_ptrace_added": sorted(observed["cap_add"] or [])
        in (["SYS_PTRACE"], ["CAP_SYS_PTRACE"]),
        "default_seccomp": not any("seccomp" in s for s in sec),
        "no_bind_mounts_or_volumes": not observed["binds"] and not observed["mounts"],
        "core_dumps_disabled": observed["ulimit_core"] == (0, 0),
        "docker_init_pid1": observed["init"] is True,
        "own_pid_namespace": observed["pid_mode"] in ("", None, "private"),
        "no_docker_exec": True,  # this launcher never calls docker exec; nothing else touches it
    }
    if relay_net is None:
        checks["no_network"] = observed["network_mode"] == "none"
    else:  # live roles: only the --internal relay network (egress proven by relay_smoke probes)
        checks["relay_only_network"] = (
            observed["network_mode"] == relay_net["network"] and relay_net["internal"] is True
        )
    return observed, checks


def run(args):
    out = Path(args.out)
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    name = f"simkit-harness-{args.mode}-{uuid.uuid4().hex[:8]}"
    entry_args = [args.mode]
    if args.mode == "case":
        if not args.case_id:
            raise SystemExit("run case needs --case-id")
        entry_args.append(args.case_id)
    if args.no_trace:
        entry_args.append("--no-trace")
    if args.kill_tracer_after:
        entry_args += ["--kill-tracer-after", str(args.kill_tracer_after)]
    run_args, env, relay_net = list(RUN_ARGS), None, None
    if args.relay_dir:
        run_args, env, relay_net = relay_settings(args.relay_dir)
    cmd = ["docker", "create", "--name", name, *run_args, args.image, *entry_args]
    check_cmd(cmd)
    sh(cmd, capture_output=True, env=env)
    keep = bool(getattr(args, "keep_container", False))
    if keep:
        # Named before anything can fail, so the workflow's always() removal step finds it.
        (out / "container.txt").write_text(name + "\n", encoding="utf-8")
    record = {
        "schema": "simkit.harness-launch/1",
        "mode": args.mode,
        "image": args.image,
        "container": name,
        "docker_create": cmd,
        "entry_args": entry_args,
    }
    if keep:
        record["container_kept"] = True  # the caller removes it (docker rm -f, container.txt)
    try:
        observed, checks = conditions_from_inspect(name, relay_net)
        record["run_conditions_observed"] = observed
        record["run_conditions"] = checks
        record["relay_network"] = relay_net["network"] if relay_net else None
        t0 = time.monotonic()
        try:
            proc = subprocess.run(
                ["docker", "start", "-a", name],
                capture_output=True,
                text=True,
                timeout=args.timeout,
            )
            record["docker_start_rc"] = proc.returncode
            (out / "container.stdout").write_text(proc.stdout, encoding="utf-8")
            (out / "container.stderr").write_text(proc.stderr, encoding="utf-8")
        except subprocess.TimeoutExpired:
            record["docker_start_rc"] = "timeout"
            subprocess.run(["docker", "kill", name], capture_output=True)
        record["container_wall_seconds"] = round(time.monotonic() - t0, 3)
        state = json.loads(
            sh(["docker", "inspect", "-f", "{{json .State}}", name], capture_output=True).stdout
        )
        record["container_exit_code"] = state.get("ExitCode")
        record["oom_killed"] = state.get("OOMKilled")
        subprocess.run(
            ["docker", "cp", f"{name}:/harness-out", str(out / "harness-out")],
            capture_output=True,
            text=True,
        )
        diff = subprocess.run(["docker", "diff", name], capture_output=True, text=True)
        (out / "docker_diff.txt").write_text(diff.stdout, encoding="utf-8")
    finally:
        if not keep:
            subprocess.run(["docker", "rm", "-f", name], capture_output=True)
    hout = out / "harness-out"
    status = analyze_trace.read_status(hout / "harness_status.txt")
    record["harness_status"] = status
    if hout.is_dir():
        # Authoritative evidence is the IN-CONTAINER analysis (entry.sh step 5, trace path
        # namespace). The host re-analysis of the copied trace is a determinism cross-check only.
        host = analyze_trace.analyze(hout, status.get("run_dir"))
        (out / "evidence.host.json").write_text(
            json.dumps(host, indent=1, default=str) + "\n", encoding="utf-8"
        )
        inner = hout / "evidence.json"
        evidence = json.loads(inner.read_text(encoding="utf-8")) if inner.exists() else None
        record["in_container_evidence"] = inner.exists()
        if evidence is not None:
            shutil.copy2(inner, out / "evidence.json")
            record["host_reanalysis_equal"] = json.loads(json.dumps(host, default=str)) == evidence
        else:
            evidence = host
        if (hout / "projection.json").exists():
            shutil.copy2(hout / "projection.json", out / "projection.json")
            record["projection_status"] = json.loads(
                (hout / "projection.json").read_text(encoding="utf-8")
            ).get("status")
        for name in ("score.json", "runner_record.json", "stage.stdout", "run_dir_manifest.txt"):
            if (hout / name).exists():
                shutil.copy2(hout / name, out / name)
        if (hout / "score.json").exists():
            record["score_run_status"] = json.loads(
                (hout / "score.json").read_text(encoding="utf-8")
            ).get("run_status")
        record["tracer_status"] = evidence["harness_observations"]["tracer_status"]["status"]
        record["missing"] = evidence["missing"]
    else:
        record["tracer_status"] = "NO_HARNESS_OUTPUT"
    (out / "launch.json").write_text(
        json.dumps(record, indent=1, default=str) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {
                k: record.get(k)
                for k in (
                    "mode",
                    "entry_args",
                    "container_exit_code",
                    "container_wall_seconds",
                    "tracer_status",
                    "host_reanalysis_equal",
                    "projection_status",
                    "score_run_status",
                    "run_conditions",
                    "missing",
                )
            },
            indent=1,
        )
    )
    return 0


def admission(args):
    cmd = [
        "docker",
        "run",
        "--rm",
        *RUN_ARGS,
        "--user",
        "1000:1000",
        "-w",
        "/opt/ctl",
        "-e",
        "PYTHONDONTWRITEBYTECODE=1",
        "-e",
        "HOME=/home/runner",
        "--entrypoint",
        "python",
        args.image,
        "-B",
        "-m",
        "container_harness.case_driver",
        "admission",
        "--selector",
        args.selector,
    ]
    if args.smoke_ids:
        cmd += ["--smoke-ids", args.smoke_ids]
    assert not any(tok in FORBIDDEN or tok.startswith("--privileged") for tok in cmd), cmd
    proc = subprocess.run(cmd, capture_output=True, text=True)
    sys.stderr.write(proc.stderr)
    if proc.returncode != 0:
        raise SystemExit(f"admission container failed rc={proc.returncode}")
    result = json.loads(proc.stdout)
    result["docker_run"] = cmd
    Path(args.out).write_text(json.dumps(result, indent=1) + "\n", encoding="utf-8")
    print(json.dumps({"counts": result["counts"], "selected": result["selected"]}, indent=1))
    return 0


def relay_smoke(args):
    """One synthetic role call container -> relay -> Kong, plus in-container egress probes.

    The exited container is LEFT in place (name in DIR/container.txt) so the host key scan can
    ``docker export`` it; the workflow removes it afterwards.
    """
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    client = getattr(args, "client", "relay_smoke")
    if client not in SMOKE_CLIENTS:
        raise SystemExit(f"unknown smoke client {client!r}")
    stem = client.replace("_", "-")  # relay-smoke | relay-dedup-smoke
    name = f"simkit-{stem}-{uuid.uuid4().hex[:8]}"
    run_args, env, net = relay_settings(args.relay_dir, probe_targets=True)
    cmd = [
        "docker",
        "create",
        "--name",
        name,
        *run_args,
        "--user",
        "1000:1000",
        "-w",
        "/opt/ctl",
        "-e",
        "PYTHONDONTWRITEBYTECODE=1",
        "-e",
        "HOME=/home/runner",
        "--entrypoint",
        "python",
        args.image,
        "-B",
        f"/opt/ctl/container_harness/{client}.py",
    ]
    check_cmd(cmd)
    sh(cmd, capture_output=True, env=env)
    (out / "container.txt").write_text(name + "\n", encoding="utf-8")
    observed, checks = conditions_from_inspect(name, net)
    record = {
        "schema": "simkit.relay-smoke-launch/1",
        "client": client,
        "image": args.image,
        "container": name,
        "docker_create": cmd,
        "env_names_passed": [cmd[i + 1].split("=", 1)[0] for i, t in enumerate(cmd) if t == "-e"],
        "run_conditions_observed": observed,
        "run_conditions": checks,
        "relay_network": net["network"],
    }
    t0 = time.monotonic()
    try:
        proc = subprocess.run(
            ["docker", "start", "-a", name], capture_output=True, text=True, timeout=args.timeout
        )
        record["docker_start_rc"] = proc.returncode
        (out / "container.stderr").write_text(proc.stderr, encoding="utf-8")
        stdout = proc.stdout
    except subprocess.TimeoutExpired:
        record["docker_start_rc"] = "timeout"
        subprocess.run(["docker", "kill", name], capture_output=True)
        stdout = ""
    record["container_wall_seconds"] = round(time.monotonic() - t0, 3)
    state = json.loads(
        sh(["docker", "inspect", "-f", "{{json .State}}", name], capture_output=True).stdout
    )
    record["container_exit_code"] = state.get("ExitCode")
    try:
        smoke = json.loads(stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        smoke = None
        (out / "container.stdout.unparsed").write_text(stdout[-4000:], encoding="utf-8")
    record["smoke_parsed"] = smoke is not None
    if smoke is not None:
        (out / f"{stem}.container.json").write_text(
            json.dumps(smoke, indent=1) + "\n", encoding="utf-8"
        )
    (out / "launch.json").write_text(json.dumps(record, indent=1) + "\n", encoding="utf-8")
    print(json.dumps({k: record[k] for k in ("container", "docker_start_rc", "run_conditions")}))
    return 0


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("--tag", required=True)
    b.add_argument("--context")
    b.add_argument("--record")
    r = sub.add_parser("run")
    r.add_argument("mode", choices=["canary", "tree", "kill-tracer", "case"])
    r.add_argument("--case-id")
    r.add_argument("--image", required=True)
    r.add_argument("--out", required=True)
    r.add_argument("--no-trace", action="store_true")
    r.add_argument("--kill-tracer-after", type=float)
    r.add_argument("--timeout", type=int, default=1500)
    r.add_argument("--relay-dir", help="relay_ci.sh DIR: join the relay-only network")
    r.add_argument(
        "--keep-container",
        action="store_true",
        help="leave the exited container (name in OUT/container.txt) for the host key scan's "
        "docker export; the caller removes it. Default: removed after docker cp",
    )
    a = sub.add_parser("admission")
    a.add_argument("--image", required=True)
    a.add_argument("--out", required=True)
    a.add_argument("--selector", default="admission")
    a.add_argument("--smoke-ids")
    s = sub.add_parser("relay-smoke")
    s.add_argument("--image", required=True)
    s.add_argument("--out", required=True)
    s.add_argument("--relay-dir", required=True)
    s.add_argument("--timeout", type=int, default=600)
    s.add_argument(
        "--client",
        choices=SMOKE_CLIENTS,
        default="relay_smoke",
        help="in-container client: relay_smoke (one tiny verifier call) or relay_dedup_smoke "
        "(one reviewer-sized dedup-sha256/1 call whose binding sits only in a shared block); "
        "writes "
        "OUT/<client, dashed>.container.json",
    )
    args = ap.parse_args(argv[1:])
    commands = {"build": build, "run": run, "admission": admission, "relay-smoke": relay_smoke}
    return commands[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
