#!/bin/bash
set -ex

HERE="$(dirname "$(readlink -f "${0}")")"
cd "$HERE"
export LC_ALL=C.UTF-8

(cat "config/custom/include-ips-custom.txt"; echo ""; cat "config/include-ips-dist.txt") | awk -f scripts/sanitize-lists.awk > temp/ips.txt
(cat "config/custom/include-hosts-custom.txt"; echo ""; cat "config/include-hosts-dist.txt")| awk -f scripts/sanitize-lists.awk > temp/hosts.txt

if [ -n "$DOCKER_SUBNET" ]; then
    echo "$DOCKER_SUBNET" >> temp/ips.txt
fi

# Generate OpenVPN route file
echo -n > temp/openvpn-blocked-ranges.txt
while read -r line
do
    C_NET="$(echo $line | awk -F '/' '{print $1}')"
    C_NETMASK="$(sipcalc -- "$line" | awk '/Network mask/ {print $4; exit;}')"
    echo $"push \"route ${C_NET} ${C_NETMASK}\"" >> temp/openvpn-blocked-ranges.txt
done < temp/ips.txt

# Generate adguardhome aliases
sed -E -e 's~(.*)~@@||\1\^$dnsrewrite,client=antizapret~' temp/hosts.txt > temp/adguard_rules
echo "Adguard config generated: $(cat temp/adguard_rules | wc -l) lines"


(GLOBIGNORE="temp/.*"; mv -f temp/* result)

exit 0
