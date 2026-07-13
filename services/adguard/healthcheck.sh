#!/usr/bin/env bash
set -e

INIT_FILE="/.inited"
[ ! -f "$INIT_FILE" ] && exit 0;

ADGUARDHOME_USERNAME=${ADGUARDHOME_USERNAME:-"admin"}
ADGUARDHOME_PORT=${ADGUARDHOME_PORT:-"3000"}

AUTH=$(echo -n "$ADGUARDHOME_USERNAME:$ADGUARDHOME_PASSWORD" | base64)

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

CONFIG_LOCAL=$(curl -s "http://az-local.antizapret/config-md5/" || echo "")
NEW_MD5="$CONFIG_LOCAL"
NEW_WORLD=''
if [ "$AZ_WORLD_ENABLED" = "1" ]; then
    CONFIG_WORLD=$(curl -s "http://az-world.antizapret/config-md5/" || echo "")
    NEW_MD5="$CONFIG_LOCAL $CONFIG_WORLD"
    NEW_WORLD=$(resolve 'az-world' '')
fi
OLD_MD5=$(cat /.config_md5 2>/dev/null || echo "")

CLIENTS=$(curl -s -X GET "http://127.0.0.1:$ADGUARDHOME_PORT/control/clients" -H "Authorization: Basic $AUTH")
[[ "$CLIENTS" == 404* ]] && echo 'Adguard not ready' && exit 0;

if [ "$NEW_MD5" != "$OLD_MD5" ]; then
    echo "Config files changed"

    curl -fsS "http://127.0.0.1:$ADGUARDHOME_PORT/control/filtering/refresh" -X 'POST' -H 'Content-Type: application/json' -H "Authorization: Basic $AUTH"  --data-raw '{"whitelist":false}' &
    FILTERS_REFRESH_PID=$!
    curl -fsS "http://127.0.0.1:$ADGUARDHOME_PORT/control/filtering/refresh" -X 'POST' -H 'Content-Type: application/json' -H "Authorization: Basic $AUTH"  --data-raw '{"whitelist":true}' &
    WHITELIST_REFRESH_PID=$!

    REFRESH_FAILED=0
    wait "$FILTERS_REFRESH_PID" || REFRESH_FAILED=1
    wait "$WHITELIST_REFRESH_PID" || REFRESH_FAILED=1
    if [ "$REFRESH_FAILED" = "1" ]; then
        echo "Failed to refresh AdGuard filters" >&2
        exit 1
    fi

    curl -fsS "http://127.0.0.1:$ADGUARDHOME_PORT/control/cache_clear" -X 'POST' -H "Authorization: Basic $AUTH"
    printf '%s\n' "$NEW_MD5" > /.config_md5
fi

update_client() {
    client_name=$1
    new_ip=$2
    client_id=${3:-}
    client_updated=0

    [ -z "$new_ip" ] && return

    FULL_CLIENT=$(echo "$CLIENTS" | jq --arg name "$client_name" '.clients[] | select(.name==$name)' || echo "error response: $CLIENTS")
    if [ -n "$FULL_CLIENT" ] && [ "$FULL_CLIENT" != "null" ]; then
        CURRENT_IDS=$(echo "$FULL_CLIENT" | jq -c '.ids // []')
        DESIRED_IDS=$(jq -nc --arg id "$client_id" --arg ip "$new_ip" '[$id, $ip] | map(select(. != ""))')
        CURRENT_IDS_NORMALIZED=$(echo "$CURRENT_IDS" | jq -c 'sort | unique')
        DESIRED_IDS_NORMALIZED=$(echo "$DESIRED_IDS" | jq -c 'sort | unique')

        if [ "$CURRENT_IDS_NORMALIZED" != "$DESIRED_IDS_NORMALIZED" ]; then
            UPDATED_CLIENT=$(echo "$FULL_CLIENT" | jq --argjson ids "$DESIRED_IDS" '.ids = $ids')
            UPDATE_BODY=$(printf '{"name":"%s","data":%s}' "$client_name" "$(echo "$UPDATED_CLIENT" | jq -c .)")
            echo "Updating $client_name ids to $DESIRED_IDS"
            curl -s -X POST "http://127.0.0.1:$ADGUARDHOME_PORT/control/clients/update" -H 'Content-Type: application/json' -H "Authorization: Basic $AUTH" --data "$UPDATE_BODY"
            client_updated=1
        fi
    fi

    if [ "$client_updated" = "1" ]; then
        echo "Reset adguard DNS cache"
        curl -s "http://127.0.0.1:$ADGUARDHOME_PORT/control/cache_clear" -X 'POST' -H "Authorization: Basic $AUTH"
    fi
}

NEW_LOCAL=$(resolve 'az-local' '')
NEW_COREDNS=$(resolve 'coredns' '')

update_client "az-local" "$NEW_LOCAL" "az-local"
update_client "az-world" "$NEW_WORLD" "az-world"
update_client "coredns" "$NEW_COREDNS"
