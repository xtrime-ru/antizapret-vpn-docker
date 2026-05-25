#!/usr/bin/env bash
set -e

if [ -n "$SOCKS_USERNAME" ]; then
    export PROXY_LOGIN="$SOCKS_USERNAME"
fi

if [ -n "$SOCKS_PASSWORD" ]; then
    export PROXY_PASSWORD="$SOCKS_PASSWORD"
fi

if [ -z "$PROXY_LOGIN" ] || [ -z "$PROXY_PASSWORD" ]; then
  echo 'Login and Password required. HTTPS proxy is accessible from internet'
  sleep 1
  exit 1
fi

/routes.sh --vpn &
exec /bin/lua /entrypoint.lua