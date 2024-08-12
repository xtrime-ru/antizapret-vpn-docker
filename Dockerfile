FROM ubuntu:24.04

WORKDIR /root

RUN <<-"EOT" bash -ex
    APT_LISTCHANGES_FRONTEND=none
    DEBIAN_FRONTEND=noninteractive

    apt-get update -q
    apt-get dist-upgrade -qy
    apt-get install -qqy --no-install-suggests --no-install-recommends \
        bsdmainutils \
        ca-certificates \
        curl \
        dnsutils \
        ferm \
        gawk \
        host \
        idn \
        inetutils-ping \
        ipcalc \
        iptables \
        iproute2 \
        knot-resolver \
        moreutils \
        nano \
        openssl \
        openvpn \
        patch \
        procps \
        python3-dnslib \
        sipcalc \
        systemd-sysv \
        vim-tiny \
        wget
        # git
        # unattended-upgrades
    apt-get clean
    rm -frv /var/lib/apt/lists/*
EOT

RUN <<-"EOT" bash -ex
    ANTIZAPRET_VER=master
    ANTIZAPRET_URL=https://bitbucket.org/anticensority/antizapret-pac-generator-light/get/$ANTIZAPRET_VER.tar.gz

    EASYRSA_VER=3.2.0
    EASYRSA_URL=https://github.com/OpenVPN/easy-rsa/releases/download/v$EASYRSA_VER/EasyRSA-$EASYRSA_VER.tgz

    mkdir antizapret && curl -s -L $ANTIZAPRET_URL | tar -zxv --strip-components=1 -C $_
    mkdir easyrsa && curl -s -L $EASYRSA_URL | tar -zxv --strip-components=1 -C $_
EOT

COPY rootfs /

RUN <<-"EOF" bash -ex
    systemctl enable \
        antizapret-update.timer \
        dnsmap \
        kresd@1 \
        openvpn-server@antizapret \
        openvpn-server@antizapret-tcp \
        systemd-networkd
        # antizapret-update.service

    patch antizapret/parse.sh patches/parse.patch
    sed -i "/\b\(googleusercontent\|cloudfront\|deviantart\)\b/d" /root/antizapret/config/exclude-regexp-dist.awk
    for list in antizapret/config/*-dist.txt; do
        sed -E '/^(#.*)?[[:space:]]*$/d' $list | sort | uniq | sponge $list
    done

    rm -frv /tmp/*
EOF

COPY rootfs /rootfs

ENTRYPOINT ["/init.sh"]
