#!/bin/bash
set -euo pipefail
curl --connect-timeout 5 --max-time 60 -fsS "$1" -g -o "$2"
