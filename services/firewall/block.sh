#!/bin/bash
set -ex


SET_V4="az_firewall_v4"
SET_V6="az_firewall_v6"

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

clear

if [ "$1" = "clear" ]; then
    # only clear
    exit;
fi

# --- Create new ipsets ---
ipset create "$SET_V4" hash:net family inet hashsize 4096 maxelem 200000
ipset create "$SET_V6" hash:net family inet6 hashsize 4096 maxelem 200000

# --- Add fresh rules ---
iptables -I DOCKER-USER -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
iptables  -A DOCKER-USER -m set --match-set "$SET_V4" src -j DROP
ip6tables -I DOCKER-USER -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
ip6tables -A DOCKER-USER -m set --match-set "$SET_V6" src -j DROP

# --- Populate IPv4 set ---
if [[ -f "$V4_FILE" ]]; then
    while read -r subnet; do
        [[ -z "$subnet" ]] && continue
        ipset add "$SET_V4" "$subnet"
    done < "$V4_FILE"
fi

# --- Populate IPv6 set ---
if [[ -f "$V6_FILE" ]]; then
    while read -r subnet; do
        [[ -z "$subnet" ]] && continue
        ipset add "$SET_V6" "$subnet"
    done < "$V6_FILE"
fi