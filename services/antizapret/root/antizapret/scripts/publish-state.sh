#!/bin/bash
set -euo pipefail
shopt -s nullglob
. /usr/local/lib/docktinel/state.sh
cd /root/antizapret
ips=$(checksum_files result/ips*)
openvpn=$(checksum_files result/openvpn-blocked-ranges.txt)
# Compute both before publishing either; a failed generation keeps old markers.
publish_value result/.ips-md5 "$ips"
publish_value result/.openvpn-md5 "$openvpn"
