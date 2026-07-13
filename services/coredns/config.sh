#!/usr/bin/env bash

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

AZ_LOCAL_HOST="$(resolve 'az-local.antizapret' '169.254.0.1')"
AZ_WORLD_HOST="$(resolve 'az-world.antizapret' "$AZ_LOCAL_HOST")"
DNS_HOST="$(resolve 'adguard.antizapret' '169.254.0.3')"

if [ "$AZ_WORLD_HOST" = "$AZ_LOCAL_HOST" ]; then
    export AZ_FORWARD_HOSTS="$AZ_LOCAL_HOST $DNS_HOST"
else
    export AZ_FORWARD_HOSTS="$AZ_WORLD_HOST $AZ_LOCAL_HOST $DNS_HOST"
fi

envsubst < /root/Corefile.template > /Corefile
