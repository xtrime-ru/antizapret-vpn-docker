#!/usr/bin/env bash
#
# diagnose-ipv6-egress.sh
#
# Read-only diagnostics for selective IPv6 Egress via Tundra-NAT64 in
# antizapret-vpn-docker.
#
# Reports, without changing anything:
#   * Docker Engine and Compose versions
#   * In Compose mode: existence and parameters of the Compose-managed IPv6
#     network (driver, EnableIPv6, IPv6 gateway/subnet), az-local container
#     state, IPv6 address/routes inside az-local, Tundra-NAT64 health.
#   * In Swarm mode (--swarm): docker_gwbridge dual-stack state, Swarm task
#     containers for az-local/az-world, IPv6 address/routes inside each
#     container, Tundra-NAT64 health inside each container.
#   * Host-side forwarding/firewall signs relevant to IPv6 egress
#   * Presence of IPv6 egress environment variables (TUNDRA_SOCKET, TUNDRA_INTERFACE)
#   * Tundra-NAT64 process status inside containers (pgrep, docktinel status)
#   * Unix socket and TUN interface presence inside containers
#   * Active SIIT bindings (iptables -S dnsmap | grep dnsmap:siit:v1)
#   * VIP /32 routes to Tundra TUN interface (ip route show dev tundra)
#   * NAT66 MASQUERADE and TCPMSS rules
#
# By default nothing is modified and no external host is contacted.
# Use --check-external for an explicit outbound IPv6 connectivity test.
# Use --swarm to diagnose a Docker Swarm deployment instead of Compose.
#
# Exit codes: 0 = diagnostics collected, 1 = Docker/Compose/Swarm unavailable or
#             required configuration missing, 2 = usage error.

set -u

COMPOSE_FILES="${COMPOSE_FILES:-docker-compose.yml:docker-compose.override.yml}"
COMPOSE_PROJECT_NAME="${COMPOSE_PROJECT_NAME:-}"
IPV6_NETWORK_KEY="ipv6-egress"
EXTERNAL_CHECK=0
SWARM_MODE=0
SWARM_STACK_NAME="${SWARM_STACK_NAME:-}"

usage() {
    cat <<EOF
Usage: $0 [--check-external] [--swarm] [--stack <name>] [--help]

Options:
  --check-external  Additionally test outbound IPv6 connectivity from az-local
                    (contacts an external IPv6 address). Opt-in only.
  --swarm           Diagnose a Docker Swarm deployment instead of Compose.
                    Checks docker_gwbridge dual-stack state and Swarm task
                    containers for az-local/az-world.
  --stack <name>    Swarm stack name prefix for service name matching
                    (e.g. antizapret). Matches service names like
                    <stack>_az-local, *_az-local, or az-local.
  --help            Show this help.

Environment:
  COMPOSE_FILES           Colon-separated Compose files (default: docker-compose.yml:docker-compose.override.yml)
  COMPOSE_PROJECT_NAME    Compose project name (default: detected from Compose config / directory)
  IPV6_NETWORK_KEY        Network key in Compose config (default: ipv6-egress)
  SWARM_STACK_NAME        Swarm stack name prefix (default: empty, matches any)

Examples:
  $0
  $0 --check-external
  $0 --swarm
  $0 --swarm --check-external
  COMPOSE_PROJECT_NAME=antizapret $0
EOF
}

err() { echo "diagnose-ipv6-egress: $*" >&2; }
warn() { echo "diagnose-ipv6-egress: WARNING: $*" >&2; }

while [[ $# -gt 0 ]]; do
    case "$1" in
        --check-external) EXTERNAL_CHECK=1; shift ;;
        --swarm) SWARM_MODE=1; shift ;;
        --stack) SWARM_STACK_NAME="$2"; shift 2 ;;
        --help|-h) usage; exit 0 ;;
        *) err "unknown argument: $1"; usage; exit 2 ;;
    esac
done

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

have() { command -v "$1" >/dev/null 2>&1; }

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

# Build a docker compose argument array from COMPOSE_FILES (colon-separated),
# skipping files that do not exist unless they were explicitly requested.
compose_files_args=()
IFS=':' read -ra _compose_files <<< "$COMPOSE_FILES"
for _cf in "${_compose_files[@]}"; do
    if [[ -f "$_cf" ]]; then
        compose_files_args+=("-f" "$_cf")
    fi
done
unset _cf _compose_files

compose() {
    "${COMPOSE_CMD[@]}" "${compose_files_args[@]}" "$@"
}

# Parse a value from `docker compose config` YAML using python3 if available,
# otherwise fall back to a naive grep.
config_value() {
    # $1 = dotted path, e.g. networks.ipv6-egress.enable_ipv6
    local path="$1"
    if have python3; then
        compose config 2>/dev/null | python3 -c '
import sys, yaml
try:
    data = yaml.safe_load(sys.stdin.read())
except Exception:
    sys.exit(2)
parts = sys.argv[1].split(".")
cur = data
for p in parts:
    if isinstance(cur, dict) and p in cur:
        cur = cur[p]
    else:
        sys.exit(3)
if cur is True:
    print("true")
elif cur is False:
    print("false")
elif cur is None:
    print("")
else:
    print(cur)
' "$path" 2>/dev/null || true
    else
        compose config 2>/dev/null | grep -A2 "$path" || true
    fi
}

section() { echo; echo "=== $* ==="; }

# ---------------------------------------------------------------------------
# Prerequisites
# ---------------------------------------------------------------------------
# Detect Compose command once (only needed for Compose mode).
if [[ "$SWARM_MODE" -eq 0 ]]; then
    if docker compose version >/dev/null 2>&1; then
        COMPOSE_CMD=(docker compose)
    elif docker-compose --version >/dev/null 2>&1; then
        COMPOSE_CMD=(docker-compose)
    else
        err "docker compose is not available"
        exit 1
    fi
fi

DOCKER_VERSION="$(docker version --format '{{.Server.Version}}' 2>/dev/null || true)"

section "Docker Engine / Compose versions"

if docker version >/dev/null 2>&1; then
    echo "Docker client: $(docker version --format '{{.Client.Version}}' 2>/dev/null || true)"
    echo "Docker server: ${DOCKER_VERSION:-unknown}"
    if [[ "$SWARM_MODE" -eq 0 ]]; then
        if [[ "${COMPOSE_CMD[0]}" == "docker-compose" ]]; then
            echo "Compose standalone: $(docker-compose version --short 2>/dev/null || true)"
        else
            echo "Compose plugin: $(docker compose version --short 2>/dev/null || docker compose version 2>/dev/null | head -1 || true)"
        fi
    fi
elif docker-compose --version >/dev/null 2>&1; then
    echo "Compose standalone: $(docker-compose version --short 2>/dev/null || true)"
else
    err "docker compose is not available"
    exit 1
fi

# ---------------------------------------------------------------------------
# Network configuration check (Compose or Swarm mode)
# ---------------------------------------------------------------------------

if [[ "$SWARM_MODE" -eq 0 ]]; then
    # Detect project name and IPv6 network name from Compose config.
    if [[ -z "$COMPOSE_PROJECT_NAME" ]]; then
        COMPOSE_PROJECT_NAME="$( compose config --quiet >/dev/null 2>&1 && \
            (have python3 && compose config 2>/dev/null | python3 -c '
import sys, yaml
try:
    data = yaml.safe_load(sys.stdin.read())
except Exception:
    sys.exit(0)
print(data.get("name", ""), end="")
' 2>/dev/null || true) || true)"
        if [[ -z "$COMPOSE_PROJECT_NAME" ]]; then
            COMPOSE_PROJECT_NAME="$(basename "$PWD")"
            warn "Could not detect Compose project name; using directory name: $COMPOSE_PROJECT_NAME"
        fi
    fi
    echo "Compose project: $COMPOSE_PROJECT_NAME"

    IPV6_NETWORK_NAME="$(have python3 && compose config 2>/dev/null | python3 -c '
import sys, yaml
try:
    data = yaml.safe_load(sys.stdin.read())
except Exception:
    sys.exit(0)
net = (data.get("networks") or {}).get("'$IPV6_NETWORK_KEY'", {}) or {}
print(net.get("name", "") or "", end="")
' 2>/dev/null || true)"
    if [[ -z "$IPV6_NETWORK_NAME" ]]; then
        IPV6_NETWORK_NAME="${COMPOSE_PROJECT_NAME}_${IPV6_NETWORK_KEY}"
    fi
    echo "IPv6 network (expected): $IPV6_NETWORK_NAME"

    section "Compose configuration (docker compose config)"

    if ! compose config >/dev/null 2>&1; then
        err "docker compose config failed; fix the Compose files before continuing"
        exit 1
    fi
    echo "docker compose config: OK"

    # Check if python3 yaml module is available for YAML parsing.
    if have python3 && ! python3 -c 'import yaml' 2>/dev/null; then
        warn "python3 yaml module not available; some Compose config checks may be imprecise"
    fi

    IPV6_ENABLED="$(config_value networks.${IPV6_NETWORK_KEY}.enable_ipv6)"
    IPV6_DRIVER="$(config_value networks.${IPV6_NETWORK_KEY}.driver)"
    IPV6_EXTERNAL="$(config_value networks.${IPV6_NETWORK_KEY}.external)"
    echo "network ${IPV6_NETWORK_KEY}: driver=${IPV6_DRIVER:-<not defined>} enable_ipv6=${IPV6_ENABLED:-<not defined>} external=${IPV6_EXTERNAL:-<not defined>}"

    if [[ "$IPV6_ENABLED" != "true" ]]; then
        err "IPv6 network '${IPV6_NETWORK_KEY}' is not defined with enable_ipv6: true"
        exit 1
    fi
    if [[ "$IPV6_EXTERNAL" == "true" ]]; then
        err "IPv6 network is external: true; Compose will not create it automatically"
        exit 1
    fi

    # az-local must attach to both default and the IPv6 network.
    AZ_LOCAL_NETS="$(have python3 && compose config 2>/dev/null | python3 -c '
import sys, yaml
try:
    data = yaml.safe_load(sys.stdin.read())
except Exception:
    sys.exit(0)
svc = (data.get("services") or {}).get("az-local", {}) or {}
nets = list((svc.get("networks") or {}).keys())
print(" ".join(nets), end="")
' 2>/dev/null || true)"
    echo "az-local networks: ${AZ_LOCAL_NETS:-<none>}"
    if [[ " ${AZ_LOCAL_NETS} " != *" default "* || " ${AZ_LOCAL_NETS} " != *" ${IPV6_NETWORK_KEY} "* ]]; then
        err "az-local is not attached to both 'default' and '${IPV6_NETWORK_KEY}' networks"
        exit 1
    fi

    section "Docker IPv6 network state"

    if ! docker network inspect "$IPV6_NETWORK_NAME" >/dev/null 2>&1; then
        warn "Docker network '$IPV6_NETWORK_NAME' does not exist yet"
        warn "Run: docker compose up -d  (Compose creates it automatically)"
    else
        NET_JSON="$(docker network inspect "$IPV6_NETWORK_NAME" 2>/dev/null || true)"
        if have python3 && [[ -n "$NET_JSON" ]]; then
            echo "$NET_JSON" | python3 -c '
import sys, json
try:
    data = json.loads(sys.stdin.read())
    net = data[0] if isinstance(data, list) else data
except Exception:
    sys.exit(0)
print("driver:", net.get("Driver"))
print("enable_ipv6:", net.get("EnableIPv6"))
ipam = net.get("IPAM") or {}
configs = ipam.get("Config") or []
for c in configs:
    subnet = c.get("Subnet", "")
    if ":" in subnet:
        print("ipv6 subnet:", subnet, "gateway:", c.get("Gateway"))
    elif subnet:
        print("ipv4 subnet:", subnet)
' 2>/dev/null || echo "$NET_JSON"
        else
            echo "$NET_JSON"
        fi
    fi
else
    section "Swarm configuration (docker_gwbridge)"

    if ! docker info --format '{{.Swarm.LocalNodeState}}' 2>/dev/null | grep -q 'active'; then
        err "Not a Swarm node (or Docker not running). Initialize Swarm first: docker swarm init"
        exit 1
    fi
    echo "OK: Swarm node active"

    if ! docker network inspect docker_gwbridge >/dev/null 2>&1; then
        err "Docker network 'docker_gwbridge' does not exist yet"
        err "Run: ./scripts/setup-swarm-ipv6-egress.sh --apply"
        exit 1
    fi
    echo "docker_gwbridge network: exists"

    GWBRIDGE_JSON="$(docker network inspect docker_gwbridge 2>/dev/null || true)"
    if have python3 && [[ -n "$GWBRIDGE_JSON" ]]; then
        echo "$GWBRIDGE_JSON" | python3 -c '
import sys, json
try:
    data = json.loads(sys.stdin.read())
    net = data[0] if isinstance(data, list) else data
except Exception:
    sys.exit(0)
print("driver:", net.get("Driver"))
print("enable_ipv6:", net.get("EnableIPv6"))
ipam = net.get("IPAM") or {}
configs = ipam.get("Config") or []
for c in configs:
    subnet = c.get("Subnet", "")
    if ":" in subnet:
        print("ipv6 subnet:", subnet, "gateway:", c.get("Gateway"))
    elif subnet:
        print("ipv4 subnet:", subnet)
' 2>/dev/null || echo "$GWBRIDGE_JSON"
    else
        echo "$GWBRIDGE_JSON"
    fi

    GWBRIDGE_IPV6="$(docker network inspect --format '{{.EnableIPv6}}' docker_gwbridge 2>/dev/null || echo 'false')"
    if [[ "$GWBRIDGE_IPV6" != "true" ]]; then
        err "docker_gwbridge does not have IPv6 enabled"
        err "Run: ./scripts/setup-swarm-ipv6-egress.sh --apply"
        exit 1
    fi
    echo "OK: docker_gwbridge has IPv6 enabled"
fi

# ---------------------------------------------------------------------------
# Container state (Compose: az-local; Swarm: az-local and az-world task containers)
# ---------------------------------------------------------------------------

if [[ "$SWARM_MODE" -eq 0 ]]; then
    section "az-local container state"

    AZ_LOCAL_ID="$(compose ps -q az-local 2>/dev/null || true)"
    if [[ -z "$AZ_LOCAL_ID" ]]; then
        warn "az-local container is not running (start with: docker compose up -d az-local)"
    else
        echo "az-local container: $AZ_LOCAL_ID"
        echo "--- container network attachments ---"
        docker inspect --format '{{json .NetworkSettings.Networks}}' "$AZ_LOCAL_ID" 2>/dev/null || true
        echo "--- IPv6 addresses inside az-local ---"
        ipv6_addr="$(docker exec "$AZ_LOCAL_ID" ip -6 addr show 2>/dev/null || true)"
        if [[ -n "$ipv6_addr" ]]; then
            echo "$ipv6_addr" | head -20 || true
        else
            warn "cannot inspect IPv6 addresses inside az-local"
        fi
        echo "--- IPv6 routes inside az-local ---"
        docker exec "$AZ_LOCAL_ID" ip -6 route show 2>/dev/null || warn "cannot inspect IPv6 routes inside az-local"
        echo "--- Tundra-NAT64 environment variables ---"
        docker inspect --format '{{range .Config.Env}}{{println .}}{{end}}' "$AZ_LOCAL_ID" 2>/dev/null | \
            grep -E 'TUNDRA_SOCKET|TUNDRA_INTERFACE' || warn "no TUNDRA_* environment variables found on az-local"
        echo "--- Tundra-NAT64 process ---"
        docker exec "$AZ_LOCAL_ID" pgrep -af "/usr/bin/tundra-nat64" 2>/dev/null || warn "tundra-nat64 is not running inside az-local"
        echo "--- docktinel status ---"
        docker exec "$AZ_LOCAL_ID" docktinel status 2>/dev/null || warn "docktinel status unavailable inside az-local"
        echo "--- dnsmap process ---"
        docker exec "$AZ_LOCAL_ID" pgrep -af dnsmap 2>/dev/null || warn "dnsmap is not running inside az-local"
        echo "--- Unix socket ---"
        tundra_socket_path="$(docker inspect --format '{{range .Config.Env}}{{println .}}{{end}}' "$AZ_LOCAL_ID" 2>/dev/null | \
            grep -E '^TUNDRA_SOCKET=' | cut -d= -f2- || true)"
        tundra_socket_path="${tundra_socket_path:-/run/tundra/tundra.sock}"
        docker exec "$AZ_LOCAL_ID" test -S "$tundra_socket_path" 2>/dev/null && \
            echo "$tundra_socket_path exists" || \
            warn "$tundra_socket_path missing"
        echo "--- ip6tables FORWARD rules for fd64:ff9b::/96 ---"
        docker exec "$AZ_LOCAL_ID" ip6tables -S FORWARD 2>/dev/null | \
            grep "fd64:ff9b" || warn "no ip6tables FORWARD rule for fd64:ff9b::/96"
        echo "--- TUN interface ---"
        docker exec "$AZ_LOCAL_ID" ip link show tundra 2>/dev/null || warn "tundra TUN interface missing"
        echo "--- SIIT bindings in iptables ---"
        count="$(docker exec "$AZ_LOCAL_ID" iptables -t nat -S dnsmap 2>/dev/null | grep -c "dnsmap:siit:v1:" || true)"
        echo "${count:-0} SIIT bindings found"
        echo "--- VIP /32 routes to Tundra ---"
        routes="$(docker exec "$AZ_LOCAL_ID" ip route show dev tundra 2>/dev/null || true)"
        if [[ -n "$routes" ]]; then
            echo "$routes" | head -20 || true
        else
            warn "no routes to tundra TUN interface"
        fi
        echo "--- NAT66 MASQUERADE ---"
        docker exec "$AZ_LOCAL_ID" ip6tables -t nat -S POSTROUTING 2>/dev/null | \
            grep "MASQUERADE" || warn "no NAT66 MASQUERADE rule found"
        echo "--- TCPMSS clamping ---"
        docker exec "$AZ_LOCAL_ID" iptables -t mangle -S FORWARD 2>/dev/null | \
            grep "TCPMSS" || warn "no TCPMSS rule found"
    fi
else
    section "Swarm task containers (az-local, az-world)"

    for svc in az-local az-world; do
        echo "--- Service: $svc ---"
        CONTAINER_IDS="$(find_swarm_containers "$svc")"
        if [[ -z "$CONTAINER_IDS" ]]; then
            warn "No running Swarm task containers for service '$svc'"
            continue
        fi
        for cid in $CONTAINER_IDS; do
            echo "=== Container: $cid ==="
            echo "--- container network attachments ---"
            docker inspect --format '{{json .NetworkSettings.Networks}}' "$cid" 2>/dev/null || true
            echo "--- IPv6 addresses inside $svc ---"
            ipv6_addr="$(docker exec "$cid" ip -6 addr show 2>/dev/null || true)"
            if [[ -n "$ipv6_addr" ]]; then
                echo "$ipv6_addr" | head -20 || true
            else
                warn "cannot inspect IPv6 addresses inside $svc"
            fi
            echo "--- IPv6 routes inside $svc ---"
            docker exec "$cid" ip -6 route show 2>/dev/null || warn "cannot inspect IPv6 routes inside $svc"
            echo "--- Tundra-NAT64 environment variables ---"
            docker inspect --format '{{range .Config.Env}}{{println .}}{{end}}' "$cid" 2>/dev/null | \
                grep -E 'TUNDRA_SOCKET|TUNDRA_INTERFACE' || warn "no TUNDRA_* environment variables found on $svc"
            echo "--- Tundra-NAT64 process ---"
            docker exec "$cid" pgrep -af "/usr/bin/tundra-nat64" 2>/dev/null || warn "tundra-nat64 is not running inside $svc"
            echo "--- docktinel status ---"
            docker exec "$cid" docktinel status 2>/dev/null || warn "docktinel status unavailable inside $svc"
            echo "--- dnsmap process ---"
            docker exec "$cid" pgrep -af dnsmap 2>/dev/null || warn "dnsmap is not running inside $svc"
            echo "--- Unix socket ---"
            tundra_socket_path="$(docker inspect --format '{{range .Config.Env}}{{println .}}{{end}}' "$cid" 2>/dev/null | \
                grep -E '^TUNDRA_SOCKET=' | cut -d= -f2- || true)"
            tundra_socket_path="${tundra_socket_path:-/run/tundra/tundra.sock}"
            docker exec "$cid" test -S "$tundra_socket_path" 2>/dev/null && \
                echo "$tundra_socket_path exists" || \
                warn "$tundra_socket_path missing"
            echo "--- ip6tables FORWARD rules for fd64:ff9b::/96 ---"
            docker exec "$cid" ip6tables -S FORWARD 2>/dev/null | \
                grep "fd64:ff9b" || warn "no ip6tables FORWARD rule for fd64:ff9b::/96"
            echo "--- TUN interface ---"
            docker exec "$cid" ip link show tundra 2>/dev/null || warn "tundra TUN interface missing"
            echo "--- SIIT bindings in iptables ---"
            count="$(docker exec "$cid" iptables -t nat -S dnsmap 2>/dev/null | grep -c "dnsmap:siit:v1:" || true)"
            echo "${count:-0} SIIT bindings found"
            echo "--- VIP /32 routes to Tundra ---"
            routes="$(docker exec "$cid" ip route show dev tundra 2>/dev/null || true)"
            if [[ -n "$routes" ]]; then
                echo "$routes" | head -20 || true
            else
                warn "no routes to tundra TUN interface"
            fi
            echo "--- NAT66 MASQUERADE ---"
            docker exec "$cid" ip6tables -t nat -S POSTROUTING 2>/dev/null | \
                grep "MASQUERADE" || warn "no NAT66 MASQUERADE rule found"
            echo "--- TCPMSS clamping ---"
            docker exec "$cid" iptables -t mangle -S FORWARD 2>/dev/null | \
                grep "TCPMSS" || warn "no TCPMSS rule found"
        done
    done
fi

# ---------------------------------------------------------------------------
# Host-side IPv6 and firewall signs
# ---------------------------------------------------------------------------

section "Host-side IPv6 forwarding and firewall signs"

echo "--- global IPv6 on host interfaces ---"
ipv6_host_addr="$(ip -6 addr show 2>/dev/null | grep -E 'inet6\s' || true)"
if [[ -n "$ipv6_host_addr" ]]; then
    echo "$ipv6_host_addr" | head -20 || true
else
    warn "no IPv6 addresses found on host"
fi
echo "--- IPv6 default route on host ---"
ip -6 route show default 2>/dev/null || warn "no IPv6 default route on host (external IPv6 egress impossible without one)"
echo "--- IPv6 forwarding ---"
sysctl net.ipv6.conf.all.forwarding 2>/dev/null || warn "cannot read net.ipv6.conf.all.forwarding"
sysctl net.ipv6.conf.default.forwarding 2>/dev/null || true
echo "--- docker IPv6 bridge interface ---"
ip link show 2>/dev/null | grep -iE 'docker|br-' || warn "no docker bridge interface found"
echo "--- ip6tables NAT rules (counters only) ---"
if have ip6tables; then
    ip6tables_rules="$(ip6tables -t nat -L -n -v 2>/dev/null || true)"
    if [[ -n "$ip6tables_rules" ]]; then
        echo "$ip6tables_rules" | head -40 || true
    else
        warn "ip6tables NAT rules unavailable"
    fi
else
    warn "ip6tables not installed"
fi

# ---------------------------------------------------------------------------
# Optional external IPv6 connectivity check (opt-in)
# ---------------------------------------------------------------------------

if [[ "$EXTERNAL_CHECK" -eq 1 ]]; then
    section "External IPv6 connectivity (opt-in, contacts 2001:4860:4860::8888)"
    if [[ "$SWARM_MODE" -eq 0 ]]; then
        if [[ -n "$AZ_LOCAL_ID" ]]; then
            echo "--- ping6 from az-local ---"
            docker exec "$AZ_LOCAL_ID" ping6 -c 2 2001:4860:4860::8888 2>&1 || \
                warn "ping6 to 2001:4860:4860::8888 failed from az-local"
            echo "--- curl -6 from az-local ---"
            docker exec "$AZ_LOCAL_ID" curl -6 -fsS --max-time 15 -o /dev/null -w "HTTP %{http_code}\n" https://example.com/ 2>/dev/null || \
                warn "curl -6 https://example.com failed from az-local"
        else
            warn "az-local not running; skipping external check"
        fi
    else
        for svc in az-local az-world; do
            echo "--- ping6 from $svc ---"
            for cid in $(find_swarm_containers "$svc"); do
                docker exec "$cid" ping6 -c 2 2001:4860:4860::8888 2>&1 || \
                    warn "ping6 to 2001:4860:4860::8888 failed from $svc ($cid)"
            done
            echo "--- curl -6 from $svc ---"
            for cid in $(find_swarm_containers "$svc"); do
                docker exec "$cid" curl -6 -fsS --max-time 15 -o /dev/null -w "HTTP %{http_code}\n" https://example.com/ 2>/dev/null || \
                    warn "curl -6 https://example.com failed from $svc ($cid)"
            done
        done
    fi
fi

section "Diagnostics complete"
echo "No changes were made by this script (except the optional --check-external test)."
if [[ "$SWARM_MODE" -eq 0 ]]; then
    echo "If the IPv6 network is missing, run: docker compose up -d"
    echo "If az-local lacks an IPv6 address or route, check host IPv6 connectivity,"
    echo "forwarding (net.ipv6.conf.all.forwarding) and firewall (ip6tables) rules."
else
    echo "If docker_gwbridge lacks IPv6, run: ./scripts/setup-swarm-ipv6-egress.sh --apply"
    echo "If Swarm task containers lack IPv6 address or route, check host IPv6 connectivity,"
    echo "forwarding (net.ipv6.conf.all.forwarding) and firewall (ip6tables) rules."
fi