#!/usr/bin/env bash
set -e

DIFF=$( ( cat /root/antizapret/result/* /root/antizapret/config/custom/* | md5sum ) | cmp - /.config_md5 )
if [ -n "$DIFF" ]; then
    echo "config files changed"
    doall
    curl -s "http://127.0.0.1/update/"
    ( cat /root/antizapret/result/* /root/antizapret/config/custom/* | md5sum ) > /.config_md5
fi