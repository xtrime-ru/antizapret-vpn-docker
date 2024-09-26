#!/bin/bash

set -e
cd /opt/cloak

if [ -z "${CK_UID}" ]; then
    CK_UID=$(ck-server -uid)
fi

if [ -z "${CK_ADMIN_UID}" ]; then
    CK_ADMIN_UID=$(ck-server -uid)
fi

if [ -z "${CK_PRIVATE_KEY}" ]; then
    CK_PRIVATE_KEY=$(ck-server -key)
fi

if [ -z "${CK_REDIR_ADDR}" ]; then
    CK_REDIR_ADDR="cloudflare.com" 
fi

cat << EOF > /opt/cloak/config/config.json
{
  "ProxyBook": {
    "openvpn": [
      "udp",
      "127.0.0.1:${$OPENVPN_PORT}"
    ],
  },
  "BindAddr": [
    ":443",
    ":80"
  ],
  "BypassUID": [
    "${CK_UID}"
  ],
  "RedirAddr": "${CK_REDIR_ADDR}",
  "PrivateKey": "${CK_PRIVATE_KEY}",
  "AdminUID": "${CK_ADMIN_UID}",
  "DatabasePath": "/opt/cloak/config/userinfo.db"
}
EOF

echo "Configuration file ck-server.json created successfully."
