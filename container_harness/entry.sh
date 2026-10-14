#!/bin/sh
# Container entrypoint (runs as root, PID != 1: the launcher passes --init, so docker-init is PID 1).
#
#   entry.sh MODE [--no-trace] [--kill-tracer-after SECONDS]
#     canary       stage the synthetic fixture (pinned generator), then run the real controller
#                  (fixture roles) as uid 1000 under a root strace -ff
#     tree         the probe's synthetic owned tree (regression scenarios) + untraced decoy
#     kill-tracer  a tree that tries to SIGKILL its tracer (must be EPERM) and keeps writing
#   --no-trace                same run without the tracer (overhead baseline; evidence MISSING)
#   --kill-tracer-after S     the HARNESS kills strace after S seconds (tracer-death regression:
#                             every trace-derived field must come out MISSING, never [])
#
# Everything this shell writes lives in /harness-out (root-only, 0700): the traced tree runs as
# uid 1000 and can neither read nor alter the trace, nor signal the tracer (EPERM).
# Nothing here is ever reached by docker exec; results are taken with docker cp after exit.
set -u
MODE="${1:-}"
[ $# -gt 0 ] && shift
TRACE=1
KILL_AFTER=""
while [ $# -gt 0 ]; do
  case "$1" in
    --no-trace) TRACE=0 ;;
    --kill-tracer-after) KILL_AFTER="$2"; shift ;;
    *) echo "unknown option $1" >&2; exit 2 ;;
  esac
  shift
done
OUT=/harness-out
mkdir -p "$OUT/trace"
ulimit -c 0
echo "{\"mode\": \"$MODE\", \"trace\": $TRACE, \"kill_tracer_after\": \"$KILL_AFTER\", \"core_rlimit\": \"$(ulimit -c)\"}" > "$OUT/entry.json"

status() {  # status KEY VALUE -> harness_status.txt (KEY=VALUE lines, parsed by the analyzer)
  echo "$1=$2" >> "$OUT/harness_status.txt"
}
status mode "$MODE"
status entry_pid "$$"
status entry_pgid "$(cut -d' ' -f5 /proc/$$/stat)"
status init_pgid "$(cut -d' ' -f5 /proc/1/stat)"
status core_rlimit "$(ulimit -c)"
status ptrace_scope "$(cat /proc/sys/kernel/yama/ptrace_scope 2>/dev/null || echo absent)"
status seccomp "$(grep -E '^Seccomp:' /proc/self/status | tr -d ' \t' | cut -d: -f2)"
status cap_eff "$(grep -E '^CapEff:' /proc/self/status | tr -d ' \t' | cut -d: -f2)"

# 1. pinned inputs, again at run time
if ! python -I /opt/ctl/container_harness/verify_inputs.py /opt/inputs.json --out "$OUT/input_verification.json" > /dev/null; then
  status abort input_verification_failed
  exit 3
fi

AS_RUNNER="setpriv --reuid=1000 --regid=1000 --init-groups"
export HOME=/home/runner
# The controller copy is read-only and byte-compiled at build; never try to write bytecode.
export PYTHONDONTWRITEBYTECODE=1

# 2. untraced staging, as uid 1000
case "$MODE" in
  canary)
    if ! (cd /opt/ctl && $AS_RUNNER python /opt/ctl/container_harness/stage_case.py /work/case \
          > "$OUT/stage.stdout" 2> "$OUT/stage.stderr"); then
      status abort staging_failed
      exit 4
    fi
    ROOT_CWD=/opt/ctl
    status run_dir /work/case/run
    set -- python -m container_harness.traced_controller run \
      --request /work/case/request.json --source /work/case/source/source.zarr \
      --run-dir /work/case/run --dispatch-evidence
    ;;
  tree)
    mkdir -p /srv/outside /work/run && chown runner:runner /srv/outside /work/run
    $AS_RUNNER python /opt/ctl/container_harness/scenarios/decoy.py > "$OUT/decoy.stdout" 2>&1 &
    status decoy_launcher_pid "$!"
    i=0
    while [ ! -s /work/decoy.pid ] && [ $i -lt 200 ]; do sleep 0.05; i=$((i+1)); done
    status decoy_pid "$(cat /work/decoy.pid)"
    status decoy_pgid "$(cut -d' ' -f5 /proc/$(cat /work/decoy.pid)/stat)"
    ROOT_CWD=/work/run
    status run_dir /work/run
    set -- python /opt/ctl/container_harness/scenarios/tree.py
    ;;
  kill-tracer)
    mkdir -p /work/run && chown runner:runner /work/run
    ROOT_CWD=/work/run
    status run_dir /work/run
    set -- python /opt/ctl/container_harness/scenarios/kill_tracer.py
    ;;
  *)
    status abort "unknown_mode:$MODE"
    exit 2
    ;;
esac

# 3. pre-run baseline (existing paths and symlinks), taken just before the tracer starts
find / \( -path /proc -o -path /sys -o -path "$OUT" \) -prune -o -print > "$OUT/baseline.txt" 2>/dev/null
find / \( -path /proc -o -path /sys -o -path "$OUT" \) -prune -o -type l -printf '%p\t%l\n' \
  > "$OUT/baseline_links.tsv" 2>/dev/null
status root_cwd "$ROOT_CWD"
status run_start "$(date +%s.%N)"

# 4. the traced run. strace stays root; the tree runs as uid 1000 (-u). Default seccomp applies.
#    -q drops attach/detach chatter only; exit lines (+++) stay in the trace.
cd "$ROOT_CWD"
if [ "$TRACE" = 1 ]; then
  strace -q -ff -A -o "$OUT/trace/t" --timestamps=unix,ns -y -s 4096 -u runner \
    -e trace=%file,%process,%signal,setsid,setpgid,bind,fcntl,prctl,pidfd_open,pidfd_send_signal,io_uring_setup,io_uring_enter,io_uring_register,chdir,fchdir,memfd_create,alarm,setitimer,timer_create \
    -- "$@" > "$OUT/root.stdout" 2> "$OUT/root.stderr" &
  TPID=$!
  status tracer_pid "$TPID"
  status tracer_pgid "$(cut -d' ' -f5 /proc/$TPID/stat 2>/dev/null)"
  KPID=""
  if [ -n "$KILL_AFTER" ]; then
    (sleep "$KILL_AFTER"; kill -KILL "$TPID" 2>/dev/null && echo "harness_killed_tracer=1" >> "$OUT/harness_status.txt") &
    KPID=$!
  fi
  wait "$TPID"
  RC=$?
  [ -n "$KPID" ] && kill "$KPID" 2>/dev/null
  status tracer_exit "$RC"
else
  $AS_RUNNER "$@" > "$OUT/root.stdout" 2> "$OUT/root.stderr"
  RC=$?
  status untraced_exit "$RC"
fi
status run_end "$(date +%s.%N)"
status complete 1

# 5. untraced, in-container post-processing (the traced tree has exited): analyzer, then the
#    adapter projection for the canary, both in the trace's own path namespace (frozen score.py
#    compares created_paths to artifact paths by exact string). Root, writes only to $OUT.
RUN_DIR="$(grep -E '^run_dir=' "$OUT/harness_status.txt" | tail -1 | cut -d= -f2-)"
cd /opt/ctl
python -B -I /opt/ctl/container_harness/analyze_trace.py "$OUT" --run-dir "$RUN_DIR" \
  --out "$OUT/evidence.json" > "$OUT/analyze.stdout" 2> "$OUT/analyze.stderr"
echo "in_container_analyze_exit=$?" >> "$OUT/post_status.txt"
if [ "$MODE" = canary ] && [ -s "$OUT/evidence.json" ]; then
  PYTHONPATH=/opt/ctl python -B /opt/ctl/container_harness/project_in_container.py "$RUN_DIR" "$OUT/evidence.json" \
    "$OUT/projection.json" > "$OUT/projection.stdout" 2> "$OUT/projection.stderr"
  echo "in_container_projection_exit=$?" >> "$OUT/post_status.txt"
fi
exit 0
