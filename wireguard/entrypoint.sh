#!/usr/bin/env bash

if [ $(which dig | wc -l) -eq 0 ]; then
    apk add bind-tools
fi
if [ $(which curl | wc -l) -eq 0 ]; then
    apk add curl
fi

export WG_HOST=$(curl -4 icanhazip.com)
export AZ_HOST=$(dig +short antizapret-vpn)

ip route add 10.224.0.0/15 via $AZ_HOST
iptables -t nat -A OUTPUT -d 10.225.255.254/32 -j DNAT --to-destination $AZ_HOST
iptables -t nat -A PREROUTING -d 10.225.255.254/32 -j DNAT --to-destination $AZ_HOST
iptables -t nat -A POSTROUTING -d 10.225.255.254/32 -j MASQUERADE

exec /usr/bin/dumb-init node server.js