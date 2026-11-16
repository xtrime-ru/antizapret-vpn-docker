#!/usr/bin/env bash
set -e

# resolve domain address to ip address
function resolve () {
    # $1 domain/ip address, $2 fallback ip address
    local output address
    output=$(timeout 2 getent hosts "$1") || true
    read -r address _ <<< "$output" || true
    if [[ "$address" =~ ^[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}$ ]]; then
        printf '%s\n' "$address"
    else
        printf '%s\n' "$2"
    fi
}

AZ_LOCAL_HOST="$(resolve 'az-local.antizapret' '')"
AZ_WORLD_HOST="$(resolve 'az-world.antizapret' "$AZ_LOCAL_HOST")"
DNS_HOST="$(resolve 'adguard.antizapret' '')"

# Keep priority, deduplicate aliases, and omit missing DNS registrations.
hosts=()
for address in "$AZ_WORLD_HOST" "$AZ_LOCAL_HOST" "$DNS_HOST"; do
    if [[ -n "$address" && " ${hosts[*]} " != *" $address "* ]]; then
        hosts+=("$address")
    fi
done
AZ_FORWARD_HOSTS="${hosts[*]}"
if [ -z "$AZ_FORWARD_HOSTS" ]; then
    echo "No DNS upstreams registered; keeping the previous Corefile" >&2
    exit 1
fi
export AZ_FORWARD_HOSTS

[[ -s /Corefile ]] && cmp -s /Corefile <(envsubst < /root/Corefile.template) && exit 0

# Publish atomically: reload must never see an empty or partial Corefile.
corefile_tmp=$(mktemp /Corefile.XXXXXX)
trap 'rm -f "$corefile_tmp"' EXIT
envsubst < /root/Corefile.template > "$corefile_tmp"
test -s "$corefile_tmp"
chmod 644 "$corefile_tmp"
mv -f "$corefile_tmp" /Corefile
