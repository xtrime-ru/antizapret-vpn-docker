#!/usr/bin/env bash

export AZ_LOCAL_HOST=$(dig +short az-local | head -n1)
export AZ_WORLD_HOST=$(dig +short az-world | head -n1)
export DNS_HOST=$(dig +short adguard | head -n1)
while [ -z "${AZ_LOCAL_HOST}" ] || [ -z "${AZ_WORLD_HOST}" ] || [ -z "$DNS_HOST" ]; do
    echo "No route to antizapret container. Retrying..."
    export AZ_LOCAL_HOST=$(dig +short az-local | head -n1)
    export AZ_WORLD_HOST=$(dig +short az-world | head -n1)
    export DNS_HOST=$(dig +short adguard | head -n1)
    sleep 1;
done;

envsubst < /root/Corefile.template > /Corefile