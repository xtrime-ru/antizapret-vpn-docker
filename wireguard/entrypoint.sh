#!/usr/bin/env bash

if [ $(which dig | wc -l) -eq 0 ]; then
    apk add bind-tools
fi
if [ $(which curl | wc -l) -eq 0 ]; then
    apk add curl
fi

if [ -z "$WG_HOST" ]; then
    export WG_HOST=$(curl -4 icanhazip.com)
fi

export WG_DEFAULT_ADDRESS=${WG_DEFAULT_ADDRESS:-"10.1.166.x"}

if [ -z "$WG_ALLOWED_IPS" ]; then
    export WG_ALLOWED_IPS="${WG_DEFAULT_ADDRESS/"x"/"0"}/24,10.224.0.0/15"
    if [ -f "/opt/antizapret/result/blocked-ranges.txt" ]; then
        blocked_ranges=`tr '\n' ',' < /opt/antizapret/result/blocked-ranges.txt | sed 's/,$//g'`
        if [ -z "${blocked_ranges}" ]; then
            export WG_ALLOWED_IPS="${WG_ALLOWED_IPS},${blocked_ranges}"
        fi
    fi
fi

export AZ_HOST=$(dig +short antizapret-vpn)
ip route add 10.224.0.0/15 via $AZ_HOST

if [[ ${FORCE_FORWARD_DNS:-true} == true ]]; then
    dnsPorts=${FORCE_FORWARD_DNS_PORTS:-"53"}
    for dnsPort in $dnsPorts; do
        iptables -t nat -A PREROUTING -p udp --dport $dnsPort -j DNAT --to-destination $AZ_HOST
    done
fi

exec /usr/bin/dumb-init node server.js