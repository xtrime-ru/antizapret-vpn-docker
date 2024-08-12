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


# autoload vars when logging in into shell with 'bash -l' 
ln -sf /etc/default/antizapret /etc/profile.d/antizapret.sh


# output systemd logs to docker logs
postrun journalctl -f --since $(date +%T)


# add symlinks for calling via docker exec -it antizapret [command]
ln -sf /root/antizapret/doall.sh /usr/bin/doall



# populating files if path is mounted in Docker
cp -rv --update=none /rootfs/etc/openvpn/* /etc/openvpn


# check for files in /root/antizapret/result;
# execute doall.sh if file is missing or older than 6h

CHECKLIST=(
    blocked-ranges.txt
    dnsmasq-aliases-alt.conf
    hostlist_original.txt
    hostlist_zones.txt
    iplist_all.txt
    iplist_blockedbyip.txt
    iplist_blockedbyip_noid2971.txt
    iplist_special_range.txt
    knot-aliases-alt.conf
    openvpn-blocked-ranges.txt
    squid-whitelist-zones.conf
)

for FILE in ${CHECKLIST[@]}; do
    if [ ! -f /root/antizapret/result/$FILE ]; then
        /root/antizapret/doall.sh
        break
    else
        if test $(find /root/antizapret/result/$FILE -mmin +360); then
            /root/antizapret/doall.sh
            break
        else
            postrun /root/antizapret/process.sh
        fi
    fi
done


# check for files in /root/antizapret/config/persist

LISTS=(
    exclude-hosts-custom.txt
    exclude-ips-custom.txt
    include-hosts-custom.txt
    include-ips-custom.txt
)

for FILE in ${LISTS[@]}; do
    path=/root/antizapret/config/persist/$FILE
    if [ ! -f $path ]; then touch $path; fi
done


# generate certs/keys/profiles for OpenVPN
/root/openvpn/generate.sh


# systemd init
exec /usr/sbin/init