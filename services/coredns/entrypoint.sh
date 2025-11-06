#!/usr/bin/env bash

export AZ_HOST=$(dig +short antizapret)
export DNS_HOST=$(dig +short adguard)
while [ -z "${AZ_HOST}" ] || [ -z "${DNS_HOST}" ]; do
    echo "No route to antizapret container. Retrying..."
    export AZ_HOST=$(dig +short antizapret)
    export DNS_HOST=$(dig +short adguard)
    sleep 1;
done;

envsubst < /root/Corefile.template > /Corefile

exec /coredns -conf /Corefile