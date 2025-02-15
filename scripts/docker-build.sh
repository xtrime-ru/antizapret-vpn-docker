#!/usr/bin/env bash

SCRIPT_DIR="$( cd -- "$( dirname -- "${BASH_SOURCE[0]}" )" &> /dev/null && cd ../services/antizapret && pwd )";

exec docker buildx build --platform linux/amd64,linux/arm64 -t xtrime/antizapret-vpn:latest "$@" --push "$SCRIPT_DIR"
