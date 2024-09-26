#!/bin/bash
#VERSION 1.2 by @d3vilh@github.com aka Mr. Philipp
# Exit immediately if a command exits with a non-zero status
set -e

#Variables
OVDIR=/etc/openvpn
CERT_NAME=$1
CERT_SERIAL=$2
EASY_RSA=${OVDIR}/easy-rsa

if [ -n "$1" ]; then
  export EASYRSA_BATCH=1

  # Renew certificate.
  echo "Renew certificate: $CERT_NAME with serial: $CERT_SERIAL"
  cd $EASY_RSA
  ./easyrsa renew "$CERT_NAME" nopass  #as of now only nopass is supported
  
  echo 'All Done, hlopche!'
else
  echo "Invalid input argument: $CERT_NAME Exiting."
  exit 1
fi