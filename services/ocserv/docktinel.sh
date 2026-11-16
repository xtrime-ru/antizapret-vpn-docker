#!/usr/bin/env bash
watch_routes --vpn
wait_initialized /dev/shm/.ocserv_started 8m
docktinel add check configuration --interval 10s --failures 1 -- /healthcheck.sh
