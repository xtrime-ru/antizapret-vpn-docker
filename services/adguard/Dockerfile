ARG DOCKTINEL_IMAGE=ghcr.io/xtrime-ru/docktinel:0.2.0

FROM ${DOCKTINEL_IMAGE} AS docktinel

FROM xtrime/antizapret-vpn:6.12.1 AS antizapret

FROM adguard/adguardhome:v1.0.0 AS runtime-base

RUN apk --no-cache add iptables apache2-utils yq jq bash curl

COPY AdGuardHome.yaml check-dns dependencies.sh dns-reload.sh docktinel.sh entrypoint.sh refresh-state.sh /root/

FROM runtime-base AS test

RUN apk --no-cache add python3
COPY test_resilience.py /root/
RUN --network=none YQ=yq python3 -B -m unittest discover -s /root -v && touch /test-passed

FROM runtime-base AS runtime

RUN --mount=from=test,source=/test-passed,target=/tmp/test-passed test -f /tmp/test-passed

COPY --from=antizapret /usr/bin/routes /usr/bin/routes

ENTRYPOINT []

CMD []

COPY --from=docktinel /usr/local/bin/docktinel /usr/local/bin/docktinel
COPY --from=antizapret --chmod=0755 /usr/local/lib/docktinel/ /usr/local/lib/docktinel/
COPY --chmod=0755 docktinel.sh /etc/docktinel/tasks.sh
COPY --chmod=0755 check-dns /usr/local/lib/docktinel/check-dns
HEALTHCHECK NONE
