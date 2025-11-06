#!/bin/bash -e

HERE="$(dirname "$(readlink -f "${0}")")"
cd "$HERE"

if [ -s /etc/default/antizapret ]; then
    set -a
    source /etc/default/antizapret
    set +a
fi

echo "run parse.sh" && ./parse.sh || exit 2

echo "Rules updated"
exit 0