#!/bin/sh
docktinel add job configure --phase startup --deadline 30s --timeout 10s \
    --failure restart-container -- /init.sh
docktinel add check ready --phase after-start --deadline 30s --interval 1s -- /healthcheck.sh
docktinel add check live --interval 5s -- /healthcheck.sh
