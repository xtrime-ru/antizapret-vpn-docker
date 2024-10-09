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

ADGUARDHOME_USERNAME=${ADGUARDHOME_USERNAME:-"admin"}
if [[ -n $ADGUARDHOME_PASSWORD ]]; then
    ADGUARDHOME_PASSWORD_HASH=$(htpasswd -B -C 10 -n -b "$ADGUARDHOME_USERNAME" "$ADGUARDHOME_PASSWORD")
    ADGUARDHOME_PASSWORD_HASH=${ADGUARDHOME_PASSWORD_HASH#*:}
fi


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
ADGUARDHOME_PORT=${ADGUARDHOME_PORT:-"80"}
ADGUARDHOME_USERNAME='${ADGUARDHOME_USERNAME}'
ADGUARDHOME_PASSWORD_HASH='${ADGUARDHOME_PASSWORD_HASH}'
EOF
source /etc/default/antizapret
# autoload vars when logging in into shell with 'bash -l'
ln -sf /etc/default/antizapret /etc/profile.d/antizapret.sh


# creating custom hosts files if they have not yet been initialized
for file in $(echo {exclude,include}-hosts-custom.txt); do
    path=/root/antizapret/config/custom/$file
    [ ! -f $path ] && touch $path
done

# Changing the timer for updating lists
sed -i "s/^OnUnitActiveSec=6h/OnUnitActiveSec=$UPDATE_TIMER/g" /etc/systemd/system/antizapret-update.timer

# output systemd logs to docker logs since container boot
postrun 'until [[ "$(systemctl is-active systemd-journald)" == "active" ]]; do sleep 0.1; done; journalctl --boot --follow --lines=all --no-hostname'

# AdGuard initialization
/bin/cp --update=none /root/adguardhome/* /opt/adguardhome/conf/
yq -i '
    .users[0].name="'$ADGUARDHOME_USERNAME'" |
    .users[0].password="'$ADGUARDHOME_PASSWORD_HASH'" |
    .dns.bind_hosts=["127.0.0.1","'$SELF_IP'"]
    ' /opt/adguardhome/conf/AdGuardHome.yaml

# reload az results for mount dirs
/root/antizapret/parse.sh

# systemd init
exec /usr/sbin/init
