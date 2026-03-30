#!/bin/bash
set -e

#Variables
EASY_RSA=/usr/share/easy-rsa
OPENVPN_DIR=/etc/openvpn
echo "EasyRSA path: $EASY_RSA OVPN path: $OPENVPN_DIR"

detect_tls_mode() {
    local tls_line
    tls_line=$(grep -E '^[[:space:]]*(tls-auth|tls-crypt|tls-crypt-v2)[[:space:]]+' "$OPENVPN_DIR/server.conf" | tail -1 || true)
    case "$tls_line" in
        tls-crypt-v2\ *) printf '%s\n' "tls-crypt-v2" ;;
        tls-auth\ *) printf '%s\n' "tls-auth" ;;
        *) printf '%s\n' "tls-crypt" ;;
    esac
}

ensure_tls_crypt_v2_client_keys() {
    local issued_dir server_key cert_path client_name client_key
    issued_dir="$OPENVPN_DIR/pki/issued"
    server_key="$OPENVPN_DIR/pki/private/tls-crypt-v2-server.key"

    [[ -d "$issued_dir" ]] || return 0
    [[ -f "$server_key" ]] || return 0

    for cert_path in "$issued_dir"/*.crt; do
        [[ -f "$cert_path" ]] || continue
        client_name="$(basename "$cert_path" .crt)"
        [[ "$client_name" = "server" ]] && continue
        client_key="$OPENVPN_DIR/pki/private/${client_name}.tls-crypt-v2.key"
        if [[ ! -f "$client_key" || "$server_key" -nt "$client_key" ]]; then
            echo "Generating tls-crypt-v2 client key for $client_name..."
            openvpn --tls-crypt-v2 "$server_key" --genkey tls-crypt-v2-client "$client_key"
        fi
    done
}

generate_tls_server_material() {
    local tls_mode
    tls_mode="$(detect_tls_mode)"
    mkdir -p "$EASY_RSA/pki/private" "$OPENVPN_DIR/pki/private"
    if [[ "$tls_mode" = "tls-crypt-v2" ]]; then
        echo 'Generate tls-crypt-v2 server key...'
        openvpn --genkey tls-crypt-v2-server "$EASY_RSA/pki/private/tls-crypt-v2-server.key"
    else
        echo 'Generate HMAC signature...'
        openvpn --genkey --secret "$EASY_RSA/pki/ta.key"
    fi
}

INIT_FILE="/.inited"
rm -f "$INIT_FILE"
CONFIG_FILE="/opt/antizapret/result/openvpn-blocked-ranges.txt"
cat $CONFIG_FILE 2>/dev/null | md5sum | cut -d' ' -f1 > /.config_md5
touch $OPENVPN_DIR/openvpn-blocked-ranges.txt
if [ -f $CONFIG_FILE ]; then
    cp -f $CONFIG_FILE $OPENVPN_DIR/openvpn-blocked-ranges.txt
fi

mkdir -pv $OPENVPN_DIR/config
cp -vf /opt/app/easy-rsa.vars $OPENVPN_DIR/config/easy-rsa.vars

if [[ ! -f $OPENVPN_DIR/pki/ca.crt ]] || [[ ! -f $OPENVPN_DIR/pki/crl.pem ]]; then
    export EASYRSA_BATCH=1 # see https://superuser.com/questions/1331293/easy-rsa-v3-execute-build-ca-and-gen-req-silently
    cd $EASY_RSA

    # Building the CA
    echo 'Setting up public key infrastructure...'
    $EASY_RSA/easyrsa init-pki

    # Copy easy-rsa variables
    cp $OPENVPN_DIR/config/easy-rsa.vars $EASY_RSA/pki/vars

    # Listing env parameters:
    echo "Following EASYRSA variables will be used:"
    cat $EASY_RSA/pki/vars | awk '{$1=""; print $0}';

    echo 'Generating ertificate authority...'
    $EASY_RSA/easyrsa build-ca nopass

    # Creating the Server Certificate, Key, and Encryption Files
    echo 'Creating the Server Certificate...'
    $EASY_RSA/easyrsa gen-req server nopass

    echo 'Sign request...'
    $EASY_RSA/easyrsa sign-req server server

    echo 'Generate Diffie-Hellman key...'
    $EASY_RSA/easyrsa gen-dh

    generate_tls_server_material

    echo 'Create certificate revocation list (CRL)...'
    $EASY_RSA/easyrsa gen-crl
    chmod +r $EASY_RSA/pki/crl.pem

    # Copy to mounted volume
    cp -r $EASY_RSA/pki/. $OPENVPN_DIR/pki
else
    echo 'PKI already set up.'
    TLS_MODE="$(detect_tls_mode)"
    if [[ "$TLS_MODE" = "tls-crypt-v2" && ! -f $OPENVPN_DIR/pki/private/tls-crypt-v2-server.key ]]; then
        echo 'Missing tls-crypt-v2 server key. Generating...'
        mkdir -p "$OPENVPN_DIR/pki/private"
        openvpn --genkey tls-crypt-v2-server "$OPENVPN_DIR/pki/private/tls-crypt-v2-server.key"
    elif [[ "$TLS_MODE" != "tls-crypt-v2" && ! -f $OPENVPN_DIR/pki/ta.key ]]; then
        echo 'Missing shared TLS key. Generating...'
        openvpn --genkey --secret "$OPENVPN_DIR/pki/ta.key"
    fi
fi

if [[ "$(detect_tls_mode)" = "tls-crypt-v2" ]]; then
    ensure_tls_crypt_v2_client_keys
fi

# Listing env parameters:
echo "Following EASYRSA variables were set during CA init:"
cat $OPENVPN_DIR/pki/vars | awk '{$1=""; print $0}';

# Configure network
mkdir -p /dev/net
if [ ! -c /dev/net/tun ]; then
    mknod /dev/net/tun c 10 200
fi

if [[ ! -s fw-rules.sh ]]; then
    echo "No additional firewall rules to apply."
else
    echo "Applying firewall rules"
    ./fw-rules.sh
    echo 'Additional firewall rules applied.'
fi

mkdir -p $OPENVPN_DIR/clients
mkdir -p $OPENVPN_DIR/staticclients

echo 'Start openvpn process...'
tail -f $OPENVPN_DIR/log/*.log &

touch "$INIT_FILE"

/opt/app/update-crl.sh || kill -TERM 1
while true; do
    sleep 86400;
    echo "Update crl";
    /opt/app/update-crl.sh || kill -TERM 1
done &

exec /usr/local/sbin/openvpn --cd $OPENVPN_DIR --script-security 2 --config $OPENVPN_DIR/server.conf
