#!/bin/bash -e


HERE="$(dirname "$(readlink -f "${0}")")"
cd "$HERE"


for file in config/custom/{include,exclude}-regex-custom.txt; do
    if [[ "$file" =~ include ]]; then
        type="blocked"
    else
        type="allowed"
    fi

    #regex_allowed
    #regex_blocked
    if [ "$(cat "$file" | wc -l)" -gt 0 ]; then
        echo "regex_$type = '($(sed -E '/^(#.*)?[[:space:]]*$/d' "$file" | tr '\n' '|' | xargs))'" >> result/knot-aliases-alt.conf
    else
        echo "regex_$type = '^$'" >> result/knot-aliases-alt.conf
    fi
done

