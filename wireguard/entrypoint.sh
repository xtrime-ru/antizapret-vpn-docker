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
export WG_DEVICE=${WG_DEVICE:-"eth0"}
export WG_PORT=${WG_PORT:-51820}
export ANTIZAPRET_SUBNET=${ANTIZAPRET_SUBNET:-"10.224.0.0/15"}

if [ -f "/opt/antizapret/result/blocked-ranges-with-include.txt" ]; then
    cp -f /opt/antizapret/result/blocked-ranges-with-include.txt /app/blocked-ranges-with-include.txt
fi

if [ -z "$WG_ALLOWED_IPS" ]; then
    export WG_ALLOWED_IPS="${WG_DEFAULT_ADDRESS/"x"/"0"}/24,$ANTIZAPRET_SUBNET"
    blocked_ranges=`tr '\n' ',' < /app/blocked-ranges-with-include.txt | sed 's/,$//g'`
    if [ -n "${blocked_ranges}" ]; then
        export WG_ALLOWED_IPS="${WG_ALLOWED_IPS},${blocked_ranges}"
    fi
fi

export DOCKER_SUBNET=$(ip route | grep -oEh "^172.*/\d{1,2}")
export AZ_HOST=$(dig +short antizapret)

export WG_POST_UP=$(tr '\n' ' ' << EOF
iptables -t nat -N masq_not_local;
iptables -t nat -A POSTROUTING -s ${WG_DEFAULT_ADDRESS/"x"/"0"}/24 -o ${WG_DEVICE} -j masq_not_local;
iptables -t nat -A masq_not_local -d ${AZ_HOST} -j RETURN;
iptables -t nat -A masq_not_local -d ${ANTIZAPRET_SUBNET} -j RETURN;
iptables -t nat -A masq_not_local -j MASQUERADE;
iptables -A FORWARD -i wg0 -j ACCEPT;
iptables -A FORWARD -o wg0 -j ACCEPT;
EOF
)

export WG_POST_DOWN=$(tr '\n' ' ' << EOF
iptables -t nat -D POSTROUTING -s ${WG_DEFAULT_ADDRESS/"x"/"0"}/24 -o ${WG_DEVICE} -j masq_not_local;
iptables -t nat -F masq_not_local;
iptables -t nat -X masq_not_local;
iptables -D FORWARD -i wg0 -j ACCEPT;
iptables -D FORWARD -o wg0 -j ACCEPT;
EOF
)


ip route add $ANTIZAPRET_SUBNET via $AZ_HOST

if [[ ${FORCE_FORWARD_DNS:-true} == true ]]; then
    dnsPorts=${FORCE_FORWARD_DNS_PORTS:-"53"}
    for dnsPort in $dnsPorts; do
        iptables -t nat -A PREROUTING -p tcp --dport $dnsPort -j DNAT --to-destination $AZ_HOST
        iptables -t nat -A PREROUTING -p udp --dport $dnsPort -j DNAT --to-destination $AZ_HOST
    done
fi

if [ -n "$WIREGUARD_PASSWORD_HASH" ]; then
    PASSWORD_HASH="$WIREGUARD_PASSWORD_HASH"
else
    PASSWORD_HASH="$(wgpw "$WIREGUARD_PASSWORD" | sed "s/'//g" | sed 's/PASSWORD_HASH=//g')"
fi
export PASSWORD_HASH

exec /usr/bin/dumb-init node server.js
