#!/bin/bash
set -e

HERE="$(dirname "$(readlink -f "${0}")")"
cd "$HERE"

source config/config.sh

# Extract domains from list
if [[ $SKIP_UPDATE_FROM_ZAPRET == false ]]; then
   awk -F ';' '{print $2}' temp/list.csv | sort -u | awk '/^$/ {next} /\\/ {next} /^[а-яА-Яa-zA-Z0-9\-_\.\*]*+$/ {gsub(/\*\./, ""); gsub(/\.$/, ""); print}' | grep -Fv 'bеllonа' | CHARSET=UTF-8 idn --no-tld | grep -Fv 'xn--' > result/hostlist_original.txt
else
   echo \n > result/hostlist_original.txt
fi

for file in config/custom/{include,exclude}-hosts-custom.txt; do
    basename=$(basename $file | sed 's|-custom.txt||')
    sort -u $file config/${basename}-dist.txt > temp/${basename}.txt
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


# Generate dnsmasq aliases
echo -n > result/dnsmasq-aliases-alt.conf
while read -r line
do
    echo "server=/$line/127.0.0.4" >> result/dnsmasq-aliases-alt.conf
done < result/hostlist_zones.txt


# Generate knot-resolver aliases
echo 'blocked_hosts = {' > result/knot-aliases-alt.conf
while read -r line
do
    line="$line."
    echo "${line@Q}," >> result/knot-aliases-alt.conf
done < result/hostlist_zones.txt
echo '}' >> result/knot-aliases-alt.conf


# Generate squid zone file
echo -n > result/squid-whitelist-zones.conf
while read -r line
do
    echo ".$line" >> result/squid-whitelist-zones.conf
done < result/hostlist_zones.txt


# Print results
echo "Blocked domains: $(wc -l result/hostlist_zones.txt)" >&2

exit 0
