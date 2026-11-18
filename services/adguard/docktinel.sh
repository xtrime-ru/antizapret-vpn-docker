#!/usr/bin/env bash
watch_routes
wait_initialized /dev/shm/.inited 5m
docktinel add check dns-ready --phase after-start --deadline 5m --interval 1s --timeout 15s -- \
    bash -c 'exec 3<>/dev/tcp/127.0.0.1/53'
docktinel add check dns-serving --interval 1m --timeout 15s --failures 3 -- \
    bash -c 'exec 3<>/dev/tcp/127.0.0.1/53'
# Updating clients, DNS endpoints and changed custom rules is maintenance. Its
# failure must not turn a working resolver into a restart loop.
docktinel add job refresh-state --interval 10s --timeout 3m -- \
    /root/refresh-state.sh
