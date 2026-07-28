ARG IMAGE=ghcr.io/tarampampam/3proxy:2

FROM ${IMAGE} AS builder
FROM alpine:3.23

RUN apk del -U -r --no-cache iptables \
    && apk add -U --no-cache bash iptables \
    && apk cache clean

COPY --from=xtrime/antizapret-vpn:6.7.10  /usr/bin/routes /usr/bin/routes

COPY --from=builder /bin/portcheck /bin/3proxy /bin/lua /bin/
COPY --from=builder /entrypoint.lua /
COPY --from=builder /etc/3proxy /etc/3proxy
COPY init.sh /


CMD ["/init.sh"]
