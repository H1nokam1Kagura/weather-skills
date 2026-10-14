#!/bin/sh
# Container entrypoint (runs as root, PID != 1: the launcher passes --init, so docker-init is PID 1).
#
#   entry.sh MODE [CASE_ID] [--no-trace] [--kill-tracer-after SECONDS]
#     canary       stage the synthetic fixture (pinned generator), then run the real controller
#                  (fixture roles) as uid 1000 under a root strace -ff
#     tree         the probe's synthetic owned tree (regression scenarios) + untraced decoy
#     kill-tracer  a tree that tries to SIGKILL its tracer (must be EPERM) and keeps writing
#     case ID      one frozen v2.7 case: pinned-generator fixture staged untraced, then the frozen
#                  runner -> WeatherAdapter.run_case (real controller) traced; the runner record
#                  comes out on fd 3 (opened here onto $OUT, never a traced path); post-trace the
#                  analyzer, the adapter projection and the pinned frozen scorer run in-container
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
CASE_ID=""
if [ "$MODE" = case ]; then
  CASE_ID="${1:-}"
  [ $# -gt 0 ] && shift
  case "$CASE_ID" in
    ""|*[!A-Za-z0-9_.-]*) echo "case mode needs a plain case id" >&2; exit 2 ;;
  esac
fi
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
echo "{\"mode\": \"$MODE\", \"case_id\": \"$CASE_ID\", \"trace\": $TRACE, \"kill_tracer_after\": \"$KILL_AFTER\", \"core_rlimit\": \"$(ulimit -c)\"}" > "$OUT/entry.json"

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
  case)
    status case_id "$CASE_ID"
    if ! (cd /opt/ctl && $AS_RUNNER python -m container_harness.case_driver stage "$CASE_ID" /work \
          > "$OUT/stage.stdout" 2> "$OUT/stage.stderr"); then
      status abort staging_failed
      exit 4
    fi
    ROOT_CWD=/opt/ctl
    status run_dir /work/case/run
    set -- python -m container_harness.case_driver run "$CASE_ID" /work
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
# The relay endpoint (host:port only, never the token) lets the analyzer tell the controller's
# role transport from a data fetch; absent for --network none containers.
if [ -n "${SIMKIT_RELAY_URL:-}" ]; then
  status relay_endpoint "$(printf '%s' "$SIMKIT_RELAY_URL" | sed -E 's#^[A-Za-z]+://##; s#/.*$##')"
fi

# Harness RAM sampler (root, outside the traced tree; harness_observations
# .min_free_ram_while_running_gib). Started and given its first reading before run_start so the
# run window is covered from its first instant; stopped after run_end.
python -I -B /opt/ctl/container_harness/ram_sampler.py "$OUT/ram_samples.txt" 0.2 \
  > "$OUT/ram_sampler.stdout" 2>&1 &
RPID=$!
status ram_sampler_pid "$RPID"
status ram_sampler_pgid "$(cut -d' ' -f5 /proc/$RPID/stat 2>/dev/null)"
i=0
while [ $i -lt 200 ]; do
  n="$(grep -c -v '^#' "$OUT/ram_samples.txt" 2>/dev/null)"
  [ "${n:-0}" -ge 1 ] && break
  sleep 0.05; i=$((i+1))
done
status run_start "$(date +%s.%N)"

# 4. the traced run. strace stays root; the tree runs as uid 1000 (-u). Default seccomp applies.
#    -q drops attach/detach chatter only; exit lines (+++) stay in the trace.
#    socket/connect/sendto/sendmsg/sendmmsg: data_fetch_attempts (trace_network=1 says so).
#    fd 3 = runner record, fd 4 = run_case's projection inputs (case mode): both opened here onto
#    $OUT, so writing them creates no path in the traced tree.
cd "$ROOT_CWD"
if [ "$TRACE" = 1 ]; then
  status trace_network 1
  strace -q -ff -A -o "$OUT/trace/t" --timestamps=unix,ns -y -s 4096 -u runner \
    -e trace=%file,%process,%signal,setsid,setpgid,bind,fcntl,prctl,pidfd_open,pidfd_send_signal,io_uring_setup,io_uring_enter,io_uring_register,chdir,fchdir,memfd_create,alarm,setitimer,timer_create,socket,connect,sendto,sendmsg,sendmmsg \
    -- "$@" > "$OUT/root.stdout" 2> "$OUT/root.stderr" 3> "$OUT/runner_record.json" \
    4> "$OUT/projection_inputs.json" &
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
  $AS_RUNNER "$@" > "$OUT/root.stdout" 2> "$OUT/root.stderr" 3> "$OUT/runner_record.json" \
    4> "$OUT/projection_inputs.json"
  RC=$?
  status untraced_exit "$RC"
fi
status run_end "$(date +%s.%N)"
kill -TERM "$RPID" 2>/dev/null
wait "$RPID"
status ram_sampler_exit "$?"
status complete 1

# 5. untraced, in-container post-processing (the traced tree has exited): analyzer, then the
#    adapter projection for the canary, both in the trace's own path namespace (frozen score.py
#    compares created_paths to artifact paths by exact string). Root, writes only to $OUT.
RUN_DIR="$(grep -E '^run_dir=' "$OUT/harness_status.txt" | tail -1 | cut -d= -f2-)"
cd /opt/ctl
python -B -I /opt/ctl/container_harness/analyze_trace.py "$OUT" --run-dir "$RUN_DIR" \
  --out "$OUT/evidence.json" > "$OUT/analyze.stdout" 2> "$OUT/analyze.stderr"
echo "in_container_analyze_exit=$?" >> "$OUT/post_status.txt"
if { [ "$MODE" = canary ] || [ "$MODE" = case ]; } && [ -s "$OUT/evidence.json" ]; then
  # case mode: run_case's in-process harness inputs (fd 4) are merged with the trace
  # observations; the canary has no run_case, so it projects from the trace alone.
  INPUTS_ARG=""
  [ "$MODE" = case ] && INPUTS_ARG="--inputs $OUT/projection_inputs.json"
  # shellcheck disable=SC2086  # INPUTS_ARG is two words or empty by construction
  PYTHONPATH=/opt/ctl python -B /opt/ctl/container_harness/project_in_container.py "$RUN_DIR" "$OUT/evidence.json" \
    "$OUT/projection.json" $INPUTS_ARG > "$OUT/projection.stdout" 2> "$OUT/projection.stderr"
  echo "in_container_projection_exit=$?" >> "$OUT/post_status.txt"
fi
if { [ "$MODE" = canary ] || [ "$MODE" = case ]; } && [ -d "$RUN_DIR" ]; then
  # Diagnostic copy of the controller's own records (state, dispatch, tool/process/role JSON,
  # reports): small regular files only, never artifacts/ or scratch/, so a refused gate can be
  # read after the run. Untraced and post-exit; it does not alter created_paths.
  mkdir -p "$OUT/run-copy"
  (cd "$RUN_DIR" && find . \( -path ./artifacts -o -path ./scratch \) -prune -o -type f \
     \( -name '*.json' -o -name '*.jsonl' -o -name '*.log' -o -name '*.txt' -o -name '*.html' \) \
     -size -2M -print0 | xargs -0 -r cp --parents -t "$OUT/run-copy")
  echo "run_copy_exit=$?" >> "$OUT/post_status.txt"
fi
if [ "$MODE" = case ]; then
  # Manifest of everything the case root holds after the run (the run dir itself is not copied
  # out: results leave the container only through /harness-out via docker cp).
  (cd /work && find case -type f -exec sha256sum {} + | sort -k2) > "$OUT/run_dir_manifest.txt" 2>/dev/null
  # Pinned frozen guarded scorer, same container and path namespace; runs even when evidence or
  # projection is absent so the case is recorded (HARNESS_ERROR / NOT_EXECUTED), never dropped.
  PYTHONPATH=/opt/ctl python -B -m container_harness.case_driver score "$CASE_ID" \
    "$OUT/runner_record.json" "$OUT/evidence.json" "$OUT/projection.json" "$OUT/score.json" \
    > "$OUT/score.stdout" 2> "$OUT/score.stderr"
  echo "in_container_score_exit=$?" >> "$OUT/post_status.txt"
fi
exit 0
