HTTPS_CERT_DIR="${HTTPS_CERT_DIR:-/https-data/ocserv}"
CERT_IDENTITY_FILE="$HTTPS_CERT_DIR/identity"
CERT_TYPE_FILE="$HTTPS_CERT_DIR/identity.type"

find_https_certificate() {
    SERVER_CERT="$HTTPS_CERT_DIR/certificate.crt"
    SERVER_KEY="$HTTPS_CERT_DIR/certificate.key"
    if ! openssl x509 -in "$SERVER_CERT" -noout -checkend 0 >/dev/null 2>&1 \
        || [ ! -s "$SERVER_KEY" ]; then
        return 1
    fi
    export SERVER_CERT SERVER_KEY
}

certificate_checksum() {
    find_https_certificate \
        && [ -s "$CERT_IDENTITY_FILE" ] \
        && [ -s "$CERT_TYPE_FILE" ] \
        || return 1

    {
        printf 'identity\0'
        cat "$CERT_IDENTITY_FILE"
        printf '\0type\0'
        cat "$CERT_TYPE_FILE"
        printf '\0certificate\0'
        cat "$SERVER_CERT"
    } | md5sum | cut -d' ' -f1
}
