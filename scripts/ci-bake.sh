#!/usr/bin/env bash
set -euo pipefail

# Preserve the Dockerfile, platforms and build arguments from each service.
while IFS=$'\t' read -r name source compose target; do
    (cd "$source" && docker buildx bake -f "$compose" --print "$target") |
        jq --arg name "$name" --arg source "$source" --arg target "$target" '
            .target[$target] | .context = ($source + "/" + .context) |
            .tags = ["antizapret-" + $name + ":ci"] |
            if $name == "antizapret" or $name == "cloak" then .
            else .contexts.antizapret = "target:shared-runtime" end |
            {($name): .}'
done < <(jq -r '.images[] | [.name,.source,.compose,.target] | @tsv' versions.json) |
    jq -se --arg repo "$BUILD_CACHE_REPO" --arg branch "${CACHE_WRITE_BRANCH:-}" '
        def cache($service; $branch): "type=registry,ref=" + ($repo | ascii_downcase) + ":" + $service + "-build-" + $branch;
        add | select(has("antizapret")) |
        [keys[] as $service | ("dev","v6") as $b | cache($service; $b)] as $imports |
        with_entries(.key as $name | .value."cache-from" = $imports |
            if $branch == "dev" or $branch == "v6" then
                .value."cache-to" = [cache($name; $branch) + ",mode=max"]
            else . end) |
        {group: {default: {targets: keys}}, target: (. + {
            "shared-runtime": (.antizapret | del(.tags, ."cache-to") | .target = "service-runtime")
        })}'
