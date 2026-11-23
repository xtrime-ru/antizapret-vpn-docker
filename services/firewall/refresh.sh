#!/bin/bash
set -euo pipefail
cd /root
cache=/var/lib/antizapret-firewall
action=${1:-}
mkdir -p "$cache"
exec 9>"$cache/lock"
if [[ "$action" == --check ]]; then
    # A refresh already in progress owns the rules until it releases the lock.
    status=0
    flock -n -E 75 9 || status=$?
    case "$status" in
        0) ;;
        75) exit 0 ;;
        *) exit "$status" ;;
    esac
else
    flock 9
fi

read -r generation < /run/antizapret-firewall-generation
owner_file="$cache/applied-generation"
latest=0 state=
if [[ -f "$owner_file" ]]; then
    read -r latest state < "$owner_file"
fi
if (( generation < latest )); then
    # A predecessor may observe the rules but must never change them.
    [[ "$action" != --check ]] || ./block.sh check
    exit
fi
if [[ "$action" != --restore ]]; then
    [[ "$generation" == "$latest" && "$state" != cleared ]] || exit 0
elif [[ "$generation" == "$latest" && "$state" == cleared ]]; then
    exit 0
fi

if [[ "$action" == --clear ]]; then
    # Keep the generation so an older task cannot restore after cleanup.
    printf '%s cleared\n' "$generation" > "$owner_file.tmp"
    ./block.sh clear
    mv -f -- "$owner_file.tmp" "$owner_file"
    exit 0
fi

archive="$cache/lists.tar"
if [[ "$action" == --check ]]; then
    ./block.sh check && exit 0
    echo 'Reapplying firewall rules from the last working lists' >&2
fi

staged=$(mktemp -d "$cache/.refresh.XXXXXX")
trap 'rm -rf -- "$staged"' EXIT
printf '%s active\n' "$generation" > "$staged/owner"
apply_rules() {
    # Record who may clean up a partial first apply. Keep a previous active
    # owner in charge until the replacement has applied both families.
    if [[ "$state" != active ]]; then
        printf '%s pending\n' "$generation" > "$staged/pending" &&
            mv -f -- "$staged/pending" "$owner_file" || return 1
    fi
    V4_FILE="$staged/v4" V6_FILE="$staged/v6" ./block.sh &&
        mv -f -- "$staged/owner" "$owner_file"
}

if [[ "$action" == --check || ( "$action" == --restore && -s "$archive" ) ]]; then
    if tar -xf "$archive" -C "$staged" v4 v6 &&
        [[ -s "$staged/v4" && -s "$staged/v6" ]] &&
        apply_rules; then
        exit 0
    fi
    [[ "$action" != --check ]] || exit 1
    echo 'Could not apply cached firewall lists, trying available lists' >&2
fi

if [[ "$action" == --restore && -s "$V4_FILE" && -s "$V6_FILE" ]]; then
    cp -- "$V4_FILE" "$staged/v4"
    cp -- "$V6_FILE" "$staged/v6"
else
    curl --connect-timeout 5 --max-time 60 -fsS -g -o "$staged/v4" -- "$V4_URL"
    curl --connect-timeout 5 --max-time 60 -fsS -g -o "$staged/v6" -- "$V6_URL"
fi
test -s "$staged/v4"
test -s "$staged/v6"
tar -cf "$staged/lists.tar" -C "$staged" v4 v6

# Publish both families in a single rename, only after the rules are applied.
apply_rules
mv -f -- "$staged/lists.tar" "$archive"
mv -f -- "$staged/v4" "$V4_FILE"
mv -f -- "$staged/v6" "$V6_FILE"
