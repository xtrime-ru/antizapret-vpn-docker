#!/usr/bin/env bash

set -e

CONFIG_DIR="/etc/nginx"
CERT_DIR="$CONFIG_DIR/ssl"
CERT_CRT="$CERT_DIR/selfsigned.crt"
CERT_KEY="$CERT_DIR/selfsigned.key"
SNIPPETS_DIR="$CONFIG_DIR/snippets"
SSL_SNIPPET="$SNIPPETS_DIR/ssl-common.conf"
PROXY_SNIPPET="$SNIPPETS_DIR/proxy-params.conf"
DASHBOARD_CONF="$CONFIG_DIR/conf.d/default.conf"
SITES_AVAILABLE_DIR="$CONFIG_DIR/sites-available"
SITES_ENABLED_CONF="$CONFIG_DIR/conf.d/enabled-sites.conf"
SITES_ENABLED_DIR="$CONFIG_DIR/sites-enabled"
SERVICES_JSON="$CONFIG_DIR/services.json"
WWW_ROOT_DIR="/usr/share/nginx/html"


declare -a SERVICES

generate_certificate() {
    echo "[INFO] Generating or checking SSL certificates..."

    mkdir -p "$CERT_DIR"
    if [ ! -f "$CERT_KEY" ] || [ ! -f "$CERT_CRT" ]; then
        openssl req -x509 -nodes -days 3650 -newkey rsa:2048 \
          -keyout "$CERT_KEY" \
          -out "$CERT_CRT" \
          -subj "/O=ANTIZAPRET/OU=ANTIZAPRET/CN=ANTIZAPRET"
        echo "[INFO] Certificates have been generated."
    else
        echo "[INFO] Certificates already exist. Skipping generation."
    fi
    echo
}

create_snippets(){
    echo "[INFO] Creating snippets configuration..."

    mkdir -p "$SNIPPETS_DIR"
    cat <<EOF | tee "$PROXY_SNIPPET" > /dev/null
proxy_http_version 1.1;
proxy_set_header Host $$host;
proxy_set_header X-Real-IP $$remote_addr;
proxy_set_header X-Forwarded-For $$proxy_add_x_forwarded_for;
proxy_set_header X-Forwarded-Proto $$scheme;
EOF

    cat <<EOF | tee "$SSL_SNIPPET" > /dev/null
server_name _;
ssl_certificate     $CERT_CRT;
ssl_certificate_key $CERT_KEY;
ssl_protocols TLSv1.2 TLSv1.3;
ssl_ciphers  HIGH:!aNULL:!MD5;
EOF
    echo "[INFO] Configuration files in $SNIPPETS_DIR have been created."
}

create_dashboard() {
    echo "[INFO] Creating dashboard configuration..."

    if [ -z "$DASHBOARD_USERNAME" ] || [ -z "$DASHBOARD_PASSWORD" ]; then
      echo "[ERROR] DASHBOARD_USERNAME or DASHBOARD_PASSWORD environment variables are not set."
      echo "Example usage:"
      echo "  export DASHBOARD_USERNAME=user"
      echo "  export DASHBOARD_PASSWORD=secret"
      exit 1
    fi

    HTPASSWD_FILE="$CONFIG_DIR/.htpasswd"

    HASHED_PASS="$(openssl passwd -apr1 "$DASHBOARD_PASSWORD")"
    echo "$DASHBOARD_USERNAME:$HASHED_PASS" | tee "$HTPASSWD_FILE" > /dev/null
    echo "[INFO] The file $HTPASSWD_FILE has been successfully created."

    cat <<EOF | tee "$DASHBOARD_CONF" > /dev/null
server {
    listen 1443 ssl;

    include $SSL_SNIPPET;

    add_header "Access-Control-Allow-Origin" "*" always;
    add_header "Access-Control-Allow-Methods" "*" always;
    add_header "Access-Control-Allow-Headers" "*" always;

    auth_basic           "Restricted Area";
    auth_basic_user_file $HTPASSWD_FILE;

    location / {
        root   $WWW_ROOT_DIR;
        index  index.html index.htm;
    }

    location /services.json {
        alias $SERVICES_JSON;
    }
}
EOF
    echo "[INFO] The file $DASHBOARD_CONF has been successfully created."
    echo
}

read_services_env() {
    local counter=1
    while true; do
        local varname="SERVICE_${counter}"
        local value="${!varname}"
        if [ -z "$value" ]; then
            break
        fi

        value="${value//\"/}"
        SERVICES+=("$value")
        ((counter++))
    done

    if [ ${#SERVICES[@]} -eq 0 ]; then
        echo "[ERROR] No variables of the type SERVICE_1, SERVICE_2, etc., were found."
        exit 1
    fi
}

create_services() {
    echo "[INFO] Creating proxy configurations..."

    mkdir -p "$SITES_AVAILABLE_DIR"
    rm -rf "${SITES_AVAILABLE_DIR:?}"/*

    echo "[INFO] Creating configuration files in $SITES_AVAILABLE_DIR..."
    for service in "${SERVICES[@]}"; do
        IFS=":" read -r name external_port internal_host internal_port <<< "$service"
        conf_file="$SITES_AVAILABLE_DIR/${internal_host}.conf"
        tee "$conf_file" > /dev/null <<EOF
#${name}#
server {
    listen $external_port ssl;
    include $SSL_SNIPPET;

    location / {
        include $PROXY_SNIPPET;
        proxy_pass http://$internal_host:$internal_port;
    }
}
EOF
    done
    echo "[INFO] Configuration files in $SITES_AVAILABLE_DIR have been created."
    echo
}

enable_services() {
    echo "[INFO] Enabling proxy configurations..."

    mkdir -p "$SITES_ENABLED_DIR"
    find "$SITES_ENABLED_DIR" -type l -exec rm -f {} \;

    echo "include $SITES_ENABLED_DIR/*;" > $SITES_ENABLED_CONF

    echo "[INFO] Checking service availability..."

    for conf_file in "$SITES_AVAILABLE_DIR"/*.conf; do
        local backend_url host

        backend_url=$(grep -E "proxy_pass\s+https?://" "$conf_file" 2>/dev/null \
          | sed -E 's/.*proxy_pass\s+(https?:\/\/[^;]+);.*/\1/')
        if [ -z "$backend_url" ]; then
            echo "[INFO] No 'proxy_pass http/https' found in $conf_file — skipping."
            continue
        fi

        host=$(echo "$backend_url" | sed -E 's|^https?://([^/]+)/?.*|\1|' | cut -d: -f1)

        if getent hosts "$host" >/dev/null; then
            ln -sf "$conf_file" "$SITES_ENABLED_DIR/$(basename "$conf_file")"
            echo "[OK] $host is available — enabling configuration $(basename "$conf_file")."
        else
            echo "[WARN] $host is unavailable — skipping symlink creation."
        fi
    done
    echo
}

export_services() {
    echo "[INFO] Configuring services in dashboard ..."

    rm -f "$SERVICES_JSON"

    echo "[" > "$SERVICES_JSON"

    local index=1

    for service in "${SERVICES[@]}"; do
        IFS=":" read -r name external_port internal_host internal_port <<< "$service"

        local conf_file="$SITES_ENABLED_DIR/${internal_host}.conf"
        if [ ! -f "$conf_file" ]; then
            echo "[WARN] $conf_file not found, skipping..."
            continue
        fi

        local backend_url enabled_name enabled_port enabled_host

        backend_url=$(grep -E "proxy_pass\s+https?://" "$conf_file" 2>/dev/null \
          | sed -E 's/.*proxy_pass\s+(https?:\/\/[^;]+);.*/\1/')
        if [ -z "$backend_url" ]; then
            echo "[INFO] No 'proxy_pass http/https' found in $conf_file — skipping."
            continue
        fi

        enabled_host=$(echo "$backend_url" | sed -E 's|^https?://([^/]+)/?.*|\1|' | cut -d: -f1)
        enabled_port="$(grep -m1 'listen' "$conf_file" | sed 's/^.*listen \([0-9]\+\) ssl.*$/\1/')"
        enabled_name="$(grep -m1 '^#' "$conf_file" | sed 's/^#\([^#]*\)#$/\1/')"

        [ -z "$enabled_host" ] && enabled_name="$internal_host"
        [ -z "$enabled_name" ] && enabled_name="$name"
        [ -z "$enabled_port" ] && enabled_port="$external_port"

        cat <<EOF >>"$SERVICES_JSON"
  {
    "name": "$enabled_name",
    "hash": "$enabled_host",
    "port": $enabled_port
  },
EOF

        ((index++))
    done
    sed -i -e '1h;1!H;$!d;x;s/\(.*\),/\1/' $SERVICES_JSON
    echo "]" >> "$SERVICES_JSON"
    echo "[INFO] $SERVICES_JSON created."
}

generate_certificate
create_snippets
create_dashboard
read_services_env
create_services
enable_services
export_services
echo "[INFO] All steps completed."