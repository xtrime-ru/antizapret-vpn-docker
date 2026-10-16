#!/usr/bin/env bash
set -e

# resolve domain address to ip address
function resolve () {
    # $1 domain/ip address, $2 fallback ip address
    res="$(timeout 3s getent ahostsv4 "$1" | awk 'NR == 1 {print $1}')"
    if [[ "$res" =~ ^[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}$ ]]; then
        echo "$res"
    else
        echo "$2"
    fi
}

AZ_LOCAL_HOST="$(resolve 'az-local.antizapret' '')"
AZ_WORLD_HOST="$(resolve 'az-world.antizapret' "$AZ_LOCAL_HOST")"
DNS_HOST="$(resolve 'adguard.antizapret' '')"

if [ -z "$AZ_LOCAL_HOST" ] || [ -z "$DNS_HOST" ]; then
    echo 'Required Docker DNS address unavailable; preserving Corefile' >&2
    # An existing configuration can continue serving during a discovery outage.
    [ -s /Corefile ] && exit 0
    exit 1
fi

if [ "$AZ_WORLD_HOST" = "$AZ_LOCAL_HOST" ]; then
    export AZ_FORWARD_HOSTS="$AZ_LOCAL_HOST $DNS_HOST"
else
    export AZ_FORWARD_HOSTS="$AZ_WORLD_HOST $AZ_LOCAL_HOST $DNS_HOST"
fi

candidate=$(mktemp /Corefile.XXXXXXXX)
trap 'rm -f "$candidate"' EXIT
envsubst < /root/Corefile.template > "$candidate"
if ! cmp -s "$candidate" /Corefile; then
    mv -f "$candidate" /Corefile
fi
