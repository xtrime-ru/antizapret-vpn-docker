#!/usr/bin/env bash

export AZ_LOCAL_HOST=$(dig +short az-local)
export AZ_WORLD_HOST=$(dig +short az-world)
export DNS_HOST=$(dig +short adguard)
while [ -z "${AZ_LOCAL_HOST}" ] || [ -z "${AZ_WORLD_HOST}" ] || [ -z "$DNS_HOST" ]; do
    echo "No route to antizapret container. Retrying..."
    export AZ_LOCAL_HOST=$(dig +short az-local)
    export AZ_WORLD_HOST=$(dig +short az-world)
    export DNS_HOST=$(dig +short adguard)
    sleep 1;
done;

envsubst < /root/Corefile.template > /Corefile