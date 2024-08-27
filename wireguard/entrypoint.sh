#!/usr/bin/env bash

if [ $(which dig | wc -l) -eq 0 ]; then
    apk add bind-tools
fi
if [ $(which curl | wc -l) -eq 0 ]; then
    apk add curl
fi

export WG_HOST=$(curl -4 icanhazip.com)
export WG_DEFAULT_DNS=$(dig +short antizapret-vpn | head -n1)

ip route add 10.0.0.0/8 via $(dig +short antizapret-vpn)

exec /usr/bin/dumb-init node server.js