#!/usr/bin/env bash
#
# setup-swarm-ipv6-egress.sh
#
# Prepare a Docker Swarm node for selective IPv6 Egress via Tundra-NAT64.
#
# In Swarm mode, docker stack deploy cannot automatically create a dual-stack
# docker_gwbridge, and compose2swarm strips the `devices:` directive (so
# /dev/net/tun is not bind-mounted into the container). This script performs
# the host-side preparation that Compose mode does automatically:
#
#   * ensure net.ipv6.conf.all.forwarding=1
#   * ensure /dev/net/tun exists with correct permissions
#   * ensure docker_gwbridge exists with IPv6 enabled (dual-stack)
#
# Exit codes: 0 = node is ready (or successfully prepared), 1 = not ready /
# preparation failed, 2 = usage error.

set -euo pipefail

SWARM_GWBRIDGE_IPV4_SUBNET="${SWARM_GWBRIDGE_IPV4_SUBNET:-172.18.0.0/16}"
SWARM_GWBRIDGE_IPV6_SUBNET="${SWARM_GWBRIDGE_IPV6_SUBNET:-fd64:ff9c::/112}"
SWARM_STACK_NAME="${SWARM_STACK_NAME:-}"
RECREATE=0
SYSCTL_CONF="/etc/sysctl.d/99-antizapret-ipv6-egress.conf"

usage() {
    cat <<EOF
Usage: $0 [--check] [--apply] [--recreate] [--stack <name>] [--help]

Modes:
  --check     (default) Read-only diagnostics. Returns 0 if the current
              Swarm node is ready for IPv6 Egress via Tundra-NAT64,
              otherwise 1 with a description of what is missing.
  --apply     Idempotently prepare the node for Swarm IPv6 Egress:
              enable IPv6 forwarding, ensure /dev/net/tun, and create or
              reconcile docker_gwbridge as dual-stack.
  --recreate  With --apply: if docker_gwbridge exists without IPv6 and has
              no active container endpoints, delete and recreate it as
              dual-stack. Without --recreate, an existing single-stack
              docker_gwbridge with active containers is left untouched
              and the script exits with 1.
  --stack     Swarm stack name prefix for service name matching
              (e.g. antizapret). Matches service names like
              <stack>_az-local, *_az-local, or az-local.
  --help      Show this help.

Environment:
  SWARM_GWBRIDGE_IPV4_SUBNET  IPv4 subnet for docker_gwbridge
                              (default: 172.18.0.0/16)
  SWARM_GWBRIDGE_IPV6_SUBNET  IPv6 subnet for docker_gwbridge
                              (default: fd64:ff9c::/112)
  SWARM_STACK_NAME            Swarm stack name prefix (default: empty, matches any)

Examples:
  $0
  $0 --check
  $0 --apply
  $0 --swarm --stack antizapret --check
  SWARM_GWBRIDGE_IPV4_SUBNET=10.0.0.0/16 SWARM_GWBRIDGE_IPV6_SUBNET=fd00:1::/112 $0 --apply --recreate
EOF
}

err() { echo "setup-swarm-ipv6-egress: $*" >&2; }
warn() { echo "setup-swarm-ipv6-egress: WARNING: $*" >&2; }

MODE="check"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --check) MODE="check"; shift ;;
        --apply) MODE="apply"; shift ;;
        --recreate) RECREATE=1; shift ;;
        --stack) SWARM_STACK_NAME="$2"; shift 2 ;;
        --help|-h) usage; exit 0 ;;
        *) err "unknown argument: $1"; usage; exit 2 ;;
    esac
done

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

have() { command -v "$1" >/dev/null 2>&1; }

swarm_node_state() {
    docker info --format '{{.Swarm.LocalNodeState}}' 2>/dev/null || true
}

is_swarm_node_active() {
    [[ "$(swarm_node_state)" == "active" ]]
}

docker_gwbridge_exists() {
    docker network inspect docker_gwbridge >/dev/null 2>&1
}

docker_gwbridge_has_ipv6() {
    local enable_ipv6
    enable_ipv6="$(docker network inspect --format '{{.EnableIPv6}}' docker_gwbridge 2>/dev/null || echo 'false')"
    [[ "$enable_ipv6" == "true" ]]
}

docker_gwbridge_active_endpoints() {
    local count
    count="$(docker network inspect --format '{{len .Containers}}' docker_gwbridge 2>/dev/null || echo '0')"
    echo "$count"
}

# Find Swarm task containers for a given service name.
# Uses `docker ps --filter "label=com.docker.swarm.service.name"` to get all
# containers with that label, then filters in Bash:
#   - if SWARM_STACK_NAME is set: exact match ${SWARM_STACK_NAME}_${svc}
#   - otherwise: match *_${svc} or ${svc}
find_swarm_containers() {
    local svc="$1"
    local container_ids=""
    local all_ids
    all_ids="$(docker ps --filter "label=com.docker.swarm.service.name" --format '{{.ID}} {{.Label "com.docker.swarm.service.name"}}' 2>/dev/null || true)"
    if [[ -z "$all_ids" ]]; then
        echo ""
        return
    fi
    local line cid cname
    while read -r cid cname; do
        [[ -z "$cid" || -z "$cname" ]] && continue
        if [[ -n "$SWARM_STACK_NAME" ]]; then
            [[ "$cname" == "${SWARM_STACK_NAME}_${svc}" ]] || continue
        else
            [[ "$cname" == *"_${svc}" || "$cname" == "$svc" ]] || continue
        fi
        container_ids+="$cid "
    done <<< "$all_ids"
    echo "${container_ids% }"
}

# Check if any existing Docker network has an IPv6 subnet that overlaps with
# the given IPv6 subnet.
# Returns 0 if overlap detected, 1 if no overlap, 2 if the check itself
# failed (e.g. python3 missing or Docker commands failed) — callers must
# treat 2 as fail-closed and NOT proceed with docker_gwbridge changes.
# Uses python3 + ipaddress.IPv6Network.overlaps() for proper CIDR overlap
# detection across all IPAM.Config entries of every network.
docker_gwbridge_ipv6_overlaps() {
    local target_subnet="$1"
    if ! have python3; then
        err "python3 is required to validate IPv6 CIDR overlap"
        return 2
    fi
    local net_ids json
    net_ids="$(docker network ls -q 2>/dev/null || true)"
    [[ -n "$net_ids" ]] || return 2
    json="$(docker network inspect $net_ids 2>/dev/null || true)"
    [[ -n "$json" ]] || return 2
    printf '%s' "$json" | python3 -c '
import sys, json, ipaddress
try:
    target = ipaddress.IPv6Network(sys.argv[1], strict=False)
    data = json.load(sys.stdin)
    for net in data:
        if net.get("Name") == "docker_gwbridge":
            continue
        for cfg in (net.get("IPAM") or {}).get("Config", []) or []:
            subnet = cfg.get("Subnet")
            if not subnet or ":" not in subnet:
                continue
            try:
                if target.overlaps(ipaddress.IPv6Network(subnet, strict=False)):
                    sys.exit(0)
            except Exception:
                continue
    sys.exit(1)
except Exception:
    sys.exit(2)
' "$target_subnet"
}

# ---------------------------------------------------------------------------
# Read-only check mode
# ---------------------------------------------------------------------------

check_node() {
    local failures=0

    echo "=== Swarm IPv6 Egress readiness check ==="

    local swarm_state
    swarm_state="$(swarm_node_state)"
    if [[ "$swarm_state" != "active" ]]; then
        if [[ -z "$swarm_state" ]]; then
            err "Not a Swarm node (or Docker not running). Initialize Swarm first: docker swarm init"
            return 1
        fi
        warn "Swarm node is not active (inactive, state=$swarm_state) — continuing host readiness check anyway"
    else
        echo "OK: Swarm node active"
    fi

    if ! have sysctl; then
        err "sysctl not available"
        failures=$((failures + 1))
    else
        local fwd
        fwd="$(sysctl -n net.ipv6.conf.all.forwarding 2>/dev/null || echo '0')"
        if [[ "$fwd" == "1" ]]; then
            echo "OK: net.ipv6.conf.all.forwarding=1"
        else
            err "net.ipv6.conf.all.forwarding is not enabled (got: $fwd)"
            failures=$((failures + 1))
        fi
    fi

    if [[ -c /dev/net/tun ]]; then
        echo "OK: /dev/net/tun exists (character device)"
    else
        err "/dev/net/tun does not exist (TUNDRA_INTERFACE=tundra requires TUN)"
        failures=$((failures + 1))
    fi

    if docker_gwbridge_exists; then
        if docker_gwbridge_has_ipv6; then
            echo "OK: docker_gwbridge exists with IPv6 enabled"
        else
            err "docker_gwbridge exists but IPv6 is not enabled"
            failures=$((failures + 1))
        fi
    else
        err "docker_gwbridge does not exist (needed for Swarm IPv6 Egress)"
        failures=$((failures + 1))
    fi

    # Check TUN device access inside running Swarm task containers for az-local/az-world
    if is_swarm_node_active; then
        echo "--- TUN device access inside Swarm task containers ---"
        for svc in az-local az-world; do
            local cids
            cids="$(find_swarm_containers "$svc")"
            if [[ -z "$cids" ]]; then
                warn "No running Swarm task container for service '$svc'; skipping TUN check inside container"
                continue
            fi
            for cid in $cids; do
                local tundra_socket
                tundra_socket="$(docker exec "$cid" sh -c 'echo "$TUNDRA_SOCKET"' 2>/dev/null || true)"
                if [[ -z "$tundra_socket" ]]; then
                    continue
                fi
                if ! docker exec "$cid" test -c /dev/net/tun 2>/dev/null; then
                    err "/dev/net/tun not accessible inside $svc container ($cid)"
                    failures=$((failures + 1))
                else
                    echo "OK: /dev/net/tun accessible inside $svc container ($cid)"
                fi
                if ! docker exec "$cid" ip link show tundra 2>/dev/null | grep -q tundra; then
                    err "tundra TUN interface not present inside $svc container ($cid)"
                    failures=$((failures + 1))
                else
                    echo "OK: tundra TUN interface present inside $svc container ($cid)"
                fi
                if docker exec "$cid" test -f /run/tundra/disabled 2>/dev/null; then
                    err "/run/tundra/disabled present inside $svc container ($cid) — Tundra disabled"
                    failures=$((failures + 1))
                fi
            done
        done
    fi

    # Check if sysctl persistence file exists
    if [[ -f "$SYSCTL_CONF" ]]; then
        echo "OK: $SYSCTL_CONF exists"
    else
        warn "$SYSCTL_CONF not found (IPv6 forwarding will not persist across reboots)"
    fi

    echo
    if [[ $failures -eq 0 ]]; then
        echo "Node is ready for IPv6 Egress via Tundra-NAT64 in Swarm mode."
        return 0
    else
        echo "Node is NOT ready: ${failures} issue(s) found."
        return 1
    fi
}

# ---------------------------------------------------------------------------
# Apply mode
# ---------------------------------------------------------------------------

apply_node() {
    local failures=0

    echo "=== Swarm IPv6 Egress node preparation ==="

    # docker_gwbridge and host-level setup can be done before joining Swarm.
    # Swarm membership is only required for checking TUN access inside
    # running task containers (done in check_node()).
    if ! is_swarm_node_active; then
        warn "Not a Swarm node — proceeding with host-level preparation only."
        warn "TUN access inside task containers cannot be verified until Swarm is active."
    else
        echo "OK: Swarm node active"
    fi

    # 1. Enable IPv6 forwarding
    echo "--- Enabling IPv6 forwarding ---"
    if sysctl -w net.ipv6.conf.all.forwarding=1 >/dev/null 2>&1; then
        echo "OK: net.ipv6.conf.all.forwarding=1"
    else
        warn "Could not set net.ipv6.conf.all.forwarding=1 (may require root / privileged container)"
        failures=$((failures + 1))
    fi
    if sysctl -w net.ipv6.conf.default.forwarding=1 >/dev/null 2>&1; then
        echo "OK: net.ipv6.conf.default.forwarding=1"
    else
        warn "Could not set net.ipv6.conf.default.forwarding=1 (may require root / privileged container)"
        failures=$((failures + 1))
    fi
    # Persist IPv6 forwarding settings across reboots
    if ! echo "net.ipv6.conf.all.forwarding=1
net.ipv6.conf.default.forwarding=1" > "$SYSCTL_CONF" 2>/dev/null; then
        warn "Could not write $SYSCTL_CONF (may require root / privileged container)"
        failures=$((failures + 1))
    else
        echo "OK: IPv6 forwarding settings saved to $SYSCTL_CONF"
    fi
    sysctl -p "$SYSCTL_CONF" >/dev/null 2>&1 || true

    # 2. Ensure /dev/net/tun
    echo "--- Ensuring /dev/net/tun ---"
    if [[ -c /dev/net/tun ]]; then
        echo "OK: /dev/net/tun already exists"
    else
        if mkdir -p /dev/net && mknod /dev/net/tun c 10 200 2>/dev/null; then
            chmod 0666 /dev/net/tun 2>/dev/null || true
            echo "OK: /dev/net/tun created"
        else
            warn "Could not create /dev/net/tun (may require --privileged or --device /dev/net/tun on the host)"
            failures=$((failures + 1))
        fi
    fi

    # 3. Ensure docker_gwbridge with IPv6
    echo "--- Ensuring docker_gwbridge with IPv6 ---"
    if docker_gwbridge_exists; then
        if docker_gwbridge_has_ipv6; then
            echo "OK: docker_gwbridge already has IPv6 enabled"
        else
            local active
            active="$(docker_gwbridge_active_endpoints)"
            if [[ "$active" -gt 0 ]]; then
                err "docker_gwbridge exists without IPv6 and has $active active container endpoint(s)"
                if [[ "$RECREATE" -eq 1 ]]; then
                    warn "--recreate specified but active endpoints exist; refusing to destroy docker_gwbridge"
                fi
                err "To fix: leave Swarm (docker swarm leave), then re-run this script with --apply --recreate, then rejoin Swarm."
                failures=$((failures + 1))
            else
                echo "docker_gwbridge exists without IPv6 and has no active endpoints; recreating as dual-stack"
                local ipv4_subnet ipv6_subnet
                ipv4_subnet="${SWARM_GWBRIDGE_IPV4_SUBNET}"
                ipv6_subnet="${SWARM_GWBRIDGE_IPV6_SUBNET}"
                # Preserve existing IPv4 subnet if possible
                local existing_subnet
                existing_subnet="$(docker network inspect --format '{{(index .IPAM.Config 0).Subnet}}' docker_gwbridge 2>/dev/null || echo '')"
                if [[ -n "$existing_subnet" && "$existing_subnet" != "<no value>" ]]; then
                    ipv4_subnet="$existing_subnet"
                    echo "Preserving existing IPv4 subnet: $ipv4_subnet"
                fi
                # Check IPAM overlap before removing docker_gwbridge.
                # Return code 2 means the check itself failed (e.g. no
                # python3); treat as fail-closed and do not modify
                # docker_gwbridge.
                overlap_rc=0
                docker_gwbridge_ipv6_overlaps "$ipv6_subnet" || overlap_rc=$?
                case $overlap_rc in
                    0)
                        err "IPv6 subnet $ipv6_subnet overlaps with an existing Docker network"
                        failures=$((failures + 1))
                        ;;
                    2)
                        err "Could not validate IPv6 CIDR overlap for $ipv6_subnet; refusing to modify docker_gwbridge"
                        failures=$((failures + 1))
                        ;;
                    1)
                        if docker network rm docker_gwbridge >/dev/null 2>&1; then
                            if docker network create \
                                --subnet "$ipv4_subnet" \
                                --ipv6 \
                                --subnet "$ipv6_subnet" \
                                -o com.docker.network.bridge.name=docker_gwbridge \
                                -o com.docker.network.bridge.enable_icc=false \
                                -o com.docker.network.bridge.enable_ip_masquerade=true \
                                docker_gwbridge >/dev/null 2>&1; then
                                echo "OK: docker_gwbridge recreated as dual-stack ($ipv4_subnet / $ipv6_subnet)"
                            else
                                err "Failed to recreate docker_gwbridge with IPv6"
                                # Try to restore the old docker_gwbridge without IPv6
                                if docker network create \
                                    --subnet "$ipv4_subnet" \
                                    -o com.docker.network.bridge.name=docker_gwbridge \
                                    docker_gwbridge >/dev/null 2>&1; then
                                    warn "Restored docker_gwbridge as IPv4-only ($ipv4_subnet)"
                                else
                                    err "Failed to restore docker_gwbridge"
                                    failures=$((failures + 1))
                                fi
                                failures=$((failures + 1))
                            fi
                        else
                            err "Failed to remove existing docker_gwbridge"
                            failures=$((failures + 1))
                        fi
                        ;;
                esac
            fi
        fi
    else
        echo "docker_gwbridge does not exist; creating as dual-stack"
        # Check IPAM overlap before creating.
        # Return code 2 means the check itself failed (e.g. no python3);
        # treat as fail-closed and do not create docker_gwbridge.
        overlap_rc=0
        docker_gwbridge_ipv6_overlaps "$SWARM_GWBRIDGE_IPV6_SUBNET" || overlap_rc=$?
        case $overlap_rc in
            0)
                err "IPv6 subnet $SWARM_GWBRIDGE_IPV6_SUBNET overlaps with an existing Docker network"
                failures=$((failures + 1))
                ;;
            2)
                err "Could not validate IPv6 CIDR overlap for $SWARM_GWBRIDGE_IPV6_SUBNET; refusing to create docker_gwbridge"
                failures=$((failures + 1))
                ;;
            1)
                if docker network create \
                    --subnet "$SWARM_GWBRIDGE_IPV4_SUBNET" \
                    --ipv6 \
                    --subnet "$SWARM_GWBRIDGE_IPV6_SUBNET" \
                    -o com.docker.network.bridge.name=docker_gwbridge \
                    -o com.docker.network.bridge.enable_icc=false \
                    -o com.docker.network.bridge.enable_ip_masquerade=true \
                    docker_gwbridge >/dev/null 2>&1; then
                    echo "OK: docker_gwbridge created as dual-stack ($SWARM_GWBRIDGE_IPV4_SUBNET / $SWARM_GWBRIDGE_IPV6_SUBNET)"
                else
                    err "Failed to create docker_gwbridge with IPv6"
                    failures=$((failures + 1))
                fi
                ;;
        esac
    fi

    echo
    if [[ $failures -eq 0 ]]; then
        echo "Node is prepared for IPv6 Egress via Tundra-NAT64 in Swarm mode."
        return 0
    else
        echo "Node preparation failed: ${failures} issue(s) found."
        return 1
    fi
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if [[ "$MODE" == "check" ]]; then
    check_node
else
    apply_node
fi
