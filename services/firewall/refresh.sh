#!/bin/bash
set -euo pipefail
cd /root
cache=/var/lib/antizapret-firewall
mkdir -p "$cache"
exec 9>"$cache/lock"
flock 9
staged=$(mktemp -d "$cache/.refresh.XXXXXX")
trap 'rm -rf -- "$staged"' EXIT

archive="$cache/lists.tar"
if [[ "${1:-}" == --restore && -s "$archive" ]]; then
    if tar -xf "$archive" -C "$staged" v4 v6 &&
        [[ -s "$staged/v4" && -s "$staged/v6" ]] &&
        V4_FILE="$staged/v4" V6_FILE="$staged/v6" ./block.sh; then
        exit 0
    fi
    echo 'Could not apply cached firewall lists, trying available lists' >&2
fi

if [[ "${1:-}" == --restore && -s "$V4_FILE" && -s "$V6_FILE" ]]; then
    cp -- "$V4_FILE" "$staged/v4"
    cp -- "$V6_FILE" "$staged/v6"
else
    ./download.sh "$V4_URL" "$staged/v4"
    ./download.sh "$V6_URL" "$staged/v6"
fi
test -s "$staged/v4"
test -s "$staged/v6"
tar -cf "$staged/lists.tar" -C "$staged" v4 v6

# Publish both families in a single rename, only after the rules are applied.
V4_FILE="$staged/v4" V6_FILE="$staged/v6" ./block.sh
mv -f -- "$staged/lists.tar" "$archive"
mv -f -- "$staged/v4" "$V4_FILE"
mv -f -- "$staged/v6" "$V6_FILE"
