#!/usr/bin/env bash
set -e

# Until the entrypoint writes the first Corefile it is still waiting for Docker
# DNS registration. That is startup, not a failure that should restart us.
[ -s /Corefile ] || exit 0

/root/config.sh
