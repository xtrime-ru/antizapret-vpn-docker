ARG IMAGE=ghcr.io/wg-easy/wg-easy:15.3

FROM ${IMAGE} AS awg-tools-build

ARG AWG_TOOLS_VERSION=v3.0.20260805

RUN apk add -U --no-cache build-base git linux-headers \
    && git clone --depth 1 --branch "${AWG_TOOLS_VERSION}" \
        https://github.com/amnezia-vpn/amneziawg-tools.git /tmp/amneziawg-tools \
    && make -C /tmp/amneziawg-tools/src

FROM ${IMAGE}

COPY --from=awg-tools-build /tmp/amneziawg-tools/src/wg /usr/bin/awg
COPY --from=awg-tools-build /tmp/amneziawg-tools/src/wg-quick/linux.bash /usr/bin/awg-quick

RUN apk del -U -r --no-cache iptables \
    && apk add -U --no-cache iptables curl bind-tools ipcalc sqlite bird2 gettext \
    && chmod +x /usr/bin/awg /usr/bin/awg-quick \
    && apk cache clean

COPY --from=xtrime/antizapret-vpn:6.8.0  /usr/bin/routes /usr/bin/routes

COPY bird.conf.template /
COPY init.sh /
COPY healthcheck.sh /

CMD ["/init.sh"]
