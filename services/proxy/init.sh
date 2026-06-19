#!/usr/bin/env bash
set -e

if [ -n "$SOCKS_USERNAME" ]; then
    export PROXY_LOGIN="$SOCKS_USERNAME"
fi

if [ -n "$SOCKS_PASSWORD" ]; then
    export PROXY_PASSWORD="$SOCKS_PASSWORD"
fi

if [ -z "$PROXY_LOGIN" ] || [ -z "$PROXY_PASSWORD" ]; then
  echo '[proxy] !!!WARNING!!! Login and Password are empty.
  Make sure HTTPS proxy is not accessible from internet. There are two options:
   - Make https container ENV variables for proxy-local.antizapret and proxy-world.antizapret
   - Change hostname in your docker-compose.override.yml, so caddy/https cant reach them by default proxy-local.antizapret.
  ' >&2
fi

routes --vpn &

exec /bin/lua /entrypoint.lua