#!/bin/bash
# Some protection
set -Eeuo pipefail

# Define default server vars if they are not set
export SRV_CN="${SRV_CN:=example.com}"
export SRV_CA="${SRV_CA:=Example CA}"
export OCSERV_DIR="/etc/ocserv"
export CA_DIR="${OCSERV_DIR}/ca"
export SSL_DIR="${OCSERV_DIR}/ssl"
OC_DEFAULT_ADDRESS=${OC_DEFAULT_ADDRESS:-"10.1.164.x"}
export OC_PORT=${OC_PORT:-"465"}
OC_USER=${OC_USER:-"admin"}
OC_USERPASS=${OC_USERPASS:-"pASSword"}
export OC_IPV4_CIDR="${OC_DEFAULT_ADDRESS/"x"/"0"}/24"
export OC_SECRET=${OC_SECRET:-"kvn"}
OC_ROUTE="/etc/ocserv/config-per-group"
CONFIG_FILES="/opt/antizapret/result/ips*"

# Create certs dirs
for sub_dir in $OCSERV_DIR/{"ssl","ca","config-per-group"}; do
    if [[ ! -d "$sub_dir" ]]; then
        mkdir -p "$sub_dir"
    fi
done
# Create old sample.config
if [[ -r /usr/share/doc/ocserv/sample.config && ! -e $OCSERV_DIR/sample.config ]]; then
    cp /usr/share/doc/ocserv/sample.config $OCSERV_DIR/
fi

# Create ocserv config files
if [[ ! -e $OCSERV_DIR/ocserv.conf ]]; then
  envsubst < /ocserv.tmpl  > $OCSERV_DIR/ocserv.conf
fi
if [[ ! -e $OC_ROUTE/az.0 ]]; then
  envsubst < /az.tmpl  > $OC_ROUTE/az.0
fi
# oc routes
cp -f $OC_ROUTE/az.0 $OC_ROUTE/az
sort -V $CONFIG_FILES | sed 's_.*_route = &_' >> $OC_ROUTE/az
# add user
if [[ ! -e $OCSERV_DIR/ocpasswd ]]; then
  ocpasswd -g az -c /etc/ocserv/ocpasswd $OC_USER
  printf '%s\n%s\n' "$OC_USERPASS" "$OC_USERPASS" | ocpasswd -g az -c $OCSERV_DIR/ocpasswd "$OC_USER"
fi


# Create template for CA SSL cert
if [[ ! -e "${CA_DIR}"/ca.tmpl ]]; then
  envsubst < /ca.tmpl  > "${CA_DIR}"/ca.tmpl
fi

# Generate empty revoke file
if [[ ! -e "${CA_DIR}"/crl.tmpl ]]; then
  envsubst < /crl.tmpl  > "${CA_DIR}"/crl.tmpl
fi

# Create template for server self-signed SSL cert
if [[ ! -e $SSL_DIR/server.tmpl ]]; then
  envsubst < /server.tmpl  > $SSL_DIR/server.tmpl
fi

# Copy certs from HTTPS-service
if [[ -e /https-data/privkey.pem \
&& -e /https-data/fullchain.pem ]]; then
    echo "Copy certs from HTTPS-service"
    cp -f /https-data/privkey.pem "$SSL_DIR/privkey.pem"
	cp -f /https-data/fullchain.pem "$SSL_DIR/fullchain.pem"
fi

routes --vpn &

# Start ocserv service
if [[ ! -e $SSL_DIR/privkey.pem \
|| ! -e $SSL_DIR/fullchain.pem ]]; then
    # Server certificates generation
    echo "generate-self-signed"
    certtool --generate-privkey --outfile "${CA_DIR}"/ca-key.pem
    certtool --generate-self-signed --load-privkey "${CA_DIR}"/ca-key.pem \
		--template "${CA_DIR}"/ca.tmpl --outfile "${CA_DIR}"/ca-cert.pem
    certtool --generate-crl --load-ca-privkey "${CA_DIR}"/ca-key.pem \
		--load-ca-certificate "${CA_DIR}"/ca-cert.pem \
		--template "${CA_DIR}"/crl.tmpl \
		--outfile "${CA_DIR}"/crl.pem
    certtool --generate-privkey --outfile $SSL_DIR/privkey.pem
    certtool --generate-certificate --load-privkey $SSL_DIR/privkey.pem \
	    --load-ca-certificate "${CA_DIR}"/ca-cert.pem \
		--load-ca-privkey "${CA_DIR}"/ca-key.pem \
		--template $SSL_DIR/server.tmpl \
		--outfile $SSL_DIR/fullchain.pem
fi

cat $CONFIG_FILES 2>/dev/null | md5sum | cut -d' ' -f1 > /.config_md5

# Configure network
mkdir -p /dev/net
if [ ! -c /dev/net/tun ]; then
    mknod /dev/net/tun c 10 200
fi

echo "Starting OpenConnect Server"
exec "$@" || { echo "Starting failed" >&2; exit 1; }
