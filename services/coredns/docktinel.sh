#!/usr/bin/env bash
docktinel add job initial-config --phase startup --deadline 1m --interval 1s \
    --timeout 10s --failure restart-container -- /root/config.sh
docktinel add check ready --phase after-start --deadline 30s --interval 1s -- \
    curl -fsS --connect-timeout 2 --max-time 3 http://127.0.0.1:8080/health
docktinel add check live --interval 5s -- \
    curl -fsS --connect-timeout 2 --max-time 3 http://127.0.0.1:8080/health
docktinel add job refresh-endpoints --interval 5s --timeout 10s -- /root/config.sh
