#!/usr/bin/env bash

if [ $(which dig | wc -l) -eq 0 ]; then
    apk add bind-tools
fi
if [ $(which curl | wc -l) -eq 0 ]; then
    apk add curl
fi

export WG_HOST=$(curl -4 icanhazip.com)

ip route add 10.0.0.0/8 via $(dig +short antizapret-vpn)
iptables -t nat -A OUTPUT -d 10.0.0.1/32 -j DNAT --to-destination $(dig +short antizapret-vpn)
iptables -t nat -A PREROUTING -d 10.0.0.1/32 -j DNAT --to-destination $(dig +short antizapret-vpn)
iptables -t nat -A POSTROUTING -d 10.0.0.1/32 -j MASQUERADE

exec /usr/bin/dumb-init node server.js