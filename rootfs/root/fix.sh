#!/usr/bin/env bash

# fix invalid domains
# https://ntc.party/t/129/636
patch /root/antizapret/parse.sh /root/patches/parse.patch || exit 1

sed -i "/\b\(googleusercontent\|cloudfront\|deviantart\)\b/d" /root/antizapret/config/exclude-regexp-dist.awk

echo "" > /root/antizapret/config/exclude-hosts-dist.txt

echo "
t.co
twimg.com
fbcdn.net
cdninstagram.com
fb.com
messenger.com
theins.ru
openai.com
intercomcdn.com
oaistatic.com
oaiusercontent.com
chatgpt.com
bing.com
bing.net
microsoft.com
msn.com
live.com
cloudflare.com
microsoftonline.com
bingapis.com
cloud.microsoft
office.com
youtube.com
youtu.be
ytimg.com
ggpht.com
googleusercontent.com
googlevideo.com
ua
com.ua
" >> /root/antizapret/config/include-hosts-dist.txt

sed -i '/^[[:space:]]*$/d' /root/antizapret/config/include-hosts-dist.txt

sort --unique /root/antizapret/config/include-hosts-dist.txt -o /root/antizapret/config/include-hosts-dist.txt
