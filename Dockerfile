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
        iperf3 \
        curl \
        dnsutils \
        ferm \
        gawk \
        host \
        idn \
        inetutils-ping \
        ipcalc \
        ipcalc-ng \
        iptables \
        iproute2 \
        knot-resolver \
        moreutils \
        nano \
        openssl \
        patch \
        procps \
        python3-dnslib \
        sipcalc \
        systemd-sysv \
        vim-tiny \
        wget
    apt-get clean
    rm -frv /var/lib/apt/lists/*
EOT

COPY rootfs /

RUN <<-"EOF" bash -ex
    systemctl enable \
        antizapret-update.service \
        antizapret-update.timer \
        dnsmap \
        kresd@1 \
        systemd-networkd \
        iperf3-server@1

    for list in antizapret/config/*-dist.txt; do
        sed -E '/^(#.*)?[[:space:]]*$/d' $list | sort | uniq | sponge $list
    done
    
    for list in antizapret/config/*-custom.txt; do rm -f $list; done

    ln -sf /root/antizapret/doall.sh /usr/bin/doall
    ln -sf /root/antizapret/dnsmap.py /usr/bin/dnsmap

    rm -frv /tmp/*
EOF

RUN <<-"EOF" bash -ex
    (STAGE_1=true STAGE_2=true STAGE_3=false /root/antizapret/doall.sh)
    cp /root/antizapret/result/knot-aliases-alt.conf /etc/knot-resolver/knot-aliases-alt.conf
EOF

ENTRYPOINT ["/init.sh"]
