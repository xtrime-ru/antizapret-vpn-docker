#!/usr/bin/env bash
set -euo pipefail
/opt/app/update-crl.sh
if [ -f /dev/shm/.crl_renewed ]; then
    docktinel restart --reason 'OpenVPN CRL renewed'
fi
