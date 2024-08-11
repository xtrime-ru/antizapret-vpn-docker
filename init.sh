#!/usr/bin/env bash

if [[ -n "${DNS}" ]]; then
    echo "nameserver $DNS" >> /etc/resolv.conf
fi

start=$(date +%T)
nohup bash -c "sleep 1 && cd /root/antizapret/ && ./process.sh && journalctl -f --since=$start" &

/root/generate.sh \
&& exec /usr/sbin/init