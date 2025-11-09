#!/usr/bin/env bash

cp -n /root/AdGuardHome.yaml /opt/adguardhome/conf/AdGuardHome.yaml

( cat /root/antizapret/result/* /root/antizapret/config/custom/* | md5sum ) > /.config_md5

ADGUARDHOME_PORT=${ADGUARDHOME_PORT:-"3000"}
ADGUARDHOME_USERNAME=${ADGUARDHOME_USERNAME:-"admin"}
if [[ -n $ADGUARDHOME_PASSWORD ]]; then
    ADGUARDHOME_PASSWORD_HASH=$(htpasswd -B -C 10 -n -b "$ADGUARDHOME_USERNAME" "$ADGUARDHOME_PASSWORD")
    ADGUARDHOME_PASSWORD_HASH=${ADGUARDHOME_PASSWORD_HASH#*:}
fi

AZ_LOCAL_HOST=$(dig +short az-local)
AZ_WORLD_HOST=$(dig +short az-world)
COREDNS_HOST=$(dig +short coredns)
while [ -z "${AZ_LOCAL_HOST}" ] || [ -z "${AZ_WORLD_HOST}" ] || [ -z "$COREDNS_HOST" ]; do
    echo "No route to antizapret container. Retrying..."
    AZ_LOCAL_HOST=$(dig +short az-local)
    AZ_WORLD_HOST=$(dig +short az-world)
    COREDNS_HOST=$(dig +short coredns)
    sleep 1;
done;

yq -i '
    .http.address="0.0.0.0:'$ADGUARDHOME_PORT'" |
    .users[0].name="'$ADGUARDHOME_USERNAME'" |
    .users[0].password="'$ADGUARDHOME_PASSWORD_HASH'" |
    .clients.persistent[0].ids=["'$AZ_LOCAL_HOST'"] |
    .clients.persistent[1].ids=["'$AZ_WORLD_HOST'"] |
    .clients.persistent[2].ids=["'$COREDNS_HOST'"]
    ' /opt/adguardhome/conf/AdGuardHome.yaml

while true; do /root/routes.sh; sleep 60; done &

exec /opt/adguardhome/AdGuardHome "$@"