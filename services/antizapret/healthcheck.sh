#!/usr/bin/env bash
set -e

OLD=$( cat /.config_md5 )
NEW=$( cat /root/antizapret/result/* /root/antizapret/config/custom/* 2>/dev/null | md5sum )
if [[ "$OLD" != "$NEW" ]]; then
    echo "config files changed"
    timeout 5m doall || echo 'doall timeout in healthcheck'
    curl --max-time 60 -sf "http://127.0.0.1/update/"
    ( cat /root/antizapret/result/* /root/antizapret/config/custom/* 2>/dev/null | md5sum ) > /.config_md5
fi