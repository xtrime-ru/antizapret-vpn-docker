#!/bin/sh
exec curl -fsSI --connect-timeout 2 --max-time 3 \
    --user "$DASHBOARD_USERNAME:$DASHBOARD_PASSWORD" http://127.0.0.1/config.json >/dev/null
