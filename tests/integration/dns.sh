#!/usr/bin/env bash
set -euo pipefail

network=$(docker network create --internal "ci-dns-$$-$RANDOM")
containers=()
cleanup() {
    local status=$? container
    for container in "${containers[@]}"; do
        if ((status != 0)); then docker logs "$container" >&2 || true; fi
        docker rm -f "$container" >/dev/null || true
    done
    docker network rm "$network" >/dev/null || true
}
trap cleanup EXIT

# The internal network uses a deterministic local resolver in place of public DNS.
run_adguard() {
    docker run -d --pull=never --network "$network" --network-alias "$1" \
        -e COREDNS_ADDRESS="${2:-}" --entrypoint /bin/sh "${ADGUARD_TEST_IMAGE:-antizapret-adguard:ci}" -ec '
        mkdir -p /opt/adguardhome/conf /opt/adguardhome/work
        config=/opt/adguardhome/conf/AdGuardHome.yaml
        yq ".users = [] | .filters = [] | .whitelist_filters = [] | .dns.fallback_dns = []" \
            /root/AdGuardHome.yaml > "$config"
        if [ -n "$COREDNS_ADDRESS" ]; then
            yq -i ".dns.upstream_dns = [strenv(COREDNS_ADDRESS)] |
                .clients.persistent |= map(select(.name == \"coredns\")) |
                .clients.persistent[0].ids = [strenv(COREDNS_ADDRESS)] |
                .clients.persistent[0].upstreams = [\"ci-dns-upstream\"]" "$config"
        else
            yq -i ".dns.upstream_dns = [\"127.0.0.1:9\"] |
                .clients.persistent |= map(select(.name == \"az-local\" or .name == \"az-world\" or .name == \"az-resolver\")) |
                .clients.persistent[].upstreams = [\"127.0.0.1:9\"] |
                .user_rules = [
                    \"||ci-direct.test^\$dnsrewrite=192.0.2.42\",
                    \"||ci-direct.test^\$dnsrewrite=REFUSED,client=az-local|az-world|az-resolver,important\"
                ] |
                .filtering.rewrites = [{\"domain\": \"ci-probe.test\", \"answer\": \"192.0.2.42\", \"enabled\": true}]" "$config"
        fi
        exec /opt/adguardhome/AdGuardHome --no-check-update \
            -c "$config" -w /opt/adguardhome/work
    '
}

run_exit() {
    docker run -d --pull=never --network "$network" --cap-add NET_ADMIN --network-alias "$1.antizapret" -e CLIENT="$1" \
        --entrypoint /bin/sh "${ANTIZAPRET_TEST_IMAGE:-antizapret-antizapret:ci}" -ec '
            iptables -t nat -N dnsmap
            exec /usr/bin/dnsmap --upstream ci-dns-upstream --asn-file /dev/null --iprange "$1"
        ' sh "$2"
}

expect_dns() {
    local answer=$1 ttl=${2:-positive} domain=${3:-ci-probe.test.} result deadline=$((SECONDS + 30))
    while ((SECONDS < deadline)); do
        result=$(docker exec "$coredns" dig @127.0.0.1 "$domain" A \
            +noall +comments +answer +time=3 +tries=1) || true
        if [[ "$answer" == SERVFAIL && "$result" == *'status: SERVFAIL,'* ]] ||
            awk -v ip="$answer" -v ttl="$ttl" '$4 == "A" && $5 == ip && (ttl == "0" ? $2 == 0 : $2 > 0) {found=1} END {exit !found}' <<< "$result"; then
            return 0
        fi
        sleep 1
    done
    printf 'Expected %s with TTL %s; last response:\n%s\n' "$answer" "$ttl" "$result" >&2
    return 1
}

address() {
    docker inspect --format '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' "$1"
}

coredns=$(docker run -d --pull=never --network "$network" --init -e AZ_WORLD_ENABLED=1 \
    --entrypoint /usr/local/lib/docktinel/entrypoint "${COREDNS_TEST_IMAGE:-antizapret-coredns:ci}" \
    /coredns -conf /Corefile)
containers+=("$coredns")
docker exec "$coredns" test ! -e /Corefile

resolver=$(run_adguard ci-dns-upstream)
containers+=("$resolver")
local_exit=$(run_exit az-local 14.16.0.0/29)
containers+=("$local_exit")
world_exit=$(run_exit az-world 14.18.0.0/29)
containers+=("$world_exit")
adguard=$(run_adguard adguard.antizapret "$(address "$coredns")")
containers+=("$adguard")

expect_dns 14.18.0.2
expect_dns 192.0.2.42 positive ci-direct.test.
docker exec "$world_exit" iptables -t nat -C dnsmap -d 14.18.0.2 -j DNAT --to-destination 192.0.2.42

docker stop -t 1 "$world_exit" >/dev/null
expect_dns 14.16.0.2
expect_dns 192.0.2.42 0 ci-direct.test.
docker stop -t 1 "$local_exit" >/dev/null
expect_dns 192.0.2.42 0

# Query AdGuard as a regular client, so the request goes through CoreDNS and
# back as the distinct coredns client. A loop cannot reach the local resolver.
docker exec "$adguard" nslookup -type=A ci-probe.test. 127.0.0.1 | grep -Eq 'Address: 192\.0\.2\.42$'
docker start "$local_exit" >/dev/null
expect_dns 14.16.0.2
docker exec "$adguard" nslookup -type=A ci-probe.test. 127.0.0.1 | grep -Eq 'Address: 14\.16\.0\.2$'

docker stop -t 1 "$adguard" >/dev/null
expect_dns 14.16.0.2
docker stop -t 1 "$local_exit" >/dev/null
expect_dns SERVFAIL
docker exec "$coredns" docktinel healthcheck --no-kill

docker start "$adguard" >/dev/null
expect_dns 192.0.2.42 0
docker start "$world_exit" >/dev/null
expect_dns 14.18.0.2
echo 'CoreDNS DNS failover, cache recovery and priority recovery passed'
