#!/usr/bin/env bash
set -e

CONFIG_FILES="/opt/antizapret/result/ips*"

NEW_MD5=$(cat $CONFIG_FILES 2>/dev/null | md5sum | cut -d' ' -f1)
OLD_MD5=$(cat /.config_md5 2>/dev/null || echo "")

if [ "$NEW_MD5" != "$OLD_MD5" ]; then
    echo "Config files changed. Restarting"
    exit 1
fi
