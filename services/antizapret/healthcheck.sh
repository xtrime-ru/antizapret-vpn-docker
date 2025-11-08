#!/usr/bin/env bash

( ( cat /root/antizapret/result/* /root/antizapret/config/custom/* | md5sum ) | cmp - /tmp/config_md5 ) || systemctl restart antizapret-api

( cat /root/antizapret/result/* /root/antizapret/config/custom/* | md5sum ) > /tmp/config_md5