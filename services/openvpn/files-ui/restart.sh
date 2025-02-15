#!/bin/bash
# Exit immediately if a command exits with a non-zero status
set -e

#Variables
ACTION=$1  #passed via OpenVPN-UI GUI

# if ACTION is not defined then define it as openvpn-server
if [ -z "$ACTION" ]; then
      ACTION="openvpn-server"
fi

if [ "$ACTION" = "openvpn-server" ]; then  # Restartnig openvpn server
      # Get the container ID for ^openvpn$
      CONTAINER_ID=$(curl --unix-socket /var/run/docker.sock "http://v1.40/containers/json?filters=%7B%22name%22%3A%5B%22%5Eantizapret-openvpn-%5B0-9%5D%2B%24%22%5D%7D" | grep '"Id":' | cut -d '"' -f 4)
 elif [ "$ACTION" = "openvpn-ui" ]; then  # Restartnig openvpn-ui
      # Get the container ID for ^openvpn-ui$
      CONTAINER_ID=$(curl --unix-socket /var/run/docker.sock "http://v1.40/containers/json?filters=%7B%22name%22%3A%5B%22%5Eantizapret-openvpn-ui-%5B0-9%5D%2B%24%22%5D%7D" | grep '"Id":' | cut -d '"' -f 4)
 else
      echo "Invalid input argument: $ACTION Exiting..."
      exit 1
fi

if [ -z "$CONTAINER_ID" ]; then
    echo "Container not found"
    exit 2
fi
# Restart the container
curl --unix-socket /var/run/docker.sock -X POST "http://v1.40/containers/$CONTAINER_ID/restart"
# Print the restarted container ID
echo "Restarted container $CONTAINER_ID"