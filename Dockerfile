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

RUN <<-"EOT" bash -ex
    ANTIZAPRET_VER=6eae76b095ef4d719043a109c05d94900aaa3791
    ANTIZAPRET_URL=https://bitbucket.org/anticensority/antizapret-pac-generator-light/get/$ANTIZAPRET_VER.tar.gz

    EASYRSA_VER=3.2.1
    EASYRSA_URL=https://github.com/OpenVPN/easy-rsa/releases/download/v$EASYRSA_VER/EasyRSA-$EASYRSA_VER.tgz

    mkdir antizapret && curl -s -L $ANTIZAPRET_URL | tar -zxv --strip-components=1 -C $_
    mkdir easyrsa && curl -s -L $EASYRSA_URL | tar -zxv --strip-components=1 -C $_
EOT

RUN <<-"EOT" bash -ex
    OPENVPN_VER=2.6.12
    LIBS="libnl-genl-3-dev libssl-dev libcap-ng-dev liblz4-dev libsystemd-dev"
    LIBS_TEMP="git build-essential pkg-config gcc cmake make"
    apt-get update
    apt-get install -y $LIBS $LIBS_TEMP
    mkdir -p /opt/openvpn_install && cd /opt/openvpn_install
    wget "https://raw.githubusercontent.com/Tunnelblick/Tunnelblick/master/third_party/sources/openvpn/openvpn-$OPENVPN_VER/openvpn-$OPENVPN_VER.tar.gz"
    tar xvf "openvpn-$OPENVPN_VER.tar.gz"
    cd "openvpn-$OPENVPN_VER"
    #
    patches=(
        "02-tunnelblick-openvpn_xorpatch-a.diff"
        "03-tunnelblick-openvpn_xorpatch-b.diff"
        "04-tunnelblick-openvpn_xorpatch-c.diff"
        "05-tunnelblick-openvpn_xorpatch-d.diff"
        "06-tunnelblick-openvpn_xorpatch-e.diff"
    )

    for patch in "${patches[@]}"; do
        wget "https://raw.githubusercontent.com/Tunnelblick/Tunnelblick/master/third_party/sources/openvpn/openvpn-$OPENVPN_VER/patches/$patch"
        git apply "$patch"
    done
    # Patch to overcome DPI start (works only for UDP connections, taken from https://github.com/GubernievS/AntiZapret-VPN/blob/main/setup/root/patch-openvpn.sh
    sed -i '/link_socket_write_udp(struct link_socket \*sock/,/\/\* write a TCP or UDP packet to link \*\//c\
    link_socket_write_udp(struct link_socket *sock,\
    					struct buffer *buf,\
    					struct link_socket_actual *to)\
    {\
    	uint16_t stuffing_sent = 0;\
    	uint8_t opcode = *BPTR(buf) >> 3;\
    if (opcode == 7 || opcode == 8 || opcode == 10)\
    {\
    	srand(time(NULL));\
    	for (int i=0; i<2; i++) {\
    		int stuffing_len = rand() % 91 + 10;\
    		uint8_t stuffing_data[100];\
    		for (int j=0; j<stuffing_len; j++) {\
    			stuffing_data[j] = rand() % 256;\
    		}\
    		struct buffer stuffing_buf = alloc_buf(100);\
    		buf_write(&stuffing_buf, stuffing_data, stuffing_len);\
    		for (int j=0; j<100; j++) {\
    #ifdef _WIN32\
    			stuffing_sent =+ link_socket_write_win32(sock, &stuffing_buf, to);\
    #else\
    			stuffing_sent =+ link_socket_write_udp_posix(sock, &stuffing_buf, to);\
    #endif\
    		}\
    		free_buf(&stuffing_buf);\
    	}\
    }\
    #ifdef _WIN32\
    	stuffing_sent =+ link_socket_write_win32(sock, buf, to);\
    #else\
    	stuffing_sent =+ link_socket_write_udp_posix(sock, buf, to);\
    #endif\
    	return stuffing_sent;\
    }\
    \
    \/\* write a TCP or UDP packet to link \*\/' "/opt/openvpn_install/openvpn-$OPENVPN_VER/src/openvpn/socket.h"
    # Patch to overcome DPI end

    ./configure --enable-static=yes --enable-shared  --enable-systemd=yes --disable-lzo --disable-debug --disable-plugin-auth-pam --disable-dependency-tracking
    make -j$(nproc)
    make install

    cd /root
    rm -rf /opt/openvpn_install/
    apt-get purge -y $LIBS_TEMP
    apt-get autoremove && apt-get clean

EOT

COPY rootfs /

RUN <<-"EOF" bash -ex
    systemctl enable \
        antizapret-update.service \
        antizapret-update.timer \
        dnsmap \
        kresd@1 \
        openvpn-server@antizapret \
        openvpn-server@antizapret-tcp \
        systemd-networkd \
        iperf3-server@1

    sed -i "/\b\(googleusercontent\|cloudfront\|deviantart\)\b/d" /root/antizapret/config/exclude-regexp-dist.awk
    for list in antizapret/config/*-dist.txt; do
        sed -E '/^(#.*)?[[:space:]]*$/d' $list | sort | uniq | sponge $list
    done
    for list in antizapret/config/*-custom.txt; do rm -f $list; done

    ln -sf /root/antizapret/doall.sh /usr/bin/doall
    ln -sf /root/antizapret/dnsmap.py /usr/bin/dnsmap

    rm -frv /tmp/*
EOF

COPY rootfs/etc/openvpn /etc/openvpn-default

RUN <<-"EOF" bash -ex
    (STAGE_1=true STAGE_2=true STAGE_3=false /root/antizapret/doall.sh)
    cp /root/antizapret/result/knot-aliases-alt.conf /etc/knot-resolver/knot-aliases-alt.conf
EOF

ENTRYPOINT ["/init.sh"]
