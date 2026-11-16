ARG IMAGE=ghcr.io/tarampampam/3proxy:2
ARG DOCKTINEL_IMAGE=ghcr.io/xtrime-ru/docktinel:0.2.0

FROM ${DOCKTINEL_IMAGE} AS docktinel

FROM xtrime/antizapret-vpn:6.12.1 AS antizapret

FROM ${IMAGE} AS builder
FROM alpine:3.23

RUN apk del -U -r --no-cache iptables \
    && apk add -U --no-cache bash iptables \
    && apk cache clean

COPY --from=antizapret /usr/bin/routes /usr/bin/routes

COPY --from=builder /bin/portcheck /bin/3proxy /bin/lua /bin/
COPY --from=builder /entrypoint.lua /
COPY --from=builder /etc/3proxy /etc/3proxy
COPY init.sh /

CMD []
COPY --chmod=0755 healthcheck.sh /healthcheck.sh

COPY --from=docktinel /usr/local/bin/docktinel /usr/local/bin/docktinel
COPY --from=antizapret --chmod=0755 /usr/local/lib/docktinel/ /usr/local/lib/docktinel/
COPY --chmod=0755 docktinel.sh /etc/docktinel/tasks.sh
ENTRYPOINT []
HEALTHCHECK NONE
