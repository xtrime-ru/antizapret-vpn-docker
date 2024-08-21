#!/usr/bin/env bash

domain="$(echo "$1" | sed -nr 's/(.*)\.$/\1/p')"
regex_allowed="$2"
regex_blocked="$3"

if [[ -n "$regex_allowed" ]] && [[ "$domain" =~ $regex_allowed ]]; then
    echo "allowed"
elif [[ -n "$regex_blocked"  ]] && [[ "$domain" =~ $regex_blocked ]]; then
    echo "blocked"
fi