#!/bin/sh
# Container entrypoint (runs as root, PID != 1: the launcher passes --init, so docker-init is PID 1).
#
#   entry.sh canary [--no-trace]   stage the synthetic fixture (pinned generator), then run the real
#                                  controller (fixture roles) as uid 1000 under a root strace -ff
#   entry.sh tree   [--no-trace]   the probe's synthetic owned tree (regression scenarios) + decoy
#
# Everything this shell writes lives in /harness-out (root-only, 0700): the traced tree runs as
# uid 1000 and can neither read nor alter the trace, nor signal the tracer (EPERM).
# Nothing here is ever reached by docker exec; results are taken with docker cp after exit.
set -u
MODE="${1:-}"
TRACE=1
[ "${2:-}" = "--no-trace" ] && TRACE=0
OUT=/harness-out
mkdir -p "$OUT/trace"
ulimit -c 0
echo "{\"mode\": \"$MODE\", \"trace\": $TRACE, \"core_rlimit\": \"$(ulimit -c)\"}" > "$OUT/entry.json"

status() {  # status KEY VALUE... -> harness_status.txt (KEY=VALUE lines, parsed by the launcher)
  echo "$1=$2" >> "$OUT/harness_status.txt"
}
status entry_pid "$$"
status entry_pgid "$(cut -d' ' -f5 /proc/$$/stat)"
status init_pgid "$(cut -d' ' -f5 /proc/1/stat)"

# 1. pinned inputs, again at run time
if ! python -I /opt/ctl/container_harness/verify_inputs.py /opt/inputs.json --out "$OUT/input_verification.json" > /dev/null; then
  status abort input_verification_failed
  exit 3
fi

AS_RUNNER="setpriv --reuid=1000 --regid=1000 --init-groups"
export HOME=/home/runner

# 2. untraced staging, as uid 1000
case "$MODE" in
  canary)
    if ! (cd /opt/ctl && $AS_RUNNER python /opt/ctl/container_harness/stage_case.py /work/case \
          > "$OUT/stage.stdout" 2> "$OUT/stage.stderr"); then
      status abort staging_failed
      exit 4
    fi
    ROOT_CWD=/opt/ctl
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
    set -- python /opt/ctl/container_harness/scenarios/tree.py
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
cd "$ROOT_CWD"
if [ "$TRACE" = 1 ]; then
  strace -ff -A -o "$OUT/trace/t" --timestamps=unix,ns -y -s 4096 -u runner \
    -e trace=%file,%process,%signal,setsid,setpgid,bind,fcntl,prctl,pidfd_open,pidfd_send_signal,io_uring_setup,io_uring_enter,io_uring_register,chdir,fchdir,memfd_create,alarm,setitimer,timer_create \
    -- "$@" > "$OUT/root.stdout" 2> "$OUT/root.stderr" &
  TPID=$!
  status tracer_pid "$TPID"
  status tracer_pgid "$(cut -d' ' -f5 /proc/$TPID/stat 2>/dev/null)"
  wait "$TPID"
  RC=$?
  status tracer_exit "$RC"
else
  $AS_RUNNER "$@" > "$OUT/root.stdout" 2> "$OUT/root.stderr"
  RC=$?
  status untraced_exit "$RC"
fi
status run_end "$(date +%s.%N)"
status complete 1
exit 0
