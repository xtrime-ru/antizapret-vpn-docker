#!/usr/bin/env bash

sed -i "s/leftsubnet=0.0.0.0\/0/leftsubnet=10.224.0.0\/15/g" /opt/src/run.sh

nohup bash -c '
    until ps | grep -q xl2tpd; do sleep 0.1; done
    antizapret_ip=$(dig +short antizapret-vpn)
    ip route add 10.224.0.0/15 via "$antizapret_ip"
' &

exec /opt/src/run.sh
