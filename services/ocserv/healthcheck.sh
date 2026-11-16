#!/usr/bin/env bash

set -Eeuo pipefail
shopt -s nullglob

FLAG_FILE="/dev/shm/.ocserv_started"
CONFIG_FILES=(/opt/antizapret/result/ips*)
. /certificate.sh
. /usr/local/lib/docktinel/state.sh

if [[ ! -f "$FLAG_FILE" ]]; then
    echo "Waiting for ocserv initialization" >&2
    exit 1
fi

NEW_MD5=$(config_checksum /opt/antizapret/result/.ips-md5 "${CONFIG_FILES[@]}")
OLD_MD5=$(cat /dev/shm/.config_md5 2>/dev/null || true)

if [ "$NEW_MD5" != "$OLD_MD5" ]; then
    echo "healthcheck: config files changed"
    exit 1
fi

if ! NEW_CERT_MD5=$(certificate_checksum); then
    echo "healthcheck: HTTPS certificate state is unavailable"
    exit 1
fi
OLD_CERT_MD5=$(cat /dev/shm/.certificate_state_md5 2>/dev/null || true)
if [[ -z "$NEW_CERT_MD5" || "$NEW_CERT_MD5" != "$OLD_CERT_MD5" ]]; then
    echo "healthcheck: HTTPS certificate state changed"
    exit 1
fi
