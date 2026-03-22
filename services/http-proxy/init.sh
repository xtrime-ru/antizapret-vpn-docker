#!/usr/bin/env bash
set -e

# Enable basic auth if credentials are provided
if [ -n "${HTTP_PROXY_USERNAME:-}" ] && [ -n "${HTTP_PROXY_PASSWORD:-}" ]; then
    htpasswd -cb /etc/squid/htpasswd "$HTTP_PROXY_USERNAME" "$HTTP_PROXY_PASSWORD"
    SQUID_AUTH_SECTION="$(cat <<'AUTHEOF'
auth_param basic program /usr/lib/squid/basic_ncsa_auth /etc/squid/htpasswd
auth_param basic realm proxy
acl authenticated proxy_auth REQUIRED
http_access deny !authenticated
AUTHEOF
)"
else
    SQUID_AUTH_SECTION=""
fi

export SQUID_AUTH_SECTION

# Set default ports if not provided
export HTTP_PROXY_PORT="${HTTP_PROXY_PORT:-3128}"
export HTTPS_PROXY_PORT="${HTTPS_PROXY_PORT:-3129}"

envsubst < /squid.conf.template > /etc/squid/squid.conf

# Generate self-signed certificate for HTTPS port
openssl req -new -newkey rsa:2048 -days 3650 -nodes -x509 \
    -subj "/CN=antizapret-proxy" \
    -keyout /etc/squid/ssl/key.pem \
    -out /etc/squid/ssl/cert.pem 2>/dev/null
chown proxy:proxy /etc/squid/ssl/key.pem /etc/squid/ssl/cert.pem

/routes.sh &
exec squid -N
