ARG DOCKTINEL_IMAGE=ghcr.io/xtrime-ru/docktinel:0.2.0

FROM ${DOCKTINEL_IMAGE} AS docktinel

FROM xtrime/antizapret-vpn:6.12.1 AS antizapret

FROM jitesoft/lighttpd
RUN apk add -U --no-cache apache2-utils jq curl
RUN sed -i "/mimetype\.assign\s*=\s*(/a \  \".json\"         =>      \"application/json\"," "/etc/lighttpd/conf.d/000-mime.conf"
COPY files/www/. /var/www/html
COPY files/init.sh /init.sh
COPY --chmod=0755 files/healthcheck.sh /healthcheck.sh
CMD []

COPY --from=docktinel /usr/local/bin/docktinel /usr/local/bin/docktinel
COPY --from=antizapret --chmod=0755 /usr/local/lib/docktinel/ /usr/local/lib/docktinel/
COPY --chmod=0755 docktinel.sh /etc/docktinel/tasks.sh
ENTRYPOINT []
HEALTHCHECK NONE
