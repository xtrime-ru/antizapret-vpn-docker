#!/usr/bin/env bash

cp -n /root/AdGuardHome.yaml /opt/adguardhome/conf/AdGuardHome.yaml

CONFIG_FILES="/root/antizapret/result/* /root/antizapret/config/custom/*"
cat $CONFIG_FILES 2>/dev/null | md5sum | cut -d' ' -f1 > /.config_md5

ADGUARDHOME_PORT=${ADGUARDHOME_PORT:-"3000"}
ADGUARDHOME_USERNAME=${ADGUARDHOME_USERNAME:-"admin"}
if [[ -n $ADGUARDHOME_PASSWORD ]]; then
    ADGUARDHOME_PASSWORD_HASH=$(htpasswd -B -C 10 -n -b "$ADGUARDHOME_USERNAME" "$ADGUARDHOME_PASSWORD")
    ADGUARDHOME_PASSWORD_HASH=${ADGUARDHOME_PASSWORD_HASH#*:}
fi


/root/routes.sh

function resolve () {
    # $1 domain/ip address, $2 fallback ip address
    res="$(dig +short "$1" | head -n1)"
    if [ -z "$res" ]; then
        echo "$2"
    else
        echo "$res"
    fi
}

AZ_LOCAL_HOST=$(resolve az-local '')
AZ_WORLD_HOST=$(resolve az-world '')
while [ -z "${AZ_LOCAL_HOST}" ] || [ -z "${AZ_WORLD_HOST}" ]; do
    echo "No route to antizapret container. Retrying..."
    AZ_LOCAL_HOST=$(resolve az-local '')
    AZ_WORLD_HOST=$(resolve az-world '')
    sleep 1;
done;

COREDNS_HOST=$(resolve coredns '169.0.0.3')

yq -i '
    .http.address="0.0.0.0:'$ADGUARDHOME_PORT'" |
    .users[0].name="'$ADGUARDHOME_USERNAME'" |
    .users[0].password="'$ADGUARDHOME_PASSWORD_HASH'" |
    .clients.persistent[0].ids=["'$AZ_LOCAL_HOST'"] |
    .clients.persistent[1].ids=["'$AZ_WORLD_HOST'"] |
    .clients.persistent[2].ids=["'$COREDNS_HOST'"]
    ' /opt/adguardhome/conf/AdGuardHome.yaml



exec /opt/adguardhome/AdGuardHome "$@"