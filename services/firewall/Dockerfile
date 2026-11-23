ARG DOCKTINEL_IMAGE=ghcr.io/xtrime-ru/docktinel:0.3.0

FROM ${DOCKTINEL_IMAGE} AS docktinel

FROM ubuntu:24.04

RUN apt update -y  \
    && apt install -y curl ipset iptables \
    && apt-get clean \
    && rm -frv /var/lib/apt/lists/*

WORKDIR /root
ADD ./ /root

CMD []

COPY --from=docktinel /usr/local/bin/docktinel /usr/local/bin/docktinel
ENTRYPOINT []
HEALTHCHECK NONE
