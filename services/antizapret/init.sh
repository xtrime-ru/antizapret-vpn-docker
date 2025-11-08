#!/bin/bash

set -e
set -x

# run commands after systemd initialization
function postrun () {
    local waiter="until ps -p 1 | grep -q systemd; do sleep 0.1; done"
    nohup bash -c "$waiter; $@" &
}


# save DNS variables to /etc/default/antizapret
# in order to systemd services can access them
cat << EOF | sponge /etc/default/antizapret
PYTHONUNBUFFERED=1
SELF_IP=$(hostname -i)
DOCKER_SUBNET=$(ip r | awk '/default/ {dev=$5} !/default/ && $0 ~ dev {print $1}')
ROUTES='${ROUTES:-""}'
DNS=${DNS:-"127.0.0.1"}
LC_ALL=C.UTF-8
EOF
source /etc/default/antizapret
# autoload vars when logging in into shell with 'bash -l'
ln -sf /etc/default/antizapret /etc/profile.d/antizapret.sh


# creating custom hosts files if they have not yet been initialized
for file in $(echo {exclude,include}-{hosts,ips}-custom.txt); do
    path=/root/antizapret/config/custom/$file
    [ ! -f $path ] && touch $path
done

rm /root/antizapret/config/custom/exclude-ips-custom.txt

(cat /root/antizapret/result/* /root/antizapret/config/custom/* | md5sum) > /tmp/config_md5

# add routes from env ROUTES
postrun 'while true; do /routes.sh; sleep 60; done'

# output systemd logs to docker logs since container boot
postrun 'until [[ "$(systemctl is-active systemd-journald)" == "active" ]]; do sleep 1; done; journalctl --boot --follow --lines=all --no-hostname'

[ -d /root/antizapret/result_dist ] && /bin/cp --update=none /root/antizapret/result_dist/* /root/antizapret/result/

# systemd init
exec /usr/sbin/init
