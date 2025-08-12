#!/bin/bash
set -ex

HERE="$(dirname "$(readlink -f "${0}")")"
cd "$HERE"
RESOLVE_NXDOMAIN="no"
export LC_ALL=C.UTF-8

echo "Size of temp/list.csv: $(cat temp/list.csv | wc -l) lines"
echo "Size of temp/nxdomain.txt: $(cat temp/nxdomain.txt | wc -l) lines"

# Extract domains from list
if [[ "$SKIP_UPDATE_FROM_ZAPRET" == false ]] && [ -s temp/list.csv ]; then
   awk -F ';' '{print $2}' temp/list.csv | awk '/^$/ {next} /\\/ {next} /^[а-яА-Яa-zA-Z0-9\-_\.\*]*+$/ {gsub(/\*\./, ""); gsub(/\.$/, ""); print}' | grep -Fv 'bеllonа'  > temp/hostlist_original.txt
else
   echo -n > temp/hostlist_original.txt
fi

if [ -s temp/nxdomain.txt ]; then
    sort -o temp/hostlist_original.txt -u temp/hostlist_original.txt temp/nxdomain.txt
fi

for file in config/custom/{include,exclude}-{hosts,ips,regexp}-custom.txt; do
    [ ! -f "$file" ] && continue
    basename=$(basename $file | sed 's|-custom.txt||')
    (cat "$file"; echo ""; cat "config/${basename}-dist.txt") | awk -f scripts/sanitize-lists.awk > temp/${basename}.txt
done
sort -o temp/hostlist_original.txt -u temp/include-hosts.txt temp/hostlist_original.txt

awk -F ';' '{split($1, a, /\|/); for (i in a) {print a[i]";"$2}}' temp/list.csv | \
 grep -f config/exclude-hosts-by-ips-dist.txt | awk -F ';' '{print $2}' >> temp/exclude-hosts.txt

if [[ "$RESOLVE_NXDOMAIN" == "yes" ]];
then
    timeout 2h scripts/resolve-dns-nxdomain.py result/hostlist_zones.txt > temp/nxdomain-exclude-hosts.txt
    sort -o temp/exclude-hosts.txt -u temp/nxdomain-exclude-hosts.txt temp/exclude-hosts.txt
fi

awk -f scripts/getzones.awk temp/hostlist_original.txt | grep -v -F -x -f temp/exclude-hosts.txt | grep -v -E -f  temp/exclude-regexp.txt | CHARSET=UTF-8 idn --no-tld > temp/hostlist_zones.txt
grep -E '^[^\.]+\.[^\.]+$' temp/hostlist_zones.txt > temp/hostlist_zones_2_level.txt
grep -v -F -f temp/hostlist_zones_2_level.txt temp/hostlist_zones.txt > temp/hostlist_zones_without_2+_level.txt
sort -u temp/hostlist_zones_2_level.txt temp/hostlist_zones_without_2+_level.txt > result/hostlist_zones.txt

awk -F ';' '$1 ~ /\// {print $1}' temp/list.csv | (egrep -o '([0-9]{1,3}\.){3}[0-9]{1,3}\/[0-9]{1,2}' || echo -n) > result/blocked-ranges.txt
sort -o result/blocked-ranges-with-include.txt -u temp/include-ips.txt result/blocked-ranges.txt

if [ -n "$DOCKER_SUBNET" ]; then
    echo "$DOCKER_SUBNET" >> result/blocked-ranges-with-include.txt
fi

# Generate OpenVPN route file
echo -n > result/openvpn-blocked-ranges.txt
while read -r line
do
    C_NET="$(echo $line | awk -F '/' '{print $1}')"
    C_NETMASK="$(sipcalc -- "$line" | awk '/Network mask/ {print $4; exit;}')"
    echo $"push \"route ${C_NET} ${C_NETMASK}\"" >> result/openvpn-blocked-ranges.txt
done < result/blocked-ranges-with-include.txt

# Generate adguardhome aliases
/bin/cp --update=none /root/adguardhome/* /opt/adguardhome/conf
sed -E -e 's~(.*)~@@||\1\^\$client=127.0.0.1~' result/hostlist_zones.txt > result/adguard_rules
RULES_DIR="/opt/adguardhome/work/data/userfilters/"
if [ ! -d "$RULES_DIR" ]; then
  mkdir "$RULES_DIR"
fi
/bin/cp -f result/adguard_rules "${RULES_DIR}adguard_rules"

echo "Adguard config generated: $(cat "${RULES_DIR}adguard_rules" | wc -l) lines"

rm temp/hostlist* temp/{include,exclude}*

exit 0
