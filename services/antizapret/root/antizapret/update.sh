#!/bin/bash
set -e

HERE="$(dirname "$(readlink -f "${0}")")"
cd "$HERE"

if [[ $SKIP_UPDATE_FROM_ZAPRET == true ]]; then
    echo "Skip download of lists"
    rm -f temp/list.csv temp/nxdomain.txt
    echo -n > temp/list.csv
    echo -n > temp/nxdomain.txt
    exit 0
fi

echo -n > temp/list.csv
echo "Filling temp/list.csv"
if [ -n "$IP_LIST" ]; then
    echo "Downloading ip list from $IP_LIST"
    timeout 30 curl -f --fail-early --compressed -o temp/list.csv.gz "$IP_LIST" || exit 1
    LISTSIZE="$(timeout 30 curl -sI "$IP_LIST"| awk 'BEGIN {IGNORECASE=1;} /content-length/ {sub(/[ \t\r\n]+$/, "", $2); print $2}')"
    [[ "$LISTSIZE" != "$(stat -c '%s' temp/list.csv.gz)" ]] && echo "List 1 size differs" && exit 2
    gunzip -fd temp/list.csv.gz || exit 3
    iconv -f cp1251 -t utf8 temp/list.csv | sponge temp/list.csv
fi

echo "Filling temp/nxdomain.txt"
echo -n > temp/nxdomain.txt
for NXDOMAINLINK in ${LISTS//;/ }; do
    echo "Downloading lists from $NXDOMAINLINK"
    timeout 30 curl -f --fail-early --compressed -o temp/nxdomain-temp.txt "$NXDOMAINLINK" || exit 1
    LISTSIZE="$(curl -sI "$NXDOMAINLINK" | awk 'BEGIN {IGNORECASE=1;} /content-length/ {sub(/[ \t\r\n]+$/, "", $2); print $2}')"
    [[ "$LISTSIZE" != "$(stat -c '%s' temp/nxdomain-temp.txt)" ]] && echo "List 2 size differs" && exit 2
    cat temp/nxdomain-temp.txt >> temp/nxdomain.txt
    rm temp/nxdomain-temp.txt
done

exit 0