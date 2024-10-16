#!/bin/bash
set -e

HERE="$(dirname "$(readlink -f "${0}")")"
cd "$HERE"

if [[ $SKIP_UPDATE_FROM_ZAPRET == true ]]; then
    rm -f temp/list.csv temp/nxdomain.txt
    echo -n > temp/list.csv
    echo -n > temp/nxdomain.txt
    exit 0
fi

LISTLINK='https://raw.githubusercontent.com/zapret-info/z-i/master/dump.csv'
NXDOMAINLINK='https://raw.githubusercontent.com/zapret-info/z-i/master/nxdomain.txt'
curl -f --fail-early --compressed -o temp/list_orig.csv "$LISTLINK" || exit 1
iconv -f cp1251 -t utf8 temp/list_orig.csv > temp/list.csv
curl -f --fail-early --compressed -o temp/nxdomain.txt "$NXDOMAINLINK" || exit 1

exit 0
