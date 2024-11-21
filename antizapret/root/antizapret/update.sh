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

LISTLINK='https://raw.githubusercontent.com/zapret-info/z-i/master/dump.csv.gz'
curl -f --fail-early --compressed -o temp/list.csv.gz "$LISTLINK" || exit 1
LISTSIZE="$(curl -sI "$LISTLINK"| awk 'BEGIN {IGNORECASE=1;} /content-length/ {sub(/[ \t\r\n]+$/, "", $2); print $2}')"
[[ "$LISTSIZE" != "$(stat -c '%s' temp/list.csv.gz)" ]] && echo "List 1 size differs" && exit 2
gunzip -fd temp/list.csv.gz || exit 3
iconv -f cp1251 -t utf8 temp/list.csv | sponge temp/list.csv


NXDOMAINLINK='https://raw.githubusercontent.com/zapret-info/z-i/master/nxdomain.txt'
curl -f --fail-early --compressed -o temp/nxdomain.txt "$NXDOMAINLINK" || exit 1
LISTSIZE="$(curl -sI "$NXDOMAINLINK" | awk 'BEGIN {IGNORECASE=1;} /content-length/ {sub(/[ \t\r\n]+$/, "", $2); print $2}')"
[[ "$LISTSIZE" != "$(stat -c '%s' temp/nxdomain.txt)" ]] && echo "List 2 size differs" && exit 2

exit 0