#!/bin/bash
export DOCKER_COMMAND="2"
export IP_CHOICE="2"
export IPV6_SUPPORT="n"
export PORT_CHOICE="2"
export DNS="13"
export CUSTOMIZE_ENC="y"
export SET_MGMT="management 127.0.0.1 2080"

export EXTERNAL_IP=$(curl -4 icanhazip.com)
export ENDPOINT=${OPENVPN_IP_FOR_CLIENT:-EXTERNAL_IP}
export IP_RANGE="10.1.165.0"
export PROTOCOL_CHOICE="1" #udp
export PORT="1194"
export TUN_NUMBER="0"
export DNS1="10.224.0.1"
export COMPRESSION_ENABLED="n"
export CIPHER_CHOICE="1" #AES-128-GCM
export CERT_TYPE="1" #ECDSA
export CERT_CURVE_CHOICE="1" #prime256v1
export RSA_KEY_SIZE_CHOICE="1" #prime256v1
export CC_CIPHER_CHOICE="1" #TLS-ECDHE-ECDSA-WITH-AES-128-GCM-SHA256
export DH_TYPE="1" #ECDH
export DH_CURVE_CHOICE="1" #prime256v1
export DH_KEY_SIZE_CHOICE="1" #prime256v1
export HMAC_ALG_CHOICE="1" #SHA256
export TLS_SIG="4" #no tls
export DISABLE_DEF_ROUTE_FOR_CLIENTS="y"
export CLIENT_TO_CLIENT="y"

if [[ ! -e /etc/openvpn/server.conf ]]; then
        bash /opt/scripts/openvpn-install-v2.sh
        echo "" > /etc/openvpn/.provisioned
		echo "script-security 2" >> /etc/openvpn/server.conf
        echo "auth-user-pass-verify /opt/scripts/auth_client.sh via-file" >> /etc/openvpn/server.conf
        echo "auth-user-pass" >> /etc/openvpn/client-template.txt
        echo "push \"route 10.224.0.0 255.254.0.0\"" >> /etc/openvpn/server.conf
        echo "    --= SETUP IS DONE ==-"
fi