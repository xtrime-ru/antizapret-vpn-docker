#!/usr/bin/env bash
. /usr/local/lib/docktinel/state.sh
set -e

INIT_FILE="/dev/shm/.inited"
[ ! -f "$INIT_FILE" ] && exit 0;

CONFIG_FILES="/opt/antizapret/result/openvpn-blocked-ranges.txt"
NEW_MD5=$(config_checksum /opt/antizapret/result/.openvpn-md5 $CONFIG_FILES)
OLD_MD5=$(cat /dev/shm/.config_md5 2>/dev/null || echo "")

if [ "$NEW_MD5" != "$OLD_MD5" ]; then
    echo "Config files changed. Restarting"
    exit 1
fi
