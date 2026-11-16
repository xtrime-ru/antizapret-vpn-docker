#!/bin/sh
set -eu
/init.sh
/sync-certificate.sh --initial
caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
