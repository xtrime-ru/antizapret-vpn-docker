#!/usr/bin/env bash
set -e
shopt -s nullglob
logs=(/etc/openvpn/log/*.log)
((${#logs[@]})) || exit 1
exec tail -F "${logs[@]}"
