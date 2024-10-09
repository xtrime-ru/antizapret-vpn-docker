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

export DOCKER_SUBNET=${DOCKER_SUBNET:-"172.18.0.0/16"}
export ANTIZAPRET_SUBNET=${ANTIZAPRET_SUBNET:-"10.224.0.0/15"}

tr '\n' ' ' << EOF
iptables -t nat -N masq_not_local;
iptables -t nat -A POSTROUTING -s \${module.exports.WG_DEFAULT_ADDRESS.replace('x', '0')}/24 -o \${module.exports.WG_DEVICE} -j masq_not_local;
iptables -A INPUT -p udp -m udp --dport \${module.exports.WG_PORT} -j ACCEPT;
iptables -t nat -A masq_not_local -d \${module.exports.DOCKER_SUBNET} -j RETURN;
iptables -t nat -A masq_not_local -d \${module.exports.ANTIZAPRET_SUBNET} -j RETURN;
iptables -t nat -A masq_not_local -j MASQUERADE;
iptables -A FORWARD -i wg0 -j ACCEPT;
iptables -A FORWARD -o wg0 -j ACCEPT;
EOF


export AZ_HOST=$(dig +short antizapret-vpn)
ip route add 10.224.0.0/15 via $AZ_HOST

if [[ ${FORCE_FORWARD_DNS:-true} == true ]]; then
    dnsPorts=${FORCE_FORWARD_DNS_PORTS:-"53"}
    for dnsPort in $dnsPorts; do
        iptables -t nat -A PREROUTING -p udp --dport $dnsPort -j DNAT --to-destination $AZ_HOST
    done
fi

exec /usr/bin/dumb-init node server.js