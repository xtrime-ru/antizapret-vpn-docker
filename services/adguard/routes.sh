#!/bin/bash

# resolve domain address to ip address
function resolve () {
    # $1 domain/ip address, $2 fallback ip address
    ipcalc () { dig +short $1 2> /dev/null; }
    echo "$(ipcalc $1 || echo $2)"
}

ROUTES_EXISTING=$(ip route)

for route in ${ROUTES//;/ }; do
    host_route=${route%:*}
    gateway=$(resolve $host_route '')
    #echo "Checking route: $route;  gateway: $gateway"

    if [ -z "$gateway" ] || [[ "$ROUTES_EXISTING" == *"$gateway"* ]]; then continue; fi
    subnet=${route#*:};
    ip route add $subnet via $gateway
    echo "Route add: $subnet via $gateway"
done