#!/bin/sh
docktinel add job configure --phase startup --deadline 2m --timeout 30s \
    --failure restart-container -- /opt/start.sh
docktinel add check ready --phase after-start --deadline 30s --interval 1s -- /opt/healthcheck.sh
docktinel add check live --interval 5s -- /opt/healthcheck.sh
