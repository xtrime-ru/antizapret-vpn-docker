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
DOCKER_SUBNET=$(ip r | awk '/default/ {dev=$5} !/default/ && $0 ~ dev {print $1}')
ROUTES='${ROUTES:-""}'
DNS=${DNS:-"127.0.0.1"}
CLIENT=${CLIENT:-"az-local"}
DOALL_DISABLED={$DOALL_DISABLED:-""}
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
iptables -t nat -A POSTROUTING -o eth0 -j MASQUERADE

# add routes from env ROUTES
postrun 'while true; do /routes.sh; sleep 60; done'

# output systemd logs to docker logs since container boot

postrun 'while true; do /opt/api/app; done'
postrun 'while true; do /usr/bin/doall; sleep 6h; done'
postrun 'while true; do /usr/bin/iperf3 -s -1; done'

# systemd init
exec /usr/bin/dnsmap -a 0.0.0.0 --iprange "$AZ_SUBNET"
