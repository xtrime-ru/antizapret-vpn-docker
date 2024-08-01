#!/usr/bin/env bash

# fix invalid domains
# https://ntc.party/t/129/636
sed -i -E "s/(CHARSET=UTF-8 idn)/\1 --no-tld | grep -Fv 'xn--'/g" /root/antizapret/parse.sh

# fix apple.com \
# https://ntc.party/t/129/372
sed -i "/-- PATCH_START/,/-- PATCH_END/d" /etc/knot-resolver/kresd.conf
cat /root/antizapret/patches/kresd.conf >> /etc/knot-resolver/kresd.conf