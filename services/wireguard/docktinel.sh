#!/usr/bin/env bash
watch_routes --vpn
wait_initialized /dev/shm/.wireguard_started 8m
docktinel add check configuration --interval 10s --failures 1 -- /healthcheck.sh
if [ "${BGP_ENABLE:-false}" = true ]; then
    docktinel add process bird -- /usr/sbin/bird -d
fi
