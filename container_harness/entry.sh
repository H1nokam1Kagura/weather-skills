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
# >>> decoy-harness (B3a): FX-DECOY-SPAWN only; decoy_harness.py documents the process model
DECOY=0
DECOY_ENV=""
# <<< decoy-harness

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
    # >>> decoy-harness (B3a)
    # FX-DECOY-SPAWN (E12-P2 only). Untraced and before the baseline: the decoy runs the frozen
    # literal argv as uid 1000 in its own session (D7), a child of this shell; a root strace -p
    # records every signal it receives. Its pid reaches only the untraced prepare (DECOY_ENV).
    # Every other case: the need check writes nothing and nothing below runs.
    DECOY_NEEDS_OUT="$(cd /opt/ctl && $AS_RUNNER python -m container_harness.decoy_harness needs "$CASE_ID" 2>&1)"
    DECOY_NEED=$?
    if [ "$DECOY_NEED" != 1 ]; then
      printf '%s\n' "$DECOY_NEEDS_OUT" > "$OUT/decoy_needs.stdout"
      if [ "$DECOY_NEED" = 0 ]; then status decoy_needed 1; else status decoy_needed error; fi
    fi
    if [ "$DECOY_NEED" = 0 ]; then
      DECOY_ARGV="$(cd /opt/ctl && $AS_RUNNER python -m container_harness.decoy_harness spec "$CASE_ID" \
        2> "$OUT/decoy_spec.stderr")"
      DECOY_SPEC_RC=$?
      if [ "$DECOY_SPEC_RC" != 0 ] || [ -z "$DECOY_ARGV" ]; then
        status decoy_spawn "failed:spec_exit_$DECOY_SPEC_RC"
      else
        # The subshell is not a process-group leader (no job control), so setsid(1) calls
        # setsid() in place and execs: $! is the decoy's own pid.
        (
          set --
          while IFS= read -r a; do set -- "$@" "$a"; done <<DECOY_ARGV_EOF
$DECOY_ARGV
DECOY_ARGV_EOF
          cd /
          exec $AS_RUNNER setsid "$@"
        ) < /dev/null > "$OUT/decoy.stdout" 2>&1 &
        DPID=$!
        i=0
        while [ $i -lt 200 ]; do
          [ "$(tr '\0' '\n' < /proc/$DPID/cmdline 2>/dev/null)" = "$DECOY_ARGV" ] && break
          sleep 0.05; i=$((i+1))
        done
        DPGID="$(cut -d' ' -f5 /proc/$DPID/stat 2>/dev/null)"
        DSID="$(cut -d' ' -f6 /proc/$DPID/stat 2>/dev/null)"
        if [ $i -ge 200 ] || [ "$DPGID" != "$DPID" ] || [ "$DSID" != "$DPID" ]; then
          status decoy_spawn "failed:not_the_literal_argv_in_its_own_session"
          kill -KILL "$DPID" 2>/dev/null
          wait "$DPID" 2>/dev/null
        else
          status decoy_spawn ok
          status decoy_pid "$DPID"
          status decoy_pgid "$DPGID"
          status decoy_sid "$DSID"
          status decoy_uid "$(grep -E '^Uid:' /proc/$DPID/status | tr -s ' \t' ' ' | cut -d' ' -f2)"
          strace -p "$DPID" -e trace=none -e signal=all --timestamps=unix,ns -o "$OUT/decoy.trace" \
            < /dev/null > /dev/null 2> "$OUT/decoy_tracer.stderr" &
          DTPID=$!
          status decoy_tracer_pid "$DTPID"
          status decoy_tracer_pgid "$(cut -d' ' -f5 /proc/$DTPID/stat 2>/dev/null)"
          i=0
          while [ $i -lt 200 ]; do
            [ "$(grep -E '^TracerPid:' /proc/$DPID/status 2>/dev/null | tr -s ' \t' ' ' | cut -d' ' -f2)" = "$DTPID" ] && break
            sleep 0.05; i=$((i+1))
          done
          if [ $i -lt 200 ]; then status decoy_tracer_attached 1; else status decoy_tracer_attached 0; fi
          DECOY=1
          DECOY_ENV="env SIMKIT_DECOY_PID=$DPID"
        fi
      fi
    fi
    # <<< decoy-harness
    # The case root (source/, harness/manifest.json, empty run/) is staged here, untraced and
    # before the baseline, so the traced run creates nothing outside /work/case/run
    # (frozen v2.7: created_paths all_under HARNESS_RUN_DIR).
    if ! (cd /opt/ctl && $DECOY_ENV $AS_RUNNER python -m container_harness.case_driver prepare "$CASE_ID" /work \
          > "$OUT/prepare.stdout" 2> "$OUT/prepare.stderr"); then
      status abort prepare_failed
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
# Interrupt broker (traced case mode, only for a case whose injection_schedule carries
# harness_stop_controller; interrupt_broker.py documents the protocol): root and UNTRACED (a
# child of this shell, never of strace), it SIGKILLs a controller incarnation from outside the
# traced tree. Its FIFOs live in $OUT/interrupt-broker (made by the broker before the trace,
# never a traced path); they are opened here AFTER the RAM sampler started, so only strace and
# the traced tree inherit them: fd 5 = requests, fd 6 = acks (SIMKIT_INTERRUPT_BROKER).
BPID=""
if [ "$MODE" = case ] && [ "$TRACE" = 1 ]; then
  (cd /opt/ctl && $AS_RUNNER python -m container_harness.interrupt_broker needs "$CASE_ID" \
     > "$OUT/broker_needs.stdout" 2> "$OUT/broker_needs.stderr")
  NEED=$?
  if [ "$NEED" = 0 ]; then
    status interrupt_broker_needed 1
    python -I -B /opt/ctl/container_harness/interrupt_broker.py serve \
      --dir "$OUT/interrupt-broker" --record "$OUT/interrupt_broker.json" --entry-pid "$$" \
      --status-file "$OUT/harness_status.txt" \
      > "$OUT/interrupt_broker.stdout" 2> "$OUT/interrupt_broker.stderr" &
    BPID=$!
    status interrupt_broker_pid "$BPID"
    status interrupt_broker_pgid "$(cut -d' ' -f5 /proc/$BPID/stat 2>/dev/null)"
    i=0
    while [ ! -e "$OUT/interrupt-broker/ready" ] && [ $i -lt 200 ]; do sleep 0.05; i=$((i+1)); done
    if [ -e "$OUT/interrupt-broker/ready" ] && [ -p "$OUT/interrupt-broker/request.fifo" ] \
       && [ -p "$OUT/interrupt-broker/ack.fifo" ]; then
      exec 5<>"$OUT/interrupt-broker/request.fifo" 6<>"$OUT/interrupt-broker/ack.fifo"
      export SIMKIT_INTERRUPT_BROKER="request_fd=5,ack_fd=6"
      status interrupt_broker_ready 1
    else
      status interrupt_broker_ready 0
    fi
  elif [ "$NEED" = 1 ]; then
    status interrupt_broker_needed 0
  else
    status interrupt_broker_needed error
  fi
fi
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
# >>> decoy-harness (B3a)
# After run_end: the alive check (a dead child stays a zombie until reaped here, so the state
# letter, not kill -0, decides), the tracer check (alive AND still the decoy's tracer), then
# detach the tracer (SIGINT: strace detaches and exits) and only then kill the decoy, so the
# harness's own kill is never in the decoy's signal record.
if [ "$DECOY" = 1 ]; then
  DSTATE="$(cut -d' ' -f3 /proc/$DPID/stat 2>/dev/null)"
  status decoy_state_after_run "${DSTATE:-gone}"
  if [ -n "$DSTATE" ] && [ "$DSTATE" != Z ] && [ "$DSTATE" != X ]; then
    status decoy_alive_after_run 1
  else
    status decoy_alive_after_run 0
  fi
  DTSTATE="$(cut -d' ' -f3 /proc/$DTPID/stat 2>/dev/null)"
  DTRACER="$(grep -E '^TracerPid:' /proc/$DPID/status 2>/dev/null | tr -s ' \t' ' ' | cut -d' ' -f2)"
  if [ -n "$DTSTATE" ] && [ "$DTSTATE" != Z ] && [ "$DTRACER" = "$DTPID" ]; then
    status decoy_tracer_alive_at_run_end 1
  else
    status decoy_tracer_alive_at_run_end 0
  fi
  kill -INT "$DTPID" 2>/dev/null
  (sleep 10; kill -KILL "$DTPID" 2>/dev/null) &
  DTWPID=$!
  wait "$DTPID"
  status decoy_tracer_exit "$?"
  kill "$DTWPID" 2>/dev/null
  kill -KILL "$DPID" 2>/dev/null
  wait "$DPID"
  status decoy_kill_exit "$?"
fi
# <<< decoy-harness
kill -TERM "$RPID" 2>/dev/null
wait "$RPID"
status ram_sampler_exit "$?"
if [ -n "$BPID" ]; then
  # The traced tree has exited: drop the channel (post-processing never inherits it), then stop
  # the broker (it finishes a request in flight, writes its final record, exits 0). Bounded.
  if [ -n "${SIMKIT_INTERRUPT_BROKER:-}" ]; then
    exec 5>&- 6>&-
    unset SIMKIT_INTERRUPT_BROKER
  fi
  kill -TERM "$BPID" 2>/dev/null
  (sleep 30; kill -KILL "$BPID" 2>/dev/null) &
  BWPID=$!
  wait "$BPID"
  status interrupt_broker_exit "$?"
  kill "$BWPID" 2>/dev/null
fi
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
