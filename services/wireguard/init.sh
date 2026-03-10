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

# Escape single quotes in values for SQLite
sql_escape() { echo "$1" | sed "s/'/''/g"; }

# wg-easy v15 environment variables
export PORT=${PORT:-51821}
export INSECURE=${INSECURE:-true}
export DISABLE_IPV6=${DISABLE_IPV6:-true}

# Unattended initial setup (only used on first run when DB does not exist)
export INIT_ENABLED=true
export INIT_USERNAME="${WIREGUARD_USERNAME:-admin}"
export INIT_PASSWORD="${WIREGUARD_PASSWORD}"
export INIT_HOST="$WG_HOST"
export INIT_PORT="$WG_PORT"
export INIT_DNS="$WG_DEFAULT_DNS_VALUE"
export INIT_IPV4_CIDR="$WG_IPV4_CIDR"
export INIT_IPV6_CIDR="${WG_IPV6_CIDR:-fdcc:ad94:bacf:61a4::cafe:0/112}"
export INIT_ALLOWED_IPS="$WG_ALLOWED_IPS"

I1_VAL="${I1}"
if [ -n "$I1_VAL" ]; then
  I1_VAL="'$(sql_escape "$I1_VAL")'"
else
  I1_VAL=null
fi

if [ ${#INIT_PASSWORD} -lt 12 ]; then
    echo "Error: Password must be at least 12 characters long."
    exit 1
fi

# Make sure v14 env vars are not set (v15 throws error if these exist)
unset PASSWORD
unset PASSWORD_HASH

# Compute custom PostUp/PostDown for iptables
CUSTOM_POST_UP="iptables -t nat -N masq_not_local; iptables -t nat -A POSTROUTING -s ${WG_IPV4_CIDR} -j masq_not_local; iptables -t nat -A masq_not_local -d ${DOCKER_SUBNET} -p tcp --dport 53 -j RETURN; iptables -t nat -A masq_not_local -d ${DOCKER_SUBNET} -p udp --dport 53 -j RETURN; iptables -t nat -A masq_not_local -d ${DOCKER_SUBNET} -j MASQUERADE; iptables -t nat -A masq_not_local -d ${AZ_SUBNET} -j RETURN; iptables -t nat -A masq_not_local -j MASQUERADE; iptables -A FORWARD -i wg0 -j ACCEPT; iptables -A FORWARD -o wg0 -j ACCEPT;"
CUSTOM_POST_DOWN="iptables -t nat -D POSTROUTING -s ${WG_IPV4_CIDR} -j masq_not_local; iptables -t nat -F masq_not_local; iptables -t nat -X masq_not_local; iptables -D FORWARD -i wg0 -j ACCEPT; iptables -D FORWARD -o wg0 -j ACCEPT;"

DB_FILE="/etc/wireguard/wg-easy.db"
WG_JSON="/etc/wireguard/wg0.json"

# Convert allowed IPs to JSON array for database
ALLOWED_IPS_JSON="[$(echo "$WG_ALLOWED_IPS" | tr ',' '\n' | awk '{printf "\"%s\",", $1}' | sed 's/,$//')]"
DNS_JSON="[\"${WG_DEFAULT_DNS_VALUE}\"]"

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

        sqlite3 "$DB_FILE" "UPDATE interfaces_table SET j_c=${JC_VAL}, j_min=${JMIN_VAL}, j_max=${JMAX_VAL} WHERE name='wg0';"
        sqlite3 "$DB_FILE" "UPDATE user_configs_table SET default_j_c=${JC_VAL}, default_j_min=${JMIN_VAL}, default_j_max=${JMAX_VAL}, default_i1=${I1_VAL} WHERE id='wg0';"
    fi

    # migrate legacy wg0.json server and client entries if present
    if [ -f "$WG_JSON" ]; then
        # server migration
        srv_priv=$(jq -r '.server.privateKey // empty' "$WG_JSON")
        srv_jc=$(jq -r '.server.jc // empty' "$WG_JSON")
        srv_jmin=$(jq -r '.server.jmin // empty' "$WG_JSON")
        srv_jmax=$(jq -r '.server.jmax // empty' "$WG_JSON")
        if [ -n "$srv_priv" ]; then
            srv_pub=$(jq -r '.server.publicKey // empty' "$WG_JSON")

            update_query="UPDATE interfaces_table SET"
            update_query+=" private_key='$(sql_escape "$srv_priv")'"
            update_query+=" , public_key='$(sql_escape "$srv_pub")'"
            [ -n "$srv_jc" ] && update_query+=" , j_c=${srv_jc}"
            [ -n "$srv_jmin" ] && update_query+=" , j_min='${srv_jmin}'"
            [ -n "$srv_jmax" ] && update_query+=" , j_max='${srv_jmax}'"
            update_query+=" WHERE name='wg0';"
            sqlite3 "$DB_FILE" "$update_query"
        fi

        # client migration (only if table currently empty to avoid duplicates)
        # ensure clients_table exists before attempting to insert
        if sqlite3 "$DB_FILE" "PRAGMA table_info('clients_table');" | grep -q .; then
            clients_cnt=$(sqlite3 "$DB_FILE" "SELECT count(*) FROM clients_table;" )
        else
            clients_cnt=0
        fi

        if [ "$clients_cnt" -eq 0 ]; then
            ipv6_prefix=${INIT_IPV6_CIDR%:*}
            cid=1
            jq -c '.clients[]' "$WG_JSON" | while read -r c; do
                ((cid += 1))
                cname=$(echo "$c" | jq -r '.name')
                caddr=$(echo "$c" | jq -r '.address')
                cpriv=$(echo "$c" | jq -r '.privateKey')
                cpub=$(echo "$c" | jq -r '.publicKey')
                cpsk=$(echo "$c" | jq -r '.preSharedKey')
                ccreated=$(echo "$c" | jq -r '.createdAt')
                cupdated=$(echo "$c" | jq -r '.updatedAt')
                cexpire=$(echo "$c" | jq -r '.expiredAt')
                if [ "$cexpire" != 'null' ]; then
                  cexpire="'$cexpire'";
                fi
                cenabled=$(echo "$c" | jq -r '.enabled')
                if [ "$cenabled" = "true" ]; then
                    c_enabled_val=1
                else
                    c_enabled_val=0
                fi

                if [ -n "$srv_jc" ]; then
                  awg_keys=',j_c,j_min,j_max,i1'
                  awg_values=",${srv_jc},'${srv_jmin}','${srv_jmax}',${I1_VAL}"
                else
                  awg_keys=''
                  awg_values=''
                fi
                sqlite3 "$DB_FILE" "INSERT INTO
                  clients_table(user_id,interface_id,name,ipv4_address,ipv6_address, server_allowed_ips,persistent_keepalive,mtu,private_key,public_key,pre_shared_key,expires_at,enabled,created_at,updated_at${awg_keys})
                  VALUES(1,'wg0','$(sql_escape "$cname")','${caddr}','$(printf "$ipv6_prefix:%x" ${cid})','[]',${WG_PERSISTENT_KEEPALIVE},1420,'$(sql_escape "$cpriv")','$(sql_escape "$cpub")','$(sql_escape "$cpsk")',${cexpire},${c_enabled_val},'${ccreated}','${cupdated}'${awg_values});
                "
            done
        fi

        mv -f "$WG_JSON" "$WG_JSON.backup"
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
