#!/usr/bin/env bash
set -e

INIT_FILE="/dev/shm/.inited"
[ ! -f "$INIT_FILE" ] && exit 0;

ADGUARDHOME_USERNAME=${ADGUARDHOME_USERNAME:-"admin"}
ADGUARDHOME_PORT=${ADGUARDHOME_PORT:-"3000"}

AUTH=$(echo -n "$ADGUARDHOME_USERNAME:$ADGUARDHOME_PASSWORD" | base64)

DNS_DEPENDENCY_STATE_FILE=/dev/shm/.dns_dependencies
source /root/dns-reload.sh

adguard_api() {
    local path=$1
    shift
    curl -fsS --connect-timeout 2 --max-time 15 \
        "http://127.0.0.1:$ADGUARDHOME_PORT$path" -H "Authorization: Basic $AUTH" "$@"
}

# resolve domain address to ip address
function resolve () {
    # $1 domain/ip address, $2 fallback ip address
    res="$(timeout 2 getent hosts "$1.antizapret" | head -n1 | awk '{print $1}')"
    if [[ "$res" =~ ^[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}$ ]]; then
        echo "$res"
    else
        echo "$2"
    fi
}

NEW_WORLD=''
if [ "$AZ_WORLD_ENABLED" = "1" ]; then
    NEW_WORLD=$(resolve 'az-world' '')
fi

CLIENTS=$(adguard_api /control/clients)
printf '%s' "$CLIENTS" | jq -e '.clients | type == "array"' >/dev/null

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
            adguard_api /control/clients/update -X POST -H 'Content-Type: application/json' --data "$UPDATE_BODY"
            client_updated=1
        fi
    fi

    if [ "$client_updated" = "1" ]; then
        echo "Reset adguard DNS cache"
        adguard_api /control/cache_clear -X POST
    fi
}

NEW_LOCAL=$(resolve 'az-local' '')
NEW_COREDNS=$(resolve 'coredns' '')

# Repair DNS caches before contacting dependency HTTP endpoints.
reload_dns_on_dependency_change "$NEW_LOCAL" "$NEW_WORLD" "$NEW_COREDNS"

update_client "az-local" "$NEW_LOCAL" "az-local"
update_client "az-world" "$NEW_WORLD" "az-world"
update_client "coredns" "$NEW_COREDNS" "coredns"

CONFIG_LOCAL=$(curl -fsS --connect-timeout 2 --max-time 5 "http://az-local.antizapret/config-md5/" || echo "")
[ -n "$CONFIG_LOCAL" ] || exit 1
NEW_MD5="$CONFIG_LOCAL"
if [ "$AZ_WORLD_ENABLED" = "1" ]; then
    CONFIG_WORLD=$(curl -fsS --connect-timeout 2 --max-time 5 "http://az-world.antizapret/config-md5/" || echo "")
    [ -n "$CONFIG_WORLD" ] || exit 1
    NEW_MD5="$CONFIG_LOCAL $CONFIG_WORLD"
fi
OLD_MD5=$(cat /dev/shm/.config_md5 2>/dev/null || echo "")

if [ "$NEW_MD5" != "$OLD_MD5" ]; then
    echo "Config files changed"

    # AdGuard permits only one filter update at a time.
    for whitelist in false true; do
        response=$(curl --connect-timeout 2 --max-time 60 -sS --write-out '\n%{http_code}' \
            "http://127.0.0.1:$ADGUARDHOME_PORT/control/filtering/refresh" \
            -X POST -H 'Content-Type: application/json' \
            -H "Authorization: Basic $AUTH" \
            --data-raw "{\"whitelist\":$whitelist}") || exit 1

        case "$response" in
            *$'\n'2??) ;;
            *"filters update procedure is already running"*$'\n'500)
                # Keep the old hash so the next maintenance run retries.
                echo "AdGuard filter refresh is already running; retry later"
                exit 1 ;;
            *)
                echo "Failed to refresh AdGuard filters: $response" >&2
                exit 1 ;;
        esac
    done

    adguard_api /control/cache_clear -X POST
    printf '%s\n' "$NEW_MD5" > /dev/shm/.config_md5
fi
