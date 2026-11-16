#!/usr/bin/env bash
set -eo pipefail

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
    local output address
    output=$(timeout 2 getent hosts "$1.antizapret") || true
    read -r address _ <<< "$output" || true
    if [[ "$address" =~ ^[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}$ ]]; then
        printf '%s\n' "$address"
    else
        printf '%s\n' "$2"
    fi
}

NEW_WORLD=''
if [ "$AZ_WORLD_ENABLED" = "1" ]; then
    NEW_WORLD=$(resolve 'az-world' '')
fi

NEW_LOCAL=$(resolve 'az-local' '')
NEW_COREDNS=$(resolve 'coredns' '')

# Repair DNS caches before contacting dependency HTTP endpoints.
reload_dns_on_dependency_change "$NEW_LOCAL" "$NEW_WORLD" "$NEW_COREDNS"

updates=$(adguard_api /control/clients | jq -c \
    --arg local "$NEW_LOCAL" --arg world "$NEW_WORLD" --arg core "$NEW_COREDNS" '
    if (.clients | type) != "array" then error("invalid clients response") else .clients end as $clients |
    [ {name:"az-local", ip:$local}, {name:"az-world", ip:$world}, {name:"coredns", ip:$core} ] as $desired |
    [ $desired[] as $d | $clients[] | select(.name == $d.name) |
      ([$d.name, $d.ip] | map(select(. != ""))) as $ids |
      select((.ids | sort) != ($ids | sort)) |
      {client: ., ids: $ids} ] as $changes |
    # Release old addresses first so another client can claim a recycled IP.
    ($changes[] | select(.client.ids != [.client.name]) |
        {name:.client.name, data:(.client | .ids = [.name])}),
    ($changes[] | select(.ids | length > 1) |
        {name:.client.name, data:(.client + {ids:.ids})})')
# Remember dirty caches across partial client updates or failed cache_clear calls.
if [[ -n "$updates" ]]; then
    touch /dev/shm/.clients_cache_dirty
    while IFS= read -r body; do
        adguard_api /control/clients/update -X POST -H 'Content-Type: application/json' --data "$body"
    done <<< "$updates"
fi
if [[ -f /dev/shm/.clients_cache_dirty ]]; then
    adguard_api /control/cache_clear -X POST
    rm -f /dev/shm/.clients_cache_dirty
fi

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
