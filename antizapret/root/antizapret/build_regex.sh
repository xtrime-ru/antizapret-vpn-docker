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
    REGEX=$(sed -E '/^(#.*)?[[:space:]]*$/d' "$file" | tr '\n' '|' | xargs | sed "s/|$//g");
    if [[ -n "$REGEX" ]]; then
        echo "regex_$type = '($REGEX)'" >> result/knot-aliases-alt.conf
    else
        echo "regex_$type = ''" >> result/knot-aliases-alt.conf
    fi
done

