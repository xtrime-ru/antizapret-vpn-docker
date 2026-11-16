ARG DOCKTINEL_IMAGE=ghcr.io/xtrime-ru/docktinel:0.2.0

FROM ${DOCKTINEL_IMAGE} AS docktinel

FROM xtrime/antizapret-vpn:6.12.1 AS antizapret

FROM adguard/adguardhome:v1.0.0-b.2

RUN apk --no-cache add iptables apache2-utils yq jq bash curl

COPY --from=antizapret /usr/bin/routes /usr/bin/routes

ADD . /root/

ENTRYPOINT []

CMD []

COPY --from=docktinel /usr/local/bin/docktinel /usr/local/bin/docktinel
COPY --from=antizapret --chmod=0755 /usr/local/lib/docktinel/ /usr/local/lib/docktinel/
COPY --chmod=0755 docktinel.sh /etc/docktinel/tasks.sh
COPY --chmod=0755 check-dns /usr/local/lib/docktinel/check-dns
HEALTHCHECK NONE
