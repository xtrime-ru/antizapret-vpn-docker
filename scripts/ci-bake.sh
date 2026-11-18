#!/usr/bin/env bash
set -euo pipefail

shared=false
if [[ "$SERVICE" != antizapret && "$SERVICE" != cloak ]] &&
   grep -qiE '^FROM[[:space:]].*[[:space:]]AS[[:space:]]+service-runtime([[:space:]]|$)' services/antizapret/Dockerfile; then
    shared=true
fi

sources=$(jq -er --arg service "$SERVICE" --argjson shared "$shared" '
    .images[] | select(.name == $service or ($shared and .name == "antizapret")) |
    [.name,.source,.compose,.target] | @tsv
' versions.json)
# Preserve the Compose build arguments, including those of the shared runtime.
while IFS=$'\t' read -r name source compose target; do
    (cd "$source" && docker buildx bake -f "$compose" --print "$target") |
        jq -e --arg name "$name" --arg source "$source" --arg target "$target" '
            .target[$target] | select(. != null) |
            {($name): (.context = ($source + "/" + .context))}'
done <<< "$sources" |
    jq -se --arg repo "$BUILD_CACHE_REPO" --arg branch "${CACHE_WRITE_BRANCH:-}" --arg base "${CACHE_BASE_BRANCH:-}" \
        --arg arch "$BUILD_ARCH" --arg service "$SERVICE" --arg oci "$OCI_DIR" \
        --argjson tags "${CACHE_TAGS:-null}" \
        --slurpfile manifest versions.json --argjson shared "$shared" '
        def release_branch: test("^(dev|v[0-9]+)$");
        def tag($name; $branch): $name + "-build-" + $branch + "-" + $arch;
        def cache: "type=registry,ref=" + ($repo | ascii_downcase) + ":" + .;
        (["dev", $base, $branch] | map(select(release_branch)) | unique) as $branches |
        add | select(has($service)) |
        # null means discovery failed. An empty array means no cache tags are available.
        [$manifest[0].images[].name as $name | $branches[] | tag($name; .) |
            . as $tag | select($tags == null or ($tags | index($tag)) != null) | cache] as $imports |
        def native: .platforms = ["linux/" + $arch] | ."cache-from" = $imports;
        . as $targets |
        {target: {($service): ($targets[$service] | native |
            .tags = ["antizapret-" + $service + ":ci"] |
            .output = ["type=oci,dest=" + $oci + "/" + $service + "-" + $arch + ".tar"] |
            if ($branch | release_branch) then ."cache-to" = [(tag($service; $branch) | cache) + ",mode=max"] else . end
        )}} |
        if $shared then
            .target[$service].contexts.antizapret = "target:shared-runtime" |
            .target["shared-runtime"] = ($targets.antizapret | select(. != null) | native | del(.tags) | .target = "service-runtime")
        else . end'
