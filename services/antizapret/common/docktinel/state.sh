#!/bin/bash
# The producer publishes small checksums; old result volumes remain supported.
checksum_files() {
    local file
    { for file in "$@"; do
        [[ ! -f "$file" ]] || cat -- "$file" || return 1
    done; } | md5sum | cut -d ' ' -f1
}

config_checksum() {
    local marker=$1 value
    shift
    if [[ -f "$marker" ]]; then
        IFS= read -r value < "$marker" || return 1
        [[ "$value" =~ ^[0-9a-f]{32}$ ]] || return 1
        printf '%s\n' "$value"
    else
        checksum_files "$@"
    fi
}

files_metadata() {
    (($#)) || return 0
    # Follow symlinks, include inode and nanosecond ctime as well as mtime.
    stat -L -c '%n|%d|%i|%s|%y|%z' -- "$@"
}

publish_value() {
    local path=$1 value=$2 previous=''
    [[ ! -f "$path" ]] || IFS= read -r previous < "$path" || true
    [[ "$previous" != "$value" ]] || return 0
    printf '%s\n' "$value" > "$path.tmp"
    mv -f "$path.tmp" "$path"
}
