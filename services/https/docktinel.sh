#!/bin/sh
docktinel add job configure --phase startup --deadline 2m --timeout 30s \
    --failure restart-container -- /entrypoint.sh
docktinel add check ready --phase after-start --deadline 30s --interval 1s -- curl -fsS --connect-timeout 2 --max-time 3 http://127.0.0.1:2018/healthz
docktinel add check live --interval 5s -- curl -fsS --connect-timeout 2 --max-time 3 http://127.0.0.1:2018/healthz
docktinel add job sync-certificate --interval 10s --timeout 20s -- /sync-certificate.sh
