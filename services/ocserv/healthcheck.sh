#!/usr/bin/env bash
exec > >(tee /proc/1/fd/1) 2>&1

set -Eeuo pipefail
shopt -s nullglob

FLAG_FILE="/tmp/.ocserv_started"
CONFIG_FILES=(/opt/antizapret/result/ips*)
. /certificate.sh

cleanup() {
    local exit_code=$?
    trap - EXIT

    if ((exit_code != 0)); then
        echo "healthcheck failed. Killing pid 1."
        kill -TERM 1
        sleep 10
        kill -KILL 1
    fi
}

if [[ -f "$FLAG_FILE" ]]; then
    trap cleanup EXIT HUP INT QUIT PIPE TERM
else
    echo "healthcheck: waiting for ocserv initialization"
    exit 1
fi

if ((${#CONFIG_FILES[@]})); then
    NEW_MD5=$(cat "${CONFIG_FILES[@]}" | md5sum | cut -d' ' -f1)
else
    NEW_MD5=$(md5sum </dev/null | cut -d' ' -f1)
fi
OLD_MD5=$(cat /.config_md5 2>/dev/null || true)

if [ "$NEW_MD5" != "$OLD_MD5" ]; then
    echo "healthcheck: config files changed"
    exit 1
fi

NEW_CERT_IDENTITY=$(cat "$CERT_IDENTITY_FILE" 2>/dev/null || true)
OLD_CERT_IDENTITY=$(cat /.certificate_identity 2>/dev/null || true)
if [[ -z "$NEW_CERT_IDENTITY" || "$NEW_CERT_IDENTITY" != "$OLD_CERT_IDENTITY" ]]; then
    echo "healthcheck: HTTPS certificate identity changed"
    exit 1
fi

if ! find_https_certificate; then
    echo "healthcheck: HTTPS certificate is unavailable"
    exit 1
fi
NEW_CERT_MD5=$(md5sum "$SERVER_CERT" 2>/dev/null | cut -d' ' -f1 || true)
OLD_CERT_MD5=$(cat /.certificate_md5 2>/dev/null || true)
if [[ -z "$NEW_CERT_MD5" || "$NEW_CERT_MD5" != "$OLD_CERT_MD5" ]]; then
    echo "healthcheck: HTTPS certificate renewed"
    exit 1
fi
