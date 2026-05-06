#!/bin/bash -e

HERE="$(dirname "$(readlink -f "${0}")")"
cd "$HERE"

if [ -s /etc/default/antizapret ]; then
    set -a
    source /etc/default/antizapret
    set +a
fi

if [ -n "$DOALL_DISABLED" ]; then
    echo "DoAll disabled. Exiting now..."
    exit 0
fi

lock_file="/tmp/.doall_lock"
while [ -f "$lock_file" ]; do
  echo "DoAll already running. Waiting..."
  sleep 5
done

touch "$lock_file"

trap 'rm -f $lock_file' \
    SIGTERM SIGINT SIGQUIT EXIT

download_failed=false
echo "run download.sh" && ./download.sh || download_failed=true
echo "run parse.sh" && ./parse.sh || exit 2

echo "Rules updated"
if [ "$download_failed" = true ]; then
  echo 'Warning: Cant download some lists'
fi
exit 0