FROM d3vilh/openvpn-server:0.5.5

RUN apk add -U --no-cache ipcalc grep moreutils && apk del openvpn

ARG OPENVPN_VER=2.6.14
ADD files/socket.patch /socket.patch
# OpenVPN
RUN <<-"EOT" bash -ex
    LIBS="libnl3-dev openssl-dev libcap-ng-dev lz4-dev"
    LIBS_TEMP="linux-headers patch pkgconf git gcc g++ make"
    apk add -U --no-cache $LIBS $LIBS_TEMP
    mkdir -p /opt/openvpn_install && cd /opt/openvpn_install
    wget "https://github.com/OpenVPN/openvpn/releases/download/v${OPENVPN_VER}/openvpn-${OPENVPN_VER}.tar.gz"
    tar xvf "openvpn-${OPENVPN_VER}.tar.gz"
    cd "openvpn-${OPENVPN_VER}"

    patch -p1 "/opt/openvpn_install/openvpn-$OPENVPN_VER/src/openvpn/socket.h" /socket.patch
    ./configure --enable-static=yes --enable-dco --enable-shared --enable-systemd=no --disable-lzo --disable-debug --disable-plugin-auth-pam --disable-dependency-tracking
    make -j$(nproc)
    make install

    rm /socket.patch
    cd /root
    rm -rf /opt/openvpn_install/
    apk del $LIBS_TEMP
EOT

COPY easy-rsa.vars /opt/app
COPY files/fw-rules.sh /opt/app
COPY files/checkpsw.sh /opt/app
COPY files/server-start.sh /opt/app/docker-entrypoint.sh
COPY routes.sh /opt/app