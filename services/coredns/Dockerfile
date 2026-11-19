ARG DOCKTINEL_IMAGE=ghcr.io/xtrime-ru/docktinel:0.2.0
ARG COREDNS_VERSION=1.15.0

FROM ${DOCKTINEL_IMAGE} AS docktinel

FROM xtrime/antizapret-vpn:6.12.1 AS antizapret

FROM golang:1.27 AS builder
ARG COREDNS_VERSION
WORKDIR /src
RUN go mod download "github.com/coredns/coredns@v${COREDNS_VERSION}" \
    && cp -R "/go/pkg/mod/github.com/coredns/coredns@v${COREDNS_VERSION}/." . \
    && chmod -R u+w . \
    && go mod download
COPY fallbackttl.go plugin/fallbackttl/fallbackttl.go
RUN sed -i '/^forward:forward$/i fallbackttl:fallbackttl' plugin.cfg \
    && go generate coredns.go \
    && CGO_ENABLED=0 go build -tags grpcnotrace -ldflags "-s -w" -o /coredns .

FROM builder AS test

COPY Corefile.template fallbackttl_test.go plugin/fallbackttl/
RUN --network=none CGO_ENABLED=0 COREDNS=/coredns \
    go test -count=1 -tags grpcnotrace -v ./plugin/fallbackttl && touch /test-passed

FROM ubuntu:24.04 AS runtime

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl dnsutils gettext-base \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /
COPY --from=builder /coredns /coredns

COPY Corefile.template config.sh /root/
RUN --mount=from=test,source=/test-passed,target=/tmp/test-passed test -f /tmp/test-passed

ENTRYPOINT []
CMD []

COPY --from=docktinel /usr/local/bin/docktinel /usr/local/bin/docktinel
COPY --from=antizapret --chmod=0755 /usr/local/lib/docktinel/ /usr/local/lib/docktinel/
COPY --chmod=0755 docktinel.sh /etc/docktinel/tasks.sh
HEALTHCHECK NONE
