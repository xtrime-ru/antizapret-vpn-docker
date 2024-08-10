#!/usr/bin/env bash

sed -i "s/leftsubnet=0.0.0.0\/0/leftsubnet=10.0.0.0\/8/g" /opt/src/run.sh

nohup bash -c '
    until ps | grep -q xl2tpd; do sleep 0.1; done
    antizapret_ip=$(dig +short antizapret-vpn)
    ip route add 10.0.0.0/8 via "$antizapret_ip"
    iptables -t nat -A OUTPUT -d 10.0.0.1/32 -j DNAT --to-destination "$antizapret_ip"
    iptables -t nat -A PREROUTING -d 10.0.0.1/32 -j DNAT --to-destination "$antizapret_ip"
' &

exec /opt/src/run.sh
