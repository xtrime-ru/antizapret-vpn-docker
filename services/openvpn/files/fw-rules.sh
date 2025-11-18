#!/bin/bash
# Exit immediately if a command exits with a non-zero status
set -e
set -x

cat << EOF | sponge /etc/environment
OPENVPN_LOCAL_IP_RANGE='${OPENVPN_LOCAL_IP_RANGE:-"10.1.165.0"}'
OPENVPN_DNS='${OPENVPN_DNS:-"10.224.0.1"}'
AZ_LOCAL_SUBNET=${AZ_LOCAL_SUBNET:-"10.224.0.0/15"}
AZ_WORLD_SUBNET=${AZ_WORLD_SUBNET:-"10.226.0.0/15"}
DOCKER_SUBNET='$(ip r | awk '/default/ {dev=$5} !/default/ && $0 ~ dev {print $1}' | tail -n1)'
NIC='$(ip -4 route ls | grep default | grep -Po '(?<=dev )(\S+)' | head -1)'
OVDIR='${OVDIR:-"/etc/openvpn"}'
EOF
source /etc/environment
ln -sf /etc/environment /etc/profile.d/environment.sh

iptables -t nat -N masq_not_local;
iptables -t nat -A POSTROUTING -s ${OPENVPN_LOCAL_IP_RANGE}/24 -j masq_not_local;
iptables -t nat -A masq_not_local -d ${DOCKER_SUBNET} -j RETURN;
iptables -t nat -A masq_not_local -d ${AZ_LOCAL_SUBNET} -j RETURN;
iptables -t nat -A masq_not_local -d ${AZ_WORLD_SUBNET} -j RETURN;
iptables -t nat -A masq_not_local -j MASQUERADE;

touch $OVDIR/openvpn-blocked-ranges.txt
if [ -f "/opt/antizapret/result/openvpn-blocked-ranges.txt" ]; then
    cp -f /opt/antizapret/result/openvpn-blocked-ranges.txt $OVDIR/openvpn-blocked-ranges.txt
fi

./routes.sh --vpn
