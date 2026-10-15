#!/bin/bash
set -euo pipefail

# Blocked sources are dropped in our own chain, hooked at the top of DOCKER-USER.
# Lists are refreshed by filling a staging ipset and swapping it with the live one,
# so a refresh never removes protection, and a bad list never replaces a good one.

CHAIN="AZ-FIREWALL"
HOOK=(-m comment --comment antizapret-firewall -j "$CHAIN")
SET_V4="az_firewall_v4"
SET_V6="az_firewall_v6"

# Serialize runs: a manual `block.sh apply` must not interleave with the periodic update.
exec 9>/run/antizapret-firewall.lock
flock 9

# Optional exceptions: "interface destination-IP tcp|udp port[,port...]" per line.
# Matched against the original destination before Docker DNAT (conntrack), so the
# published address/port is used, not the container's. A match skips the blocklist
# (RETURN), it does not ACCEPT: later Docker/UFW rules still apply.
# Prints iptables-restore lines prefixed with 4 or 6; fails on any invalid line.
exception_rules() {
    local file="${EXCEPTIONS_FILE:-}"
    [ -n "$file" ] || return 0
    awk -v chain="$CHAIN" '
        function valid_v4(value,    octets, i) {
            if (value !~ /^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$/) return 0
            split(value, octets, ".")
            for (i = 1; i <= 4; i++) if ((octets[i] + 0) > 255) return 0
            return 1
        }
        {
            sub(/#.*/, "")
            if (NF == 0) next
            error = ""
            family = valid_v4($2) ? 4 : ($2 ~ /^[0-9A-Fa-f:.]+$/ && $2 ~ /:/) ? 6 : 0
            count = split($4, ports, ",")
            if (NF != 4) error = "expected: interface address tcp|udp ports"
            else if ($1 !~ /^[A-Za-z0-9_.:-]+$/ || length($1) > 15) error = "invalid interface " $1
            else if (!family) error = "invalid address " $2 " (single IPv4/IPv6, no CIDR)"
            else if ($3 != "tcp" && $3 != "udp") error = "invalid protocol " $3
            else
                for (i = 1; i <= count; i++)
                    if (ports[i] !~ /^[0-9]+$/ || ports[i] + 0 < 1 || ports[i] + 0 > 65535) error = "invalid port " ports[i]
            if (error != "") {
                printf "%s:%d: %s\n", FILENAME, FNR, error > "/dev/stderr"
                failed = 1
                next
            }
            for (i = 1; i <= count; i++)
                printf "%d -A %s -i %s -p %s -m conntrack --ctdir ORIGINAL --ctorigdst %s --ctorigdstport %d -j RETURN\n",
                    family, chain, $1, $3, $2, ports[i]
        }
        END { exit failed }
    ' "$file"
}

# Rules created by the previous version directly in DOCKER-USER. It inserted one
# ESTABLISHED,RELATED ACCEPT per run and removed one per run, so remove one.
remove_legacy_rules() {
    local cmd="$1" setname="$2"
    "$cmd" -w -D DOCKER-USER -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT 2>/dev/null || true
    while "$cmd" -w -D DOCKER-USER -m set --match-set "$setname" src -j DROP 2>/dev/null; do :; done
}

clear() {
    local cmd setname
    for cmd in iptables ip6tables; do
        [ "$cmd" = iptables ] && setname="$SET_V4" || setname="$SET_V6"
        while "$cmd" -w -D DOCKER-USER "${HOOK[@]}" 2>/dev/null; do :; done
        "$cmd" -w -F "$CHAIN" 2>/dev/null || true
        "$cmd" -w -X "$CHAIN" 2>/dev/null || true
        remove_legacy_rules "$cmd" "$setname"
        ipset destroy "$setname" 2>/dev/null || true
        ipset destroy "${setname}_next" 2>/dev/null || true
    done
}

# Print valid networks of one family; report skipped lines on stderr.
valid_entries() {
    local family="$1" file="$2"
    awk -v family="$family" -v file="$file" '
        function valid_v4(value,    parts, octets, count, i) {
            if (value !~ /^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+(\/[0-9]+)?$/) return 0
            count = split(value, parts, "/")
            if (count == 2 && (parts[2] + 0) > 32) return 0
            split(parts[1], octets, ".")
            for (i = 1; i <= 4; i++) if ((octets[i] + 0) > 255) return 0
            return 1
        }
        function valid_v6(value,    parts, count) {
            if (value !~ /^[0-9A-Fa-f:.]+(\/[0-9]+)?$/ || value !~ /:/) return 0
            count = split(value, parts, "/")
            return !(count == 2 && (parts[2] + 0) > 128)
        }
        {
            sub(/#.*/, "")
            gsub(/^[ \t\r]+|[ \t\r]+$/, "")
            if ($0 == "") next
            if ((family == "inet" && valid_v4($0)) || (family == "inet6" && valid_v6($0))) print
            else skipped++
        }
        END { if (skipped) printf "Skipping %d invalid or non-%s lines in %s\n", skipped, family, file > "/dev/stderr" }
    ' "$file"
}

# Fill <set>_next from the file and swap it in. The live set is untouched on failure.
# Called as `refresh_set ... || status=1`, where set -e does not apply: every step
# that must not be skipped is checked explicitly.
refresh_set() {
    local setname="$1" family="$2" file="$3" entries
    entries=$(valid_entries "$family" "$file") || entries=""
    if [ -z "$entries" ]; then
        echo "No valid entries in $file; keeping the current $setname" >&2
        return 1
    fi
    ipset create "$setname" hash:net family "$family" hashsize 4096 maxelem 200000 -exist &&
        ipset create "${setname}_next" hash:net family "$family" hashsize 4096 maxelem 200000 -exist &&
        ipset flush "${setname}_next" || return 1
    if ! sed "s/^/add ${setname}_next /" <<< "$entries" | ipset restore -exist; then
        echo "Failed to load $file; keeping the current $setname" >&2
        ipset destroy "${setname}_next" 2>/dev/null || true
        return 1
    fi
    ipset swap "${setname}_next" "$setname" || return 1
    ipset destroy "${setname}_next" || true
    echo "$setname: $(wc -l <<< "$entries") networks"
}

# Replace our chain in one iptables-restore transaction and hook it once.
install_chain() {
    local cmd="$1" setname="$2" exceptions="$3"
    "${cmd}-restore" --wait 10 --noflush <<EOF
*filter
:$CHAIN - [0:0]
-A $CHAIN -m conntrack --ctstate ESTABLISHED,RELATED -j RETURN
${exceptions}
-A $CHAIN -m set --match-set $setname src -j DROP
COMMIT
EOF
    "$cmd" -w -C DOCKER-USER "${HOOK[@]}" 2>/dev/null || "$cmd" -w -I DOCKER-USER 1 "${HOOK[@]}"
    remove_legacy_rules "$cmd" "$setname"
}

if [ "${1:-apply}" = "clear" ]; then
    clear
    exit 0
fi

# An invalid exceptions file stops the update before anything changes.
if ! exceptions=$(exception_rules); then
    echo "Invalid EXCEPTIONS_FILE; keeping the current rules" >&2
    exit 1
fi
exceptions_v4=$(sed -n 's/^4 //p' <<< "$exceptions")
exceptions_v6=$(sed -n 's/^6 //p' <<< "$exceptions")

# Each family is updated independently: a bad list keeps its previous set and
# makes the run fail (the caller retries), while the other family still updates.
status=0
refresh_set "$SET_V4" inet "$V4_FILE" || status=1
refresh_set "$SET_V6" inet6 "$V6_FILE" || status=1
if ipset list -n "$SET_V4" >/dev/null 2>&1; then install_chain iptables "$SET_V4" "$exceptions_v4"; fi
if ipset list -n "$SET_V6" >/dev/null 2>&1; then install_chain ip6tables "$SET_V6" "$exceptions_v6"; fi
exit "$status"
