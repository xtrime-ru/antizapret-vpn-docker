ARG IMAGE

FROM ${IMAGE}

RUN apk add -U --no-cache curl bind-tools

COPY init.sh /

CMD ["/init.sh"]