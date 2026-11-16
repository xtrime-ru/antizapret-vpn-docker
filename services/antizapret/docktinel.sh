#!/usr/bin/env bash
watch_routes
wait_initialized /dev/shm/.dns_started 8m
docktinel add check dnsmap --interval 1m --timeout 3s --failures 3 -- /healthcheck.sh
docktinel add check warp --interval 1m -- /healthcheck.sh --warp
docktinel add process api -- /opt/api/app
docktinel add process iperf -- /usr/bin/iperf3 -s -1
docktinel add job refresh-state --interval 30s --timeout 6m --lock lists -- /refresh-state.sh
docktinel add job refresh-lists --interval 6h --grace 6h --timeout 10m \
    --lock lists -- /usr/bin/doall
