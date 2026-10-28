#!/usr/bin/env bash
#
# diagnose-ipv6-egress.sh
#
# Read-only diagnostics for selective IPv6 Egress in antizapret-vpn-docker.
#
# Reports, without changing anything:
#   * Docker Engine and Compose versions
#   * Existence and parameters of the Compose-managed IPv6 network
#     (driver, EnableIPv6, IPv6 gateway/subnet)
#   * Actual az-local container network attachments
#   * IPv6 address and default route inside az-local
#   * Host-side forwarding/firewall signs relevant to IPv6 egress
#   * Presence of IPv6 egress environment variables
#   * Translator process status inside az-local
#   * Presence of the domains and mappings files (host and container paths)
#
# By default nothing is modified and no external host is contacted.
# Use --check-external for an explicit outbound IPv6 connectivity test.
#
# Exit codes: 0 = diagnostics collected, 1 = Docker/Compose unavailable or
#             required configuration missing, 2 = usage error.

set -u

COMPOSE_FILES="${COMPOSE_FILES:-docker-compose.yml:docker-compose.override.yml}"
COMPOSE_PROJECT_NAME="${COMPOSE_PROJECT_NAME:-}"
IPV6_NETWORK_KEY="ipv6"
EXTERNAL_CHECK=0

usage() {
    cat <<EOF
Usage: $0 [--check-external] [--help]

Options:
  --check-external  Additionally test outbound IPv6 connectivity from az-local
                    (contacts an external IPv6 address). Opt-in only.
  --help            Show this help.

Environment:
  COMPOSE_FILES           Colon-separated Compose files (default: docker-compose.yml:docker-compose.override.yml)
  COMPOSE_PROJECT_NAME    Compose project name (default: detected from Compose config / directory)
  IPV6_NETWORK_KEY        Network key in Compose config (default: ipv6)

Examples:
  $0
  $0 --check-external
  COMPOSE_PROJECT_NAME=antizapret $0
EOF
}

err() { echo "diagnose-ipv6-egress: $*" >&2; }
warn() { echo "diagnose-ipv6-egress: WARNING: $*" >&2; }

while [[ $# -gt 0 ]]; do
    case "$1" in
        --check-external) EXTERNAL_CHECK=1; shift ;;
        --help|-h) usage; exit 0 ;;
        *) err "unknown argument: $1"; usage; exit 2 ;;
    esac
done

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

have() { command -v "$1" >/dev/null 2>&1; }

# Run docker compose with the configured files.
compose() {
    docker compose -f "${COMPOSE_FILES//:/ -f }" "$@"
}

# Parse a value from `docker compose config` YAML using python3 if available,
# otherwise fall back to a naive grep.
config_value() {
    # $1 = dotted path, e.g. networks.ipv6.enable_ipv6
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

section "Docker Engine / Compose versions"

if ! have docker; then
    err "docker is not installed or not in PATH"
    exit 1
fi
DOCKER_VERSION="$(docker version --format '{{.Server.ClientInfo.ServerVersion}}' 2>/dev/null || docker version --format '{{.ServerVersion}}' 2>/dev/null || true)"
echo "Docker client: $(docker version --format '{{.Client.Version}}' 2>/dev/null || true)"
echo "Docker server: ${DOCKER_VERSION:-unknown}"

if have docker compose; then
    echo "Compose plugin: $(docker compose version --short 2>/dev/null || docker compose version 2>/dev/null | head -1 || true)"
elif have docker-compose; then
    echo "Compose standalone: $(docker-compose version --short 2>/dev/null || true)"
else
    err "docker compose is not available"
    exit 1
fi

# Detect project name and IPv6 network name from Compose config.
if [[ -z "$COMPOSE_PROJECT_NAME" ]]; then
    COMPOSE_PROJECT_NAME="$(compose config --quiet >/dev/null 2>&1 && \
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

# ---------------------------------------------------------------------------
# Compose configuration check
# ---------------------------------------------------------------------------

section "Compose configuration (docker compose config)"

if ! compose config >/dev/null 2>&1; then
    err "docker compose config failed; fix the Compose files before continuing"
    exit 1
fi
echo "docker compose config: OK"

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

# az-local must attach to both default and the IPv6 network; no port 9000 publish.
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

PUBLISHED_9000="$(have python3 && compose config 2>/dev/null | python3 -c '
import sys, yaml
try:
    data = yaml.safe_load(sys.stdin.read())
except Exception:
    sys.exit(0)
svc = (data.get("services") or {}).get("az-local", {}) or {}
for port in (svc.get("ports") or []):
    if isinstance(port, dict):
        print(port.get("published", ""), end=" ")
    elif isinstance(port, str) and ":9000" in port:
        print("9000", end=" ")
print("", end="")
' 2>/dev/null || true)"
if [[ -n "${PUBLISHED_9000// }" ]]; then
    err "az-local publishes port 9000 (internal translator port must not be exposed)"
    exit 1
fi
echo "az-local published ports: none (port 9000 not published)"

# ---------------------------------------------------------------------------
# Docker network state
# ---------------------------------------------------------------------------

section "Docker IPv6 network state"

if ! docker network inspect "$IPV6_NETWORK_NAME" >/dev/null 2>&1; then
    warn "Docker network '$IPV6_NETWORK_NAME' does not exist yet"
    warn "Run: docker compose up -d  (Compose creates it automatically)"
else
    NET_JSON="$(docker network inspect --format 'json' "$IPV6_NETWORK_NAME" 2>/dev/null || true)"
    if have python3 && [[ -n "$NET_JSON" ]]; then
        echo "$NET_JSON" | python3 -c '
import sys, json
try:
    net = json.load(sys.stdin.read())[0] if isinstance(json.load(sys.stdin.read()), list) else json.load(sys.stdin.read())
except Exception:
    sys.exit(0)
print("driver:", net.get("Driver"))
print("enable_ipv6:", net.get("EnableIPv6"))
ipam = net.get("IPAM") or {}
configs = ipam.get("Config") or []
for c in configs:
    if c.get("IPv6"):
        print("ipv6 subnet:", c.get("Subnet"), "gateway:", c.get("Gateway"))
    elif c.get("Subnet"):
        print("ipv4 subnet:", c.get("Subnet"))
' 2>/dev/null || echo "$NET_JSON"
    else
        echo "$NET_JSON"
    fi
fi

# ---------------------------------------------------------------------------
# az-local container state
# ---------------------------------------------------------------------------

section "az-local container state"

AZ_LOCAL_ID="$(compose ps -q az-local 2>/dev/null || true)"
if [[ -z "$AZ_LOCAL_ID" ]]; then
    warn "az-local container is not running (start with: docker compose up -d az-local)"
else
    echo "az-local container: $AZ_LOCAL_ID"
    echo "--- container network attachments ---"
    docker inspect --format '{{json .NetworkSettings.Networks}}' "$AZ_LOCAL_ID" 2>/dev/null || true
    echo "--- IPv6 addresses inside az-local ---"
    docker exec "$AZ_LOCAL_ID" ip -6 addr show 2>/dev/null || warn "cannot inspect IPv6 addresses inside az-local"
    echo "--- IPv6 routes inside az-local ---"
    docker exec "$AZ_LOCAL_ID" ip -6 route show 2>/dev/null || warn "cannot inspect IPv6 routes inside az-local"
    echo "--- translator process ---"
    docker exec "$AZ_LOCAL_ID" pgrep -af ipv6_translator.py 2>/dev/null || warn "ipv6_translator.py is not running inside az-local"
    echo "--- IPv6 egress environment variables ---"
    docker inspect --format '{{range .Config.Env}}{{println .}}{{end}}' "$AZ_LOCAL_ID" 2>/dev/null | \
        grep -E 'IPV6_EGRESS|IPV6_TRANSLATOR' || warn "no IPV6_EGRESS_* environment variables found on az-local"
fi

# ---------------------------------------------------------------------------
# Host-side IPv6 and firewall signs
# ---------------------------------------------------------------------------

section "Host-side IPv6 forwarding and firewall signs"

echo "--- global IPv6 on host interfaces ---"
ip -6 addr show 2>/dev/null | grep -E 'inet6\s' | head -20 || warn "no IPv6 addresses found on host"
echo "--- IPv6 default route on host ---"
ip -6 route show default 2>/dev/null || warn "no IPv6 default route on host (external IPv6 egress impossible without one)"
echo "--- IPv6 forwarding ---"
sysctl net.ipv6.conf.all.forwarding 2>/dev/null || warn "cannot read net.ipv6.conf.all.forwarding"
sysctl net.ipv6.conf.default.forwarding 2>/dev/null || true
echo "--- docker IPv6 bridge interface ---"
ip link show 2>/dev/null | grep -iE 'docker|br-' || warn "no docker bridge interface found"
echo "--- ip6tables NAT rules (counters only) ---"
if have ip6tables; then
    ip6tables -t nat -L -n -v 2>/dev/null | head -40 || warn "ip6tables NAT rules unavailable"
else
    warn "ip6tables not installed"
fi

# ---------------------------------------------------------------------------
# Configuration files
# ---------------------------------------------------------------------------

section "IPv6 egress configuration files"

HOST_DOMAINS_FILE="config/antizapret/custom/ipv6-egress-domains-local.txt"
HOST_MAPPINGS_FILE="config/antizapret/result/ipv6-egress-mappings-local.txt"
CONTAINER_DOMAINS_FILE="/root/antizapret/config/custom/ipv6-egress-domains-local.txt"
CONTAINER_MAPPINGS_FILE="/root/antizapret/result/ipv6-egress-mappings-local.txt"

echo "host domains file:    $HOST_DOMAINS_FILE"
if [[ -f "$HOST_DOMAINS_FILE" ]]; then
    echo "  exists: yes  size: $(stat -c %s "$HOST_DOMAINS_FILE") bytes"
else
    echo "  exists: no  (init.sh creates it on first start)"
fi
echo "host mappings file:   $HOST_MAPPINGS_FILE"
if [[ -f "$HOST_MAPPINGS_FILE" ]]; then
    echo "  exists: yes  size: $(stat -c %s "$HOST_MAPPINGS_FILE") bytes"
else
    echo "  exists: no  (created by dnsmap/translator on first mapping)"
fi

if [[ -n "$AZ_LOCAL_ID" ]]; then
    echo "--- files inside az-local ---"
    docker exec "$AZ_LOCAL_ID" test -f "$CONTAINER_DOMAINS_FILE" && \
        echo "container domains file: $CONTAINER_DOMAINS_FILE exists" || \
        echo "container domains file: $CONTAINER_DOMAINS_FILE missing"
    docker exec "$AZ_LOCAL_ID" test -f "$CONTAINER_MAPPINGS_FILE" && \
        echo "container mappings file: $CONTAINER_MAPPINGS_FILE exists" || \
        echo "container mappings file: $CONTAINER_MAPPINGS_FILE missing"
fi

# ---------------------------------------------------------------------------
# Optional external IPv6 connectivity check (opt-in)
# ---------------------------------------------------------------------------

if [[ "$EXTERNAL_CHECK" -eq 1 ]]; then
    section "External IPv6 connectivity (opt-in, contacts 2001:4860:4860::8888)"
    if [[ -n "$AZ_LOCAL_ID" ]]; then
        echo "--- ping6 from az-local ---"
        docker exec "$AZ_LOCAL_ID" ping6 -c 3 -W 5 2001:4860:4860::8888 2>/dev/null || \
            warn "ping6 to 2001:4860:4860::8888 failed from az-local"
        echo "--- curl -6 from az-local ---"
        docker exec "$AZ_LOCAL_ID" curl -6 -fsS --max-time 15 -o /dev/null -w "HTTP %{http_code}\n" https://example.com/ 2>/dev/null || \
            warn "curl -6 https://example.com failed from az-local"
    else
        warn "az-local not running; skipping external check"
    fi
fi

section "Diagnostics complete"
echo "No changes were made by this script (except the optional --check-external test)."
echo "If the IPv6 network is missing, run: docker compose up -d"
echo "If az-local lacks an IPv6 address or route, check host IPv6 connectivity,"
echo "forwarding (net.ipv6.conf.all.forwarding) and firewall (ip6tables) rules."
