#!/usr/bin/env bash

INIT_FILE="/.inited"
rm -f "$INIT_FILE"

cp -n /root/AdGuardHome.yaml /opt/adguardhome/conf/AdGuardHome.yaml

CONFIG_LOCAL=$(curl -s "http://az-local.antizapret/config-md5/" || echo "")
CONFIG_WORLD=$(curl -s "http://az-world.antizapret/config-md5/" || echo "")
echo "$CONFIG_LOCAL $CONFIG_WORLD" > /.config_md5

ADGUARDHOME_PORT=${ADGUARDHOME_PORT:-"3000"}
ADGUARDHOME_USERNAME=${ADGUARDHOME_USERNAME:-"admin"}
if [[ -n $ADGUARDHOME_PASSWORD ]]; then
    ADGUARDHOME_PASSWORD_HASH=$(htpasswd -B -C 10 -n -b "$ADGUARDHOME_USERNAME" "$ADGUARDHOME_PASSWORD")
    ADGUARDHOME_PASSWORD_HASH=${ADGUARDHOME_PASSWORD_HASH#*:}
fi


/root/routes.sh &

function resolve () {
    # $1 domain/ip address, $2 fallback ip address
    res="$(getent hosts "$1" | head -n1 | awk '{print $1}')"
    if [[ "$res" =~ ^[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}$ ]]; then
        echo "$res"
    else
        echo "$2"
    fi
}

while :; do
    AZ_LOCAL_HOST=$(resolve az-local '')
    AZ_WORLD_HOST=$(resolve az-world '')
    COREDNS_HOST=$(resolve coredns '169.0.0.3')
    [ -n "${AZ_LOCAL_HOST}" ] && [ -n "${AZ_WORLD_HOST}" ] && break
    sleep 1;
    echo "Waiting az-local/az-world and coredns containers to register in DNS..."
done;


yq -i '
    .http.address="0.0.0.0:'$ADGUARDHOME_PORT'" |
    .users[0].name="'$ADGUARDHOME_USERNAME'" |
    .users[0].password="'$ADGUARDHOME_PASSWORD_HASH'" |
    (.clients.persistent[] | select(.name == "az-local") | .ids) = ["'$AZ_LOCAL_HOST'"] |
    (.clients.persistent[] | select(.name == "az-world") | .ids) = ["'$AZ_WORLD_HOST'"] |
    (.clients.persistent[] | select(.name == "coredns") | .ids) = ["'$COREDNS_HOST'"]
    ' /opt/adguardhome/conf/AdGuardHome.yaml

SERVER_COUNTRY=$( (curl -s https://ipinfo.io | jq -r '.country') || echo 'RU' )
if [ "$SERVER_COUNTRY" = "RU" ]; then
  yq -i '
      .dns.edns_client_subnet.enabled=false
      ' /opt/adguardhome/conf/AdGuardHome.yaml
fi

sed -i 's/antizapret-vpn-docker\/v5/antizapret-vpn-docker\/v6/g' /opt/adguardhome/conf/AdGuardHome.yaml

touch "$INIT_FILE"
exec /opt/adguardhome/AdGuardHome "$@"