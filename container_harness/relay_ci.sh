#!/usr/bin/env bash
# Host-side Kong relay plumbing for GitHub-hosted Linux runners (simkit-cases.yml).
#
# Owner rulings: container_route_ruling_2026-10-13 (live roles go container -> host relay; the
# Kong key never enters a container) and kong_secret_ruling_2026-10-13 (KONG_ANTHROPIC_KEY is a
# GitHub Actions secret used ONLY by the relay on the runner host).
#
#   relay_ci.sh net-up DIR       docker --internal network + firewall: the container can reach
#                                ONLY the relay port on the bridge gateway (no other host port,
#                                no forwarding, no IPv6); writes DIR/net.json + DIR/net-*.txt
#   relay_ci.sh relay-start DIR  starts relay/kong_relay_host.py --client runner bound to the
#                                bridge gateway only, spend caps from RELAY_MAX_REQUESTS /
#                                RELAY_MAX_TOTAL_TOKENS / RELAY_BUDGET (see below); needs
#                                KONG_ANTHROPIC_KEY in THIS step's env
#                                (it reaches the relay process tree and nothing else); the relay
#                                generates the per-run token (DIR/relay.token, 0600, masked)
#   relay_ci.sh teardown DIR     stops the relay, removes rules + network, deletes the token file
#
# Nothing here prints the key or the token. DIR must not be an uploaded directory for the
# token file: relay.token lives in DIR/secret/ which teardown deletes and uploads exclude.
set -euo pipefail

NET=simkit-relay-net
BR=simkit-relay0
SUBNET=172.30.99.0/24
GW=172.30.99.1
PORT=18765
CMD="${1:?usage: relay_ci.sh net-up|relay-start|teardown DIR}"
DIR="${2:?missing DIR}"
mkdir -p "$DIR" "$DIR/secret"
chmod 0700 "$DIR/secret"
REPO="$(cd "$(dirname "$0")/.." && pwd)"

rules() {  # rules -I|-D : the exact rule set, inserted (top of chain) or deleted
  local op="$1" fwd=DOCKER-USER
  sudo iptables -n -L DOCKER-USER >/dev/null 2>&1 || fwd=FORWARD
  if [ "$op" = -I ]; then
    sudo iptables -I "$fwd" 1 -i "$BR" -j DROP
    sudo iptables -I "$fwd" 1 -o "$BR" -j DROP
    sudo iptables -I INPUT 1 -i "$BR" -j DROP
    sudo iptables -I INPUT 1 -i "$BR" -d "$GW" -p tcp --dport "$PORT" -j ACCEPT
    sudo ip6tables -I INPUT 1 -i "$BR" -j DROP
    sudo ip6tables -I FORWARD 1 -i "$BR" -j DROP
  else
    sudo iptables -D "$fwd" -i "$BR" -j DROP || true
    sudo iptables -D "$fwd" -o "$BR" -j DROP || true
    sudo iptables -D INPUT -i "$BR" -d "$GW" -p tcp --dport "$PORT" -j ACCEPT || true
    sudo iptables -D INPUT -i "$BR" -j DROP || true
    sudo ip6tables -D INPUT -i "$BR" -j DROP || true
    sudo ip6tables -D FORWARD -i "$BR" -j DROP || true
  fi
  echo "$fwd"
}

case "$CMD" in
  net-up)
    docker network create --internal --driver bridge --subnet "$SUBNET" --gateway "$GW" \
      -o com.docker.network.bridge.name="$BR" "$NET" >/dev/null
    sudo ip link set dev "$BR" up
    gw_assigned=docker
    if ! ip -4 -o addr show dev "$BR" | grep -q " $GW/"; then
      # Some engine versions leave an --internal bridge without its gateway address; the relay
      # needs a host address on this bridge (and only on this bridge) to bind to.
      sudo ip addr add "$GW/24" dev "$BR"
      gw_assigned=relay_ci
    fi
    fwd_chain="$(rules -I)"
    docker version --format '{{json .}}' > "$DIR/net-docker-version.json" 2>/dev/null || true
    docker network inspect "$NET" > "$DIR/net-inspect.json"
    ip -o addr show dev "$BR" > "$DIR/net-bridge-addr.txt"
    sudo iptables -S > "$DIR/net-iptables.txt"
    sudo ip6tables -S > "$DIR/net-ip6tables.txt"
    internal="$(docker network inspect -f '{{.Internal}}' "$NET")"
    kong_ip="$(getent ahostsv4 ai-kong-gateway.bmgf.io | awk 'NR==1{print $1}')"
    host_ip="$(hostname -I | awk '{print $1}')"
    docker0="$(ip -4 -o addr show dev docker0 2>/dev/null | awk '{print $4}' | cut -d/ -f1)"
    python3 - "$DIR/net.json" <<EOF
import json, sys
json.dump({"network": "$NET", "bridge": "$BR", "subnet": "$SUBNET", "gateway": "$GW",
           "relay_port": $PORT, "internal": "$internal" == "true",
           "gateway_address_assigned_by": "$gw_assigned", "forward_chain": "$fwd_chain",
           "probe_targets": {"kong_ip_host_resolved": "$kong_ip" or None,
                             "runner_host_ip": "$host_ip" or None,
                             "docker0_gateway": "$docker0" or None}},
          open(sys.argv[1], "w"), indent=1)
EOF
    cat "$DIR/net.json"
    ;;
  relay-start)
    if [ -z "${KONG_ANTHROPIC_KEY:-}" ]; then
      echo "KONG_ANTHROPIC_KEY is not available to this step (secret unset or not scoped here)" >&2
      exit 1
    fi
    role_args=()
    for role in ${RELAY_ROLES:-verifier}; do role_args+=(--allow-role "$role"); done
    # Spend-guard caps (LIVE-WAVE-BRIEF "Spend guard"): the relay refuses BEFORE forwarding any
    # call that could exceed one (reason_code relay_spend_cap). The workflow sets them per job;
    # the fallbacks are the brief's per-case ceilings.
    #   RELAY_MAX_REQUESTS      calls that may reach the gateway               (default 24)
    #   RELAY_MAX_TOTAL_TOKENS  observed + worst-case-charged tokens           (default 1000000)
    #   RELAY_BUDGET            USD, Anthropic list-price estimate             (default 5)
    #   RELAY_MAX_TOKENS        per-call max_tokens; planner/repairer get 4096 unless
    #                           RELAY_ROLE_MAX_TOKENS overrides ("planner=4096 repairer=4096")
    role_tokens=()
    for item in ${RELAY_ROLE_MAX_TOKENS:-}; do role_tokens+=(--role-max-tokens "$item"); done
    nohup python -B "$REPO/relay/kong_relay_host.py" --client runner --ack-not-sensitive \
      "${role_args[@]}" --bind "$GW" --port "$PORT" \
      --max-requests "${RELAY_MAX_REQUESTS:-24}" \
      --max-total-tokens "${RELAY_MAX_TOTAL_TOKENS:-1000000}" \
      --budget "${RELAY_BUDGET:-5}" \
      --run-seconds "${RELAY_RUN_SECONDS:-1500}" --call-seconds 180 \
      --max-tokens "${RELAY_MAX_TOKENS:-256}" ${role_tokens[@]+"${role_tokens[@]}"} \
      --model claude-sonnet-4-6 \
      --audit-log "$DIR/relay-audit.jsonl" --token-file "$DIR/secret/relay.token" \
      --ready-file "$DIR/relay-ready.json" > "$DIR/relay.stdout" 2> "$DIR/relay.stderr" &
    echo $! > "$DIR/relay.pid"
    for _ in $(seq 1 60); do
      [ -s "$DIR/relay-ready.json" ] && break
      kill -0 "$(cat "$DIR/relay.pid")" 2>/dev/null || { echo "relay exited" >&2; cat "$DIR/relay.stderr" >&2; exit 1; }
      sleep 0.5
    done
    test -s "$DIR/relay-ready.json"
    echo "::add-mask::$(cat "$DIR/secret/relay.token")"
    cat "$DIR/relay-ready.json"; echo
    ;;
  teardown)
    if [ -s "$DIR/relay.pid" ]; then
      pid="$(cat "$DIR/relay.pid")"
      if kill -0 "$pid" 2>/dev/null; then
        kill -TERM "$pid" 2>/dev/null || true  # background jobs ignore SIGINT
        sleep 2
        kill -KILL "$pid" 2>/dev/null || true
        echo "relay_killed_at_teardown=1" >> "$DIR/teardown.txt"
      else
        echo "relay_already_stopped=1" >> "$DIR/teardown.txt"
      fi
    fi
    rm -rf "$DIR/secret"
    rules -D >/dev/null 2>&1 || true
    docker network rm "$NET" >/dev/null 2>&1 || true
    echo "teardown_complete=1" >> "$DIR/teardown.txt"
    ;;
  *)
    echo "unknown command $CMD" >&2
    exit 2
    ;;
esac
