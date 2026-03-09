#!/usr/bin/env bash

set -euo pipefail

# Configuration
EASY_RSA="/usr/share/easy-rsa"
export EASYRSA_PKI="/etc/openvpn/pki"
CRL_PATH="/etc/openvpn/pki/crl.pem"       # Where OpenVPN looks for the CRL
THRESHOLD=604800                          # Update if less than 1 week hours (604800 sec) remain

# Function to parse OpenSSL date string (e.g. "Jul 16 10:58:42 2026 GMT") to epoch seconds (UTC)
parse_date_to_epoch() {
    local datestr="$1"
    # Remove trailing " GMT" or " UTC" and normalize spaces
    datestr=$(echo "$datestr" | sed 's/ \(GMT\|UTC\)$//; s/^ *//; s/  */ /g')
    
    # Expected format: "Jul 16 10:58:42 2026"
    local month_str=$(echo "$datestr" | awk '{print $1}')
    local day=$(echo "$datestr" | awk '{print $2}')
    local time=$(echo "$datestr" | awk '{print $3}')
    local year=$(echo "$datestr" | awk '{print $4}')
    
    case "$month_str" in
        Jan) month=01 ;; Feb) month=02 ;; Mar) month=03 ;; Apr) month=04 ;;
        May) month=05 ;; Jun) month=06 ;; Jul) month=07 ;; Aug) month=08 ;;
        Sep) month=09 ;; Oct) month=10 ;; Nov) month=11 ;; Dec) month=12 ;;
        *) echo "ERROR: Unknown month $month_str" >&2; exit 1 ;;
    esac
    
    local formatted="${year}-${month}-${day} ${time}"
    date -u -d "$formatted" +%s
}

# Function to actually update CRL
update_crl() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') Updating CRL..."

    cd "$EASY_RSA"
    "$EASY_RSA/easyrsa" gen-crl
    chmod +r $EASY_RSA/pki/crl.pem
    echo "$(date '+%Y-%m-%d %H:%M:%S') CRL updated successfully"
}

need_update=false

if [[ ! -f "$CRL_PATH" ]]; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') CRL file missing. This should not happened. Restarting container."
    exit 1
fi

nextupdate_str=$(openssl crl -in "$CRL_PATH" -noout -nextupdate 2>/dev/null | cut -d= -f2-)
if [[ -z "$nextupdate_str" ]]; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') Cannot read nextupdate. Restarting container."
    exit 2
fi

nextupdate_epoch=$(parse_date_to_epoch "$nextupdate_str")
current_epoch=$(date -u +%s)
remaining=$((nextupdate_epoch - current_epoch))
echo "[$(date '+%Y-%m-%d %H:%M:%S')] CRL expiration date: $nextupdate_str; remaining: $remaining sec;"

if [[ $remaining -le 0 ]]; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] CRL expired (remaining: $remaining sec) → updating"
    need_update=true
elif [[ $remaining -le $THRESHOLD ]]; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] CRL expiration date less than $THRESHOLD sec remaining ($remaining sec) → updating"
    need_update=true
fi

if [[ $need_update == true ]]; then
    update_crl
    echo "CRL updated. Restarting openvpn server."
    exit 1
fi