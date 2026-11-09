ARG CADDY_VERSION="2.11.7"

FROM caddy:${CADDY_VERSION}-builder AS builder

ARG CADDY_VERSION
ARG CADDY_L4_VERSION="v0.1.2"
RUN xcaddy build "v${CADDY_VERSION}" \
    --with github.com/mholt/caddy-l4@${CADDY_L4_VERSION}

FROM caddy:${CADDY_VERSION}

COPY --from=builder /usr/bin/caddy /usr/bin/caddy
RUN apk add -U --no-cache curl idn2-utils openssl
COPY --chmod=0755 files/entrypoint.sh /entrypoint.sh
COPY --chmod=0755 files/init.sh /init.sh
ENTRYPOINT [ "/entrypoint.sh" ]
