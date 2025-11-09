#!/usr/bin/env sh
set -e

DIFF=$( ( cat /root/antizapret/result/* /root/antizapret/config/custom/* | md5sum ) | cmp - /.config_md5 )
if [ -n "$DIFF" ]; then
    echo "Config files changed"
    BASE64_DATA=$(echo -n "$ADGUARDHOME_USERNAME:$ADGUARDHOME_PASSWORD" | base64)
    curl -s 'http://127.0.0.1:3000/control/filtering/refresh' -X 'POST' -H 'Content-Type: application/json' -H "Authorization: Basic $BASE64_DATA"  --data-raw '{"whitelist":false}'
    curl -s 'http://127.0.0.1:3000/control/filtering/refresh' -X 'POST' -H 'Content-Type: application/json' -H "Authorization: Basic $BASE64_DATA"  --data-raw '{"whitelist":true}'
    ( cat /root/antizapret/result/* /root/antizapret/config/custom/* | md5sum ) > /.config_md5
fi