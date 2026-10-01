#!/bin/bash
set -ex


SET_V4="az_firewall_v4"
SET_V6="az_firewall_v6"
NEXT_V4="${SET_V4}_new_$$"
NEXT_V6="${SET_V6}_new_$$"

function cleanup() {
    ipset destroy "$NEXT_V4" 2>/dev/null || true
    ipset destroy "$NEXT_V6" 2>/dev/null || true
}

trap cleanup EXIT

function clear() {
    # --- Remove old iptables rules referencing the sets ---
    iptables  -D DOCKER-USER -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT 2>/dev/null || true
    iptables  -D DOCKER-USER -m set --match-set "$SET_V4" src -j DROP 2>/dev/null || true
    ip6tables  -D DOCKER-USER -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT 2>/dev/null || true
    ip6tables -D DOCKER-USER -m set --match-set "$SET_V6" src -j DROP 2>/dev/null || true

    # --- Delete old ipsets if they exist ---
    ipset destroy "$SET_V4" 2>/dev/null || true
    ipset destroy "$SET_V6" 2>/dev/null || true
}

if [ "${1:-}" = "clear" ]; then
    # only clear
    clear
    exit;
fi

# --- Keep active ipsets and populate temporary ones ---
ipset create "$SET_V4" hash:net family inet hashsize 4096 maxelem 200000 -exist
ipset create "$SET_V6" hash:net family inet6 hashsize 4096 maxelem 200000 -exist
ipset create "$NEXT_V4" hash:net family inet hashsize 4096 maxelem 200000
ipset create "$NEXT_V6" hash:net family inet6 hashsize 4096 maxelem 200000

# --- Populate IPv4 set ---
if [[ -f "$V4_FILE" ]]; then
    while read -r subnet; do
        [[ -z "$subnet" ]] && continue
        ipset add "$NEXT_V4" "$subnet"
    done < "$V4_FILE"
fi

# --- Populate IPv6 set ---
if [[ -f "$V6_FILE" ]]; then
    while read -r subnet; do
        [[ -z "$subnet" ]] && continue
        ipset add "$NEXT_V6" "$subnet"
    done < "$V6_FILE"
fi

# --- Atomically replace each active set after both lists are loaded ---
ipset swap "$NEXT_V4" "$SET_V4"
ipset swap "$NEXT_V6" "$SET_V6"

# --- Install rules once; updates keep referencing the active sets ---
iptables -C DOCKER-USER -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT 2>/dev/null || \
    iptables -I DOCKER-USER -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
iptables -C DOCKER-USER -m set --match-set "$SET_V4" src -j DROP 2>/dev/null || \
    iptables -A DOCKER-USER -m set --match-set "$SET_V4" src -j DROP
ip6tables -C DOCKER-USER -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT 2>/dev/null || \
    ip6tables -I DOCKER-USER -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
ip6tables -C DOCKER-USER -m set --match-set "$SET_V6" src -j DROP 2>/dev/null || \
    ip6tables -A DOCKER-USER -m set --match-set "$SET_V6" src -j DROP
