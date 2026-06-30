#!/bin/bash
set -ex

HERE="$(dirname "$(readlink -f "${0}")")"
cd "$HERE"
export LC_ALL=C.UTF-8

(cat "config/custom/include-ips-custom.txt"; echo ""; cat "config/include-ips-dist.txt") | awk -f scripts/sanitize-lists.awk | (grep -v -E -f config/custom/exclude-ips-custom.txt || echo "") | sort | uniq > temp/ips.txt
(cat "config/custom/include-ips-world-custom.txt"; echo ""; cat "config/include-ips-world-dist.txt") | awk -f scripts/sanitize-lists.awk | (grep -v -E -f config/custom/exclude-ips-world-custom.txt || echo "") | sort | uniq > temp/ips-world.txt
(cat "config/custom/include-asn-custom.txt"; echo ""; cat "config/include-asn-dist.txt") | awk -f scripts/sanitize-lists.awk | (grep -v -i -F -x -f config/custom/exclude-asn-custom.txt || echo "") | sort -f | uniq -i > temp/asn.txt
(cat "config/custom/include-asn-world-custom.txt"; echo ""; cat "config/include-asn-world-dist.txt") | awk -f scripts/sanitize-lists.awk | (grep -v -i -F -x -f config/custom/exclude-asn-world-custom.txt || echo "") | sort -f | uniq -i > temp/asn-world.txt

# Generate OpenVPN route file
echo -n > temp/openvpn-blocked-ranges.txt
set +x
while read -r line
do
    [ -z "$line" ] && continue
    C_NET="$(echo $line | awk -F '/' '{print $1}')"
    C_NETMASK="$(sipcalc -- "$line" | awk '/Network mask/ {print $4; exit;}')"
    echo $"push \"route ${C_NET} ${C_NETMASK}\"" >> temp/openvpn-blocked-ranges.txt
done < <( cat temp/ips*; echo "$DOCKER_SUBNET" )
set -x


(GLOBIGNORE="temp/.*"; mv -f temp/* result)

exit 0
