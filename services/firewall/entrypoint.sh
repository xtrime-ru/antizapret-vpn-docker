#!/usr/bin/env bash
set -ex

first_run=true
running=true
trap 'trap - EXIT; running=false; ( [ -n "$sleep_pid" ] && kill "$sleep_pid" ); ./block.sh clear' \
    EXIT HUP INT QUIT PIPE TERM

while [ "$running" = true ]; do
    FAILED=true
    if ./download.sh "$V4_URL" "$V4_FILE" && ./download.sh "$V6_URL" "$V6_FILE"; then
      echo 'download ok'
      FAILED=false
    else
        if [ "$first_run" = true ] && [ ! -f "$V4_FILE" ]; then
          echo 'Error: Download failed on start. Exiting.'
          exit 2
        fi
    fi


    if [ "$FAILED" = false ] || [ "$first_run" = true ]; then
      ./block.sh
    fi

    first_run=false

    if [ "$FAILED" = true ]; then
      sleep 30 &
    else
      sleep "$INTERVAL" &
    fi
    sleep_pid=$!
    wait "$sleep_pid" || true
    sleep_pid=
done
