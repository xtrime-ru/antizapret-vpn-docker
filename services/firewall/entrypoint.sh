#!/usr/bin/env bash
set -ex

first_run=true
sleep_pid=

cleanup() {
    trap - HUP INT QUIT PIPE TERM
    if [ -n "$sleep_pid" ]; then
        kill "$sleep_pid" 2>/dev/null || true
    fi
    ./block.sh clear
    exit 0
}

trap cleanup HUP INT QUIT PIPE TERM

while true; do
    delay=30
    if ./download.sh "$V4_URL" "$V4_FILE" && ./download.sh "$V6_URL" "$V6_FILE"; then
        echo 'download ok'
        ./block.sh
        delay="$INTERVAL"
    elif [ "$first_run" = true ]; then
        if [ ! -f "$V4_FILE" ]; then
            echo 'Error: Download failed on start. Exiting.'
            exit 2
        fi
        ./block.sh
    fi

    first_run=false
    sleep "$delay" &
    sleep_pid=$!
    wait "$sleep_pid" || true
    sleep_pid=
done
