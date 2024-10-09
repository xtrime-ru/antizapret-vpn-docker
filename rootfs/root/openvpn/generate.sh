#!/bin/bash -e

HERE="$(dirname "$(readlink -f "${0}")")"
cd "$HERE/../easyrsa"

export EASYRSA_CERT_EXPIRE=3650

set +e

export PORT=${OPENVPN_PORT:-1194}
export SERVER="$OPENVPN_HOST"

if [[ -z $OPENVPN_HOST ]]; then
    for i in 1 2 3 4 5; do
        SERVER="$(curl -s -4 icanhazip.com)"
        [[ "$?" == "0" ]] && break
        sleep 2
    done
    [[ ! "$SERVER" ]] && echo "Can't determine global IP address!" && exit 8
fi

set -e


render() {
    local IFS=''
    local File="$1"
    while read -r line ; do
        while [[ "$line" =~ ^[^#] ]] && [[ "$line" =~ (\$\{[a-zA-Z_][a-zA-Z_0-9]*\}) ]] ; do
        local LHS=${BASH_REMATCH[1]}
        local RHS="$(eval echo "\"$LHS\"")"
        line=${line//$LHS/$RHS}
        done
        echo "$line"
    done < $File
}

load_key() {
    CA_CERT=$(grep -A 999 'BEGIN CERTIFICATE' -- "/etc/openvpn/server/keys/ca.crt")
    CLIENT_CERT=$(grep -A 999 'BEGIN CERTIFICATE' -- "/etc/openvpn/client/keys/antizapret-client.crt")
    CLIENT_KEY=$(cat -- "/etc/openvpn/client/keys/antizapret-client.key")
    CLIENT_TLS_CRYPT=$(grep -v '^#' -- "/etc/openvpn/server/keys/antizapret-tls-crypt.key")
    if [ ! "$CA_CERT" ] || [ ! "$CLIENT_CERT" ] || [ ! "$CLIENT_KEY" ] || [ ! "$CLIENT_TLS_CRYPT" ]
    then
            echo "Can't load client keys!"
            exit 7
    fi
}

build_pki() {
	rm -rf ./pki/
    ./easyrsa init-pki
    EASYRSA_BATCH=1 EASYRSA_REQ_CN="AntiZapret CA" ./easyrsa build-ca nopass
    EASYRSA_BATCH=1 ./easyrsa --days=36500 build-server-full "antizapret-server" nopass
    EASYRSA_BATCH=1 ./easyrsa --days=36500 build-client-full "antizapret-client" nopass
}

copy_keys() {
    cp ./pki/ca.crt /etc/openvpn/server/keys/ca.crt
    cp ./pki/issued/antizapret-server.crt /etc/openvpn/server/keys/antizapret-server.crt
	cp ./pki/private/antizapret-server.key /etc/openvpn/server/keys/antizapret-server.key

    cp ./pki/issued/antizapret-client.crt /etc/openvpn/client/keys/antizapret-client.crt
    cp ./pki/private/antizapret-client.key /etc/openvpn/client/keys/antizapret-client.key
}

if [[ ! -f  /etc/openvpn/server/keys/antizapret-tls-crypt.key ]]
then
	openvpn --genkey secret /etc/openvpn/server/keys/antizapret-tls-crypt.key
fi

if [[ ! -f /etc/openvpn/server/keys/ca.crt ]] || \
   [[ ! -f /etc/openvpn/server/keys/antizapret-server.crt ]] || \
   [[ ! -f  /etc/openvpn/server/keys/antizapret-server.key ]] || \
   [[ ! -f /etc/openvpn/client/keys/antizapret-client.crt ]] || \
   [[ ! -f  /etc/openvpn/client/keys/antizapret-client.key ]]
then
	build_pki
    copy_keys
fi

load_key
render "$HERE/templates/udp.conf" > "/etc/openvpn/client/antizapret-client-udp.ovpn"
render "$HERE/templates/tcp.conf" > "/etc/openvpn/client/antizapret-client-tcp.ovpn"
