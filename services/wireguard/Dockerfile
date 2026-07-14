ARG IMAGE=ghcr.io/wg-easy/wg-easy:15

FROM ${IMAGE}

RUN apk del -U -r --no-cache iptables \
    && apk add -U --no-cache iptables curl bind-tools ipcalc sqlite bird2 gettext \
    && apk cache clean

COPY --from=xtrime/antizapret-vpn:6.7.8  /usr/bin/routes /usr/bin/routes

COPY bird.conf.template /
COPY init.sh /
COPY healthcheck.sh /

CMD ["/init.sh"]
