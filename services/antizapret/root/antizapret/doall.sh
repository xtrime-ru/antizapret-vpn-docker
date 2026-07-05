#!/bin/bash -e

HERE="$(dirname "$(readlink -f "${0}")")"
cd "$HERE"

if [ -s /etc/default/antizapret ]; then
    set -a
    source /etc/default/antizapret
    set +a
fi

function reload_dnsmap () {
    pkill -HUP -f '[d]nsmap' 2>/dev/null || true
}

LOCAL_OWNER_FILE="/tmp/.doall_owner"
RESULT_OWNER_FILE="/root/antizapret/result/.doall_owner"
LOCAL_OWNER="$(cat "$LOCAL_OWNER_FILE" 2>/dev/null || true)"
RESULT_OWNER="$(cat "$RESULT_OWNER_FILE" 2>/dev/null || true)"

if [ -n "$DOALL_DISABLED" ] || { [ -n "$RESULT_OWNER" ] && [ "$LOCAL_OWNER" != "$RESULT_OWNER" ]; }; then
    echo "DoAll disabled or owner mismatch. Reloading dnsmap only..."
    reload_dnsmap
    exit 0
fi

lock_file="/tmp/.doall_lock"
while [ -f "$lock_file" ]; do
  echo "DoAll already running. Waiting..."
  sleep 5
done

touch "$lock_file"

trap 'trap - EXIT; rm -f $lock_file' \
    EXIT HUP INT QUIT PIPE TERM

download_failed=false
echo "run download.sh" && ./download.sh || download_failed=true
echo "run parse.sh" && ./parse.sh || exit 2

# dnsmap applies the new ASN list on the next lookup without a restart.
reload_dnsmap

echo "Rules updated"
if [ "$download_failed" = true ]; then
  echo 'Warning: Cant download some lists'
fi
exit 0
