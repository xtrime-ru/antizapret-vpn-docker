#!/bin/sh

OCSERV_CERT_DIR="/data/ocserv"
CERT_CRT="$OCSERV_CERT_DIR/certificate.crt"
CERT_KEY="$OCSERV_CERT_DIR/certificate.key"
FALLBACK_CRT="$OCSERV_CERT_DIR/fallback.crt"
FALLBACK_KEY="$OCSERV_CERT_DIR/fallback.key"
CERT_IDENTITY_FILE="$OCSERV_CERT_DIR/identity"
CERT_TYPE_FILE="$OCSERV_CERT_DIR/identity.type"

. /usr/local/lib/docktinel/certificate-cache.sh

valid_certificate() {
    certificate_cached "$@"
}

generate_fallback_certificate() {
    mkdir -p "$OCSERV_CERT_DIR"
    old_identity=$(cat "$CERT_IDENTITY_FILE" 2>/dev/null || true)
    if [ "$old_identity" != "$CERT_IDENTITY" ] \
        || ! valid_certificate "$FALLBACK_CRT" "$FALLBACK_KEY" 86400; then
        [ "$CERT_TYPE" = "ip" ] && san="IP:$CERT_IDENTITY" || san="DNS:$CERT_IDENTITY"
        echo "[INFO] Generating fallback certificate for $CERT_TYPE:$CERT_IDENTITY"
        openssl req -x509 -newkey rsa:2048 -sha256 -nodes -days 365 \
            -subj "/CN=$CERT_IDENTITY" -addext "subjectAltName=$san" \
            -keyout "$FALLBACK_KEY.tmp" -out "$FALLBACK_CRT.tmp" >/dev/null 2>&1
        chmod 600 "$FALLBACK_KEY.tmp"
        mv -f "$FALLBACK_KEY.tmp" "$FALLBACK_KEY"
        mv -f "$FALLBACK_CRT.tmp" "$FALLBACK_CRT"
        valid_certificate "$FALLBACK_CRT" "$FALLBACK_KEY" || return 1
    fi
    FALLBACK_FINGERPRINT=$CERT_FINGERPRINT
    if [ "$old_identity" != "$CERT_IDENTITY" ]; then
        printf '%s\n' "$CERT_IDENTITY" > "$CERT_IDENTITY_FILE.tmp"
        mv -f "$CERT_IDENTITY_FILE.tmp" "$CERT_IDENTITY_FILE"
    fi
    if [ "$(cat "$CERT_TYPE_FILE" 2>/dev/null || true)" != "$CERT_TYPE" ]; then
        printf '%s\n' "$CERT_TYPE" > "$CERT_TYPE_FILE.tmp"
        mv -f "$CERT_TYPE_FILE.tmp" "$CERT_TYPE_FILE"
    fi
}
