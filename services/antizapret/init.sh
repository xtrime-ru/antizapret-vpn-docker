#!/bin/bash

set -e
set -x

WARP_PROTOCOL=${WARP_PROTOCOL:-WireGuard}
WARP_PORT=${WARP_PORT:-500}

DOCKER_SUBNET="$(ipcalc "$(ip -4 addr show dev eth0 | awk '$1=="inet" {print $2; exit}')" | awk '/Network:/ {print $2}')"

# save DNS variables to /etc/default/antizapret
# in order to systemd services can access them
cat << EOF | sponge /etc/default/antizapret
PYTHONUNBUFFERED=1
DOCKER_SUBNET=${DOCKER_SUBNET}
DNS=${DNS:-"127.0.0.1"}
CLIENT=${CLIENT:-"az-local"}
DOALL_DISABLED=${DOALL_DISABLED:-""}
IPTABLES_SAVE_DISABLED=${IPTABLES_SAVE_DISABLED:-""}
WARP_ENABLED=${WARP_ENABLED:-"0"}
ZAPRET_ENABLED=${ZAPRET_ENABLED:-"0"}
export ZAPRET_CONFIG='${ZAPRET_CONFIG:-"/opt/zapret2/config/zapret.conf"}'
IPS_URL='${IPS_URL:-""}'
IPS_WORLD_URL='${IPS_WORLD_URL:-""}'
ASN_URL='${ASN_URL:-""}'
ASN_WORLD_URL='${ASN_WORLD_URL:-""}'
ASN_FILES='${ASN_FILES:-""}'
AZ_SUBNET=${AZ_SUBNET:-"14.16.0.0/15"}
LC_ALL=C.UTF-8
EOF
printf 'TUNDRA_SOCKET=%q\nTUNDRA_INTERFACE=%q\n' "${TUNDRA_SOCKET:-}" "${TUNDRA_INTERFACE:-tundra}" >> /etc/default/antizapret
source /etc/default/antizapret
# autoload vars when logging in into shell with 'bash -l'
ln -sf /etc/default/antizapret /etc/profile.d/antizapret.sh

# Compute DNS address dynamically from AZ_SUBNET
# so az-local uses 14.16.0.1 and az-world uses 14.18.0.1.
AZ_DNS_ADDR="$(python3 -c 'import ipaddress, sys; net = ipaddress.IPv4Network(sys.argv[1]); print(net[1])' "$AZ_SUBNET")"

# TUNDRA_ROUTER_IPV4 is only needed when Tundra-NAT64 is enabled
# (TUNDRA_SOCKET is non-empty). Computing net[254] unconditionally would
# raise IndexError for AZ_SUBNET smaller than /24 and break IPv4-only startup.
if [ -n "${TUNDRA_SOCKET:-}" ]; then
    TUNDRA_ROUTER_IPV4="${TUNDRA_ROUTER_IPV4:-$(python3 -c 'import ipaddress, sys; net = ipaddress.IPv4Network(sys.argv[1]); print(net[254])' "$AZ_SUBNET")}"
fi

# creating custom hosts files if they have not yet been initialized
for file in $(echo {exclude,include}-{hosts,ips,ips-world,asn,asn-world}-custom.txt); do
    path=/root/antizapret/config/custom/$file
    [ ! -f $path ] && touch $path
done

mkdir -p /root/antizapret/result
# Tundra-NAT64 needs a directory for its Unix socket; dnsmap creates the socket
# itself, but the directory must exist before Tundra tries to connect.
mkdir -p /run/tundra
for file in ips ips-world asn asn-world; do
    path=/root/antizapret/result/$file.txt
    [ ! -f $path ] && touch $path
done

function generated_lists_available () {
    [ -z "${IPS_URL:-}" ] || [ -s /root/antizapret/result/ips.txt ] || return 1
    [ -z "${IPS_WORLD_URL:-}" ] || [ -s /root/antizapret/result/ips-world.txt ] || return 1
    [ -z "${ASN_URL:-}" ] || [ -s /root/antizapret/result/asn.txt ] || return 1
    [ -z "${ASN_WORLD_URL:-}" ] || [ -s /root/antizapret/result/asn-world.txt ] || return 1
}

. /usr/local/lib/docktinel/state.sh
checksum_files /root/antizapret/result/* /root/antizapret/config/custom/* > /dev/shm/.config_md5

# Prepare iptables for dnsmap.py
CHAIN=dnsmap
iptables -t nat -N "$CHAIN" 2>/dev/null || true
# Anchor rules are idempotent: repeated container restarts must not duplicate them.
iptables -t nat -C PREROUTING -d "${AZ_SUBNET}" -j "$CHAIN" 2>/dev/null || \
    iptables -t nat -A PREROUTING -d "${AZ_SUBNET}" -j "$CHAIN"
iptables -t nat -C OUTPUT -d "${AZ_SUBNET}" -j "$CHAIN" 2>/dev/null || \
    iptables -t nat -A OUTPUT -d "${AZ_SUBNET}" -j "$CHAIN"
for eth in $(ip link | grep -oE "eth[0-9]"); do
    iptables -t nat -C POSTROUTING -o "$eth" -j MASQUERADE 2>/dev/null || \
        iptables -t nat -A POSTROUTING -o "$eth" -j MASQUERADE
done

HOSTNAME=$(hostname -s)
IPTABLES_SAVE="/root/antizapret/iptables/$HOSTNAME.rules"

set +x
if [ "$IPTABLES_SAVE_DISABLED" != "1" ] && [ -f "$IPTABLES_SAVE" ]; then
  LINES=$(wc -l < "$IPTABLES_SAVE")
  echo "restoring iptables rules: $LINES"
  if [ "$LINES" -gt 130000 ]; then
    echo "iptables-save too big. removing old file."
    rm -rf "$IPTABLES_SAVE"
  else
    IPTABLES_RESTORE_FILE=$(mktemp)
    {
      printf '*nat\n'
      grep -E "^-A $CHAIN " "$IPTABLES_SAVE" || true
      printf 'COMMIT\n'
    } > "$IPTABLES_RESTORE_FILE"

    if iptables-restore --noflush "$IPTABLES_RESTORE_FILE"; then
      echo "iptables rules restored"
    else
      echo "cant restore iptables rules"
    fi
    rm -f "$IPTABLES_RESTORE_FILE"
  fi
fi
set -x

if [ "$ZAPRET_ENABLED" = "1" ]; then
    if [ -s "$ZAPRET_CONFIG" ]; then
        # Regenerate only known, unmodified bundled configs; keep custom rules.
        case "$(md5sum < "$ZAPRET_CONFIG" | awk '{print $1}')" in
            5f71da81191ae869d849ed4f11576c56|0c7a96834a14bde6258d1dc283787b4e|c1c4af3a708375336a0c2af953d4d3c6|12297f4db14c2e1c13ca254901cd3853|daf37377faa1ee86efee404d448433d3|b003a523e159ab428102d8a5ceb9734d|65c0a2f4e1803849fce07637aa2cc56c|b3646b00464e8730b0572b61b86f200a)
                echo "Replacing old default zapret2 config: $ZAPRET_CONFIG"
                rm -f -- "$ZAPRET_CONFIG"
                ;;
        esac
    fi
    if [ ! -s "$ZAPRET_CONFIG" ]; then
        mkdir -p "$(dirname "$ZAPRET_CONFIG")"
        cp /root/zapret2/config.default "$ZAPRET_CONFIG"
    fi
    /opt/zapret2/init.d/sysv/zapret2 start
    touch /dev/shm/.zapret_started
fi

if [ "$WARP_ENABLED" = "1" ]; then
    mkdir -p /dev/net /run/dbus /var/lib/cloudflare-warp
    [ -e /dev/net/tun ] || mknod /dev/net/tun c 10 200
    rm -f /run/dbus/pid
    dbus-daemon --config-file=/usr/share/dbus-1/system.conf
    warp-svc --accept-tos &

    timeout 30 bash -c 'until warp-cli --accept-tos status >/dev/null 2>&1; do sleep 1; done'

    warp_read_config() {
        python3 - <<'PY'
import json
with open('/var/lib/cloudflare-warp/conf.json') as f:
    config = json.load(f)
print(config['tunnel_key_data']['tunnel_type'], config['endpoints'][0]['v4'].rsplit(':', 1)[0])
PY
    }

    warp-cli --accept-tos disconnect
    [ -f /var/lib/cloudflare-warp/reg.json ] || warp-cli --accept-tos registration new
    read -r warp_previous_protocol warp_endpoint < <(warp_read_config)
    warp-cli --accept-tos tunnel protocol set "$WARP_PROTOCOL"
    if [ "$warp_previous_protocol" != "${WARP_PROTOCOL,,}" ]; then
        echo "Re-registering WARP: $warp_previous_protocol -> $WARP_PROTOCOL"
        warp-cli --accept-tos registration delete
        warp-cli --accept-tos registration new
        read -r warp_previous_protocol warp_endpoint < <(warp_read_config)
    fi
    if [ "$WARP_PORT" = 0 ]; then
        warp-cli --accept-tos tunnel endpoint reset
    else
        warp-cli --accept-tos tunnel endpoint set "$warp_endpoint:$WARP_PORT"
    fi
    if [ "$WARP_PROTOCOL" = MASQUE ]; then
        warp-cli --accept-tos tunnel masque-options set h3-with-h2-fallback
    fi
    warp-cli --accept-tos mode tunnel_only
    while read -r nameserver; do
        warp-cli --accept-tos tunnel ip add "$nameserver" || true
    done < <(awk '$1 == "nameserver" {print $2}' /etc/resolv.conf)
    warp-cli --accept-tos connect
    timeout --kill-after=5s 30 bash -c 'until warp-cli --accept-tos status | grep -q "^Status update: Connected$"; do sleep 1; done'

    iptables -t nat -A POSTROUTING -o CloudflareWARP -j MASQUERADE
    iptables -t mangle -A FORWARD -o CloudflareWARP -p tcp --tcp-flags SYN,RST SYN -j TCPMSS --clamp-mss-to-pmtu
fi

if ! timeout --foreground --kill-after=5s 5m /usr/bin/doall; then
    if generated_lists_available; then
        echo 'doall failed during startup, continuing with existing lists'
    else
        echo 'doall failed during startup and no generated lists are available'
        exit 1
    fi
fi

# Initialize IPv6 egress via Tundra-NAT64 (packet SIIT translator).
# Replaces the former TCP/UDP relay (ipv6_translator.py). dnsmap now installs
# SIIT bindings (virtual IPv4 <-> real IPv6) directly into Tundra over its
# external mapping protocol Unix socket and routes each VIP /32 to the Tundra
# TUN interface before sending the DNS answer to the client.
DNSMAP_EXTRA_ARGS=()

if [ "$CLIENT" = "az-local" ]; then
    IPV6_CLIENT_ID="az-ipv6-local"
elif [ "$CLIENT" = "az-world" ]; then
    IPV6_CLIENT_ID="az-ipv6-world"
else
    IPV6_CLIENT_ID=""
fi

# Tundra-NAT64 external mapping protocol Unix socket. When unset or empty,
# IPv6 egress via Tundra is disabled and dnsmap falls back to IPv4-only.
TUNDRA_SOCKET="${TUNDRA_SOCKET:-}"
TUNDRA_INTERFACE="${TUNDRA_INTERFACE:-tundra}"

if [ -n "$TUNDRA_SOCKET" ] && [ -n "$IPV6_CLIENT_ID" ]; then
    echo "IPv6 egress via Tundra-NAT64 enabled for $CLIENT (socket $TUNDRA_SOCKET)"

    # Track which SIIT resources were successfully added, for transactional
    # rollback on failure.
    ADDED_IPV6_ROUTE=0
    ADDED_ROUTER_ROUTE=0
    ADDED_NAT66=0
    ADDED_FWD_ACCEPT=0
    ADDED_FWD_DROP=0

    # Rollback all Tundra-NAT64 resources that were successfully added.
    rollback_tundra_network() {
        if [ "$ADDED_FWD_DROP" -eq 1 ]; then
            ip6tables -D FORWARD -d fd64:ff9b::/96 -j DROP 2>/dev/null || true
        fi
        if [ "$ADDED_FWD_ACCEPT" -eq 1 ]; then
            ip6tables -D FORWARD -d fd64:ff9b::/96 -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT 2>/dev/null || true
        fi
        if [ "$ADDED_NAT66" -eq 1 ]; then
            ip6tables -t nat -D POSTROUTING -s fd64:ff9b::/96 -o "${IPV6_EGRESS_IF}" -j MASQUERADE 2>/dev/null || true
        fi
        if [ "$ADDED_ROUTER_ROUTE" -eq 1 ]; then
            ip route del "${TUNDRA_ROUTER_IPV4}/32" dev "${TUNDRA_INTERFACE}" 2>/dev/null || true
        fi
        if [ "$ADDED_IPV6_ROUTE" -eq 1 ]; then
            ip -6 route del fd64:ff9b::/96 dev "${TUNDRA_INTERFACE}" 2>/dev/null || true
        fi
    }

    # Setup Tundra-NAT64 network: routes, NAT66, ip6tables FORWARD, TCPMSS.
    # Uses the global ADDED_* flags for transactional rollback on failure.
    # Returns 0 on success, 1 on failure (with rollback already performed).
    setup_tundra_network() {
        # Step 1: IPv6 route fd64:ff9b::/96 -> TUNDRA_INTERFACE
        if ! ip -6 route show fd64:ff9b::/96 dev "${TUNDRA_INTERFACE}" 2>/dev/null | grep -q .; then
            if ! ip -6 route replace fd64:ff9b::/96 dev "${TUNDRA_INTERFACE}"; then
                echo "NAT66: failed to add route fd64:ff9b::/96 -> ${TUNDRA_INTERFACE}" >&2
                rollback_tundra_network
                return 1
            fi
            ADDED_IPV6_ROUTE=1
        fi

        # Step 2: Router route ${TUNDRA_ROUTER_IPV4}/32 -> TUNDRA_INTERFACE
        if ! ip route show "${TUNDRA_ROUTER_IPV4}/32" dev "${TUNDRA_INTERFACE}" 2>/dev/null | grep -q .; then
            if ! ip route replace "${TUNDRA_ROUTER_IPV4}/32" dev "${TUNDRA_INTERFACE}"; then
                echo "NAT66: failed to add route ${TUNDRA_ROUTER_IPV4}/32 -> ${TUNDRA_INTERFACE}" >&2
                rollback_tundra_network
                return 1
            fi
            ADDED_ROUTER_ROUTE=1
        fi

        # Step 3: POSTROUTING MASQUERADE for fd64:ff9b::/96 on IPV6_EGRESS_IF
        if ! ip6tables -t nat -C POSTROUTING -s fd64:ff9b::/96 -o "${IPV6_EGRESS_IF}" -j MASQUERADE 2>/dev/null; then
            if ! ip6tables -t nat -A POSTROUTING -s fd64:ff9b::/96 -o "${IPV6_EGRESS_IF}" -j MASQUERADE; then
                echo "NAT66: failed to add MASQUERADE rule for fd64:ff9b::/96 on ${IPV6_EGRESS_IF}" >&2
                rollback_tundra_network
                return 1
            fi
            ADDED_NAT66=1
        fi

        # Step 4: ip6tables FORWARD ACCEPT for fd64:ff9b::/96 ESTABLISHED,RELATED
        if ! ip6tables -C FORWARD -d fd64:ff9b::/96 -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT 2>/dev/null; then
            if ! ip6tables -A FORWARD -d fd64:ff9b::/96 -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT; then
                echo "ERROR: could not add ip6tables FORWARD ACCEPT for fd64:ff9b::/96 ESTABLISHED,RELATED" >&2
                rollback_tundra_network
                return 1
            fi
            ADDED_FWD_ACCEPT=1
        fi

        # Step 5: ip6tables FORWARD DROP for fd64:ff9b::/96
        if ! ip6tables -C FORWARD -d fd64:ff9b::/96 -j DROP 2>/dev/null; then
            if ! ip6tables -A FORWARD -d fd64:ff9b::/96 -j DROP; then
                echo "ERROR: could not add ip6tables FORWARD DROP for fd64:ff9b::/96" >&2
                rollback_tundra_network
                return 1
            fi
            ADDED_FWD_DROP=1
        fi

        # TCPMSS clamping (only after all 5 mandatory steps succeed)
        iptables -t mangle -C FORWARD -o "${TUNDRA_INTERFACE}" -p tcp --tcp-flags SYN,RST SYN -j TCPMSS --clamp-mss-to-pmtu 2>/dev/null || \
            iptables -t mangle -A FORWARD -o "${TUNDRA_INTERFACE}" -p tcp --tcp-flags SYN,RST SYN -j TCPMSS --clamp-mss-to-pmtu 2>/dev/null || true

        return 0
    }

    # Generate Tundra-NAT64 configuration from the template (shipped with the
    # image at /etc/tundra-nat64/tundra-nat64.conf.template). dnsmap acts as
    # the external address translation server; Tundra connects to dnsmap's Unix
    # socket to resolve SIIT address pairs per translated packet.
    mkdir -p /etc/tundra-nat64
    # Pass variables explicitly to envsubst: they are local bash variables
    # (not exported), so envsubst would otherwise see empty values.
    TUNDRA_INTERFACE="$TUNDRA_INTERFACE" \
    TUNDRA_ROUTER_IPV4="$TUNDRA_ROUTER_IPV4" \
    TUNDRA_SOCKET="$TUNDRA_SOCKET" \
    envsubst '${TUNDRA_INTERFACE} ${TUNDRA_ROUTER_IPV4} ${TUNDRA_SOCKET}' \
        < /etc/tundra-nat64/tundra-nat64.conf.template > /etc/tundra-nat64/tundra-nat64.conf

    # Validate Tundra-NAT64 configuration before starting the translator.
    if ! tundra-nat64 --config-file=/etc/tundra-nat64/tundra-nat64.conf validate-config; then
        echo "WARNING: Tundra-NAT64 configuration validation failed. IPv6 egress via Tundra-NAT64 disabled." >&2
        DNSMAP_EXTRA_ARGS=()
        touch /run/tundra/disabled
    fi

    # Ensure /dev/net/tun exists inside the container (needed for TUNDRA_INTERFACE).
    # In Docker Compose this is provided via devices:; in Swarm it may need mknod.
    # Create it independently of WARP_ENABLED so Tundra works in Swarm too.
    # Do not exit on failure: some Swarm cgroups deny TUNSETIFF, in which case
    # IPv6 egress is simply disabled and dnsmap falls back to IPv4-only.
    if [ ! -c /dev/net/tun ] && [ ! -f /run/tundra/disabled ]; then
        mkdir -p /dev/net
        if ! mknod /dev/net/tun c 10 200; then
            echo "WARNING: /dev/net/tun could not be created (cgroup may deny TUNSETIFF). IPv6 egress via Tundra-NAT64 disabled." >&2
            DNSMAP_EXTRA_ARGS=()
            touch /run/tundra/disabled
        fi
    fi
    [ -c /dev/net/tun ] && chmod 0666 /dev/net/tun || true

    # Create persistent TUN interface for Tundra-NAT64 if it doesn't exist yet.
    # Using `tundra-nat64 mktun` guarantees the TUN device flags match the
    # translator's expectations. tundra-nat64 will open this interface for
    # SIIT translation.
    if [ -c /dev/net/tun ] && [ ! -f /run/tundra/disabled ] && ! ip link show dev "${TUNDRA_INTERFACE}" >/dev/null 2>&1; then
        if ! tundra-nat64 --config-file=/etc/tundra-nat64/tundra-nat64.conf mktun; then
            echo "WARNING: tundra-nat64 mktun failed (cgroup may deny TUNSETIFF). IPv6 egress via Tundra-NAT64 disabled." >&2
            DNSMAP_EXTRA_ARGS=()
            touch /run/tundra/disabled
        else
            ip link set dev "${TUNDRA_INTERFACE}" up
            echo "TUN: created ${TUNDRA_INTERFACE} interface via tundra-nat64 mktun"
        fi
    fi

    # NAT66: masquerade the client prefix fd64:ff9b::/96 on the egress IPv6
    # interface and route it to the Tundra TUN interface. The egress interface
    # is detected dynamically from the IPv6 default route.
    # If IPv6 egress has been disabled (no TUN, no mktun, or explicit marker),
    # skip all IPV6_EGRESS_IF configuration and do not pass --tundra-socket to dnsmap.
    if [ -f /run/tundra/disabled ]; then
        echo "IPv6 egress via Tundra-NAT64 disabled (marker file /run/tundra/disabled present)."
        DNSMAP_EXTRA_ARGS=()
    else
        # IPv6 egress interface detected dynamically from the IPv6 default route.
        IPV6_EGRESS_IF="$(ip -6 route get 2001:4860:4860::8888 2>/dev/null \
            | awk '/dev/ {for(i=1;i<=NF;i++) if($i=="dev") {print $(i+1); exit}}')"
        if [ -n "$IPV6_EGRESS_IF" ]; then
            # Verify real external IPv6 connectivity with a short timeout before
            # committing to Tundra-NAT64. Try multiple independent targets;
            # success of any one is enough. Timeout 3s per attempt.
            ipv6_ok=0
            for target in "2001:4860:4860::8888/443" "2606:4700:4700::1111/443" "2001:4860:4860::8888/53"; do
                host="${target%/*}"
                port="${target#*/}"
                if timeout 3 bash -c "exec 3<>/dev/tcp/${host}/${port}" 2>/dev/null; then
                    ipv6_ok=1
                    break
                fi
            done
            if [ "$ipv6_ok" -eq 0 ]; then
                echo "NAT66: external IPv6 unreachable on all probes, disabling IPv6 egress via Tundra-NAT64"
                rollback_tundra_network
                DNSMAP_EXTRA_ARGS=()
                touch /run/tundra/disabled
            else
                if ! setup_tundra_network; then
                    DNSMAP_EXTRA_ARGS=()
                    touch /run/tundra/disabled
                else
                    # Exclude Tundra service addresses from the dnsmap VIP pool so they are
                    # never assigned as virtual IPs for SIIT bindings.
                    EXCLUDED_ADDRESSES="${AZ_DNS_ADDR},${TUNDRA_ROUTER_IPV4}"
                    DNSMAP_EXTRA_ARGS=(
                        --tundra-socket "$TUNDRA_SOCKET"
                        --tundra-interface "$TUNDRA_INTERFACE"
                        --ipv6-client-id "$IPV6_CLIENT_ID"
                        --tundra-router-ipv4 "$TUNDRA_ROUTER_IPV4"
                        --excluded-addresses "$EXCLUDED_ADDRESSES"
                    )
                    # AAAA priority: prefer IPv6 via Tundra for dual-stack domains
                    # without requiring AdGuard az-ipv6-* authorization.
                    if [ "${AAAA_PRIORITY:-0}" = "1" ]; then
                        DNSMAP_EXTRA_ARGS+=(--aaaa-priority)
                    fi
                    # Per-domain IPv6 egress map: domain:group,domain:group,...
                    # group: local, world, direct
                    if [ -n "${IPV6_EGRESS_MAP:-}" ]; then
                        DNSMAP_EXTRA_ARGS+=(--ipv6-egress-map "$IPV6_EGRESS_MAP")
                    fi

                    echo "NAT66: fd64:ff9b::/96 -> ${TUNDRA_INTERFACE}, masquerade on ${IPV6_EGRESS_IF}, MSS clamp on ${TUNDRA_INTERFACE}"

                    # Signal to docktinel that Tundra-NAT64 is ready to start: config file
                    # and TUN interface are created, routes and NAT66 are configured.
                    # docktinel.sh (sourced before init.sh) waits for this marker before
                    # launching tundra-nat64.
                    touch /run/tundra/ready
                fi
            fi
        else
            echo "NAT66: no IPv6 default route found, disabling IPv6 egress via Tundra-NAT64"
            # No IPv6 egress: do not create /run/tundra/ready, do not pass
            # --tundra-socket to dnsmap. Tundra will not start and dnsmap will
            # fall back to IPv4-only behavior.
            DNSMAP_EXTRA_ARGS=()
            touch /run/tundra/disabled
        fi
fi
else
    echo "IPv6 egress via Tundra-NAT64 disabled for $CLIENT"
fi

/usr/bin/dnsmap -a 0.0.0.0 --iprange "$AZ_SUBNET" --asn-file "$ASN_FILES" "${DNSMAP_EXTRA_ARGS[@]}" &
DNSMAP_PID=$!
trap 'stop_services; kill -TERM "$DNSMAP_PID" 2>/dev/null || true; exit 0' HUP INT QUIT PIPE TERM
wait "$DNSMAP_PID"
