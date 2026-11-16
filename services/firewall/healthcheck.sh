#!/bin/bash
set -euo pipefail
ipset list -terse az_firewall_v4 >/dev/null
ipset list -terse az_firewall_v6 >/dev/null
iptables -C DOCKER-USER -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
iptables -C DOCKER-USER -m set --match-set az_firewall_v4 src -j DROP
ip6tables -C DOCKER-USER -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
ip6tables -C DOCKER-USER -m set --match-set az_firewall_v6 src -j DROP
