ARG IMAGE

FROM ${IMAGE}

RUN apk add -U --no-cache curl bind-tools ipcalc

COPY init.sh /
COPY routes.sh /

CMD ["/init.sh"]