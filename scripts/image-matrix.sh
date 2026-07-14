#!/usr/bin/env bash
set -euo pipefail

OLD_VERSIONS=${1:?Path to the previous versions.json is required}
CURRENT_VERSIONS=${2:-versions.json}
DOCKERHUB_NAMESPACE=${DOCKERHUB_NAMESPACE:-xtrime}
GHCR_NAMESPACE=${GHCR_NAMESPACE:-xtrime-ru}

validate_manifest() {
    jq -e '
        .images as $images |
        ($images | type == "array") and
        all($images[];
            (.name | type == "string") and
            (.repository | type == "string") and
            (.version | type == "string") and
            (.source | type == "string") and
            (.compose | type == "string") and
            (.target | type == "string") and
            (.aliases | type == "array")
        )
    ' "$1" >/dev/null
}

validate_manifest "$OLD_VERSIONS"
validate_manifest "$CURRENT_VERSIONS"

jq -c \
    --arg dockerhub "$DOCKERHUB_NAMESPACE" \
    --arg ghcr "$GHCR_NAMESPACE" \
    --slurpfile old "$OLD_VERSIONS" '
    def publish_refs($image):
        [
            "ghcr.io/\($ghcr)/\($image.ghcr_repository // $image.repository):\($image.version)"
        ] + [
            $image.aliases[] as $tag |
            "\($dockerhub)/\($image.repository):\($tag)",
            "ghcr.io/\($ghcr)/\($image.ghcr_repository // $image.repository):\($tag)"
        ];
    def tag_overrides($image):
        publish_refs($image) |
        map("\($image.target).tags+=\(.)") |
        join("\n");
    {
        include: [
            .images[] as $current |
            (($old[0].images // []) | map(select(.name == $current.name)) | first) as $previous |
            select($previous == null or $previous.version != $current.version) |
            $current + {set_tags: tag_overrides($current)}
        ]
    }
' "$CURRENT_VERSIONS"
