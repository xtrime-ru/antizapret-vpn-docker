#!/bin/sh

set -eu

CADDYFILE="/etc/caddy/Caddyfile"
CERT_DIR="/data/ocserv"
CERT_IDENTITY_FILE="$CERT_DIR/identity"
CERT_STORAGE="/data/caddy/certificates"
ACTIVE_CERT="$CERT_DIR/certificate.crt"
ACTIVE_KEY="$CERT_DIR/certificate.key"
FALLBACK_CERT="$CERT_DIR/fallback.crt"
FALLBACK_KEY="$CERT_DIR/fallback.key"

find_managed_certificate() {
    identity=$(cat "$CERT_IDENTITY_FILE" 2>/dev/null || true)
    for certificate in "$CERT_STORAGE"/*/"$identity"/"$identity.crt"; do
        key="${certificate%.crt}.key"
        if openssl x509 -in "$certificate" -noout -checkend 0 >/dev/null 2>&1 \
            && [ -s "$key" ]; then
            MANAGED_CERT="$certificate"
            MANAGED_KEY="$key"
            return 0
        fi
    done
    return 1
}

activate_certificate() {
    cp -f "$2" "$ACTIVE_KEY.tmp"
    cp -f "$1" "$ACTIVE_CERT.tmp"
    chmod 600 "$ACTIVE_KEY.tmp"
    mv -f "$ACTIVE_KEY.tmp" "$ACTIVE_KEY"
    mv -f "$ACTIVE_CERT.tmp" "$ACTIVE_CERT"
}

watch_managed_certificate() {
    active_source="$1"
    previous_checksum="$2"
    while sleep 10; do
        if ! find_managed_certificate; then
            if [ "$active_source" = "managed" ]; then
                activate_certificate "$FALLBACK_CERT" "$FALLBACK_KEY"
                if caddy reload --force --config "$CADDYFILE" --adapter caddyfile; then
                    active_source="fallback"
                    previous_checksum=""
                    echo "[WARN] Managed certificate is unavailable; fallback certificate activated"
                fi
            fi
            continue
        fi
        checksum=$(openssl x509 -in "$MANAGED_CERT" -noout -fingerprint -sha256)
        if [ "$active_source" = "managed" ] && [ "$checksum" = "$previous_checksum" ]; then
            continue
        fi
        activate_certificate "$MANAGED_CERT" "$MANAGED_KEY"
        if caddy reload --force --config "$CADDYFILE" --adapter caddyfile; then
            active_source="managed"
            previous_checksum="$checksum"
            echo "[INFO] Managed certificate activated: $MANAGED_CERT"
        fi
    done
}

/init.sh

initial_checksum=""
if [ "${PROXY_CERT_MODE:-auto}" = "auto" ] && find_managed_certificate; then
    activate_certificate "$MANAGED_CERT" "$MANAGED_KEY"
    initial_source="managed"
    initial_checksum=$(openssl x509 -in "$MANAGED_CERT" -noout -fingerprint -sha256)
else
    activate_certificate "$FALLBACK_CERT" "$FALLBACK_KEY"
    initial_source="fallback"
fi

if [ "${PROXY_CERT_MODE:-auto}" = "auto" ]; then
    watch_managed_certificate "$initial_source" "$initial_checksum" &
fi

exec caddy run --config "$CADDYFILE" --adapter caddyfile
