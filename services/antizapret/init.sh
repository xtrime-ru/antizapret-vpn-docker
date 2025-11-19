#!/bin/bash

set -e
set -x

# run commands after start
function postrun () {
    nohup bash -c "$@" &
}


# save DNS variables to /etc/default/antizapret
# in order to systemd services can access them
cat << EOF | sponge /etc/default/antizapret
PYTHONUNBUFFERED=1
SELF_IP=$(hostname -i)
DOCKER_SUBNET='$(ip -4 addr show dev eth0 | awk '$1=="inet" {print $2; exit}')'
DNS=${DNS:-"127.0.0.1"}
CLIENT=${CLIENT:-"az-local"}
DOALL_DISABLED=${DOALL_DISABLED:-""}
AZ_SUBNET=${AZ_SUBNET:-"10.224.0.0/15"}
LC_ALL=C.UTF-8
EOF
source /etc/default/antizapret
# autoload vars when logging in into shell with 'bash -l'
ln -sf /etc/default/antizapret /etc/profile.d/antizapret.sh


# creating custom hosts files if they have not yet been initialized
for file in $(echo {exclude,include}-{hosts,ips}-custom.txt); do
    path=/root/antizapret/config/custom/$file
    [[ "$file" == "exclude-ips-custom.txt" ]] && continue
    [ ! -f $path ] && touch $path
done

( cat /root/antizapret/result/* /root/antizapret/config/custom/* | md5sum ) > /.config_md5

# Prepare iptables for dnsmap.py
iptables -t nat -N dnsmap
iptables -t nat -A PREROUTING -d "${AZ_SUBNET}" -j dnsmap
iptables -t nat -A OUTPUT -d "${AZ_SUBNET}" -j dnsmap
for eth in $(ip link | grep -oE "eth[0-9]"); do
    iptables -t nat -A POSTROUTING -o "$eth" -j MASQUERADE
done

/routes.sh

# output systemd logs to docker logs since container boot

postrun 'while true; do /opt/api/app; done'
postrun 'while true; do /usr/bin/doall; sleep 6h; done'
postrun 'while true; do /usr/bin/iperf3 -s -1; done'

# systemd init
exec /usr/bin/dnsmap -a 0.0.0.0 --iprange "$AZ_SUBNET"
