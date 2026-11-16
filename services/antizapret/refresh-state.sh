#!/usr/bin/env bash
set -eo pipefail
source /etc/default/antizapret

. /usr/local/lib/docktinel/state.sh
shopt -s nullglob
inputs=(/root/antizapret/result/* /root/antizapret/config/custom/*)
metadata=$(files_metadata "${inputs[@]}" | md5sum)
config_metadata() { files_metadata /root/antizapret/result/* /root/antizapret/config/custom/* | md5sum; }
previous=$(cat /dev/shm/.config_metadata 2>/dev/null || true)
[[ "$metadata" != "$previous" ]] || exit 0
old=$(cat /dev/shm/.config_md5 2>/dev/null || true)
new=$(checksum_files "${inputs[@]}")
if [[ "$old" != "$new" ]]; then
    echo "refresh-state: config files changed"
    custom_metadata=$(files_metadata /root/antizapret/config/custom/* | md5sum)
    timeout --kill-after=5s 5m doall || {
        echo 'refresh-state: doall failed, keeping current state and retrying later' >&2
        exit 1
    }
    curl --connect-timeout 2 --max-time 10 -fsS http://127.0.0.1/update/ || exit 1
    inputs=(/root/antizapret/result/* /root/antizapret/config/custom/*)
    metadata=$(files_metadata "${inputs[@]}" | md5sum)
    new=$(checksum_files "${inputs[@]}")
fi
if [[ -n "${custom_metadata:-}" ]]; then
    [[ "$custom_metadata" = "$(files_metadata /root/antizapret/config/custom/* | md5sum)" ]] || exit 1
fi
# Do not acknowledge files that changed while they were being read.
[[ "$metadata" = "$(config_metadata)" ]] || exit 1
publish_value /dev/shm/.config_md5 "$new"
publish_value /dev/shm/.config_metadata "$metadata"
