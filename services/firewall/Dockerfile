ARG DOCKTINEL_IMAGE=ghcr.io/xtrime-ru/docktinel:0.2.0

FROM ${DOCKTINEL_IMAGE} AS docktinel

FROM xtrime/antizapret-vpn:6.12.1 AS antizapret

FROM ubuntu:24.04

RUN apt update -y  \
    && apt install -y curl ipset iptables \
    && apt-get clean \
    && rm -frv /var/lib/apt/lists/*

WORKDIR /root
ADD ./ /root

CMD []

COPY --from=docktinel /usr/local/bin/docktinel /usr/local/bin/docktinel
COPY --from=antizapret --chmod=0755 /usr/local/lib/docktinel/ /usr/local/lib/docktinel/
COPY --chmod=0755 docktinel.sh /etc/docktinel/tasks.sh
ENTRYPOINT []
HEALTHCHECK NONE
