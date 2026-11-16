#!/bin/bash
set -euo pipefail
cleanup() {
    trap - HUP INT QUIT PIPE TERM
    kill "$idle_pid" 2>/dev/null || true
    /root/block.sh clear
    exit 0
}
sleep infinity &
idle_pid=$!
trap cleanup HUP INT QUIT PIPE TERM
wait "$idle_pid"
