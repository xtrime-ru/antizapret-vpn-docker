ARG IMAGE=ghcr.io/wg-easy/wg-easy:15

FROM ${IMAGE}

RUN apk add -U --no-cache curl bind-tools ipcalc sqlite jq bird2 gettext

COPY bird.conf.template /
COPY init.sh /
COPY routes.sh /
COPY healthcheck.sh /

CMD ["/init.sh"]
