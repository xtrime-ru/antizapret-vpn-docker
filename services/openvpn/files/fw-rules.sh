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

# Optional server-side client firewall (VPN_CLIENT_FIREWALL=true).
# Clients may only reach what is pushed to them (AZ_SUBNET + blocked ranges) plus internal services
# and their own subnet, and must not ping through the tunnel. tun has no cryptokey routing like
# WireGuard, so without this a client could run `ip route add <ip> dev tun0` and use the server as
# an exit to any address. Leave it off for full-tunnel (redirect-gateway) or custom client routes.
if [ "${VPN_CLIENT_FIREWALL:-false}" = "true" ]; then
    iptables -N az_clients;
    # Established flows skip the list: the destination is checked once, on the first packet.
    iptables -A az_clients -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT;
    iptables -A az_clients -d ${AZ_SUBNET} -j ACCEPT;
    iptables -A az_clients -d ${DOCKER_SUBNET} -j ACCEPT;
    iptables -A az_clients -d ${OPENVPN_LOCAL_IP_RANGE}/24 -j ACCEPT;
    # Blocked ranges pushed as `push "route <net> <mask>"` (same file the client config is built from).
    # iptables accepts the <net>/<mask> notation as is. The list changes -> healthcheck restarts the container.
    grep -oE 'route [0-9.]+ [0-9.]+' /opt/antizapret/result/openvpn-blocked-ranges.txt 2>/dev/null \
        | awk '{print $2 "/" $3}' | sort -u | while read -r range; do
        iptables -A az_clients -d "${range}" -j ACCEPT;
    done
    iptables -A az_clients -j REJECT --reject-with icmp-admin-prohibited;
    iptables -I FORWARD -s ${OPENVPN_LOCAL_IP_RANGE}/24 -j az_clients;

    # echo-request only, keep PMTUD/traceroute ICMP. Inserted last so it ends up above the az_clients jump.
    iptables -I FORWARD -s ${OPENVPN_LOCAL_IP_RANGE}/24 -p icmp --icmp-type echo-request -j DROP;
fi

routes --vpn &
