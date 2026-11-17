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
manifest_args=(versions.json --args --)
if [[ -n "${services[0]:-}" ]]; then
    manifest_args+=("${services[@]}")
fi
images=$(jq -er '
    if $ARGS.positional == [] then .images[] else
        INDEX(.images[]; .name) as $images |
        $ARGS.positional[] | $images[.] // error("Unknown service: " + .)
    end | [.source,.compose,.target,.repository] | @tsv
' "${manifest_args[@]}")

while IFS=$'\t' read -r source compose target repository; do
    tags=()
    for version in "${versions[@]}"; do
        tags+=(--set "$target.tags=xtrime/$repository:$version")
    done
    (cd "$source" && docker buildx bake -f "$compose" "${tags[@]}" --push "$target")
done <<< "$images"
