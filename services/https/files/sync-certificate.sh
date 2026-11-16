#!/bin/sh
set -eu
. /certificates.sh

CERT_IDENTITY=$(cat "$CERT_IDENTITY_FILE")
CERT_TYPE=$(cat "$CERT_TYPE_FILE")
generate_fallback_certificate
source_cert="$FALLBACK_CRT"
source_key="$FALLBACK_KEY"
checksum=$FALLBACK_FINGERPRINT
if [ "${PROXY_CERT_MODE:-auto}" = auto ]; then
    for certificate in /data/caddy/certificates/*/"$CERT_IDENTITY"/"$CERT_IDENTITY.crt"; do
        key="${certificate%.crt}.key"
        if valid_certificate "$certificate" "$key"; then
            source_cert="$certificate"
            source_key="$key"
            checksum=$CERT_FINGERPRINT
            break
        fi
    done
fi

state="${DOCKTINEL_RUNTIME_DIR:-/run/docktinel}/certificate.sha256"
if [ "${1:-}" != --initial ] && [ "$checksum" = "$(cat "$state" 2>/dev/null || true)" ] \
    && cmp -s "$source_cert" "$CERT_CRT" && cmp -s "$source_key" "$CERT_KEY"; then
    exit 0
fi
cp "$source_key" "$CERT_KEY.tmp"
cp "$source_cert" "$CERT_CRT.tmp"
chmod 600 "$CERT_KEY.tmp"
mv -f "$CERT_KEY.tmp" "$CERT_KEY"
mv -f "$CERT_CRT.tmp" "$CERT_CRT"
if [ "${1:-}" != --initial ]; then
    # Commit the snapshot only after a successful reload, so failures retry.
    caddy reload --force --config /etc/caddy/Caddyfile --adapter caddyfile
fi
printf '%s\n' "$checksum" > "$state.tmp"
mv -f "$state.tmp" "$state"
echo "[INFO] Certificate activated: $source_cert"
