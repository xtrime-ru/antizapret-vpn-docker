# ========STAGE 1: BUILD========

FROM ubuntu:24.04 AS builder

ARG OCSERV_VERSION="1.5.0"
ENV OCSERV_VERSION="${OCSERV_VERSION}"

LABEL maintainer="Ivan Cherniy <kar-kar@r4ven.me>"

ARG DEBIAN_FRONTEND=noninteractive

SHELL ["/bin/bash", "-Eeuo", "pipefail", "-c"]

RUN apt-get update && \
    apt-get install --yes --no-install-recommends \
        ca-certificates curl xz-utils build-essential meson ninja-build pkg-config gperf ipcalc \
        libev-dev libgnutls28-dev libwrap0-dev \
        libpam0g-dev liblz4-dev libseccomp-dev libreadline-dev libnl-route-3-dev \
        libkrb5-dev libtasn1-bin liboath-dev libradcli-dev libprotobuf-c-dev libtalloc-dev \
        libhttp-parser-dev protobuf-c-compiler \
        libcurl4-gnutls-dev libcjose-dev libjansson-dev libssl-dev && \
    curl --fail --location --retry 3 --retry-all-errors \
        --output ocserv-"${OCSERV_VERSION}".tar.gz \
        https://gitlab.com/openconnect/ocserv/-/archive/"${OCSERV_VERSION}"/ocserv-"${OCSERV_VERSION}".tar.gz && \
    tar -xzf ./ocserv-"${OCSERV_VERSION}".tar.gz && \
    cd ./ocserv-"${OCSERV_VERSION}"/ && \
    meson setup build \
        --prefix=/usr \
        --libexecdir=libexec \
        -Doidc-auth=enabled \
        -Dfirewall-script=iptables && \
    ninja -C build && \
    DESTDIR=/opt/ocserv meson install -C build && \
    install -D -m 0644 ./doc/sample.config \
        /opt/ocserv/usr/share/doc/ocserv/sample.config

# ========STAGE 2: RUNTIME========

FROM ubuntu:24.04

ARG OCSERV_VERSION="1.5.0"
ENV OCSERV_VERSION="${OCSERV_VERSION}"

LABEL maintainer="Ivan Cherniy <kar-kar@r4ven.me>"

STOPSIGNAL SIGTERM

COPY --from=builder /opt/ocserv/ /

ENV OCSERV_DIR="/etc/ocserv"
ENV SCRIPTS_DIR="${OCSERV_DIR}/scripts"
ENV PATH="${SCRIPTS_DIR}:${PATH}"

WORKDIR $OCSERV_DIR

ARG DEBIAN_FRONTEND=noninteractive

RUN apt-get update && \
    apt-get install --yes --no-install-recommends \
        adduser \
        openssl \
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
        gettext \
        iptables \
        iproute2 \
        iputils-ping \
        ca-certificates && \
    apt-get autoremove --yes && \
    apt-get clean --yes && \
    rm -rf /var/lib/apt/lists/* /var/cache/apt/archives/*

COPY --from=xtrime/antizapret-vpn:6.7.10  /usr/bin/routes /usr/bin/routes

COPY --chmod=0755 ./ocserv.sh /
COPY ./certificate.sh /
COPY ./ocserv.tmpl /
COPY ./az.tmpl /
COPY --chmod=0755 ./healthcheck.sh /

ENTRYPOINT ["/ocserv.sh", "/usr/sbin/ocserv", "--config", "/etc/ocserv/ocserv.conf", "--foreground", "--log-stderr"]
