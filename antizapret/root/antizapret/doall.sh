#!/bin/bash -e


HERE="$(dirname "$(readlink -f "${0}")")"
cd "$HERE"


FORCE=${FORCE:-false}

STAGE_1=${STAGE_1:-false}
STAGE_2=${STAGE_2:-false}
STAGE_3=${STAGE_3:-false}

SKIP_UPDATE_FROM_ZAPRET=${SKIP_UPDATE_FROM_ZAPRET:-false}
export SKIP_UPDATE_FROM_ZAPRET

FILES=(
    temp/list.csv
    temp/nxdomain.txt
    result/adguard_upstream_dns_file
)


create_hash () {
    path=./config/custom
    echo $(
        sed -E '/^(#.*)?[[:space:]]*$/d' $path/*.txt | \
            sort -u | sha1sum | awk '{print $1}'
    )
}

diff_hashes () {
    path=./config/custom
    if [[ ! -f /root/.hash ]]; then
        create_hash > /root/.hash
        hash_1=
    else
        hash_1=$(cat /root/.hash)
    fi
    hash_2=$(create_hash)

    if [[ "$hash_1" != "$hash_2" ]]; then
        echo "Hashes are different: $hash_1 != $hash_2"
        return 1
    else
        echo "Hashes are the same: $hash_1 == $hash_2"
        return 0
    fi
}


# force update
# FORCE=true ./doall.sh
if [[ $FORCE == true ]]; then
    echo 'Force update detected!'
    ./update.sh
    ./parse.sh
    ./process.sh
    exit
fi


for file in "${FILES[@]}"; do
    if [ -f $file ]; then
        if test "$(find $file -mmin +300)"; then
            echo "$file is outdated!"
            STAGE_1=true; STAGE_2=true; break
        fi
    else
        echo "$file is missing!"
        [[ $file =~ ^temp/(list.csv|nxdomain.txt)$ ]] && STAGE_1=true
        STAGE_2=true; break
    fi
done


if ! diff_hashes; then create_hash > /root/.hash; STAGE_2=true; fi

if [[ $STAGE_1 == true ]] || [[ $STAGE_2 == true ]]; then
    STAGE_3=true;
fi

[[ $STAGE_1 == true ]] && (echo "run update.sh" && ./update.sh || exit 1)

[[ $STAGE_2 == true ]] && (echo "run parse.sh" && ./parse.sh || exit 2)

[[ $STAGE_3 == true ]] && (echo "run process.sh" && ./process.sh 2> /dev/null || exit 3)

echo "Rules updated"
exit 0