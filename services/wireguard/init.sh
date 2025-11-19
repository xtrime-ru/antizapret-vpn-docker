#!/usr/bin/env bash

if [ -z "$WG_HOST" ]; then
    export WG_HOST=$(curl -4 icanhazip.com)
fi

export WG_DEFAULT_ADDRESS=${WG_DEFAULT_ADDRESS:-"10.1.166.x"}
export WG_DEVICE=${WG_DEVICE:-"eth0"}
export WG_PORT=${WG_PORT:-51820}
export AZ_LOCAL_SUBNET=${AZ_LOCAL_SUBNET:-"10.224.0.0/15"}
export AZ_WORLD_SUBNET=${AZ_WORLD_SUBNET:-"10.226.0.0/15"}

if [ -f "/opt/antizapret/result/ips.txt" ]; then
    cp -f /opt/antizapret/result/ips.txt /app/ips.txt
fi

export DOCKER_SUBNET=$(ip -4 addr show dev eth0 | awk '$1=="inet" {print $2; exit}')
if [ -z "$WG_ALLOWED_IPS" ]; then
    export WG_ALLOWED_IPS="${WG_DEFAULT_ADDRESS/"x"/"0"}/24,$AZ_LOCAL_SUBNET,$AZ_WORLD_SUBNET,$DOCKER_SUBNET"
    blocked_ranges=$(tr '\n' ',' < /app/ips.txt | sed 's/,$//g')
    if [ -n "${blocked_ranges}" ]; then
        export WG_ALLOWED_IPS="${WG_ALLOWED_IPS},${blocked_ranges}"
    fi
fi

/routes.sh --vpn

export WG_POST_UP=$(tr '\n' ' ' << EOF
iptables -t nat -N masq_not_local;
iptables -t nat -A POSTROUTING -s ${WG_DEFAULT_ADDRESS/"x"/"0"}/24 -o ${WG_DEVICE} -j masq_not_local;
iptables -t nat -A masq_not_local -d ${DOCKER_SUBNET} -j RETURN;
iptables -t nat -A masq_not_local -d ${AZ_LOCAL_SUBNET} -j RETURN;
iptables -t nat -A masq_not_local -d ${AZ_WORLD_SUBNET} -j RETURN;
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

if [ -n "$WIREGUARD_PASSWORD_HASH" ]; then
    PASSWORD_HASH="$WIREGUARD_PASSWORD_HASH"
else
    PASSWORD_HASH="$(wgpw "$WIREGUARD_PASSWORD" | sed "s/'//g" | sed 's/PASSWORD_HASH=//g')"
fi
export PASSWORD_HASH

exec /usr/bin/dumb-init node server.js