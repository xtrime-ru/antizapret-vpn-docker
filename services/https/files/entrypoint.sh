#!/usr/bin/env sh

/init.sh

exec caddy run --config /etc/caddy/Caddyfile --adapter caddyfile