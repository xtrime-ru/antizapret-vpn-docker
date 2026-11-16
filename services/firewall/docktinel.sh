#!/bin/sh
docktinel add job initial-rules --phase startup --deadline 3m --timeout 130s \
    --failure restart-container -- /root/initialize.sh
docktinel add check ready --phase after-start --deadline 30s --interval 1s -- /root/healthcheck.sh
docktinel add check live --interval 5s -- /root/healthcheck.sh
interval=${INTERVAL:-3h}
case "$interval" in *[!0-9]*) ;; *) interval="${interval}s" ;; esac
docktinel add job refresh-lists --interval "$interval" --timeout 130s --lock firewall -- /root/refresh.sh
