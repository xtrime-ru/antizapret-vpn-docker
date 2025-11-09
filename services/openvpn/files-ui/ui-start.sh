#!/bin/bash
# Exit immediately if a command exits with a non-zero status
set -e

# Directory where OpenVPN configuration files are stored
OPENVPN_DIR=$(grep -E "^OpenVpnPath\s*=" openvpn-ui/conf/app.conf | cut -d= -f2 | tr -d '"' | tr -d '[:space:]')
echo "Init. OVPN path: $OPENVPN_DIR"

# Change to the /opt directory
cd /opt/

# If the provisioned file does not exist in the OpenVPN directory, prepare the certificates and create the provisioned file
if [ ! -f $OPENVPN_DIR/.provisioned ]; then
  #echo "Preparing certificates"
  mkdir -p $OPENVPN_DIR
  mkdir -p $OPENVPN_DIR/log

  # Uncomment line below to generate CA and server certificates (should be done on the side of OpenVPN container or server however)
  #./scripts/generate_ca_and_server_certs.sh

  # Create the provisioned file
  touch $OPENVPN_DIR/.provisioned

  echo "First OpenVPN UI start."
fi

# Change to the OpenVPN GUI directory
cd /opt/openvpn-ui

# Create the database directory if it does not exist
mkdir -p db

export DOCKER_SUBNET=$(ip r | awk '/default/ {dev=$5} !/default/ && $0 ~ dev {print $1}' | tail -n1)

cat << EOF | sponge /etc/environment
OPENVPN_EXTERNAL_IP='${OPENVPN_EXTERNAL_IP:-$(curl -4 icanhazip.com)}'
OPENVPN_LOCAL_IP_RANGE='${OPENVPN_LOCAL_IP_RANGE:-"10.1.165.0"}'
DOCKER_SUBNET='${DOCKER_SUBNET}'
OPENVPN_DNS='${OPENVPN_DNS:-"10.224.0.1"}'
NIC='$(ip -4 route | grep default | grep -Po '(?<=dev )(\S+)' | head -1)'
OVDIR='${OVDIR:-"/etc/openvpn"}'
EOF
source /etc/environment
ln -sf /etc/environment /etc/profile.d/environment.sh

if [ ! -f /opt/openvpn-ui/db/data.db ]; then
    cp /opt/openvpn-ui/init.db /opt/openvpn-ui/db/data.db

    sqlite3 /opt/openvpn-ui/db/data.db <<EOS
        update o_v_client_config set server_address = '${OPENVPN_EXTERNAL_IP}' where profile = 'default';
        update o_v_config set
            server = 'server ${OPENVPN_LOCAL_IP_RANGE} 255.255.255.0',
            route = 'route ${DOCKER_SUBNET} 255.255.255.0',
            d_n_s_server1 = 'push "dhcp-option DNS ${OPENVPN_DNS}"'
        where profile = 'default';
EOS
    [ $? -gt 0 ] && echo "SQLite migration failed" && exit 1

fi

# Start the OpenVPN GUI
echo "Starting OpenVPN UI!"
exec ./openvpn-ui