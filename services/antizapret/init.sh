#!/bin/bash

set -e
set -x

# run commands after systemd initialization
function postrun () {
    local waiter="until ps -p 1 | grep -q systemd; do sleep 0.1; done"
    nohup bash -c "$waiter; $@" &
}


# resolve domain address to ip address
function resolve () {
    # $1 domain/ip address, $2 fallback ip address
    ipcalc () { ipcalc-ng --no-decorate -o $1 2> /dev/null; }
    echo "$(ipcalc $1 || echo $2)"
}


ADGUARDHOME_USERNAME=${ADGUARDHOME_USERNAME:-"admin"}
if [[ -n $ADGUARDHOME_PASSWORD ]]; then
    ADGUARDHOME_PASSWORD_HASH=$(htpasswd -B -C 10 -n -b "$ADGUARDHOME_USERNAME" "$ADGUARDHOME_PASSWORD")
    ADGUARDHOME_PASSWORD_HASH=${ADGUARDHOME_PASSWORD_HASH#*:}
fi


# save DNS variables to /etc/default/antizapret
# in order to systemd services can access them
cat << EOF | sponge /etc/default/antizapret
PYTHONUNBUFFERED=1
SELF_IP=$(hostname -i)
DOCKER_SUBNET=$(ip r | awk '/default/ {dev=$5} !/default/ && $0 ~ dev {print $1}')
SKIP_UPDATE_FROM_ZAPRET=${SKIP_UPDATE_FROM_ZAPRET:-false}
UPDATE_TIMER=${UPDATE_TIMER:-"6h"}
ROUTES='${ROUTES:-""}'
IP_LIST='${IP_LIST:-""}'
LIST='${LISTS:-""}'
ADGUARDHOME_PORT=${ADGUARDHOME_PORT:-"3000"}
ADGUARDHOME_USERNAME='${ADGUARDHOME_USERNAME}'
ADGUARDHOME_PASSWORD_HASH='${ADGUARDHOME_PASSWORD_HASH}'
DNS=${DNS:-"8.8.8.8"}
EOF
source /etc/default/antizapret
# autoload vars when logging in into shell with 'bash -l'
ln -sf /etc/default/antizapret /etc/profile.d/antizapret.sh


# creating custom hosts files if they have not yet been initialized
for file in $(echo {exclude,include}-{hosts,ips}-custom.txt); do
    path=/root/antizapret/config/custom/$file
    [ ! -f $path ] && touch $path
done

# add routes from env ROUTES
postrun '/routes.sh'

# output systemd logs to docker logs since container boot
postrun 'until [[ "$(systemctl is-active systemd-journald)" == "active" ]]; do sleep 1; done; journalctl --boot --follow --lines=all --no-hostname'

# AdGuard initialization
/bin/cp --update=none /root/adguardhome/* /opt/adguardhome/conf/
if [ -d /root/antizapret/result_dist ]; then
    /bin/cp --update=none /root/antizapret/result_dist/* /root/antizapret/result/
fi
/bin/cp --update=none /root/antizapret/result/adguard_upstream_dns_file /opt/adguardhome/conf/upstream_dns_file

yq -i '
    .http.address="0.0.0.0:'$ADGUARDHOME_PORT'" |
    .users[0].name="'$ADGUARDHOME_USERNAME'" |
    .users[0].password="'$ADGUARDHOME_PASSWORD_HASH'" |
    .dns.bind_hosts=["127.0.0.1","'$SELF_IP'"]
    ' /opt/adguardhome/conf/AdGuardHome.yaml

# systemd init
exec /usr/sbin/init
