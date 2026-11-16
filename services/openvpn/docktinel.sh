#!/usr/bin/env bash
watch_routes --vpn
wait_initialized /dev/shm/.inited 8m
docktinel add check configuration --interval 10s --failures 1 -- /opt/app/healthcheck.sh
docktinel add job rotate-logs --interval 24h --timeout 30s -- \
    logrotate --state /etc/openvpn/log/.logrotate.status /etc/logrotate.d/openvpn
docktinel add job renew-crl --interval 24h --timeout 2m -- /opt/app/renew-crl.sh
docktinel add process logs -- /opt/app/follow-logs.sh
