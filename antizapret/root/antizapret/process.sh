#!/bin/bash


HERE="$(dirname "$(readlink -f "${0}")")"
cd "$HERE"

systemctl restart adguardhome

exit 0
