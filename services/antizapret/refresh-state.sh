#!/usr/bin/env bash
set -eo pipefail
source /etc/default/antizapret

. /usr/local/lib/docktinel/state.sh
shopt -s nullglob
config_metadata() { files_metadata /root/antizapret/result/* /root/antizapret/config/custom/* | md5sum; }
input_checksum() { checksum_files /root/antizapret/result/* /root/antizapret/config/custom/*; }
retry() { printf 'refresh-state: %s, retrying later\n' "$1" >&2; exit 1; }

metadata=$(config_metadata)
previous=$(cat /dev/shm/.config_metadata 2>/dev/null || true)
[[ "$metadata" != "$previous" ]] || exit 0
old=$(cat /dev/shm/.config_md5 2>/dev/null || true)
new=$(input_checksum)
prepared=/dev/shm/.config_prepared_md5
# A matching prepared checksum means only the API update needs a retry.
if [[ "$old" != "$new" && "$new" != "$(cat "$prepared" 2>/dev/null || true)" ]]; then
    echo "refresh-state: config files changed"
    custom_metadata=$(files_metadata /root/antizapret/config/custom/* | md5sum)
    timeout --kill-after=5s 5m doall || retry 'doall failed'
    metadata=$(config_metadata)
    new=$(input_checksum)
    [[ "$custom_metadata" == "$(files_metadata /root/antizapret/config/custom/* | md5sum)" &&
       "$metadata" == "$(config_metadata)" ]] || retry 'inputs changed during list generation'
    publish_value "$prepared" "$new"
fi
if [[ -f "$prepared" ]]; then
    curl --noproxy '*' --connect-timeout 2 --max-time 10 -fsS http://127.0.0.1/update/ || retry 'local /update/ failed'
fi
# Do not acknowledge files that changed while they were being read.
[[ "$metadata" == "$(config_metadata)" ]] || retry 'inputs changed during refresh'
publish_value /dev/shm/.config_md5 "$new"
publish_value /dev/shm/.config_metadata "$metadata"
rm -f -- "$prepared"
