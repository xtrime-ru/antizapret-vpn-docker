#!/bin/bash

set +x

VPN=false
self=$(hostname -s)

while [[ $# -gt 0 ]]; do
    case $1 in
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
    res="$(dig +short $1 | head -n1)"
    if [ -z "$res" ]; then
        echo "$2"
    else
        echo "$res"
    fi
}

running=true
trap 'running=false' SIGTERM SIGINT SIGQUIT

function update_addresses() {
    for route in ${ROUTES//;/ }; do

        host=${route%:*}
        gateway=$(resolve $host '')
        #echo "Checking route: $route;  gateway: $gateway"

        if [ -z "$gateway" ]; then continue; fi
        if [ "$host" = "$self" ]; then
            # Skipping route to self
            continue
        fi
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
        else
            if [ "$VPN" = true ] && [ "$host" = "adguard" ]; then
                iptables -t nat -D PREROUTING -p tcp --dport 53 -j DNAT --to-destination $current_gateway || true
                iptables -t nat -D PREROUTING -p udp --dport 53 -j DNAT --to-destination $current_gateway || true
                iptables -t nat -A PREROUTING -p tcp --dport 53 -j DNAT --to-destination $gateway
                iptables -t nat -A PREROUTING -p udp --dport 53 -j DNAT --to-destination $gateway
            fi
            ip route change "$subnet" via "$gateway"
            echo "Route changed: $subnet via $gateway"
        fi
    done
}

while [ "$running" = true ]; do
    update_addresses
    sleep 1
done &
