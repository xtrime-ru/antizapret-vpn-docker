#!/bin/bash -e


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

# save DNS variables to /etc/default/antizapret
# in order to systemd services can access them

cat << EOF | sponge /etc/default/antizapret
DNS=$(resolve $DNS 127.0.0.11)
DNS_RU=$(resolve $DNS_RU 77.88.8.8)
ADGUARD=${ADGUARD:-0}
LOG_DNS=${LOG_DNS:-0}
PYTHONUNBUFFERED=1
SELF_IP=$(hostname -i)
SKIP_UPDATE_FROM_ZAPRET=${SKIP_UPDATE_FROM_ZAPRET:-false}
UPDATE_TIMER=${UPDATE_TIMER:-"6h"}
EOF

source /etc/default/antizapret

# autoload vars when logging in into shell with 'bash -l'
ln -sf /etc/default/antizapret /etc/profile.d/antizapret.sh


for file in $(echo {exclude,include}-{hosts,regex}-custom.txt); do
    path=/root/antizapret/config/custom/$file
    [ ! -f $path ] && touch $path
done

# Changing the timer for updating lists
sed -i "s/^OnUnitActiveSec=6h/OnUnitActiveSec=$UPDATE_TIMER/g" /etc/systemd/system/antizapret-update.timer

# output systemd logs to docker logs since container boot
postrun 'until [[ "$(systemctl is-active systemd-journald)" == "active" ]]; do sleep 0.1; done; journalctl --boot --follow --lines=all --no-hostname'

# systemd init
exec /usr/sbin/init
