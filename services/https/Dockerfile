ARG CADDY_VERSION="2.11.9"
ARG DOCKTINEL_IMAGE=ghcr.io/xtrime-ru/docktinel:0.2.0

FROM ${DOCKTINEL_IMAGE} AS docktinel

FROM xtrime/antizapret-vpn:6.12.2 AS antizapret

FROM caddy:${CADDY_VERSION}-builder AS builder

ARG CADDY_VERSION
ARG CADDY_L4_VERSION="v0.1.3"
RUN xcaddy build "v${CADDY_VERSION}" \
    --with github.com/mholt/caddy-l4@${CADDY_L4_VERSION}

FROM caddy:${CADDY_VERSION}

COPY --from=builder /usr/bin/caddy /usr/bin/caddy
RUN apk add -U --no-cache curl idn2-utils openssl
COPY --chmod=0755 files/entrypoint.sh /entrypoint.sh
COPY --chmod=0755 files/init.sh /init.sh
CMD []
COPY files/certificates.sh /certificates.sh
COPY --chmod=0755 files/sync-certificate.sh /sync-certificate.sh

COPY --from=docktinel /usr/local/bin/docktinel /usr/local/bin/docktinel
COPY --from=antizapret --chmod=0755 /usr/local/lib/docktinel/ /usr/local/lib/docktinel/
COPY --chmod=0755 docktinel.sh /etc/docktinel/tasks.sh
ENTRYPOINT []
HEALTHCHECK NONE
