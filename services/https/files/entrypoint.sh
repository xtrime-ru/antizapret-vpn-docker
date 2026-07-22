#!/bin/sh

set -eu

/init.sh

exec caddy run --config /etc/caddy/Caddyfile --adapter caddyfile
