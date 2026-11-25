#!/usr/bin/env bash
watch_routes
wait_initialized /dev/shm/.dns_started 8m
docktinel add check dnsmap --interval 1m --timeout 3s --failures 3 -- /healthcheck.sh
docktinel add check warp --interval 1m -- /healthcheck.sh --warp
docktinel add process api -- /opt/api/app
docktinel add process iperf -- /usr/bin/iperf3 -s -1
# Tundra-NAT64 external address translation server. Starts after init.sh has
# created /etc/tundra-nat64/tundra-nat64.conf, the TUN interface, routes and
# the /run/tundra/ready marker. Waits for the marker and the Unix socket
# before exec'ing tundra-nat64 so signals from docktinel go straight to it.
TUNDRA_SOCKET="${TUNDRA_SOCKET:-}"
rm -f /run/tundra/ready /run/tundra/disabled
if [ -n "$TUNDRA_SOCKET" ]; then
    docktinel add process tundra-nat64 -- /bin/sh -c 'while [ ! -f /run/tundra/ready ] || [ ! -S "$1" ]; do sleep 0.5; if [ -f /run/tundra/disabled ]; then trap "exit 0" TERM INT; while :; do sleep 3600 & wait $!; done; fi; done; exec /usr/bin/tundra-nat64 --config-file=/etc/tundra-nat64/tundra-nat64.conf translate' _ "$TUNDRA_SOCKET"
fi
docktinel add job refresh-state --interval 30s --timeout 6m --lock lists -- /refresh-state.sh
docktinel add job refresh-lists --interval 6h --grace 6h --timeout 10m \
    --lock lists -- /usr/bin/doall
docktinel add job cleanup --phase shutdown --timeout 10s -- /shutdown.sh
