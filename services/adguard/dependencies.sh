#!/usr/bin/env bash

# These addresses also become AdGuard client IDs.  Never invent an address
# for a missing dependency: healthcheck will discover it after recovery.
wait_for_dependencies() {
    # Reset Bash's elapsed timer so a one-second budget is not truncated by
    # the caller's existing fractional second. Keep this special variable global.
    SECONDS=0
    local deadline=$1
    AZ_LOCAL_HOST=''
    AZ_WORLD_HOST=''
    COREDNS_HOST=''

    while ((SECONDS < deadline)); do
        AZ_LOCAL_HOST=$(resolve_dependency az-local)
        if [ "${AZ_WORLD_ENABLED:-}" = "1" ]; then
            AZ_WORLD_HOST=$(resolve_dependency az-world)
        fi
        COREDNS_HOST=$(resolve_dependency coredns)
        if [ -n "$AZ_LOCAL_HOST" ] && [ -n "$COREDNS_HOST" ] \
            && { [ "${AZ_WORLD_ENABLED:-}" != "1" ] || [ -n "$AZ_WORLD_HOST" ]; }; then
            return 0
        fi
        if ((SECONDS < deadline)); then
            sleep 1
        fi
    done

    echo "Dependency DNS registration wait expired; starting AdGuard with available dependencies" >&2
}

resolve_dependency() {
    # deadline is local to wait_for_dependencies (Bash dynamic scope).
    local remaining=$((deadline - SECONDS))
    local address
    ((remaining > 0)) || return 0
    ((remaining <= 2)) || remaining=2
    address=$(timeout "$remaining" getent hosts "$1.antizapret" | awk 'NR == 1 {print $1}')
    if [[ "$address" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]]; then
        printf '%s\n' "$address"
    fi
    return 0
}
