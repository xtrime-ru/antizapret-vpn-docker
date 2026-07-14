#!/usr/bin/env bash

exec > >(tee /proc/1/fd/1) 2>&1

set -e

FLAG_FILE="/tmp/.dns_started"

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

RUNNING_COUNT=$(pgrep -f "[/]usr/bin/dnsmap" | wc -l)
if [ "$RUNNING_COUNT" -eq 0 ]; then
  if [ -f "$FLAG_FILE" ]; then
    echo "healthcheck: dnsmap not found"
  else
    echo "healthcheck: waiting dnsmap to start"
  fi
  exit 1
fi

OLD=$( cat /.config_md5 )
NEW=$( cat /root/antizapret/result/* /root/antizapret/config/custom/* 2>/dev/null | md5sum )
if [[ "$OLD" != "$NEW" ]]; then
    echo "healthcheck: config files changed"
    timeout 5m doall || echo 'healthcheck: doall timeout in healthcheck'
    curl --max-time 60 -sf "http://127.0.0.1/update/"
    ( cat /root/antizapret/result/* /root/antizapret/config/custom/* 2>/dev/null | md5sum ) > /.config_md5
fi
