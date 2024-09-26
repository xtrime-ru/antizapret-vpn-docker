#!/bin/bash
#VERSION 1.0
# Exit immediately if a command exits with a non-zero status
set -e

if [[ $OVDIR == "" ]]; then
	OVDIR="/etc/openvpn"
fi

# .ovpn file path
DEST_FILE_PATH="$OVDIR/clients/$1.ovpn"
INDEX_PATH="$OVDIR/easy-rsa/pki/index.txt"

# Define key serial number by keyname

STATUS_CH=$(grep -e ${1}$ -e ${1}/ ${INDEX_PATH} | awk '{print $1}' | tr -d '\n')
if [[ $STATUS_CH = "V" ]]; then
    echo "Cert is VALID"
    CERT_SERIAL=$(grep ${1}/ ${INDEX_PATH} | awk '{print $3}' | tr -d '\n')
    echo "Will remove: ${CERT_SERIAL}"
else
    echo "Cert is REVOKED"
    CERT_SERIAL=$(grep ${1}$ ${INDEX_PATH} | awk '{print $4}' | tr -d '\n')
    echo "Will remove: ${CERT_SERIAL}"
fi

# Remove user from OpenVPN
rm -f $OVDIR/easy-rsa/pki/certs_by_serial/$CERT_SERIAL.pem
rm -f $OVDIR/easy-rsa/pki/issued/$1.crt
rm -f $OVDIR/easy-rsa/pki/private/$1.key
rm -f $OVDIR/easy-rsa/pki/reqs/$1.req
rm -f $OVDIR/clients/$1.ovpn

# Fix index.txt by removing the user from the list following the serial number
sed -i'.bak' "/${CERT_SERIAL}/d" $INDEX_PATH

echo 'Remove done!'
echo 'If you want to disconnect the user please restart the service using docker-compose restart openvpn.'
