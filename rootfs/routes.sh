#!/bin/bash

source /etc/default/antizapret

# resolve domain address to ip address
function resolve () {
    # $1 domain/ip address, $2 fallback ip address
    ipcalc () { ipcalc-ng --no-decorate -o $1 2> /dev/null; }
    echo "$(ipcalc $1 || echo $2)"
}

for route in ${ROUTES//;/ }; do
    host_route=${route%:*}
    gateway=$(resolve $host_route '')
    if [ ! -n "$gateway" ]; then continue; fi
    subnet=${route#*:};
    ip route add $subnet via $gateway
done