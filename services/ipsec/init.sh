#!/usr/bin/env bash


export TARGET_SCRIPT="/opt/src/run.sh"
export ANTIZAPRET_IP=$(dig +short antizapret)
export ANTIZAPRET_SUBNET=${ANTIZAPRET_SUBNET:-"10.224.0.0/15"}


sed -i "s|leftsubnet=0.0.0.0/0|leftsubnet=${ANTIZAPRET_SUBNET}|g" $TARGET_SCRIPT

nohup bash -c "
    until ps | grep -q xl2tpd; do sleep 0.1; done
    ip route add $ANTIZAPRET_SUBNET via $ANTIZAPRET_IP
" &


exec $TARGET_SCRIPT
