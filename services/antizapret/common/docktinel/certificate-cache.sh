#!/bin/sh
# Shared by BusyBox sh (Caddy) and Bash (ocserv). Cache only parsed public data.
certificate_cached() {
    local crt=$1 key=$2 minimum=${3:-0} metadata cache root now seen status expiry fingerprint
    local cert_meta key_meta not_after cert_pub key_pub temporary
    CERT_EXPIRES=0
    CERT_FINGERPRINT=''
    metadata=$(stat -L -c '%d:%i:%s:%y:%z' "$crt" "$key" 2>/dev/null) || return 1
    root="${DOCKTINEL_RUNTIME_DIR:-/run/docktinel}/certificates"
    cache="$root/$(printf '%s' "$crt" | cksum)"
    now=$(date +%s)
    status=''
    cert_meta=''
    key_meta=''
    expiry=''
    fingerprint=''
    if [ -r "$cache" ]; then
        { IFS= read -r status; IFS= read -r cert_meta; IFS= read -r key_meta
          IFS= read -r expiry; IFS= read -r fingerprint; } < "$cache" || true
        seen=$(printf '%s\n%s' "$cert_meta" "$key_meta")
        if [ "$seen" = "$metadata" ]; then
            case "$expiry" in ''|*[!0-9]*) ;; *)
                case "$fingerprint" in ''|*[!0-9a-f]*) ;; *)
                    if [ "$status" = v1-valid ] && [ "${#fingerprint}" = 64 ]; then
                        CERT_EXPIRES=$expiry
                        CERT_FINGERPRINT=$fingerprint
                        [ "$CERT_EXPIRES" -gt "$((now + minimum))" ]
                        return $?
                    fi ;;
                esac ;;
            esac
        fi
    fi
    # Only successful parsing is cached: transient tool/I/O failures must retry.
    not_after=$(openssl x509 -in "$crt" -noout -enddate 2>/dev/null) || return 1
    cert_pub=$(openssl x509 -in "$crt" -noout -pubkey 2>/dev/null) || return 1
    key_pub=$(openssl pkey -in "$key" -pubout 2>/dev/null) || return 1
    [ "$cert_pub" = "$key_pub" ] || return 1
    not_after=${not_after#notAfter=}
    expiry=$(LC_ALL=C TZ=UTC date -d "$not_after" +%s 2>/dev/null) \
        || expiry=$(LC_ALL=C TZ=UTC date -D '%b %e %H:%M:%S %Y' -d "${not_after% GMT}" +%s 2>/dev/null) \
        || return 1
    fingerprint=$(sha256sum "$crt") || return 1
    fingerprint=${fingerprint%% *}
    status=v1-valid
    # Files changed during parsing: do not commit a mixed certificate/key pair.
    [ "$metadata" = "$(stat -L -c '%d:%i:%s:%y:%z' "$crt" "$key" 2>/dev/null)" ] || return 1
    [ -d "$root" ] || mkdir -m 700 -p "$root" || return 1
    temporary="$cache.tmp.$$"
    printf '%s\n%s\n%s\n%s\n' "$status" "$metadata" "$expiry" "$fingerprint" > "$temporary" || return 1
    mv -f "$temporary" "$cache" || return 1
    CERT_EXPIRES=$expiry
    CERT_FINGERPRINT=$fingerprint
    [ "$status" = v1-valid ] && [ "$CERT_EXPIRES" -gt "$((now + minimum))" ]
}
