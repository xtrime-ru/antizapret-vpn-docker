#!/bin/bash
set -ex

HERE="$(dirname "$(readlink -f "${0}")")"
cd "$HERE"
UPDATED=false

function download_list() {
    local urls="$1"
    local output_file="$2"

    if [ -z "$urls" ]; then
        return 0
    fi

    echo "Downloading URLs to $output_file"
    local tmp_file="${output_file}.tmp"
    rm -f "$tmp_file"

    local success=true
    for url in ${urls//;/ }; do
        url=$(echo "$url" | tr -d '[:space:]')
        if [ -z "$url" ]; then continue; fi
        if ! curl -L -f -s "$url" >> "$tmp_file"; then
            echo "Failed to download $url"
            success=false
            break
        fi
    done

    if [ "$success" = true ] && [ -s "$tmp_file" ]; then
        mv -f "$tmp_file" "$output_file"
        UPDATED=true
    else
        echo "Failed to download some URLs or resulting file is empty, keeping old file"
        rm -f "$tmp_file"
        return 1
    fi
}

download_list "$IPS_URL" "config/include-ips-dist.txt" || exit 1
download_list "$IPS_WORLD_URL" "config/include-ips-world-dist.txt" || exit 1

if [[ "$UPDATED" == "true" ]]; then
  for list in config/*-dist.txt; do
      sed -E '/^(#.*)?[[:space:]]*$/d' $list | sort | uniq | sponge $list
  done
fi

exit 0
