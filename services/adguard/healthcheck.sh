#!/usr/bin/env sh
set -e

ADGUARDHOME_USERNAME=${ADGUARDHOME_USERNAME:-"admin"}
ADGUARDHOME_PORT=${ADGUARDHOME_PORT:-"3000"}

AUTH=$(echo -n "$ADGUARDHOME_USERNAME:$ADGUARDHOME_PASSWORD" | base64)

CONFIG_FILES="/root/antizapret/result/* /root/antizapret/config/custom/*"
NEW_MD5=$(cat $CONFIG_FILES 2>/dev/null | md5sum | cut -d' ' -f1)
OLD_MD5=$(cat /.config_md5 2>/dev/null || echo "")

if [ "$NEW_MD5" != "$OLD_MD5" ]; then
    echo "Config files changed"

    curl -s "http://127.0.0.1:$ADGUARDHOME_PORT/control/filtering/refresh" -X 'POST' -H 'Content-Type: application/json' -H "Authorization: Basic $AUTH"  --data-raw '{"whitelist":false}' &
    curl -s "http://127.0.0.1:$ADGUARDHOME_PORT/control/filtering/refresh" -X 'POST' -H 'Content-Type: application/json' -H "Authorization: Basic $AUTH"  --data-raw '{"whitelist":true}' &
    echo "$NEW_MD5" > /.config_md5
    wait
fi

CLIENTS=$(curl -s -X GET "http://127.0.0.1:$ADGUARDHOME_PORT/control/clients" -H "Authorization: Basic $AUTH")

update_client() {
    client_name=$1
    new_ip=$2
    if [ -n "$new_ip" ]; then
        FULL_CLIENT=$(echo "$CLIENTS" | jq ".clients[] | select(.name==\"$client_name\")")
        if [ "$FULL_CLIENT" != "null" ]; then
            CURRENT_IP=$(echo "$FULL_CLIENT" | jq -r '.ids[0] // empty')
            if [ "$CURRENT_IP" != "$new_ip" ]; then
                UPDATED_CLIENT=$(echo "$FULL_CLIENT" | jq --arg ip "$new_ip" '.ids = [$ip]')
                UPDATE_BODY=$(printf '{"name":"%s","data":%s}' "$client_name" "$(echo "$UPDATED_CLIENT" | jq -c .)")
                echo "Updating $client_name to $new_ip"
                curl -s -X POST "http://127.0.0.1:$ADGUARDHOME_PORT/control/clients/update" -H 'Content-Type: application/json' -H "Authorization: Basic $AUTH" --data "$UPDATE_BODY"
            fi
        fi
    fi
}

NEW_LOCAL=$(dig +short az-local | head -n1)
NEW_WORLD=$(dig +short az-world | head -n1)
NEW_COREDNS=$(dig +short coredns | head -n1)

update_client "az-local" "$NEW_LOCAL"
update_client "az-world" "$NEW_WORLD"
update_client "coredns" "$NEW_COREDNS"
