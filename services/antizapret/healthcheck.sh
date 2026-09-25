#!/usr/bin/env bash

exec > >(tee /proc/1/fd/1) 2>&1

set -e

source /etc/default/antizapret
FLAG_FILE="/dev/shm/.dns_started"

function cached_lists_available() {
    [ -z "${IPS_URL:-}" ] || [ -s /root/antizapret/config/include-ips-dist.txt ] || [ -s /root/antizapret/result/ips.txt ] || return 1
    [ -z "${IPS_WORLD_URL:-}" ] || [ -s /root/antizapret/config/include-ips-world-dist.txt ] || [ -s /root/antizapret/result/ips-world.txt ] || return 1
    [ -z "${ASN_URL:-}" ] || [ -s /root/antizapret/config/include-asn-dist.txt ] || [ -s /root/antizapret/result/asn.txt ] || return 1
    [ -z "${ASN_WORLD_URL:-}" ] || [ -s /root/antizapret/config/include-asn-world-dist.txt ] || [ -s /root/antizapret/result/asn-world.txt ] || return 1
}

function cleanup() {
  excode=$?;
  trap - EXIT;

  if [ "$excode" -ne 0 ]; then
    echo 'healthcheck fail. Killing pid 1.'
    kill -TERM 1
    sleep 10
    kill -KILL 1
  fi
}
if [ -f "$FLAG_FILE" ]; then
  trap cleanup EXIT HUP INT QUIT PIPE TERM
fi

[ "$WARP_ENABLED" != "1" ] || warp-cli --accept-tos status | grep -q Connected

RUNNING_COUNT=$(pgrep -f "[/]usr/bin/dnsmap" | wc -l)
if [ "$RUNNING_COUNT" -eq 0 ]; then
  if [ -f "$FLAG_FILE" ]; then
    echo "healthcheck: dnsmap not found"
  else
    echo "healthcheck: waiting dnsmap to start"
  fi
  exit 1
fi

# List refresh failures must not trigger the liveness cleanup above.
trap - EXIT HUP INT QUIT PIPE TERM

OLD=$( cat /dev/shm/.config_md5 )
NEW=$( cat /root/antizapret/result/* /root/antizapret/config/custom/* 2>/dev/null | md5sum )
if [[ "$OLD" != "$NEW" ]]; then
    echo "healthcheck: config files changed"
    if timeout --kill-after=5s 5m doall; then
        if curl --max-time 10 -sf "http://127.0.0.1/update/"; then
            ( cat /root/antizapret/result/* /root/antizapret/config/custom/* 2>/dev/null | md5sum ) > /dev/shm/.config_md5
        else
            echo 'healthcheck: API update failed, will retry'
        fi
    elif cached_lists_available; then
        echo 'healthcheck: doall failed, keeping existing lists'
    else
        echo 'healthcheck: doall failed and no cached lists are available'
        exit 1
    fi
fi
