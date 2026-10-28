#!/bin/bash

set -e
set -x

# run commands after start
function postrun () {
    nohup bash -c "$@" &
}

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
# IPv6 egress configuration (per exit node)
IPV6_EGRESS_LOCAL_ENABLED=${IPV6_EGRESS_LOCAL_ENABLED:-0}
IPV6_EGRESS_LOCAL_FILE=${IPV6_EGRESS_LOCAL_FILE:-""}
IPV6_EGRESS_LOCAL_MODE=${IPV6_EGRESS_LOCAL_MODE:-adguard_only}
IPV6_EGRESS_LOCAL_RESERVE_COUNT=${IPV6_EGRESS_LOCAL_RESERVE_COUNT:-100}
IPV6_TRANSLATOR_LOCAL_PORT=${IPV6_TRANSLATOR_LOCAL_PORT:-9000}
IPV6_EGRESS_WORLD_ENABLED=${IPV6_EGRESS_WORLD_ENABLED:-0}
IPV6_EGRESS_WORLD_FILE=${IPV6_EGRESS_WORLD_FILE:-""}
IPV6_EGRESS_WORLD_MODE=${IPV6_EGRESS_WORLD_MODE:-adguard_only}
IPV6_EGRESS_WORLD_RESERVE_COUNT=${IPV6_EGRESS_WORLD_RESERVE_COUNT:-100}
IPV6_TRANSLATOR_WORLD_PORT=${IPV6_TRANSLATOR_WORLD_PORT:-9000}
LC_ALL=C.UTF-8
EOF
source /etc/default/antizapret
# autoload vars when logging in into shell with 'bash -l'
ln -sf /etc/default/antizapret /etc/profile.d/antizapret.sh


# creating custom hosts files if they have not yet been initialized
for file in $(echo {exclude,include}-{hosts,ips,ips-world,asn,asn-world}-custom.txt); do
    path=/root/antizapret/config/custom/$file
    [ ! -f $path ] && touch $path
done

mkdir -p /root/antizapret/result
for file in ips ips-world asn asn-world; do
    path=/root/antizapret/result/$file.txt
    [ ! -f $path ] && touch $path
done

DOALL_OWNER_FILE="/root/antizapret/result/.doall_owner"
DOALL_LOCAL_OWNER_FILE="/dev/shm/.doall_owner"
DOALL_OWNER_ID="$(date +%s%N)-$(head -c 8 /dev/urandom | od -An -tx1 | tr -d ' \n')"

function configure_doall_owner () {
    [ -n "$DOALL_DISABLED" ] && return 0

    echo "$DOALL_OWNER_ID $CLIENT" > "$DOALL_LOCAL_OWNER_FILE"

    owner="$(awk '{print $2}' "$DOALL_OWNER_FILE" 2>/dev/null || true)"
    if [ -z "$owner" ] || [ "$owner" = "$CLIENT" ]; then
        cp -f "$DOALL_LOCAL_OWNER_FILE" "$DOALL_OWNER_FILE"
        return 0
    fi

    echo "DoAll result volume is owned by $owner. This container will reload dnsmap only."
}

function cleanup_doall_owner () {
    if [ -f "$DOALL_LOCAL_OWNER_FILE" ] && [ "$(cat "$DOALL_OWNER_FILE" 2>/dev/null || true)" = "$(cat "$DOALL_LOCAL_OWNER_FILE" 2>/dev/null || true)" ]; then
        rm -f "$DOALL_OWNER_FILE"
    fi
    rm -f "$DOALL_LOCAL_OWNER_FILE" /dev/shm/.doall_lock
}

function generated_lists_available () {
    [ -z "${IPS_URL:-}" ] || [ -s /root/antizapret/result/ips.txt ] || return 1
    [ -z "${IPS_WORLD_URL:-}" ] || [ -s /root/antizapret/result/ips-world.txt ] || return 1
    [ -z "${ASN_URL:-}" ] || [ -s /root/antizapret/result/asn.txt ] || return 1
    [ -z "${ASN_WORLD_URL:-}" ] || [ -s /root/antizapret/result/asn-world.txt ] || return 1
}

configure_doall_owner
trap cleanup_doall_owner EXIT HUP INT QUIT PIPE TERM

( cat /root/antizapret/result/* /root/antizapret/config/custom/* 2>/dev/null | md5sum ) > /dev/shm/.config_md5

# Prepare iptables for dnsmap.py
CHAIN=dnsmap
iptables -t nat -N "$CHAIN" 2>/dev/null || true
# Anchor rules are idempotent: repeated container restarts must not duplicate them.
iptables -t nat -C PREROUTING -d "${AZ_SUBNET}" -j "$CHAIN" 2>/dev/null || \
    iptables -t nat -A PREROUTING -d "${AZ_SUBNET}" -j "$CHAIN"
iptables -t nat -C OUTPUT -d "${AZ_SUBNET}" -j "$CHAIN" 2>/dev/null || \
    iptables -t nat -A OUTPUT -d "${AZ_SUBNET}" -j "$CHAIN"
# Prepare iptables for IPv6 egress mappings (separate chain to avoid flush conflicts)
IPV6_CHAIN=dnsmap-ipv6
iptables -t nat -N "$IPV6_CHAIN" 2>/dev/null || true
# Flush mapping rules accumulated by previous runs before restoring the saved
# set. dnsmap reloads mappings from iptables immediately after start, so
# existing mappings are preserved and duplicates cannot accumulate.
iptables -t nat -F "$IPV6_CHAIN" 2>/dev/null || true
iptables -t nat -C PREROUTING -d "${AZ_SUBNET}" -j "$IPV6_CHAIN" 2>/dev/null || \
    iptables -t nat -A PREROUTING -d "${AZ_SUBNET}" -j "$IPV6_CHAIN"
iptables -t nat -C OUTPUT -d "${AZ_SUBNET}" -j "$IPV6_CHAIN" 2>/dev/null || \
    iptables -t nat -A OUTPUT -d "${AZ_SUBNET}" -j "$IPV6_CHAIN"
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
      grep -E "^-A $IPV6_CHAIN " "$IPTABLES_SAVE" || true
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

function save_iptables () {
    [ "$IPTABLES_SAVE_DISABLED" = "1" ] && return 0
    echo "saving iptables..."
    {
      iptables-save -t "nat" | grep -E "^-A $CHAIN "
      iptables-save -t "nat" | grep -E "^-A $IPV6_CHAIN "
    } > /tmp/iptables.rules && mv -f /tmp/iptables.rules "$IPTABLES_SAVE" && echo "iptables saved"
}

ZAPRET_STARTED=0
function stop_services () {
    trap - EXIT HUP INT QUIT PIPE TERM
    cleanup_doall_owner
    if [ "$ZAPRET_STARTED" = "1" ]; then
        /opt/zapret2/init.d/sysv/zapret2 stop || true
    fi
    save_iptables || true
}

trap stop_services EXIT HUP INT QUIT PIPE TERM

if [ "$ZAPRET_ENABLED" = "1" ]; then
    if [ -s "$ZAPRET_CONFIG" ]; then
        # Regenerate only known, unmodified bundled configs; keep custom rules.
        case "$(md5sum < "$ZAPRET_CONFIG" | awk '{print $1}')" in
            5f71da81191ae869d849ed4f11576c56|0c7a96834a14bde6258d1dc283787b4e|c1c4af3a708375336a0c2af953d4d3c6)
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
    ZAPRET_STARTED=1
fi

routes &

if [ "$WARP_ENABLED" = "1" ]; then
    mkdir -p /dev/net /run/dbus /var/lib/cloudflare-warp
    [ -e /dev/net/tun ] || mknod /dev/net/tun c 10 200
    rm -f /run/dbus/pid
    dbus-daemon --config-file=/usr/share/dbus-1/system.conf
    warp-svc --accept-tos &

    timeout 30 bash -c 'until warp-cli --accept-tos status >/dev/null 2>&1; do sleep 1; done'

    [ -f /var/lib/cloudflare-warp/reg.json ] || warp-cli --accept-tos registration new
    while read -r nameserver; do
        warp-cli --accept-tos tunnel ip add "$nameserver" || true
    done < <(awk '$1 == "nameserver" {print $2}' /etc/resolv.conf)
    warp-cli --accept-tos tunnel protocol set MASQUE
    warp-cli --accept-tos mode tunnel_only
    warp-cli --accept-tos connect
    timeout 30 bash -c 'until warp-cli --accept-tos status | grep -q Connected; do sleep 1; done'

    iptables -t nat -A POSTROUTING -o CloudflareWARP -j MASQUERADE
    iptables -t mangle -A FORWARD -o CloudflareWARP -p tcp --tcp-flags SYN,RST SYN -j TCPMSS --clamp-mss-to-pmtu
fi

if ! timeout --kill-after=5s 5m /usr/bin/doall; then
    if generated_lists_available; then
        echo 'doall failed during startup, continuing with existing lists'
    else
        echo 'doall failed during startup and no generated lists are available'
        exit 1
    fi
fi

# Initialize IPv6 egress support
# Supports both Singlebox (az-local only) and Swarm (az-local + az-world)
# Each exit node has independent configuration: domain file, mapping file, translator port
DNSMAP_EXTRA_ARGS=""

if [ "$CLIENT" = "az-local" ]; then
    IPV6_EGRESS_ENABLED_VAR="IPV6_EGRESS_LOCAL_ENABLED"
    IPV6_EGRESS_FILE_VAR="IPV6_EGRESS_LOCAL_FILE"
    IPV6_EGRESS_MODE_VAR="IPV6_EGRESS_LOCAL_MODE"
    IPV6_EGRESS_RESERVE_VAR="IPV6_EGRESS_LOCAL_RESERVE_COUNT"
    IPV6_TRANSLATOR_PORT_VAR="IPV6_TRANSLATOR_LOCAL_PORT"
    IPV6_EGRESS_CLIENT_ID="az-ipv6-local"
    DEFAULT_IPV6_EGRESS_FILE="/root/antizapret/config/custom/ipv6-egress-domains-local.txt"
    DEFAULT_IPV6_MAPPING_FILE="/root/antizapret/result/ipv6-egress-mappings-local.txt"
elif [ "$CLIENT" = "az-world" ]; then
    IPV6_EGRESS_ENABLED_VAR="IPV6_EGRESS_WORLD_ENABLED"
    IPV6_EGRESS_FILE_VAR="IPV6_EGRESS_WORLD_FILE"
    IPV6_EGRESS_MODE_VAR="IPV6_EGRESS_WORLD_MODE"
    IPV6_EGRESS_RESERVE_VAR="IPV6_EGRESS_WORLD_RESERVE_COUNT"
    IPV6_TRANSLATOR_PORT_VAR="IPV6_TRANSLATOR_WORLD_PORT"
    IPV6_EGRESS_CLIENT_ID="az-ipv6-world"
    DEFAULT_IPV6_EGRESS_FILE="/root/antizapret/config/custom/ipv6-egress-domains-world.txt"
    DEFAULT_IPV6_MAPPING_FILE="/root/antizapret/result/ipv6-egress-mappings-world.txt"
else
    IPV6_EGRESS_ENABLED_VAR=""
    IPV6_EGRESS_FILE_VAR=""
    IPV6_EGRESS_MODE_VAR=""
    IPV6_EGRESS_RESERVE_VAR=""
    IPV6_TRANSLATOR_PORT_VAR=""
    IPV6_EGRESS_CLIENT_ID=""
    DEFAULT_IPV6_EGRESS_FILE=""
    DEFAULT_IPV6_MAPPING_FILE=""
fi

if [ -n "$IPV6_EGRESS_ENABLED_VAR" ]; then
    IPV6_EGRESS_ENABLED="${!IPV6_EGRESS_ENABLED_VAR:-0}"
    IPV6_EGRESS_FILE="${!IPV6_EGRESS_FILE_VAR:-$DEFAULT_IPV6_EGRESS_FILE}"
    IPV6_EGRESS_MODE="${!IPV6_EGRESS_MODE_VAR:-adguard_only}"
    IPV6_EGRESS_RESERVE_COUNT="${!IPV6_EGRESS_RESERVE_VAR:-100}"
    IPV6_TRANSLATOR_PORT="${!IPV6_TRANSLATOR_PORT_VAR:-9000}"

    if [ "$IPV6_EGRESS_ENABLED" = "1" ] && [ -n "$IPV6_EGRESS_FILE" ]; then
        # Ensure the domains file exists
        [ ! -f "$IPV6_EGRESS_FILE" ] && touch "$IPV6_EGRESS_FILE"
        # Ensure the mapping file exists for the translator
        [ ! -f "$DEFAULT_IPV6_MAPPING_FILE" ] && touch "$DEFAULT_IPV6_MAPPING_FILE"
        # Start the IPv4→IPv6 translator service
        postrun "IPV6_MAPPING_FILE=$DEFAULT_IPV6_MAPPING_FILE IPV6_TRANSLATOR_PORT=$IPV6_TRANSLATOR_PORT python3 /root/antizapret/ipv6_translator.py"
        echo "IPv6 egress translator started for $CLIENT on port $IPV6_TRANSLATOR_PORT"
        DNSMAP_EXTRA_ARGS="--ipv6-egress-file $IPV6_EGRESS_FILE --ipv6-translator-port $IPV6_TRANSLATOR_PORT --ipv6-egress-client-id $IPV6_EGRESS_CLIENT_ID --ipv6-egress-enabled --ipv6-mapping-file $DEFAULT_IPV6_MAPPING_FILE --ipv6-egress-mode $IPV6_EGRESS_MODE --ipv6-egress-reserve-count $IPV6_EGRESS_RESERVE_COUNT"
    else
        echo "IPv6 egress disabled for $CLIENT"
    fi
fi

postrun 'while true; do /opt/api/app; done'
postrun 'while true; do sleep 6h; timeout --kill-after=5s 10m /usr/bin/doall; done'
postrun 'while true; do /usr/bin/iperf3 -s -1; done'

/usr/bin/dnsmap -a 0.0.0.0 --iprange "$AZ_SUBNET" --asn-file "$ASN_FILES" $DNSMAP_EXTRA_ARGS
