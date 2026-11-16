#!/bin/sh
set -eu
/bin/portcheck --host 127.0.0.1 --port "${PROXY_PORT:-3128}" --timeout 2
/bin/portcheck --host 127.0.0.1 --port "${SOCKS_PORT:-1080}" --timeout 2
