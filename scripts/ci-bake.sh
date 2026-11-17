#!/usr/bin/env bash
set -euo pipefail

shared=false
if grep -qiE '^FROM[[:space:]].*[[:space:]]AS[[:space:]]+service-runtime([[:space:]]|$)' services/antizapret/Dockerfile; then
    shared=true
fi

# Preserve the Dockerfile, platforms and build arguments from each service.
while IFS=$'\t' read -r name source compose target; do
    (cd "$source" && docker buildx bake -f "$compose" --print "$target") |
        jq --arg name "$name" --arg source "$source" --arg target "$target" --argjson shared "$shared" '
            .target[$target] | .context = ($source + "/" + .context) |
            .tags = ["antizapret-" + $name + ":ci"] |
            if $shared and $name != "antizapret" and $name != "cloak"
            then .contexts.antizapret = "target:shared-runtime" else . end |
            {($name): .}'
done < <(jq -r '.images[] | [.name,.source,.compose,.target] | @tsv' versions.json) |
    jq -se --arg repo "$BUILD_CACHE_REPO" --arg branch "${CACHE_WRITE_BRANCH:-}" --arg base "${CACHE_BASE_BRANCH:-}" --argjson shared "$shared" '
        def release_branch: test("^(dev|v[0-9]+)$");
        def cache($service; $branch): "type=registry,ref=" + ($repo | ascii_downcase) + ":" + $service + "-build-" + $branch;
        (["dev", $base, $branch] | map(select(release_branch)) | unique) as $branches |
        add | select(has("antizapret")) |
        [keys[] as $service | $branches[] as $b | cache($service; $b)] as $imports |
        with_entries(.key as $name | .value."cache-from" = $imports |
            if ($branch | release_branch) then
                .value."cache-to" = [cache($name; $branch) + ",mode=max"]
            else . end) |
        {group: {default: {targets: keys}}, target: .} |
        if $shared then .target["shared-runtime"] = (
            .target.antizapret | del(.tags, ."cache-to") | .target = "service-runtime"
        ) else . end'
