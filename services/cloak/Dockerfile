FROM golang:1.27-alpine AS builder
ARG CLOAK_VER=2.12.0
WORKDIR /cloak
RUN apk add -U --no-cache git curl make
RUN curl -LO https://github.com/cbeuw/Cloak/archive/refs/tags/v${CLOAK_VER}.tar.gz && \
    tar -xzf v${CLOAK_VER}.tar.gz -C /cloak --strip-components=1
RUN make server

FROM alpine:edge
RUN apk add -U --no-cache bash jo tzdata ca-certificates
COPY --from=builder /cloak/build/ck-server /usr/local/bin/ck-server
COPY init.sh /
RUN mkdir /opt/cloak && chown 1000:1000 /opt/cloak
VOLUME [ "/opt/cloak" ]
EXPOSE 443/tcp
ENTRYPOINT []
CMD []
