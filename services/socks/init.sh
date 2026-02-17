#!/usr/bin/env bash

if [ -n "$SOCKS_USERNAME" ] && [ -n "$SOCKS_PASSWORD" ]; then
    useradd -r -s /usr/sbin/nologin "$SOCKS_USERNAME" 2>/dev/null || true
    echo "$SOCKS_USERNAME:$SOCKS_PASSWORD" | chpasswd
fi

/routes.sh &
exec danted
