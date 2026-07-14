# ========STAGE 1: BUILD========

FROM ubuntu:24.04 AS builder

ENV OCSERV_VERSION="1.4.0"

LABEL maintainer="Ivan Cherniy <kar-kar@r4ven.me>"

ARG DEBIAN_FRONTEND=noninteractive

SHELL ["/bin/bash", "-Eeuo", "pipefail", "-c"]

RUN apt-get update && \
    apt-get upgrade --yes && \
    apt-get install --yes --no-install-recommends \
        curl build-essential pkg-config fakeroot devscripts \
        iputils-ping ruby-ronn openconnect libuid-wrapper \
        libnss-wrapper libsocket-wrapper gss-ntlmssp git-core make autoconf \
        libtool autopoint gettext automake nettle-dev libwrap0-dev \
        libpam0g-dev liblz4-dev libseccomp-dev libreadline-dev libnl-route-3-dev \
        libkrb5-dev liboath-dev libradcli-dev libprotobuf-c-dev libtalloc-dev \
        libhttp-parser-dev protobuf-c-compiler gperf liblockfile-bin \
        nuttcp libpam-oath libev-dev libgnutls28-dev gnutls-bin haproxy \
        yajl-tools libcurl4-gnutls-dev libcjose-dev libjansson-dev libssl-dev \
        iproute2 libpam-wrapper tcpdump libopenconnect-dev iperf3 lcov ipcalc \
        freeradius libfreeradius-dev gawk jq && \
    curl -fLO https://www.infradead.org/ocserv/download/ocserv-"${OCSERV_VERSION}".tar.xz && \
    tar -xvf ./ocserv-"${OCSERV_VERSION}".tar.xz && \
    cd ./ocserv-"${OCSERV_VERSION}"/ && \
    ./configure --enable-oidc-auth && make && \
    mkdir /usr/share/doc/ocserv/ && \
    cp ./doc/sample.config /usr/share/doc/ocserv/

WORKDIR /ocserv-"${OCSERV_VERSION}"

# ========STAGE 2: RUNTIME========

FROM ubuntu:24.04

ENV OCSERV_VERSION="1.4.0"

LABEL maintainer="Ivan Cherniy <kar-kar@r4ven.me>"

STOPSIGNAL SIGTERM

COPY --from=builder ["/ocserv-${OCSERV_VERSION}/src/occtl/occtl", "/usr/bin"]
COPY --from=builder ["/ocserv-${OCSERV_VERSION}/src/ocpasswd/ocpasswd", "/usr/bin"]
COPY --from=builder ["/ocserv-${OCSERV_VERSION}/src/ocserv-fw", "/usr/libexec"]
COPY --from=builder ["/ocserv-${OCSERV_VERSION}/src/ocserv", "/usr/sbin"]
COPY --from=builder ["/ocserv-${OCSERV_VERSION}/src/ocserv-worker", "/usr/sbin"]
COPY --from=builder ["/usr/share/doc/ocserv", "/usr/share/doc/ocserv"]

ENV OCSERV_DIR="/etc/ocserv"
ENV CERTS_DIR="${OCSERV_DIR}/certs"
ENV SSL_DIR="${OCSERV_DIR}/ssl"
ENV SECRETS_DIR="${OCSERV_DIR}/secrets"
ENV SCRIPTS_DIR="${OCSERV_DIR}/scripts"
ENV PATH="${SCRIPTS_DIR}:${PATH}"
ENV SRV_CN="example.com" 
ENV SRV_CA="Example CA"
ENV OTP_ENABLE="false"
ENV OTP_SEND_BY_EMAIL="false"
ENV OTP_SEND_BY_TELEGRAM="false"
ENV MSMTP_HOST="smtp.example.com"
ENV MSMTP_PORT="465"
ENV MSMTP_USER="mail@example.com"
ENV MSMTP_PASSWORD="PaSsw0rD"
ENV MSMTP_FROM="mail@example.com"
ENV TG_TOKEN="1234567890:QWERTYuio-PA1DFGHJ2_KlzxcVBNmqWEr3t"

WORKDIR $OCSERV_DIR

ARG DEBIAN_FRONTEND=noninteractive

RUN apt-get update && \
    apt-get install --yes --no-install-recommends \
        adduser \
        ssl-cert \
        libc6 \
        libcrypt1 \
        libev4t64 \
        libgnutls30t64 \
        libgssapi-krb5-2 \
        liblz4-1 \
        libmaxminddb0 \
        libnettle8t64 \
        libnl-3-200 \
        liboath0t64 \
        libpam0g \
        libreadline8t64 \
        libseccomp2 \
        libsystemd0 \
        libtasn1-6 \
        libtalloc2 \
        libradcli4 \
        libprotobuf-c1 \
        libnl-route-3-200 \
        libcurl3-gnutls \
        libcjose0 \
        libwrap0 \
        tini \
        gettext \
        gnutls-bin \
        iptables \
        iproute2 \
        iputils-ping \
        less \
        ca-certificates \
        xxd \
        libpam-oath \
        oathtool \
        qrencode \
        curl \
        jq \
        msmtp && \
    apt-get autoremove --yes && \
    apt-get clean --yes && \
    rm -rf /var/lib/apt/lists/* /var/cache/apt/archives/*

COPY --from=xtrime/antizapret-vpn:6.7.8  /usr/bin/routes /usr/bin/routes

COPY ./ocserv.sh /
COPY ./ca.tmpl /
COPY ./crl.tmpl /
COPY ./ocserv.tmpl /
COPY ./server.tmpl /
COPY ./az.tmpl /
COPY ./healthcheck.sh /

ENTRYPOINT ["/ocserv.sh"]

CMD ["/usr/bin/tini", "--", "/usr/sbin/ocserv", "--config", "/etc/ocserv/ocserv.conf", "--foreground" ,"--log-stderr"]

#HEALTHCHECK --interval=5m --timeout=3s \
#    CMD curl -k https://localhost:443/ || exit 1
