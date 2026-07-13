#!/usr/bin/env bash

INIT_FILE="/.inited"
rm -f "$INIT_FILE"

cp -n /root/AdGuardHome.yaml /opt/adguardhome/conf/AdGuardHome.yaml

ADGUARDHOME_PORT=${ADGUARDHOME_PORT:-"3000"}
ADGUARDHOME_USERNAME=${ADGUARDHOME_USERNAME:-"admin"}
if [[ -n $ADGUARDHOME_PASSWORD ]]; then
    ADGUARDHOME_PASSWORD_HASH=$(htpasswd -B -C 10 -n -b "$ADGUARDHOME_USERNAME" "$ADGUARDHOME_PASSWORD")
    ADGUARDHOME_PASSWORD_HASH=${ADGUARDHOME_PASSWORD_HASH#*:}
fi

ADGUARD_ADDRESS=$(echo "$ROUTES" | grep -E 'adguard:' | head -n1 | cut -d: -f2 | tr -d '[:space:];')
if [[ ! "$ADGUARD_ADDRESS" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]]; then
    echo "Error: ROUTES must contain adguard:<ipv4-address>;" >&2
    exit 1
fi

iptables -t nat -A PREROUTING -d "$ADGUARD_ADDRESS" -j DNAT --to-destination 127.0.0.1

routes &

function resolve () {
    # $1 domain/ip address, $2 fallback ip address
    res="$(getent hosts "$1" | head -n1 | awk '{print $1}')"
    if [[ "$res" =~ ^[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}$ ]]; then
        echo "$res"
    else
        echo "$2"
    fi
}

if [ "$AZ_WORLD_ENABLED" = "1" ]; then
    WAITING_MESSAGE="Waiting for az-local and az-world containers to register in DNS..."
else
    WAITING_MESSAGE="Waiting for az-local container to register in DNS..."
fi

while :; do
    AZ_LOCAL_HOST=$(resolve az-local '')
    AZ_WORLD_HOST=$(resolve az-world '')
    COREDNS_HOST=$(resolve coredns '169.0.0.3')
    if [ -n "${AZ_LOCAL_HOST}" ] && [ -n "${COREDNS_HOST}" ] && { [ "$AZ_WORLD_ENABLED" != "1" ] || [ -n "${AZ_WORLD_HOST}" ]; }; then
        break
    fi
    sleep 1;
    echo "$WAITING_MESSAGE"
done;

CONFIG_LOCAL=$(curl -s "http://az-local.antizapret/config-md5/" || echo "")
CONFIG_MD5="$CONFIG_LOCAL"
AZ_WORLD_CLIENT_IDS='["az-world"]'
if [ "$AZ_WORLD_ENABLED" = "1" ]; then
    CONFIG_WORLD=$(curl -s "http://az-world.antizapret/config-md5/" || echo "")
    CONFIG_MD5="$CONFIG_LOCAL $CONFIG_WORLD"
    AZ_WORLD_CLIENT_IDS='["az-world", "'$AZ_WORLD_HOST'"]'
fi
echo "$CONFIG_MD5" > /.config_md5

yq -i '
    .http.address="0.0.0.0:'$ADGUARDHOME_PORT'" |
    .http.doh.insecure_enabled=true |
    .dns.use_private_ptr_resolvers=false |
    .dns.local_ptr_upstreams=[] |
    .users[0].name="'$ADGUARDHOME_USERNAME'" |
    .users[0].password="'$ADGUARDHOME_PASSWORD_HASH'" |
    (.clients.persistent[] | select(.name == "az-local") | .ids) = ["az-local", "'$AZ_LOCAL_HOST'"] |
    (.clients.persistent[] | select(.name == "az-world") | .ids) = '$AZ_WORLD_CLIENT_IDS' |
    (.clients.persistent[] | select(.name == "coredns") | .ids) = ["'$COREDNS_HOST'"] |
    .clients.persistent = (
      [.clients.persistent[] | select(.name != "az-resolver")] + [{
        "name": "az-resolver",
        "ids": ["az-resolver"],
        "tags": [],
        "upstreams": ["1.1.1.1", "8.8.8.8", "8.8.4.4", "9.9.9.11", "149.112.112.11"],
        "use_global_settings": true,
        "use_global_blocked_services": true
      }]
    )
    ' /opt/adguardhome/conf/AdGuardHome.yaml

sed -i 's/antizapret-vpn-docker\/v5/antizapret-vpn-docker\/v6/g' /opt/adguardhome/conf/AdGuardHome.yaml

touch "$INIT_FILE"
exec /opt/adguardhome/AdGuardHome "$@"
