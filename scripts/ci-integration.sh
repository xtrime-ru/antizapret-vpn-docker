#!/usr/bin/env bash
set -euo pipefail

network=$(docker network create --internal "ci-dns-$$-$RANDOM")
adguard=
dnsmap=
coredns=
cleanup() {
    local status=$? container
    for container in "$coredns" "$dnsmap" "$adguard"; do
        [[ -n "$container" ]] || continue
        if ((status != 0)); then docker logs "$container" >&2 || true; fi
        docker rm -f "$container" >/dev/null || true
    done
    docker network rm "$network" >/dev/null || true
}
trap cleanup EXIT

# Use the packaged configuration and binaries with a local rewrite for a
# deterministic response. The internal network keeps public resolvers out.
adguard=$(docker run -d --pull=never --network "$network" --network-alias ci-dns-upstream \
    --entrypoint /bin/sh antizapret-adguard:ci -ec '
        mkdir -p /opt/adguardhome/conf /opt/adguardhome/work
        yq ".users = [] | .filters = [] | .whitelist_filters = [] |
            .dns.upstream_dns = [\"127.0.0.1:9\"] | .dns.fallback_dns = [] |
            .clients.persistent = [] |
            .filtering.rewrites = [{\"domain\": \"ci-probe.test\", \"answer\": \"192.0.2.42\", \"enabled\": true}]" \
            /root/AdGuardHome.yaml > /opt/adguardhome/conf/AdGuardHome.yaml
        exec /opt/adguardhome/AdGuardHome --no-check-update \
            -c /opt/adguardhome/conf/AdGuardHome.yaml -w /opt/adguardhome/work
    ')
dnsmap=$(docker run -d --pull=never --network "$network" --cap-add NET_ADMIN \
    --network-alias az-local.antizapret --network-alias az-world.antizapret \
    --entrypoint /bin/sh antizapret-antizapret:ci -ec '
        iptables -t nat -N dnsmap
        exec /usr/bin/dnsmap --upstream ci-dns-upstream --asn-file /dev/null --iprange 14.16.0.0/29
    ')
coredns=$(docker run -d --pull=never --network "$network" --entrypoint /bin/sh antizapret-coredns:ci -ec '
    /root/config.sh
    exec /coredns -conf /Corefile
')

# Only dnsmap knows the AdGuard alias, so CoreDNS cannot bypass it.
for ((attempt=0; attempt<30; attempt++)); do
    if docker exec "$coredns" dig @127.0.0.1 ci-probe.test. A +short +time=1 +tries=1 |
        grep -Fxq 14.16.0.2; then
        docker exec "$dnsmap" iptables -t nat -C dnsmap -d 14.16.0.2 -j DNAT --to-destination 192.0.2.42
        echo 'CoreDNS -> dnsmap -> AdGuard returned a mapped address with the expected DNAT rule'
        exit 0
    fi
    sleep 1
done
echo 'DNS integration query did not return the expected address' >&2
exit 1
