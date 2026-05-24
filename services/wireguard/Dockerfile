ARG IMAGE=ghcr.io/wg-easy/wg-easy:15

FROM ${IMAGE}


RUN apk del -U -r --no-cache iptables \
    && apk add -U --no-cache iptables curl bind-tools ipcalc sqlite jq bird2 gettext inotify-tools \
    && apk cache clean

COPY bird.conf.template /
COPY init.sh /
COPY routes.sh /
COPY healthcheck.sh /

CMD ["/init.sh"]
