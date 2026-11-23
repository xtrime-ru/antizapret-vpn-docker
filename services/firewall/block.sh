#!/bin/bash
set -euo pipefail

action=${1:-apply}
v4=az_firewall_v4
v6=az_firewall_v6
next_v4=${v4}_new
next_v6=${v6}_new

rule() {
    local bin=$1 insert=$2 status=0
    shift 2
    "$bin" -w 2 -C DOCKER-USER "$@" 2>/dev/null || status=$?
    case "$action:$status" in
        clear:0) "$bin" -w 2 -D DOCKER-USER "$@" ;;
        apply:1) "$bin" -w 2 "$insert" DOCKER-USER "$@" ;;
        *:0|clear:1) return 0 ;;
        *) return "$status" ;;
    esac
}

rules() {
    local bin=$1 set=$2
    local established=(-m conntrack --ctstate ESTABLISHED,RELATED)
    # Respect an existing untagged ACCEPT rule. Cleanup only deletes our own.
    if [[ "$action" == clear ]] || ! "$bin" -w 2 -C DOCKER-USER "${established[@]}" -j ACCEPT 2>/dev/null; then
        rule "$bin" -I "${established[@]}" -m comment --comment antizapret-firewall -j ACCEPT
    fi
    rule "$bin" -A -m set --match-set "$set" src -j DROP
}

discard_staged_sets() {
    ipset destroy "$next_v4" 2>/dev/null || true
    ipset destroy "$next_v6" 2>/dev/null || true
}

case "$action" in
    clear)
        rules iptables "$v4"
        rules ip6tables "$v6"
        for set in "$v4" "$v6"; do
            if ipset list -terse "$set" >/dev/null 2>&1; then
                ipset destroy "$set"
            fi
        done
        discard_staged_sets
        exit 0
        ;;
    check)
        ipset list -terse "$v4" >/dev/null
        ipset list -terse "$v6" >/dev/null
        rules iptables "$v4"
        rules ip6tables "$v6"
        exit 0
        ;;
    apply) ;;
    *) echo "Unknown firewall action: $action" >&2; exit 2 ;;
esac

load_set() {
    local set=$1 family=$2 file=$3 subnet added=0
    ipset create "$set" hash:net family "$family" hashsize 4096 maxelem 200000
    while IFS= read -r subnet || [[ -n "$subnet" ]]; do
        [[ "$subnet" =~ ^[[:space:]]*$ ]] && continue
        ipset add "$set" "$subnet"
        added=1
    done < "$file"
    if (( ! added )); then
        echo "Firewall list has no entries: $file" >&2
        return 1
    fi
}

# refresh.sh holds the node-wide lock while creating and swapping these sets.
discard_staged_sets
trap discard_staged_sets EXIT
load_set "$next_v4" inet "$V4_FILE"
load_set "$next_v6" inet6 "$V6_FILE"
ipset create "$v4" hash:net family inet hashsize 4096 maxelem 200000 -exist
ipset create "$v6" hash:net family inet6 hashsize 4096 maxelem 200000 -exist
rules iptables "$v4"
rules ip6tables "$v6"
ipset swap "$next_v4" "$v4"
if ! ipset swap "$next_v6" "$v6"; then
    ipset swap "$next_v4" "$v4"
    exit 1
fi
