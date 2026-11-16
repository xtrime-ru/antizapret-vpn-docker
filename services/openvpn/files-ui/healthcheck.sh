#!/bin/sh
set -eu
IFS= read -r url < "${DOCKTINEL_RUNTIME_DIR:-/run/docktinel}/ui-health-url"
exec curl -fsS --connect-timeout 2 --max-time 3 "$url" >/dev/null
