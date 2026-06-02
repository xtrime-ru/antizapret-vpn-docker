#!/bin/bash

set -e
set -x

rm -rf /tmp/*

# run commands after start
function postrun () {
    nohup bash -c "$@" &
}

DOCKER_SUBNET="$(ipcalc "$(ip -4 addr show dev eth0 | awk '$1=="inet" {print $2; exit}')" | awk '/Network:/ {print $2}')"

# save DNS variables to /etc/default/antizapret
# in order to systemd services can access them
cat << EOF | sponge /etc/default/antizapret
PYTHONUNBUFFERED=1
DOCKER_SUBNET=${DOCKER_SUBNET}
DNS=${DNS:-"127.0.0.1"}
CLIENT=${CLIENT:-"az-local"}
DOALL_DISABLED=${DOALL_DISABLED:-""}
IPS_URL='${IPS_URL:-""}'
IPS_WORLD_URL='${IPS_WORLD_URL:-""}'
AZ_SUBNET=${AZ_SUBNET:-"14.16.0.0/15"}
LC_ALL=C.UTF-8
EOF
source /etc/default/antizapret
# autoload vars when logging in into shell with 'bash -l'
ln -sf /etc/default/antizapret /etc/profile.d/antizapret.sh

DNS_FILE="/root/antizapret/result/dns.txt"


# creating custom hosts files if they have not yet been initialized
for file in $(echo {exclude,include}-{hosts,ips,ips-world}-custom.txt); do
    path=/root/antizapret/config/custom/$file
    [ ! -f $path ] && touch $path
done

( cat /root/antizapret/result/* /root/antizapret/config/custom/* 2>/dev/null | md5sum ) > /.config_md5

# Prepare iptables for dnsmap.py
CHAIN=dnsmap
iptables -t nat -N "$CHAIN"
iptables -t nat -A PREROUTING -d "${AZ_SUBNET}" -j "$CHAIN"
iptables -t nat -A OUTPUT -d "${AZ_SUBNET}" -j "$CHAIN"
for eth in $(ip link | grep -oE "eth[0-9]"); do
    iptables -t nat -A POSTROUTING -o "$eth" -j MASQUERADE
done

HOSTNAME=$(hostname -s)
IPTABLES_SAVE="/root/antizapret/iptables/$HOSTNAME.rules"

if [ -f "$IPTABLES_SAVE" ]; then
  LINES=$(cat "$IPTABLES_SAVE" | wc -l)
  if [ "$LINES" -gt 130000 ]; then
    echo "iptables-save too big. removing old file."
    rm -rf "$IPTABLES_SAVE"
  else
    while IFS= read -r rule; do
      if [[ "$rule" =~ ^-A[[:space:]]"$CHAIN" ]]; then
        iptables -t "nat" $rule || echo "cant add iptables rule: $rule"
      fi
    done < "$IPTABLES_SAVE"
  fi
fi
function save_iptables () {
    trap - EXIT;
    echo "saving iptables..."
    iptables-save -t "nat" | grep -E "^-A $CHAIN " > /tmp/iptables.rules && mv -f /tmp/iptables.rules "$IPTABLES_SAVE" && echo "iptables saved"
}

trap save_iptables EXIT HUP INT QUIT PIPE TERM

/usr/bin/dns-watcher --output "$DNS_FILE" --interval 5s &
/routes.sh --dns-file "$DNS_FILE" &

timeout 5m /usr/bin/doall || echo 'doall failed during startup, continuing with existing lists'

postrun 'while true; do /opt/api/app; done'
postrun 'while true; do sleep 6h; timeout 10m /usr/bin/doall; done'
postrun 'while true; do /usr/bin/iperf3 -s -1; done'

/usr/bin/dnsmap -a 0.0.0.0 --iprange "$AZ_SUBNET"
