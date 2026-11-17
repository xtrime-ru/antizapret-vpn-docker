#!/usr/bin/env bash
set -euo pipefail

artifacts=${1:?Pass the directory containing the OCI artifacts}
work=$(mktemp -d)
trap 'rm -rf -- "$work"' EXIT

name=${SERVICE:?Set the service to publish}
layout="ocidir://$work/image"
for arch in amd64 arm64; do
    regctl image import "$layout:$arch" "$artifacts/$name-$arch.tar"
    platform=$(regctl image inspect "$layout:$arch" --platform "linux/$arch" --format '{{.OS}}/{{.Architecture}}')
    if [[ "$platform" != "linux/$arch" ]]; then
        echo "$name: expected linux/$arch, got $platform" >&2
        exit 1
    fi
    regctl manifest get "$layout:$arch" --format raw-body > "$work/$arch.json"
    if ! jq -e '.manifests | type == "array"' "$work/$arch.json" >/dev/null; then
        regctl index create "$layout:index-$arch" --ref "$layout:$arch"
        regctl manifest get "$layout:index-$arch" --format raw-body > "$work/$arch.json"
    fi
done

# Join the original descriptors, keeping provenance attached to each image.
jq -es '
    {schemaVersion: 2, mediaType: "application/vnd.oci.image.index.v1+json",
     manifests: [.[].manifests[]]} |
    if ([.manifests[].platform | select(.os != "unknown") | "\(.os)/\(.architecture)"] | sort)
       == ["linux/amd64", "linux/arm64"]
    then . else error("Expected exactly linux/amd64 and linux/arm64") end
' "$work/amd64.json" "$work/arm64.json" | regctl manifest put "$layout:release"
for arch in amd64 arm64; do
    original=$(regctl image digest "$layout:$arch" --platform "linux/$arch")
    combined=$(regctl image digest "$layout:release" --platform "linux/$arch")
    if [[ "$original" != "$combined" ]]; then
        echo "$name: the linux/$arch digest changed during index creation" >&2
        exit 1
    fi
done

digest=$(regctl image digest "$layout:release" --require-list)
jq -er '.[]' <<< "${TAGS:?Set the tags to publish}" | while IFS= read -r tag; do
    regctl image copy "$layout:release" "$tag"
    published=$(regctl image digest "$tag" --require-list)
    if [[ "$published" != "$digest" ]]; then
        echo "$tag: expected $digest, got $published" >&2
        exit 1
    fi
    printf '%s %s\n' "$tag" "$digest"
done
