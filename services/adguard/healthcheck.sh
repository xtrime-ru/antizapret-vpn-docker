#!/usr/bin/env bash
set -e

INIT_FILE="/.inited"
[ ! -f "$INIT_FILE" ] && exit 0;

ADGUARDHOME_USERNAME=${ADGUARDHOME_USERNAME:-"admin"}
ADGUARDHOME_PORT=${ADGUARDHOME_PORT:-"3000"}

AUTH=$(echo -n "$ADGUARDHOME_USERNAME:$ADGUARDHOME_PASSWORD" | base64)

CONFIG_LOCAL=$(curl -s "http://az-local.antizapret/config-md5/" || echo "")
CONFIG_WORLD=$(curl -s "http://az-world.antizapret/config-md5/" || echo "")
NEW_MD5="$CONFIG_LOCAL $CONFIG_WORLD"
OLD_MD5=$(cat /.config_md5 2>/dev/null || echo "")

if [ "$NEW_MD5" != "$OLD_MD5" ]; then
    echo "Config files changed"

    curl -s "http://127.0.0.1:$ADGUARDHOME_PORT/control/filtering/refresh" -X 'POST' -H 'Content-Type: application/json' -H "Authorization: Basic $AUTH"  --data-raw '{"whitelist":false}' &
    curl -s "http://127.0.0.1:$ADGUARDHOME_PORT/control/filtering/refresh" -X 'POST' -H 'Content-Type: application/json' -H "Authorization: Basic $AUTH"  --data-raw '{"whitelist":true}' &
    echo "$NEW_MD5" > /.config_md5
    wait

    curl -s "http://127.0.0.1:$ADGUARDHOME_PORT/control/cache_clear" -X 'POST' -H "Authorization: Basic $AUTH"
fi

CLIENTS=$(curl -s -X GET "http://127.0.0.1:$ADGUARDHOME_PORT/control/clients" -H "Authorization: Basic $AUTH")
[[ "$CLIENTS" == 404* ]] && echo 'Adguard not ready' && exit 0;

update_client() {
    client_name=$1
    new_ip=$2
    client_updated=0
    if [ -n "$new_ip" ]; then
        FULL_CLIENT=$(echo "$CLIENTS" | jq ".clients[] | select(.name==\"$client_name\")" || echo "error response: $CLIENTS")
        if [ "$FULL_CLIENT" != "null" ]; then
            CURRENT_IP=$(echo "$FULL_CLIENT" | jq -r '.ids[0] // empty')
            if [ "$CURRENT_IP" != "$new_ip" ]; then
                UPDATED_CLIENT=$(echo "$FULL_CLIENT" | jq --arg ip "$new_ip" '.ids = [$ip]')
                UPDATE_BODY=$(printf '{"name":"%s","data":%s}' "$client_name" "$(echo "$UPDATED_CLIENT" | jq -c .)")
                echo "Updating $client_name to $new_ip"
                curl -s -X POST "http://127.0.0.1:$ADGUARDHOME_PORT/control/clients/update" -H 'Content-Type: application/json' -H "Authorization: Basic $AUTH" --data "$UPDATE_BODY"
                client_updated=1
            fi
        fi
    fi

    if [ "$client_updated" = "1" ]; then
        echo "Reset adguard DNS cache"
        curl -s "http://127.0.0.1:$ADGUARDHOME_PORT/control/cache_clear" -X 'POST' -H "Authorization: Basic $AUTH"
    fi
}

# resolve domain address to ip address
function resolve () {
    # $1 domain/ip address, $2 fallback ip address
    res="$(getent hosts "$1" | head -n1 | awk '{print $1}')"
    if [[ "$res" =~ ^[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}$ ]]; then
        echo "$res"
    else
        echo "$2"
    fi
}

NEW_LOCAL=$(resolve 'az-local' '')
NEW_WORLD=$(resolve 'az-world' '')
NEW_COREDNS=$(resolve 'coredns' '')

update_client "az-local" "$NEW_LOCAL"
update_client "az-world" "$NEW_WORLD"
update_client "coredns" "$NEW_COREDNS"
