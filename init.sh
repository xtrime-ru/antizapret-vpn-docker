#!/usr/bin/env bash

echo "nameserver 1.1.1.1" >> /etc/resolv.conf

if [[ ! -f /root/easy-rsa-ipsec/easyrsa3/easyrsa ]]
# We need to  easyrsa3/pki folder to be persistent.
# But we cant just symlink it, because easyrsa will try to remove it and crash during key regeneration.
# So we replace existing folder with link from host.
then
    curl -L https://github.com/OpenVPN/easy-rsa/releases/download/v3.2.0/EasyRSA-3.2.0.tgz | tar -xz
    mv EasyRSA-3.2.0/* /root/easy-rsa-ipsec/easyrsa3
    rm -rf EasyRSA-3.2.0/
fi

nohup bash -c "sleep 1; cd /root/antizapret/ && ./process.sh" &

/root/easy-rsa-ipsec/generate.sh \
&& exec /usr/sbin/init