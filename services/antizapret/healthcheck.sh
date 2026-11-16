#!/bin/bash
set -eo pipefail
if [[ "${1:-}" = --warp ]]; then
    source /etc/default/antizapret
    [[ "$WARP_ENABLED" != 1 ]] || warp-cli --accept-tos status | grep -q "^Status update: Connected$"
else
    LC_ALL=C nslookup -type=A -timeout=1 -retry=1 healthcheck.invalid. 127.0.0.1 |
        grep -Ex 'Address:[[:blank:]]+127\.0\.0\.1' >/dev/null
fi
