#!/bin/sh
set -eu
set -a
. /etc/environment
set +a
cd /opt/openvpn-ui
exec ./openvpn-ui
