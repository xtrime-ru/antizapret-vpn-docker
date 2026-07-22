HTTPS_CERT_DIR="${HTTPS_CERT_DIR:-/https-data/ocserv}"
HTTPS_CERT_STORAGE="${HTTPS_CERT_STORAGE:-/https-data/caddy/certificates}"
CERT_IDENTITY_FILE="$HTTPS_CERT_DIR/identity"
CERT_TYPE_FILE="$HTTPS_CERT_DIR/identity.type"

find_https_certificate() {
    certificate_identity=$(cat "$CERT_IDENTITY_FILE" 2>/dev/null || true)
    certificate_dir="$HTTPS_CERT_STORAGE/acme-v02.api.letsencrypt.org-directory/$certificate_identity"
    SERVER_CERT="$certificate_dir/$certificate_identity.crt"
    SERVER_KEY="$certificate_dir/$certificate_identity.key"
    if ! openssl x509 -in "$SERVER_CERT" -noout -checkend 0 >/dev/null 2>&1 \
        || [ ! -s "$SERVER_KEY" ]; then
        SERVER_CERT="$HTTPS_CERT_DIR/certificate.crt"
        SERVER_KEY="$HTTPS_CERT_DIR/certificate.key"
    fi
    if openssl x509 -in "$SERVER_CERT" -noout -checkend 0 >/dev/null 2>&1 \
        && [ -s "$SERVER_KEY" ]; then
        export SERVER_CERT SERVER_KEY
        return 0
    fi
    return 1
}
