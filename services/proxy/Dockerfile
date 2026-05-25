ARG IMAGE=ghcr.io/tarampampam/3proxy:2

FROM ${IMAGE} AS builder
FROM alpine:3.23

RUN apk del -U -r --no-cache iptables \
    && apk add -U --no-cache bash iptables curl bind-tools ipcalc jq gettext inotify-tools \
    && apk cache clean

COPY --from=builder /bin/portcheck /bin/3proxy /bin/lua /bin/
COPY --from=builder /entrypoint.lua /
COPY --from=builder /etc/3proxy /etc/3proxy
COPY init.sh routes.sh /


CMD ["/init.sh"]
