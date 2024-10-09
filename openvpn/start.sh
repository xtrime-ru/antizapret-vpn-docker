#!/bin/bash
# Exit immediately if a command exits with a non-zero status
set -e
set -x


cat << EOF | sponge /etc/environment
OPENVPN_ADMIN_USERNAME='${OPENVPN_ADMIN_USERNAME:-"admin"}'
OPENVPN_PORT=${OPENVPN_PORT:-"1194"}
OPENVPN_EXTERNAL_IP='${OPENVPN_EXTERNAL_IP:-$(curl -4 icanhazip.com)}'
OPENVPN_LOCAL_IP_RANGE='${OPENVPN_LOCAL_IP_RANGE:-"10.1.165.0"}'
OPENVPN_DNS='${OPENVPN_DNS:-"10.1.165.1"}'
DOCKER_SUBNET='172.18.0.0/16'
ANTIZAPRET_SUBNET='10.224.0.0/15'
NIC='$(ip -4 route ls | grep default | grep -Po '(?<=dev )(\S+)' | head -1)'
OVDIR='${OVDIR:-"/etc/openvpn"}'
EOF
source /etc/environment
ln -sf /etc/environment /etc/profile.d/environment.sh

# Change to the OpenVPN GUI directory
cd /opt/openvpn-gui

# Create the database directory if it does not exist
mkdir -p db
echo "db dir created on this local path:"
pwd
echo "db dir contents:"
ls -lrt

CONF_FILE="/opt/openvpn-gui/conf/app.conf"

#Add static configuration
cat > $CONF_FILE <<- EOM
; we use this when building the app.
AppName = "openvpn-ui"
Theme = "blue"
OpenVpnManagementNetwork = "tcp"
OpenVpnServerAddress = "127.0.0.1"
CopyRequestBody = true
AuthType = "password"
DbPath = "/opt/openvpn-gui/db/data.db"
EasyRsaPath = "/etc/openvpn/easy-rsa"
OpenVpnPath = "/etc/openvpn"
RunMode = prod
EnableGzip = true
EnableAdmin = false
SessionOn = true

EOM

# Set random session ID
i=$((1 + $RANDOM % 1000))
echo "SessionName = \"beegosession_$i\"" >> $CONF_FILE

# Set site name

if [[ -n "$SITE_NAME" ]]; then
    echo "SiteName = \"${SITE_NAME}\"" >> $CONF_FILE
else
    echo "SiteName = \"Admin\"" >> $CONF_FILE
fi

# Set openvpn docker container name
if [[ -n "$OPENVPN_SERVER_DOCKER_NAME" ]]; then
    echo "OpenVpnServerDockerName = \"${OPENVPN_SERVER_DOCKER_NAME}\"" >> $CONF_FILE
else
    echo "OpenVpnServerDockerName = \"openvpnserver\"" >> $CONF_FILE
fi

# Set openvpn management address
if [[ -n "$OPENVPN_MANAGEMENT_ADDRESS" ]]; then
    echo "OpenVpnManagementAddress = \"${OPENVPN_MANAGEMENT_ADDRESS}\"" >> $CONF_FILE
else
    echo "OpenVpnManagementAddress = \"127.0.0.1:2080\"" >> $CONF_FILE
fi

# Set site name
if [[ -n "$APP_PORT" ]]; then
    echo "httpport = ${APP_PORT}" >> $CONF_FILE
else
    echo "httpport = 8080" >> $CONF_FILE
fi

# Set URL PREFIX
if [[ -n "$URL_PREFIX" ]]; then
    echo "BaseURLPrefix = \"${URL_PREFIX}\"" >> $CONF_FILE
else
    echo "BaseURLPrefix = \"\"" >> $CONF_FILE
fi


# Set URL PREFIX
if [[ -n "$AUTO_INITIAL" ]]; then
    /opt/scripts/initial.sh
fi

if [ -f "/opt/antizapret/result/openvpn-blocked-ranges.txt" ]; then
    mkdir -p $OVDIR/ccd
    ln -sf /opt/antizapret/result/openvpn-blocked-ranges.txt $OVDIR/ccd/DEFAULT
fi

export AZ_HOST=$(dig +short antizapret-vpn)
ip route add 10.224.0.0/15 via $AZ_HOST

if [[ ${FORCE_FORWARD_DNS:-true} == true ]]; then
    dnsPorts=${FORCE_FORWARD_DNS_PORTS:-"53"}
    for dnsPort in $dnsPorts; do
        iptables -t nat -A PREROUTING -p udp --dport $dnsPort -j DNAT --to-destination $AZ_HOST
    done
fi

# Start the OpenVPN GUI
echo "Starting openvpn-ui !"
./openvpn-ui
