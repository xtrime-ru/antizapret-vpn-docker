#!/bin/bash

OPENVPN_DIR=/etc/openvpn
DATE=$(date +"%Y-%m-%d %H:%M:%S")

# VARIABLES
PASS_FILE=$1    # Password file passed by openvpn-server with "auth-user-pass-verify /opt/scripts/auth_client.sh via-file" in server.conf

echo "${DATE} (AUTH SCRIPT) START"

if [ ! -e "$1" ] || [ ! -s "$1" ]; then
    echo "${DATE} (AUTH SCRIPT) Argument for PASS_FILE either does not exist or is empty."
    echo "${DATE} (AUTH SCRIPT) There is no auth file. Exit with error."
    echo "${DATE} (AUTH SCRIPT) ERROR: STOP"
    exit 1
else
    echo "${DATE} (AUTH SCRIPT) Argument for PASS_FILE exists and is not empty. It is: $1"
fi

# Getting user and password passed by external user to OpenVPN server tmp file
user=$(head -1 $PASS_FILE)
pass=$(tail -1 $PASS_FILE)

echo "${DATE} (AUTH SCRIPT) Authentication attempt for user $user"

OATH_DATA_FILE=$OPENVPN_DIR/ccd/${user}
if [ -f "${OATH_DATA_FILE}" ]; then
    echo "${DATE} (AUTH SCRIPT) OATH_DATA_FILE exists and is a regular file: ${OATH_DATA_FILE}"
else
    echo "${DATE} (AUTH SCRIPT) OATH_DATA_FILE either does not exist or is not a regular file: ${OATH_DATA_FILE}"
    echo "${DATE} (AUTH SCRIPT) DISABLE ACCESS FOR ${user}"
    echo "${DATE} (AUTH SCRIPT) ERROR: STOP"
    exit 1
fi

# Parsing oath.key to getting secret entry, ignore case
key=$(grep -i -m 1 "#2FA_KEY:" ${OATH_DATA_FILE} | cut -d: -f2)
if [ -z "$key" ]; then
    echo "${DATE} (AUTH SCRIPT) OTP KEY IS EMPTY: SKIP OTP CHECKING"
    code=""
else
    echo "${DATE} (AUTH SCRIPT) OTP KEY IS OK: GENERATE OTP CODE TO VERIFY"
    # Getting 2FA code with oathtool based on our key, exiting with 0 if match:
    code=$(oathtool --totp=SHA256 ${key})
fi

# Parsing static_pass to getting secret entry, ignore case
static_pass=$(grep -i -m 1 "#STATIC_PASS:" ${OATH_DATA_FILE} | cut -d: -f2)
if [ -z "$static_pass" ]; then
    echo "${DATE} (AUTH SCRIPT) STATIC PASS IS EMPTY: SKIP ITS CHECKING"
    static_pass=""
else
    echo "${DATE} (AUTH SCRIPT) STATIC PASS IS OK: READY TO VERIFY"
fi

if [ "${static_pass}${code}" = "${pass}" ];
then
    echo "${DATE} (AUTH SCRIPT) Authentication is DONE for user $user"
    echo "${DATE} (AUTH SCRIPT) STOP"
    exit 0
else
    echo "${DATE} (AUTH SCRIPT) Password is incorrect."
fi

# If we make it here, auth hasn't succeeded, don't grant access
echo "${DATE} (AUTH SCRIPT) Authentication failed for user $user"
echo "${DATE} (AUTH SCRIPT) ERROR: STOP"
exit 1