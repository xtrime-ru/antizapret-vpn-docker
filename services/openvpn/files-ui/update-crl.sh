#!/usr/bin/env bash

set -ex

EASY_RSA="/usr/share/easy-rsa"
TEMP_PKI_DIR=/tmp/pki
mkdir -p $TEMP_PKI_DIR

cd "$EASY_RSA"
$EASY_RSA/easyrsa gen-crl
chmod +r $EASY_RSA/pki/crl.pem
openssl crl -in /etc/openvpn/pki/crl.pem -text | grep -E "Last|Next"