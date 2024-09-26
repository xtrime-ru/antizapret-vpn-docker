#!/bin/bash


HERE="$(dirname "$(readlink -f "${0}")")"
cd "$HERE"

cp result/knot-aliases-alt.conf /etc/knot-resolver/knot-aliases-alt.conf
systemctl restart kresd@1.service

exit 0
