#!/usr/bin/env bash

# Wait for Docker DNS registration, not for AdGuard's DNS/API readiness.
until /root/config.sh; do
    echo 'Waiting for az-local and adguard addresses...'
    sleep 2
done

exec /coredns -conf /Corefile
