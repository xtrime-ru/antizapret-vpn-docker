#!/usr/bin/env bash
set -ex

first_run=true
running=true
trap 'running=false; ( [ -n "$sleep_pid" ] && kill "$sleep_pid" ); ./block.sh clear' \
    SIGTERM SIGINT SIGQUIT EXIT

fail_count=0
while [ "$running" = true ]; do
    if ./download.sh "$V4_URL" "$V4_FILE" && ./download.sh "$V6_URL" "$V6_FILE"; then
        fail_count=0
    else
        if [ "$first_run" = true ]; then
          echo 'Download failed on start. Exiting.'
          exit 2
        fi
        first_run=false
        fail_count=$((fail_count + 1))
        echo "Download failed (attempt $fail_count/$FAIL_LIMIT)"
        if [ "$fail_count" -ge "$FAIL_LIMIT" ]; then
            echo "$FAIL_LIMIT download failures in a row, exiting"
            exit 1
        fi
    fi

    ./block.sh

    sleep "$INTERVAL" &
    sleep_pid=$!
    wait "$sleep_pid" || true
    sleep_pid=
done
