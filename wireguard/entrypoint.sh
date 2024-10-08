#!/usr/bin/env bash

if [ $(which dig | wc -l) -eq 0 ]; then
    apk add bind-tools
fi
if [ $(which curl | wc -l) -eq 0 ]; then
    apk add curl
fi
if [ -z "${WG_HOST}" ]; then
    export WG_HOST=$(curl -4 icanhazip.com)
fi
export AZ_HOST=$(dig +short antizapret-vpn)

ip route add 10.224.0.0/15 via $AZ_HOST

if [[ ${FORCE_FORWARD_DNS:-true} == true ]]; then
    dnsPorts=${FORCE_FORWARD_DNS_PORTS:-"53"}
    for dnsPort in $dnsPorts; do
        iptables -t nat -A PREROUTING -i wg0 -p udp -m udp --dport $dnsPort -j DNAT --to-destination $AZ_HOST
    done
fi

exec /usr/bin/dumb-init node server.js