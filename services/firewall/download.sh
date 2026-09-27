#!/bin/bash
set -euo pipefail

# Download to a temporary file: a failed or interrupted download must never
# replace the previous list with a partial or empty one.
tmp=$(mktemp "$2.XXXXXX")
trap 'rm -f "$tmp"' EXIT
curl --max-time 60 -fsS -g "$1" -o "$tmp"
[ -s "$tmp" ]
mv -f "$tmp" "$2"
