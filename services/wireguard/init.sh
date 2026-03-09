#!/usr/bin/env bash

if [ -z "$WG_HOST" ]; then
    export WG_HOST=$(curl -4 icanhazip.com)
fi

export WG_DEFAULT_ADDRESS=${WG_DEFAULT_ADDRESS:-"10.1.166.x"}
export WG_PORT=${WG_PORT:-51820}
export AZ_SUBNET=${AZ_SUBNET:-"14.16.0.0/14"}
WG_DEFAULT_DNS_VALUE="${WG_DEFAULT_DNS:-14.16.0.1}"

CONFIG_FILES="/opt/antizapret/result/ips*"
cat $CONFIG_FILES 2>/dev/null | md5sum | cut -d' ' -f1 > /.config_md5

DOCKER_SUBNET="$(ipcalc "$(ip -4 addr show dev eth0 | awk '$1=="inet" {print $2; exit}')" | awk '/Network:/ {print $2}')"

WG_IPV4_CIDR="${WG_DEFAULT_ADDRESS/"x"/"0"}/24"

# Compute allowed IPs
if [ -z "$WG_ALLOWED_IPS" ]; then
    WG_ALLOWED_IPS="${WG_IPV4_CIDR},${AZ_SUBNET},${DOCKER_SUBNET}"
    blocked_ranges=$(cat $CONFIG_FILES 2>/dev/null | grep -v '^[[:space:]]*$' | tr '\n' ',' | sed 's/,$//g')
    if [ -n "${blocked_ranges}" ]; then
        WG_ALLOWED_IPS="${WG_ALLOWED_IPS},${blocked_ranges}"
    fi
fi

/routes.sh --vpn &

# wg-easy v15 environment variables
export PORT=${PORT:-51821}
export INSECURE=${INSECURE:-true}
export DISABLE_IPV6=${DISABLE_IPV6:-true}

# Unattended initial setup (only used on first run when DB does not exist)
export INIT_ENABLED=true
export INIT_USERNAME=${WIREGUARD_USERNAME:-admin}
export INIT_PASSWORD="${WIREGUARD_PASSWORD:-password}"
export INIT_HOST="$WG_HOST"
export INIT_PORT="$WG_PORT"
export INIT_DNS="$WG_DEFAULT_DNS_VALUE"
export INIT_IPV4_CIDR="$WG_IPV4_CIDR"
export INIT_IPV6_CIDR="${WG_IPV6_CIDR:-fdcc:ad94:bacf:61a4::cafe:0/112}"
export INIT_ALLOWED_IPS="$WG_ALLOWED_IPS"

# Make sure v14 env vars are not set (v15 throws error if these exist)
unset PASSWORD
unset PASSWORD_HASH

# Compute custom PostUp/PostDown for iptables
CUSTOM_POST_UP="iptables -t nat -N masq_not_local; iptables -t nat -A POSTROUTING -s ${WG_IPV4_CIDR} -j masq_not_local; iptables -t nat -A masq_not_local -d ${DOCKER_SUBNET} -p tcp --dport 53 -j RETURN; iptables -t nat -A masq_not_local -d ${DOCKER_SUBNET} -p udp --dport 53 -j RETURN; iptables -t nat -A masq_not_local -d ${DOCKER_SUBNET} -j MASQUERADE; iptables -t nat -A masq_not_local -d ${AZ_SUBNET} -j RETURN; iptables -t nat -A masq_not_local -j MASQUERADE; iptables -A FORWARD -i wg0 -j ACCEPT; iptables -A FORWARD -o wg0 -j ACCEPT;"
CUSTOM_POST_DOWN="iptables -t nat -D POSTROUTING -s ${WG_IPV4_CIDR} -j masq_not_local; iptables -t nat -F masq_not_local; iptables -t nat -X masq_not_local; iptables -D FORWARD -i wg0 -j ACCEPT; iptables -D FORWARD -o wg0 -j ACCEPT;"

DB_FILE="/etc/wireguard/wg-easy.db"

# Convert allowed IPs to JSON array for database
ALLOWED_IPS_JSON="[$(echo "$WG_ALLOWED_IPS" | tr ',' '\n' | awk '{printf "\"%s\",", $1}' | sed 's/,$//')]"
DNS_JSON="[\"${WG_DEFAULT_DNS_VALUE}\"]"

# Escape single quotes in values for SQLite
sql_escape() { echo "$1" | sed "s/'/''/g"; }

update_db() {
    local post_up
    local post_down
    local host_val
    post_up=$(sql_escape "$CUSTOM_POST_UP")
    post_down=$(sql_escape "$CUSTOM_POST_DOWN")
    host_val=$(sql_escape "$WG_HOST")

    # Update hooks (PostUp/PostDown)
    sqlite3 "$DB_FILE" "UPDATE hooks_table SET post_up='${post_up}', post_down='${post_down}' WHERE id='wg0';"

    # Update interface port and CIDR
    sqlite3 "$DB_FILE" "UPDATE interfaces_table SET port=${WG_PORT}, ipv4_cidr='${WG_IPV4_CIDR}' WHERE name='wg0';"

    # Update user config (allowed IPs, DNS, host, port, persistent keepalive)
    sqlite3 "$DB_FILE" "UPDATE user_configs_table SET default_allowed_ips='${ALLOWED_IPS_JSON}', default_dns='${DNS_JSON}', host='${host_val}', port=${WG_PORT} WHERE id='wg0';"

    if [ -n "$WG_PERSISTENT_KEEPALIVE" ]; then
        sqlite3 "$DB_FILE" "UPDATE user_configs_table SET default_persistent_keepalive=${WG_PERSISTENT_KEEPALIVE} WHERE id='wg0';"
    fi

    # Update AmneziaWG junk packet settings if set
    if [ "$EXPERIMENTAL_AWG" = "true" ]; then
        JC_VAL=${JC:-3}
        JMIN_VAL=${JMIN:-20}
        JMAX_VAL=${JMAX:-100}
        I1_VAL="${I1}"
        sqlite3 "$DB_FILE" "UPDATE interfaces_table SET j_c=${JC_VAL}, j_min=${JMIN_VAL}, j_max=${JMAX_VAL} WHERE name='wg0';"
        sqlite3 "$DB_FILE" "UPDATE user_configs_table SET default_j_c=${JC_VAL}, default_j_min=${JMIN_VAL}, default_j_max=${JMAX_VAL}, default_i1='${I1_VAL}' WHERE name='wg0';"
    fi
}

if [ -f "$DB_FILE" ]; then
    # Subsequent start: update DB before server starts
    update_db
else
    # First start: server creates DB via migrations, then we update hooks and
    # send SIGTERM to PID 1 (tini) to restart the container so the server
    # picks up the updated DB on the next start.
    (
        while [ ! -f "$DB_FILE" ]; do sleep 1; done
        sleep 3
        update_db
        kill -TERM 1
    ) &
fi

exec /usr/bin/dumb-init node server/index.mjs
