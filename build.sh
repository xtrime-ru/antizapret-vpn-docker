#!/usr/bin/env bash

if [[ ! -f ./easyrsa3/easyrsa ]]
# We need to  easyrsa3/pki folder to be persistent.
# But we cant just symlink it, because easyrsa will try to remove it and crash during key regeneration.
# So we replace existing folder with link from host.
then
    curl -L https://github.com/OpenVPN/easy-rsa/releases/download/v3.1.0/EasyRSA-3.1.0.tgz | tar -xz
    mv EasyRSA-3.1.0 easyrsa3
fi

docker import https://antizapret.prostovpn.org/container-images/az-vpn/rootfs.tar.xz xtrime/antizapret-vpn:latest