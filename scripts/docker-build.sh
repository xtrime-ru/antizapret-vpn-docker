#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

versions=()
services=()
while (($#)); do
    case "$1" in
        -v|--version) versions+=("${2:?Missing version}");;
        -s|--services) services+=("${2:?Missing service}");;
        *) echo "Unknown argument: $1" >&2; exit 1;;
    esac
    shift 2
done

if [[ -z "${versions[0]:-}" ]]; then
    versions=(latest)
fi
if [[ -z "${services[0]:-}" ]]; then
    while IFS= read -r name; do services+=("$name"); done < <(jq -r '.images[].name' versions.json)
fi

for name in "${services[@]}"; do
    image=$(jq -er --arg name "$name" '
        .images[] | select(.name == $name) | [.source,.compose,.target,.repository] | @tsv
    ' versions.json) || { echo "Unknown service: $name" >&2; exit 1; }
    IFS=$'\t' read -r source compose target repository <<< "$image"
    tags=()
    for version in "${versions[@]}"; do
        tags+=(--set "$target.tags=xtrime/$repository:$version")
    done
    (cd "$source" && docker buildx bake -f "$compose" "${tags[@]}" --push "$target")
done
