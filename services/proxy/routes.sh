#!/usr/bin/env bash

set +x

VPN=false
self=$(hostname -s)
interval=5
DNS_FILE="/root/antizapret/result/dns.txt"

while [[ $# -gt 0 ]]; do
    case $1 in
        --dns-file)
            if [[ -z "$2" ]] || [[ "$2" == -* ]]; then
                echo "Error: --dns-file requires a non-empty option argument"
                exit 1
            fi
            DNS_FILE="$2"
            shift 2
            ;;
        --self)
            if [[ -z "$2" ]] || [[ "$2" == -* ]]; then
                echo "Error: --self requires a non-empty option argument"
                exit 1
            fi
            self="$2"
            shift 2
            ;;
        --vpn)
            VPN=true
            shift
            ;;
        --interval)
            interval="$2"
            shift 2
            ;;
        *)
            echo "Unknown option: $1"
            exit 1
            ;;
    esac
done

if [ -z "$self" ]; then
    echo "Error: --self option required"
    exit 1
fi

# resolve domain address to ip address
function resolve () {
    # $1 domain/ip address, $2 fallback ip address
    res="$(getent hosts "$1" | head -n1 | awk '{print $1}')"
    if [[ "$res" =~ ^[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}$ ]]; then
        echo "$res"
    else
        echo "$2"
    fi
}

running=true
trap 'running=false' EXIT HUP INT QUIT PIPE TERM

function update_addresses() {
    for route in ${ROUTES//;/ }; do
        route=$(echo "$route" | tr -d '[:space:]')
        if [ -z "$route" ]; then continue; fi
        host=${route%:*}
        if [ -z "$host" ] || [ "$host" = "$self" ]; then continue; fi

        gateway=$(resolve $host '')
        if [ -z "$gateway" ]; then continue; fi

        subnet=${route#*:}
        current_gateway=$(ip route show "$subnet" | awk '/via/ {print $3; exit}')
        if [ "$current_gateway" = "$gateway" ]; then
                # Route unchanged
                continue
        elif [ -z "$current_gateway" ]; then
            ip route add "$subnet" via "$gateway"
            echo "Route added: $subnet via $gateway"

            if [ "$VPN" = true ] && [ "$host" = "adguard" ]; then
                iptables -t nat -A PREROUTING -p tcp --dport 53 -j DNAT --to-destination $gateway
                iptables -t nat -A PREROUTING -p udp --dport 53 -j DNAT --to-destination $gateway
            fi

            if [ "$VPN" = true ] && [ "$host" = "az-local" ]; then
                while read -r line; do
                    [ -z $line ] && continue
                    ip route add "$line" via "$gateway" || ip route change "$line" via "$gateway"
                done < /opt/antizapret/result/ips.txt
            fi

            if [ "$VPN" = true ] && [ "$host" = "az-world" ]; then
                while read -r line; do
                    [ -z $line ] && continue
                    ip route add "$line" via "$gateway" || ip route change "$line" via "$gateway"
                done < /opt/antizapret/result/ips-world.txt
            fi
        else
            if [ "$VPN" = true ] && [ "$host" = "adguard" ]; then
                iptables -t nat -D PREROUTING -p tcp --dport 53 -j DNAT --to-destination $current_gateway || true
                iptables -t nat -D PREROUTING -p udp --dport 53 -j DNAT --to-destination $current_gateway || true
                iptables -t nat -A PREROUTING -p tcp --dport 53 -j DNAT --to-destination $gateway
                iptables -t nat -A PREROUTING -p udp --dport 53 -j DNAT --to-destination $gateway
            fi
            ip route change "$subnet" via "$gateway"
            echo "Route changed: $subnet via $gateway"

            if [ "$VPN" = true ] && [ "$host" = "az-local" ]; then
                while read -r line; do
                    [ -z "$line" ] && continue
                    ip route change "$line" via "$gateway"
                done < /opt/antizapret/result/ips.txt
            fi

            if [ "$VPN" = true ] && [ "$host" = "az-world" ]; then
                while read -r line; do
                    [ -z "$line" ] && continue
                    ip route change "$line" via "$gateway"
                done < /opt/antizapret/result/ips-world.txt
            fi
        fi
    done
}

# Ensure file exists
mkdir -p "$(dirname "$DNS_FILE")"
touch "$DNS_FILE"

# Initial update
update_addresses

while [ "$running" = true ]; do
    if inotifywait -t "$interval" -e modify,move_self,close_write "$DNS_FILE" 2>/dev/null; then
        update_addresses
    fi
done
