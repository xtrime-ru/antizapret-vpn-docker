#!/usr/bin/env bash
set -ex

running=true
trap 'running=false; ( [ -n "$sleep_pid" ] && kill "$sleep_pid" ); ./block.sh clear' \
    SIGTERM SIGINT SIGQUIT EXIT

while [ "$running" = true ]; do
    ./download.sh "$V4_URL" "$V4_FILE"
    ./download.sh "$V6_URL" "$V6_FILE"

    ./block.sh

    sleep "$INTERVAL" &
    sleep_pid=$!
    wait "$sleep_pid" || true
    sleep_pid=
done
