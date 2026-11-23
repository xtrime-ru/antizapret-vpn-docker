#!/bin/bash
set -euo pipefail

if [ -f /dev/shm/.zapret_started ]; then
    . /etc/default/antizapret
    /opt/zapret2/init.d/sysv/zapret2 stop || true
fi

if [ "${IPTABLES_SAVE_DISABLED:-}" != 1 ]; then
    save="/root/antizapret/iptables/$(hostname -s).rules"
    temporary=$(mktemp "$save.XXXXXX")
    if iptables-save -t nat | grep '^-A dnsmap ' > "$temporary" && mv -f "$temporary" "$save"; then
        echo 'iptables saved'
    else
        rm -f "$temporary"
    fi
fi
