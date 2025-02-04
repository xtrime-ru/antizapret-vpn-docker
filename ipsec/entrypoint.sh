#!/usr/bin/env bash

sed -i "s/leftsubnet=0.0.0.0\/0/leftsubnet=10.224.0.0\/15/g" /opt/src/run.sh

export ANTIZAPRET_SUBNET=${ANTIZAPRET_SUBNET:-"10.224.0.0/15"}
export ANTIZAPRET_IP=$(dig +short antizapret)

nohup bash -c "
    until ps | grep -q xl2tpd; do sleep 0.1; done
    ip route add $ANTIZAPRET_SUBNET via $ANTIZAPRET_IP
" &

exec /opt/src/run.sh
