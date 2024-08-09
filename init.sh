#!/usr/bin/env bash

echo "nameserver 1.1.1.1" >> /etc/resolv.conf

nohup bash -c "sleep 1 && cd /root/antizapret/ && ./process.sh && journalctl -f" &

/root/generate.sh \
&& exec /usr/sbin/init