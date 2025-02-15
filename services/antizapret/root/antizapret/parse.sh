#!/bin/bash
set -ex

HERE="$(dirname "$(readlink -f "${0}")")"
cd "$HERE"
RESOLVE_NXDOMAIN="no"

echo "Size of temp/list.csv: $(cat temp/list.csv | wc -l) lines"

# Extract domains from list
if [[ "$SKIP_UPDATE_FROM_ZAPRET" == false ]]; then
   awk -F ';' '{print $2}' temp/list.csv | sort -u | awk '/^$/ {next} /\\/ {next} /^[а-яА-Яa-zA-Z0-9\-_\.\*]*+$/ {gsub(/\*\./, ""); gsub(/\.$/, ""); print}' | grep -Fv 'bеllonа' | CHARSET=UTF-8 idn --no-tld | grep -Fv 'xn--' > result/hostlist_original.txt
else
   echo -e "\n" > result/hostlist_original.txt
fi

for file in config/custom/{include,exclude}-{hosts,ips}-custom.txt; do
    basename=$(basename $file | sed 's|-custom.txt||')
    sort -u $file config/${basename}-dist.txt | uniq | awk 'NF' > temp/${basename}.txt
done
sort -u temp/include-hosts.txt result/hostlist_original.txt > temp/hostlist_original_with_include.txt

awk -F ';' '{split($1, a, /\|/); for (i in a) {print a[i]";"$2}}' temp/list.csv | \
 grep -f config/exclude-hosts-by-ips-dist.txt | awk -F ';' '{print $2}' >> temp/exclude-hosts.txt

awk -f scripts/getzones.awk temp/hostlist_original_with_include.txt | grep -v -F -x -f temp/exclude-hosts.txt | sort -u > result/hostlist_zones.txt

if [[ "$RESOLVE_NXDOMAIN" == "yes" ]];
then
    timeout 2h scripts/resolve-dns-nxdomain.py result/hostlist_zones.txt > temp/nxdomain-exclude-hosts.txt
    cat temp/nxdomain-exclude-hosts.txt >> temp/exclude-hosts.txt
    awk -f scripts/getzones.awk temp/hostlist_original_with_include.txt | grep -v -F -x -f temp/exclude-hosts.txt | sort -u > result/hostlist_zones.txt
fi

awk -F ';' '$1 ~ /\// {print $1}' temp/list.csv | egrep -o '([0-9]{1,3}\.){3}[0-9]{1,3}\/[0-9]{1,2}' | sort -u > result/blocked-ranges.txt
sort -u temp/include-ips.txt result/blocked-ranges.txt > result/blocked-ranges-with-include.txt

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
/bin/cp -f /opt/adguardhome/conf/upstream_dns_file_basis result/adguard_upstream_dns_file
echo "" >> result/adguard_upstream_dns_file
sed -E -e 's~(.*)~[/\1/] 127.0.0.4~' result/hostlist_zones.txt >> result/adguard_upstream_dns_file
/bin/cp -f result/adguard_upstream_dns_file /opt/adguardhome/conf/upstream_dns_file

echo "Adguard config generated: $(cat /opt/adguardhome/conf/upstream_dns_file | wc -l) lines"

exit 0
