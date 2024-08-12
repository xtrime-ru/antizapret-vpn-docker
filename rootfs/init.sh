#!/bin/bash -e


# variables
HOSTNAME=antizapret


# fill hosts, hostname and resolv.conf
hostname -b $HOSTNAME

echo $HOSTNAME > /etc/hostname
echo 127.0.1.1 $HOSTNAME >> /etc/hosts


# run commands after systemd initialization
function postrun () {
    waiter="until ps -p 1 | grep -q systemd; do sleep 0.1; done; sleep 1"
    nohup bash -c "$waiter; $@" &
}


# output systemd logs to docker logs
postrun journalctl -f --since $(date +%T)


# add symlinks for calling via docker exec -it antizapret [command]
ln -sf /root/antizapret/doall.sh /usr/bin/doall


# populating files if path is mounted in Docker
cp -rv --update=none /rootfs/etc/openvpn/* /etc/openvpn


# check for files in /root/antizapret/result
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


# generate certs/keys/profiles for OpenVPN
/root/openvpn/generate.sh


# systemd init
exec /usr/sbin/init