#!/bin/bash
set -ex

HERE="$(dirname "$(readlink -f "${0}")")"
cd "$HERE"
RESOLVE_NXDOMAIN="no"

echo "Size of temp/list.csv: $(cat temp/list.csv | wc -l) lines"
echo "Size of temp/nxdomain.txt: $(cat temp/nxdomain.txt | wc -l) lines"

# Extract domains from list
if [[ "$SKIP_UPDATE_FROM_ZAPRET" == false ]] && [ -s temp/list.csv ]; then
   awk -F ';' '{print $2}' temp/list.csv | sort -u | awk '/^$/ {next} /\\/ {next} /^[а-яА-Яa-zA-Z0-9\-_\.\*]*+$/ {gsub(/\*\./, ""); gsub(/\.$/, ""); print}' | grep -Fv 'bеllonа' | CHARSET=UTF-8 idn --no-tld | grep -Fv 'xn--' > temp/hostlist_original.txt
else
   echo -n > temp/hostlist_original.txt
fi

if [ -s temp/nxdomain.txt ]; then
    sort -u temp/nxdomain.txt >> temp/hostlist_original.txt
fi

for file in config/custom/{include,exclude}-{hosts,ips}-custom.txt; do
    basename=$(basename $file | sed 's|-custom.txt||')
    sort -u $file config/${basename}-dist.txt | awk 'NF' > temp/${basename}.txt
done

awk -F ';' '{split($1, a, /\|/); for (i in a) {print a[i]";"$2}}' temp/list.csv | \
 grep -f config/exclude-hosts-by-ips-dist.txt | awk -F ';' '{print $2}' >> temp/exclude-hosts.txt

if [[ "$RESOLVE_NXDOMAIN" == "yes" ]];
then
    timeout 2h scripts/resolve-dns-nxdomain.py result/hostlist_zones.txt > temp/nxdomain-exclude-hosts.txt
    cat temp/nxdomain-exclude-hosts.txt >> temp/exclude-hosts.txt
fi

# Remove Windows line endings
sed -i 's/\r//' temp/include-hosts.txt
sed -i 's/\r//' temp/exclude-hosts.txt

awk -f scripts/getzones.awk temp/hostlist_original.txt > temp/hostlist_after_awk.txt
sort -u temp/include-hosts.txt temp/hostlist_after_awk.txt | grep -v -E -f temp/exclude-hosts.txt > result/hostlist_zones.txt
rm temp/hostlist*

awk -F ';' '$1 ~ /\// {print $1}' temp/list.csv | egrep -o '([0-9]{1,3}\.){3}[0-9]{1,3}\/[0-9]{1,2}' | sort -u > result/blocked-ranges.txt
sort -u temp/include-ips.txt result/blocked-ranges.txt > result/blocked-ranges-with-include.txt

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
/bin/cp -f /opt/adguardhome/conf/upstream_dns_file_basis result/adguard_upstream_dns_file
echo "" >> result/adguard_upstream_dns_file
sed -E -e 's~(.*)~[/\1/] 127.0.0.4~' result/hostlist_zones.txt >> result/adguard_upstream_dns_file
/bin/cp -f result/adguard_upstream_dns_file /opt/adguardhome/conf/upstream_dns_file

echo "Adguard config generated: $(cat /opt/adguardhome/conf/upstream_dns_file | wc -l) lines"

exit 0
