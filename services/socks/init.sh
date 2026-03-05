#!/usr/bin/env bash
set -e

# Detect external network interfaces and inject `external:` lines into /etc/danted.conf
# - Override with SOCKS_EXTERNAL_IFACES (comma-separated list)
# - Otherwise auto-detect non-loopback interfaces with an IPv4 address
# - Fallback to eth0 when none are detected
generate_external_lines() {
    if [ -n "${SOCKS_EXTERNAL_IFACES:-}" ]; then
        IFS=',' read -r -a IFACES <<< "$SOCKS_EXTERNAL_IFACES"
    else
        IFACES=()
        for ifname in $(ls /sys/class/net/); do
            case "$ifname" in
                lo|veth*|docker*|br-*|virbr*|sit*|tun*|tap*|wg*) continue ;;
            esac
            # skip interfaces without an IPv4 address
            if ! ip -4 addr show dev "$ifname" 2>/dev/null | grep -q 'inet '; then
                continue
            fi
            IFACES+=("$ifname")
        done
    fi

    if [ "${#IFACES[@]}" -eq 0 ]; then
        IFACES=(eth0)
    fi

    # produce one `external: <iface>` line per interface and export for envsubst
    EXTERNAL_IFACES="$(printf 'external: %s\n' "${IFACES[@]}")"
    export EXTERNAL_IFACES

    envsubst < /danted.conf.template  > /etc/danted.conf
}

# create the user if credentials are supplied; otherwise disable authentication
if [ -n "${SOCKS_USERNAME:-}" ] && [ -n "${SOCKS_PASSWORD:-}" ]; then
    useradd -r -s /usr/sbin/nologin "$SOCKS_USERNAME" 2>/dev/null || true
    echo "$SOCKS_USERNAME:$SOCKS_PASSWORD" | chpasswd
    export SOCKS_METHOD="username"
else
    export SOCKS_METHOD="none"
fi

# inject external interface lines into the config
generate_external_lines

/routes.sh &
exec danted
