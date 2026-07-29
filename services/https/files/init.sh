#!/bin/sh

set -eu

ACME_CA="${PROXY_ACME_CA:-https://acme-v02.api.letsencrypt.org/directory}"
CERT_MODE="${PROXY_CERT_MODE:-auto}"
HTTPS_PORT="${PROXY_HTTPS_PORT:-444}"
OCSERV_CERT_DIR="/data/ocserv"
CERT_CRT="$OCSERV_CERT_DIR/certificate.crt"
CERT_KEY="$OCSERV_CERT_DIR/certificate.key"
FALLBACK_CRT="$OCSERV_CERT_DIR/fallback.crt"
FALLBACK_KEY="$OCSERV_CERT_DIR/fallback.key"
CERT_IDENTITY_FILE="$OCSERV_CERT_DIR/identity"
CERT_TYPE_FILE="$OCSERV_CERT_DIR/identity.type"
CONFIG_FILE="/etc/caddy/Caddyfile"
SITES_ENABLED_DIR="/config/sites-enabled"
REACHABLE_SERVICES=""
CERT_IDENTITY=""
CERT_TYPE=""
PROXY_HOST=""
HAS_CERT_SITE=0

validate_ipv4() {
    printf '%s\n' "$1" | awk -F. '
        NF != 4 { exit 1 }
        {
            for (i = 1; i <= 4; i++) {
                if ($i !~ /^[0-9]+$/ || $i < 0 || $i > 255) {
                    exit 1
                }
            }
        }
    '
}

validate_port() {
    case "$1" in
        ""|*[!0-9]*) return 1 ;;
    esac
    [ "$1" -ge 1 ] && [ "$1" -le 65535 ]
}

normalize_domain() {
    idn2 --quiet "$1" | tr '[:upper:]' '[:lower:]'
}

detect_public_ipv4() {
    public_ip="${PROXY_IP:-}"
    if [ -z "$public_ip" ]; then
        public_ip=$(curl --ipv4 --fail --silent --show-error \
            --max-time "${IP_CHECK_TIMEOUT:-10}" \
            "${IP_CHECK_URL:-https://api.ipify.org}")
    fi
    public_ip=$(printf '%s' "$public_ip" | tr -d '[:space:]')

    if ! validate_ipv4 "$public_ip"; then
        echo "[ERROR] Invalid public IPv4 address: $public_ip" >&2
        exit 1
    fi

    printf '%s\n' "$public_ip"
}

resolve_certificate_identity() {
    case "$CERT_MODE" in
        auto|selfsigned) ;;
        *)
            echo "[ERROR] Invalid PROXY_CERT_MODE: $CERT_MODE. Expected: auto or selfsigned" >&2
            exit 1
            ;;
    esac

    if ! validate_port "$HTTPS_PORT"; then
        echo "[ERROR] Invalid PROXY_HTTPS_PORT: $HTTPS_PORT" >&2
        exit 1
    fi

    CERT_IDENTITY="${PROXY_DOMAIN:-}"
    if [ -n "$CERT_IDENTITY" ]; then
        CERT_IDENTITY=$(normalize_domain "$CERT_IDENTITY")
        CERT_TYPE="dns"
    else
        CERT_IDENTITY=$(detect_public_ipv4)
        CERT_TYPE="ip"
    fi

    PROXY_HOST="$CERT_IDENTITY"
}

generate_fallback_certificate() {
    mkdir -p "$OCSERV_CERT_DIR"
    old_identity=$(cat "$CERT_IDENTITY_FILE" 2>/dev/null || true)
    if [ "$old_identity" != "$CERT_IDENTITY" ] \
        || ! openssl x509 -in "$FALLBACK_CRT" -noout -checkend 86400 >/dev/null 2>&1 \
        || [ ! -s "$FALLBACK_KEY" ]; then
        [ "$CERT_TYPE" = "ip" ] && san="IP:$CERT_IDENTITY" || san="DNS:$CERT_IDENTITY"
        echo "[INFO] Generating fallback certificate for $CERT_TYPE:$CERT_IDENTITY"
        openssl req -x509 -newkey rsa:2048 -sha256 -nodes -days 365 \
            -subj "/CN=$CERT_IDENTITY" -addext "subjectAltName=$san" \
            -keyout "$FALLBACK_KEY.tmp" -out "$FALLBACK_CRT.tmp" >/dev/null 2>&1
        chmod 600 "$FALLBACK_KEY.tmp"
        mv -f "$FALLBACK_KEY.tmp" "$FALLBACK_KEY"
        mv -f "$FALLBACK_CRT.tmp" "$FALLBACK_CRT"
    fi
    printf '%s\n' "$CERT_IDENTITY" > "$CERT_IDENTITY_FILE.tmp"
    printf '%s\n' "$CERT_TYPE" > "$CERT_TYPE_FILE.tmp"
    mv -f "$CERT_IDENTITY_FILE.tmp" "$CERT_IDENTITY_FILE"
    mv -f "$CERT_TYPE_FILE.tmp" "$CERT_TYPE_FILE"
}

get_services() {
    counter=1
    while :; do
        service_var="PROXY_SERVICE_$counter"
        service_value=$(eval echo "\${$service_var:-}")

        if [ -z "$service_value" ]; then
            break
        fi

        IFS=: read -r name external_port internal_host internal_port remainder <<EOF
$service_value
EOF

        if [ -z "$name" ] || [ -z "$internal_host" ] || [ -n "$remainder" ] \
            || ! validate_port "$external_port" || ! validate_port "$internal_port"; then
            echo "[ERROR] $service_var has an invalid format. Expected: name:external_port:internal_hostname:internal_port"
            exit 1
        fi

        if [ "$external_port" -eq "$HTTPS_PORT" ]; then
            HAS_CERT_SITE=1
        fi
        REACHABLE_SERVICES=$(printf "%s\n%s" "$REACHABLE_SERVICES" "$service_value")
        counter=$((counter + 1))
    done
    echo "[INFO] Services read successfully."
}

write_tls_policy() {
    host="$1"
    if [ "$CERT_MODE" = "selfsigned" ]; then
        cat <<EOF >>"$CONFIG_FILE"
  tls $CERT_CRT $CERT_KEY
EOF
        return
    fi

    if validate_ipv4 "$host"; then
        cat <<EOF >>"$CONFIG_FILE"
  tls $CERT_CRT $CERT_KEY {
    issuer acme $ACME_CA {
      profile shortlived
      disable_tlsalpn_challenge
    }
  }
EOF
    else
        cat <<EOF >>"$CONFIG_FILE"
  tls $CERT_CRT $CERT_KEY {
    issuer acme $ACME_CA {
      disable_tlsalpn_challenge
    }
  }
EOF
    fi
}

generate_global_config() {
    if [ "$CERT_MODE" = "auto" ]; then
        auto_https_mode="ignore_loaded_certs disable_redirects"
    else
        auto_https_mode="disable_certs disable_redirects"
    fi
    cat <<EOF >>"$CONFIG_FILE"
{
  auto_https $auto_https_mode
  default_sni $CERT_IDENTITY
  http_port 80
  https_port $HTTPS_PORT
  layer4 {
    :443 {
      route {
        proxy {
          proxy_protocol v2
          upstream ocserv.antizapret:443
        }
      }
    }
  }
  servers {
    listener_wrappers {
      http_redirect
      tls
    }
  }
  servers :80 {
  }
EOF
    if [ -n "${PROXY_EMAIL:-}" ]; then
        printf '  email %s\n' "$PROXY_EMAIL" >> "$CONFIG_FILE"
    fi
    cat <<EOF >>"$CONFIG_FILE"
}
EOF
    echo "[INFO] Global configuration block created."
}

add_services_to_config() {
    echo "$REACHABLE_SERVICES" | while IFS= read -r service_value; do
        if [ -z "$service_value" ]; then
            continue
        fi

        IFS=: read -r name external_port internal_host internal_port <<EOF
$service_value
EOF
        site_address="$PROXY_HOST:$external_port"
        if [ "$CERT_TYPE" = "ip" ]; then
            site_address="$site_address, :$external_port"
        fi

        cat <<EOF >>"$CONFIG_FILE"

#$name#
$site_address {
EOF
        write_tls_policy "$PROXY_HOST"
        cat <<EOF >>"$CONFIG_FILE"
  header {
    -X-Frame-Options
  }
  reverse_proxy {
    header_up Authorization {http.request.header.Authorization}
    header_up Proxy-Authorization {http.request.header.Proxy-Authorization}
    dynamic a {
      name $internal_host
      port $internal_port
      refresh 1s
    }
  }
}
EOF
        echo "[INFO] Service added: $PROXY_HOST:$external_port -> $internal_host:$internal_port"
    done
}

add_http_redirect() {
    cat <<EOF >>"$CONFIG_FILE"

#HTTP to Dashboard#
http://$PROXY_HOST, :80 {
  redir https://{host}:$HTTPS_PORT{uri} 308
}
EOF
}

add_ocserv_certificate_site() {
    if [ "$HAS_CERT_SITE" -eq 1 ]; then
        return
    fi

    cat <<EOF >>"$CONFIG_FILE"

#ocserv certificate automation#
$CERT_IDENTITY:$HTTPS_PORT {
EOF
    write_tls_policy "$CERT_IDENTITY"
    cat <<EOF >>"$CONFIG_FILE"
  respond 204
}
EOF
}

main() {
    mkdir -p "$SITES_ENABLED_DIR"
    : >"$CONFIG_FILE"
    resolve_certificate_identity
    generate_fallback_certificate
    get_services
    generate_global_config
    add_http_redirect
    add_services_to_config
    add_ocserv_certificate_site

    cat <<EOF >>"$CONFIG_FILE"

import $SITES_ENABLED_DIR/*
EOF

    echo
    echo "[INFO] Caddyfile has been successfully created at: $CONFIG_FILE"
    echo "[INFO] ocserv certificate identity: $CERT_TYPE:$CERT_IDENTITY"
}

main
