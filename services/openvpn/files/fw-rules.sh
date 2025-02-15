#!/bin/bash
# Exit immediately if a command exits with a non-zero status
set -e
set -x

cat << EOF | sponge /etc/environment
OPENVPN_LOCAL_IP_RANGE='${OPENVPN_LOCAL_IP_RANGE:-"10.1.165.0"}'
OPENVPN_DNS='${OPENVPN_DNS:-"10.1.165.1"}'
ANTIZAPRET_SUBNET=${ANTIZAPRET_SUBNET:-"10.224.0.0/15"}
NIC='$(ip -4 route ls | grep default | grep -Po '(?<=dev )(\S+)' | head -1)'
OVDIR='${OVDIR:-"/etc/openvpn"}'
AZ_HOST='$(dig +short antizapret)'
EOF
source /etc/environment
ln -sf /etc/environment /etc/profile.d/environment.sh

iptables -t nat -N masq_not_local;
iptables -t nat -A POSTROUTING -s ${OPENVPN_LOCAL_IP_RANGE}/24 -j masq_not_local;
iptables -t nat -A masq_not_local -d ${AZ_HOST} -j RETURN;
iptables -t nat -A masq_not_local -d ${ANTIZAPRET_SUBNET} -j RETURN;
iptables -t nat -A masq_not_local -j MASQUERADE;

touch $OVDIR/openvpn-blocked-ranges.txt
if [ -f "/opt/antizapret/result/openvpn-blocked-ranges.txt" ]; then
    cp -f /opt/antizapret/result/openvpn-blocked-ranges.txt $OVDIR/openvpn-blocked-ranges.txt
fi

ip route add "$ANTIZAPRET_SUBNET" via "$AZ_HOST"

if [[ ${FORCE_FORWARD_DNS:-true} == true ]]; then
    dnsPorts=${FORCE_FORWARD_DNS_PORTS:-"53"}
    for dnsPort in $dnsPorts; do
        iptables -t nat -A PREROUTING -p tcp --dport "$dnsPort" -j DNAT --to-destination "$AZ_HOST"
        iptables -t nat -A PREROUTING -p udp --dport "$dnsPort" -j DNAT --to-destination "$AZ_HOST"
    done
fi
