#!/bin/bash -e


# run commands after systemd initialization

function postrun () {
    local waiter="until ps -p 1 | grep -q systemd; do sleep 0.1; done"
    nohup bash -c "$waiter; $@" &
}


# resolve domain address to ip address

function resolve () {
    # $1 domain/ip address, $2 fallback ip address
    ipcalc () { ipcalc-ng --no-decorate -o $1 2> /dev/null; }
    echo "$(ipcalc $1 || echo $2)"
}


# set ciphers

function set_ciphers () {
    # $1 AES-128-CBC:AES-256-CBC[:...]
    local CIPHERS=AES-128-GCM:AES-256-GCM
    local ARGS=$([ -n "$1" ] && echo "$CIPHERS:$1" || echo "$CIPHERS")
    sed -i "s|data-ciphers .*|data-ciphers \"$ARGS\"|g" /etc/openvpn/server/*.conf
}

function set_scramble () {
    local ENABLE=$1
    if [[ "$ENABLE" == 1 ]]; then
        echo "Enable scramble"
        sed -i "s/^#scramble/scramble/g" /root/openvpn/templates/*.conf
        sed -i "s/^#scramble/scramble/g" /etc/openvpn/server/*.conf
    else
        echo "Disable scramble"
        sed -i "s/^scramble/#scramble/g" /root/openvpn/templates/*.conf
        sed -i "s/^scramble/#scramble/g" /etc/openvpn/server/*.conf
    fi
}

function set_tls_crypt () {
    local ENABLE="$1"
    if [[ "$ENABLE" == 1 ]]; then
        echo "Enable TLS_CRYPT"
        sed -i "s/^#key-direction/key-direction/g" /root/openvpn/templates/*.conf
        sed -i "s/^#<tls-crypt>/<tls-crypt>/g" /root/openvpn/templates/*.conf
        sed -i "s/^#\${CLIENT_TLS_CRYPT}/\${CLIENT_TLS_CRYPT}/g" /root/openvpn/templates/*.conf
        sed -i "s/^#<\/tls-crypt>/<\/tls-crypt>/g" /root/openvpn/templates/*.conf

        sed -i "s/^#tls-crypt/tls-crypt/g" /etc/openvpn/server/*.conf
    else
        echo "Disable TLS_CRYPT"
        sed -i "s/^key-direction/#key-direction/g" /root/openvpn/templates/*.conf
        sed -i "s/^<tls-crypt>/#<tls-crypt>/g" /root/openvpn/templates/*.conf
        sed -i "s/^\${CLIENT_TLS_CRYPT}/#\${CLIENT_TLS_CRYPT}/g" /root/openvpn/templates/*.conf
        sed -i "s/^<\/tls-crypt>/#<\/tls-crypt>/g" /root/openvpn/templates/*.conf

        sed -i "s/^tls-crypt/#tls-crypt/g" /etc/openvpn/server/*.conf
    fi
}

function set_mtu () {
    local MTU=$1
    if [[ "$MTU" == 0 ]]; then
        echo "Disable MTU setting"
        sed -i "s/^tun-mtu/#tun-mtu/g" /root/openvpn/templates/*.conf
        sed -i "s/^tun-mtu/#tun-mtu/g" /etc/openvpn/server/*.conf
    else
        echo "Enable MTU setting"
        sed -E -i "s/^#?tun-mtu.*$/tun-mtu $MTU/g" /root/openvpn/templates/*.conf
        sed -E -i "s/^#?tun-mtu.*$/tun-mtu $MTU/g" /etc/openvpn/server/*.conf
    fi
}

function set_optimizations () {
    local ENABLE=$1
    if [[ "$ENABLE" == 0 ]]; then
        echo "Disable optimizations"
        sed -i "s/^sndbuf/#sndbuf/g" /root/openvpn/templates/*.conf
        sed -i "s/^rcvbuf/#rcvbuf/g" /root/openvpn/templates/*.conf
        sed -i "s/^tcp-nodelay/#tcp-nodelay/g" /root/openvpn/templates/*.conf
        sed -i "s/^fast-io/#fast-io/g" /root/openvpn/templates/*.conf

        sed -i "s/^fast-io/#fast-io/g" /etc/openvpn/server/*.conf
        sed -i "s/^tcp-nodelay/#tcp-nodelay/g" /etc/openvpn/server/*.conf
    else
        echo "Enable optimizations"
        sed -i "s/^#sndbuf/sndbuf/g" /root/openvpn/templates/*.conf
        sed -i "s/^#rcvbuf/rcvbuf/g" /root/openvpn/templates/*.conf
        sed -i "s/^#tcp-nodelay/tcp-nodelay/g" /root/openvpn/templates/*.conf
        sed -i "s/^#fast-io/fast-io/g" /root/openvpn/templates/*.conf

        sed -i "s/^#fast-io/fast-io/g" /etc/openvpn/server/*.conf
        sed -i "s/^#tcp-nodelay/tcp-nodelay/g" /etc/openvpn/server/*.conf
    fi
}

# save DNS variables to /etc/default/antizapret
# in order to systemd services can access them

cat << EOF | sponge /etc/default/antizapret
OPENVPN_CBC_CIPHERS=${OPENVPN_CBC_CIPHERS:-0}
OPENVPN_SCRAMBLE=${OPENVPN_SCRAMBLE:-0}
OPENVPN_TLS_CRYPT=${OPENVPN_TLS_CRYPT:-0}
OPENVPN_OPTIMIZATIONS=${OPENVPN_OPTIMIZATIONS:-0}
OPENVPN_MTU=${OPENVPN_MTU:-0}
DNS=$(resolve $DNS 127.0.0.11)
DNS_RU=$(resolve $DNS_RU 77.88.8.8)
ADGUARD=${ADGUARD:-0}
LOG_DNS=${LOG_DNS:-0}
PYTHONUNBUFFERED=1
SELF_IP=$(hostname -i)
SKIP_UPDATE_FROM_ZAPRET=${SKIP_UPDATE_FROM_ZAPRET:-false}
UPDATE_TIMER=${UPDATE_TIMER:-"6h"}
EOF

source /etc/default/antizapret

# autoload vars when logging in into shell with 'bash -l'
ln -sf /etc/default/antizapret /etc/profile.d/antizapret.sh


# populating directories with files
cp -rv --update=none /etc/openvpn-default/* /etc/openvpn

for file in $(echo {exclude,include}-{ips,hosts,regex}-custom.txt); do
    path=/root/antizapret/config/custom/$file
    [ ! -f $path ] && touch $path
done

# swap between legacy ciphers and DCO-required ciphers
[[ "$OPENVPN_CBC_CIPHERS" == 1 ]] && set_ciphers AES-128-CBC:AES-256-CBC || set_ciphers

# enable tunneblick xor scramble patch
set_scramble "$OPENVPN_SCRAMBLE"

set_tls_crypt "$OPENVPN_TLS_CRYPT"

set_mtu "$OPENVPN_MTU"

set_optimizations "$OPENVPN_OPTIMIZATIONS"

# Changing the timer for updating lists
sed -i "s/^OnUnitActiveSec=6h/OnUnitActiveSec=$UPDATE_TIMER/g" /etc/systemd/system/antizapret-update.timer

# generate certs/keys/profiles for OpenVPN
/root/openvpn/generate.sh

# output systemd logs to docker logs since container boot
postrun 'until [[ "$(systemctl is-active systemd-journald)" == "active" ]]; do sleep 0.1; done; journalctl --boot --follow --lines=all --no-hostname'

# systemd init
exec /usr/sbin/init
