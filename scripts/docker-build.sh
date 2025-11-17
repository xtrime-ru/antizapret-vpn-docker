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
  SERVICES=(antizapret adguard coredns wireguard wireguard-amnezia openvpn openvpn-ui https cloak filebrowser dashboard)
fi

# Build each service for all versions at once
for service in "${SERVICES[@]}"; do

  DIR="$service"
  if [[ "$service" =~ ^wireguard- ]]; then
    DIR="wireguard"
  elif [[ "$service" =~ ^openvpn- ]]; then
    DIR="openvpn"
  fi

  SERVICE_POSTFIX=""
  if [[ "$service" != "antizapret" ]]; then
    SERVICE_POSTFIX="-$service"
  fi

  SCRIPT_DIR="$( cd -- "$( dirname -- "${BASH_SOURCE[0]}" )" &> /dev/null && cd ../services/"$DIR" && pwd )"



  echo ""
  echo "==============================="
  echo "Building service: $service"
  echo "==============================="

  # Create dynamic tag setters
  SET_ARGS=()

  for version in "${VERSIONS[@]}"; do
    IMAGE_TAG="xtrime/antizapret-vpn${SERVICE_POSTFIX}:${version}"
    SET_ARGS+=(--set "${service}.tags=${IMAGE_TAG}")
  done

  echo "Running command:"
  echo "(cd $SCRIPT_DIR; docker buildx bake $service ${SET_ARGS[@]} --push)"

  # Execute bake for this service
  (cd "$SCRIPT_DIR"; docker buildx bake "$service" "${SET_ARGS[@]}" --push)
done
