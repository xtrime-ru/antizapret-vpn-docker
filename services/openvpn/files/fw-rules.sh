#!/bin/bash
# Exit immediately if a command exits with a non-zero status
set -e
set -x

DOCKER_SUBNET="$(ipcalc "$(ip -4 addr show dev eth0 | awk '$1=="inet" {print $2; exit}')" | awk '/Network:/ {print $2}')"

cat << EOF | sponge /etc/environment
OPENVPN_LOCAL_IP_RANGE='${OPENVPN_LOCAL_IP_RANGE:-"10.1.165.0"}'
OPENVPN_DNS='${OPENVPN_DNS:-"14.16.0.1"}'
AZ_SUBNET=${AZ_SUBNET:-"14.16.0.0/14"}
DOCKER_SUBNET=${DOCKER_SUBNET}
NIC='$(ip -4 route ls | grep default | grep -Po '(?<=dev )(\S+)' | head -1)'
OVDIR='${OVDIR:-"/etc/openvpn"}'
EOF
source /etc/environment
ln -sf /etc/environment /etc/profile.d/environment.sh

iptables -t nat -N masq_not_local;
iptables -t nat -A POSTROUTING -s ${OPENVPN_LOCAL_IP_RANGE}/24 -j masq_not_local;
iptables -t nat -A masq_not_local -d ${DOCKER_SUBNET} -p tcp --dport 53 -j RETURN;
iptables -t nat -A masq_not_local -d ${DOCKER_SUBNET} -p udp --dport 53 -j RETURN;
iptables -t nat -A masq_not_local -d ${DOCKER_SUBNET} -j MASQUERADE;
iptables -t nat -A masq_not_local -d ${AZ_SUBNET} -j RETURN;
iptables -t nat -A masq_not_local -j MASQUERADE;

./routes.sh --vpn --dns-file /opt/antizapret/result/dns.txt &
