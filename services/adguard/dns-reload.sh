#!/usr/bin/env bash

dependency_snapshot() {
    jq -nc --arg local_ip "$1" --arg world_ip "$2" --arg core_ip "$3" \
        '{local: $local_ip, world: $world_ip, coredns: $core_ip}'
}

reload_dns_on_dependency_change() {
    local state previous dns_config upstream_body
    state=$(dependency_snapshot "$1" "$2" "$3") || return 1
    previous=$(cat "$DNS_DEPENDENCY_STATE_FILE" 2>/dev/null || true)
    [ "$state" != "$previous" ] || return 0

    echo "Dependency addresses changed; reinitializing AdGuard DNS"
    dns_config=$(adguard_api /control/dns_info) || return 1
    upstream_body=$(printf '%s' "$dns_config" | jq -ce \
        '{upstream_dns} | select(.upstream_dns | type == "array" and length > 0)') || return 1
    # Supplying upstream_dns reconfigures both the public and internal DNS
    # proxies, even when the upstream list itself has not changed.
    adguard_api /control/dns_config -X POST -H 'Content-Type: application/json' \
        --data "$upstream_body" || return 1

    printf '%s\n' "$state" > "$DNS_DEPENDENCY_STATE_FILE.tmp" || return 1
    mv -f "$DNS_DEPENDENCY_STATE_FILE.tmp" "$DNS_DEPENDENCY_STATE_FILE"
}
