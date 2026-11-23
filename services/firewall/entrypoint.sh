#!/bin/bash
set -euo pipefail

# Assign the launch order before waiting on the lock used to apply rules.
(
    cache=/var/lib/antizapret-firewall
    mkdir -p "$cache"
    exec 9>"$cache/generation.lock"
    flock 9
    generation=0
    if [ -f "$cache/generation" ]; then
        read -r generation < "$cache/generation"
    fi
    generation=$((generation + 1))
    printf '%s\n' "$generation" > "$cache/generation.tmp"
    mv -f -- "$cache/generation.tmp" "$cache/generation"
    printf '%s\n' "$generation" > /run/antizapret-firewall-generation
)

export DOCKTINEL_CONFIG="${DOCKTINEL_CONFIG:-/dev/shm/docktinel.json}"
docktinel add job initial-rules --phase startup --deadline 3m --timeout 130s \
    --failure restart-container -- /root/refresh.sh --restore
docktinel add check ready --phase after-start --deadline 30s --interval 1s -- /root/block.sh check
docktinel add check live --interval 5s --timeout 130s -- /root/refresh.sh --check
interval=${INTERVAL:-3h}
case "$interval" in *[!0-9]*) ;; *) interval="${interval}s" ;; esac
docktinel add job refresh-lists --interval "$interval" --timeout 130s -- /root/refresh.sh
docktinel add job clear-rules --phase shutdown --timeout 10s -- /root/refresh.sh --clear
exec docktinel daemon
