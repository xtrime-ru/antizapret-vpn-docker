#!/bin/bash -e


# run commands after systemd initialization

function postrun () {
    waiter="until ps -p 1 | grep -q systemd; do sleep 0.1; done; sleep 1"
    nohup bash -c "$waiter; $@" &
}


# resolve domain address to ip address

function resolve () {
    # $1 domain/ip address, $2 fallback domain/ip address
    ipcalc () { ipcalc-ng --no-decorate -o $1 2> /dev/null; }
    ipaddr=$(ipcalc $1 || ipcalc $2)
    echo ${ipaddr:-127.0.0.11} # fallback to docker internal dns
}


# save DNS variables to /etc/default/antizapret
# in order to systemd services can access them

cat << EOF | tee /etc/default/antizapret
DNS=$(resolve $DNS)
DNS_RU=$(resolve $DNS_RU 77.88.8.8)
EOF


# add a symlink for quick access
ln -sf /root/antizapret/doall.sh /usr/bin/doall

# autoload vars when logging in into shell with 'bash -l' 
ln -sf /etc/default/antizapret /etc/profile.d/antizapret.sh


# populating directories with files
cp -rv --update=none /rootfs/etc/openvpn/* /etc/openvpn

for file in $(echo {exclude,include}-{ips,hosts}-custom.txt); do
    path=/root/antizapret/config/custom/$file
    [ ! -f $path ] && touch $path
done


# generate certs/keys/profiles for OpenVPN
/root/openvpn/generate.sh


# output systemd logs to docker logs
postrun journalctl -f --no-hostname --since '2000-01-01 00:00:00'


# systemd init
exec /usr/sbin/init