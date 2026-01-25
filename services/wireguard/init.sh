#!/usr/bin/env bash

if [ -z "$WG_HOST" ]; then
    export WG_HOST=$(curl -4 icanhazip.com)
fi

export WG_DEFAULT_ADDRESS=${WG_DEFAULT_ADDRESS:-"10.1.166.x"}
export WG_PORT=${WG_PORT:-51820}
export AZ_SUBNET=${AZ_SUBNET:-"14.16.0.0/14"}

CONFIG_FILES="/opt/antizapret/result/ips*"
cat $CONFIG_FILES 2>/dev/null | md5sum | cut -d' ' -f1 > /.config_md5

DOCKER_SUBNET="$(ipcalc "$(ip -4 addr show dev eth0 | awk '$1=="inet" {print $2; exit}')" | awk '/Network:/ {print $2}')"

if [ -z "$WG_ALLOWED_IPS" ]; then
    export WG_ALLOWED_IPS="${WG_DEFAULT_ADDRESS/"x"/"0"}/24,$AZ_SUBNET,$DOCKER_SUBNET"
    blocked_ranges=$( cat $CONFIG_FILES | grep -v '^[[:space:]]*$' | tr '\n' ',' | sed 's/,$//g')
    if [ -n "${blocked_ranges}" ]; then
        export WG_ALLOWED_IPS="${WG_ALLOWED_IPS},${blocked_ranges}"
    fi
fi

/routes.sh --vpn &

export WG_POST_UP=$(tr '\n' ' ' << EOF
iptables -t nat -N masq_not_local;
iptables -t nat -A POSTROUTING -s ${WG_DEFAULT_ADDRESS/"x"/"0"}/24 -j masq_not_local;
iptables -t nat -A masq_not_local -p icmp -d ${DOCKER_SUBNET} -j MASQUERADE;
iptables -t nat -A masq_not_local -d ${DOCKER_SUBNET} -j RETURN;
iptables -t nat -A masq_not_local -d ${AZ_SUBNET} -j RETURN;
iptables -t nat -A masq_not_local -j MASQUERADE;
iptables -A FORWARD -i wg0 -j ACCEPT;
iptables -A FORWARD -o wg0 -j ACCEPT;
EOF
)

export WG_POST_DOWN=$(tr '\n' ' ' << EOF
iptables -t nat -D POSTROUTING -s ${WG_DEFAULT_ADDRESS/"x"/"0"}/24 -j masq_not_local;
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