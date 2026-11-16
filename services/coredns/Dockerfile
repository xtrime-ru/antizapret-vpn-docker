ARG DOCKTINEL_IMAGE=ghcr.io/xtrime-ru/docktinel:0.2.0

FROM ${DOCKTINEL_IMAGE} AS docktinel

FROM xtrime/antizapret-vpn:6.12.0 AS antizapret

FROM coredns/coredns:1.15.0 AS coredns

FROM ubuntu:24.04

RUN apt update -y \
    && apt install -y gettext-base dnsutils curl \
    && apt-get clean \
    && rm -frv /var/lib/apt/lists/*

WORKDIR /
COPY --from=coredns /etc/ssl/certs/ /etc/ssl/certs/
COPY --from=coredns /coredns /coredns

COPY . /root/

ENTRYPOINT []
CMD []

COPY --from=docktinel /usr/local/bin/docktinel /usr/local/bin/docktinel
COPY --from=antizapret --chmod=0755 /usr/local/lib/docktinel/ /usr/local/lib/docktinel/
COPY --chmod=0755 docktinel.sh /etc/docktinel/tasks.sh
HEALTHCHECK NONE
