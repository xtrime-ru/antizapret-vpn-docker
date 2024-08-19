#!/bin/bash -e


# run commands after systemd initialization

function postrun () {
    local waiter="until ps -p 1 | grep -q systemd; do sleep 0.1; done; sleep 1"
    nohup bash -c "$waiter; $@" &
}


# resolve domain address to ip address

function resolve () {
    # $1 domain/ip address, $2 fallback domain/ip address
    ipcalc () { ipcalc-ng --no-decorate -o $1 2> /dev/null; }
    local ipaddr=$(ipcalc $1 || ipcalc $2)
    echo ${ipaddr:-127.0.0.11} # fallback to docker internal dns
}


# set ciphers

function set_ciphers () {
    # $1 AES-128-CBC:AES-256-CBC[:...]
    local CIPHERS=AES-128-GCM:AES-256-GCM
    local ARGS=$([ -n "$1" ] && echo "$CIPHERS:$1" || echo "$CIPHERS")
    sed -i "s|data-ciphers .*|data-ciphers \"$ARGS\"|g" /etc/openvpn/server/*.conf
}


# save DNS variables to /etc/default/antizapret
# in order to systemd services can access them

cat << EOF | sponge /etc/default/antizapret
CBC_CIPHERS=${CBC_CIPHERS:-0}
DNS=$(resolve $DNS)
DNS_RU=$(resolve $DNS_RU 77.88.8.8)
PYTHONUNBUFFERED=1
EOF


# autoload vars when logging in into shell with 'bash -l'
ln -sf /etc/default/antizapret /etc/profile.d/antizapret.sh


# populating directories with files
cp -rv --update=none /etc/openvpn-default/* /etc/openvpn

for file in $(echo {exclude,include}-{ips,hosts,regex}-custom.txt); do
    path=/root/antizapret/config/custom/$file
    [ ! -f $path ] && touch $path
done


# generate certs/keys/profiles for OpenVPN
/root/openvpn/generate.sh


# swap between legacy ciphers and DCO-required ciphers
[[ "$CBC_CIPHERS" == 1 ]] && set_ciphers AES-128-CBC:AES-256-CBC || set_ciphers


# output systemd logs to docker logs since container boot
postrun 'journalctl --boot --follow --lines=all --no-hostname'


# systemd init
exec /usr/sbin/init
