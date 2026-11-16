#!/bin/sh
watch_routes --vpn
docktinel add check ready --phase after-start --deadline 30s --interval 1s -- /healthcheck.sh
docktinel add check live --interval 5s -- /healthcheck.sh
