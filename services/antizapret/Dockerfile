ARG DOCKTINEL_IMAGE=ghcr.io/xtrime-ru/docktinel:0.2.0

FROM ${DOCKTINEL_IMAGE} AS docktinel

FROM golang:1.27 AS builder-api
WORKDIR /app
COPY ./api/go.* .
RUN go mod download
COPY ./api .
RUN go test ./... && go build -o app

FROM golang:1.27 AS builder-compose2swarm
WORKDIR /app
COPY ./compose2swarm/go.* .
RUN go mod download
COPY ./compose2swarm .
RUN go test ./... && go build -o compose2swarm

FROM golang:1.27 AS builder-routes
WORKDIR /app
COPY ./routes/go.* .
RUN go mod download
COPY ./routes .
RUN go test ./... && \
    CGO_ENABLED=0 GOOS=linux go build -a -tags netgo -ldflags '-w -s -extldflags "-static"' -o routes

# Small shared build target for dependent services; no exit-node packages.
FROM scratch AS service-runtime
COPY --from=builder-routes /app/routes /usr/bin/routes
COPY --chmod=0755 common/docktinel/ /usr/local/lib/docktinel/

FROM ubuntu:24.04 AS runtime-base

WORKDIR /root

ARG WARP_VERSION=2026.8.2115.0

RUN <<-"EOT" bash -ex
    export APT_LISTCHANGES_FRONTEND=none DEBIAN_FRONTEND=noninteractive

    apt-get update -q
    apt-get install -qqy --no-install-suggests --no-install-recommends curl ca-certificates
    curl -fsSLo /tmp/warp.deb "https://pkg.cloudflareclient.com/pool/noble/main/c/cloudflare-warp/cloudflare-warp_${WARP_VERSION}_$(dpkg --print-architecture).deb"
    apt-get install -qqy --no-install-suggests --no-install-recommends \
        bsdmainutils \
        iperf3 \
        dnsutils \
        gawk \
        host \
        idn \
        inetutils-ping \
        ipset \
        iptables \
        nftables \
        iproute2 \
        ipcalc \
        moreutils \
        procps \
        python3-dnslib \
        python3-idna \
        python3-maxminddb \
        sipcalc \
        vim-tiny \
        /tmp/warp.deb
    apt-get clean
    rm -frv /var/lib/apt/lists/* /tmp/warp.deb
EOT

RUN mkdir -p /usr/share/GeoIP && \
    curl -fL --retry 3 -o /usr/share/GeoIP/GeoLite2-ASN.mmdb \
        https://github.com/P3TERX/GeoLite.mmdb/raw/download/GeoLite2-ASN.mmdb

FROM runtime-base AS test-dnsmap
WORKDIR /root/antizapret
COPY ./root/antizapret/dnsmap.py ./root/antizapret/test_dnsmap.py ./
COPY ./root/antizapret/scripts/sanitize-asn-lists.awk ./scripts/
RUN python3 -B -m unittest discover -v

FROM test-dnsmap AS runtime-files
COPY ./root /runtime/root
COPY ./init.sh ./healthcheck.sh ./refresh-state.sh /runtime/

RUN <<-"EOF" bash -ex
    for list in /runtime/root/antizapret/config/*-custom.txt; do rm -f $list; done
    rm -f /runtime/root/antizapret/test_*.py
EOF

FROM runtime-base

WORKDIR /root

COPY --from=builder-api /app/app /opt/api/app
COPY --from=builder-compose2swarm /app/compose2swarm /usr/bin/compose2swarm
COPY --from=builder-routes /app/routes /usr/bin/routes
COPY --from=vernette/ss-zapret2:v1.0.7 /opt/zapret2/ /opt/zapret2/
COPY --from=runtime-files /runtime/ /
COPY zapret/googlevideo.lua /opt/zapret2/
COPY zapret/googlevideo-nft.sh /opt/zapret2/init.d/sysv/custom.d/

RUN <<-"EOF" bash -ex
    cp -a /opt/zapret2/lua.dist /opt/zapret2/lua
    cp -a /opt/zapret2/files/fake.dist /opt/zapret2/files/fake

    ln -sf /root/antizapret/doall.sh /usr/bin/doall
    ln -sf /root/antizapret/dnsmap.py /usr/bin/dnsmap

    rm -frv /tmp/*
EOF

CMD []

COPY --from=docktinel /usr/local/bin/docktinel /usr/local/bin/docktinel
COPY --chmod=0755 common/docktinel/ /usr/local/lib/docktinel/
COPY --chmod=0755 docktinel.sh /etc/docktinel/tasks.sh
HEALTHCHECK NONE
ENTRYPOINT []
