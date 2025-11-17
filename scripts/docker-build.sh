#!/usr/bin/env bash
set -euo pipefail

# Default versions array
VERSIONS=()
# Default services array
SERVICES=()

# Parse command-line arguments
while [[ $# -gt 0 ]]; do
  case "$1" in
    -v|--version)
      VERSIONS+=("$2")
      shift 2
      ;;
    -s|--services)
      SERVICES+=("$2")
      shift 2
      ;;
    *)
      echo "Unknown argument: $1"
      exit 1
      ;;
  esac
done

# If no versions specified, default to "latest"
if [ ${#VERSIONS[@]} -eq 0 ]; then
  VERSIONS=("latest")
fi

# If no versions specified, default to "latest"
if [ ${#SERVICES[@]} -eq 0 ]; then
  SERVICES=(antizapret coredns adguard)
fi

# Build each service for all versions at once
for service in "${SERVICES[@]}"; do
  SCRIPT_DIR="$( cd -- "$( dirname -- "${BASH_SOURCE[0]}" )" &> /dev/null && cd ../services/"$service" && pwd )"

  # Prepare multiple -t arguments
  SERVICE_POSTFIX=""
  if [[ "$service" != "antizapret" ]]; then
    SERVICE_POSTFIX="-$service"
  fi
  TAG_ARGS=()
  for version in "${VERSIONS[@]}"; do
    TAG_ARGS+=("-t" "xtrime/antizapret-vpn${SERVICE_POSTFIX}:${version}")
  done

  echo "Building $service with tags: ${VERSIONS[*]} ..."
  docker buildx build \
    --platform linux/amd64,linux/arm64 \
    "${TAG_ARGS[@]}" \
    --push \
    "$SCRIPT_DIR"
done
