#!/usr/bin/env bash
set -e

resolve() {
    local address
    read -r address _ < <(timeout 2 getent ahostsv4 "$1") || return 0
    printf '%s' "$address"
}

local_host=$(resolve az-local.antizapret)
world_host=""
AZ_EXPECTED_EXITS=1
if [[ "${AZ_WORLD_ENABLED:-0}" == 1 ]]; then
    world_host=$(resolve az-world.antizapret)
    AZ_EXPECTED_EXITS=2
fi
adguard_host=$(resolve adguard.antizapret)

hosts=()
[[ -z "$world_host" || "$world_host" == "$local_host" ]] || hosts+=("$world_host")
[[ -z "$local_host" ]] || hosts+=("$local_host")
AZ_FALLBACK_HOST=""
if [[ -n "$adguard_host" && "$adguard_host" != "$world_host" && "$adguard_host" != "$local_host" ]]; then
    hosts+=("$adguard_host")
    AZ_FALLBACK_HOST="$adguard_host:53"
fi
AZ_FORWARD_HOSTS="${hosts[*]}"
if [[ -z "$AZ_FORWARD_HOSTS" ]]; then
    echo "No DNS upstreams registered; keeping the previous Corefile" >&2
    exit 1
fi

export AZ_EXPECTED_EXITS AZ_FALLBACK_HOST AZ_FORWARD_HOSTS

corefile=$(envsubst < /root/Corefile.template)
[[ -n "$corefile" ]]
cmp -s /Corefile - <<< "$corefile" && exit 0

# Publish atomically so reload sees a complete file.
corefile_tmp=$(mktemp /Corefile.XXXXXX)
trap 'rm -f "$corefile_tmp"' EXIT
printf '%s\n' "$corefile" > "$corefile_tmp"
chmod 644 "$corefile_tmp"
mv -f "$corefile_tmp" /Corefile
