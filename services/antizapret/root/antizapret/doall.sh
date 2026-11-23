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

function cached_downloads_available () {
    [ -z "${IPS_URL:-}" ] || [ -s config/include-ips-dist.txt ] || return 1
    [ -z "${IPS_WORLD_URL:-}" ] || [ -s config/include-ips-world-dist.txt ] || return 1
    [ -z "${ASN_URL:-}" ] || [ -s config/include-asn-dist.txt ] || return 1
    [ -z "${ASN_WORLD_URL:-}" ] || [ -s config/include-asn-world-dist.txt ] || return 1
}

if [ -n "$DOALL_DISABLED" ]; then
    echo "DoAll disabled. Reloading dnsmap only..."
    /opt/api/app --publish-config-md5
    reload_dnsmap
    exit 0
fi

# Retain the existing lock path across image upgrades.
exec 9>>result/.doall_owner.lock
flock 9

download_failed=false
echo "run download.sh" && ./download.sh || download_failed=true
if [ "$download_failed" = true ] && ! cached_downloads_available; then
  echo 'Error: Cant download some lists and no cached downloads are available'
  exit 1
fi
echo "run parse.sh" && ./parse.sh || exit 2

/opt/api/app --publish-config-md5

# dnsmap applies the new ASN list on the next lookup without a restart.
reload_dnsmap

echo "Rules updated"
if [ "$download_failed" = true ]; then
  echo 'Warning: Cant download some lists'
fi
exit 0
