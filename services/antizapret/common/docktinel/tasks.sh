#!/bin/sh

watch_routes() {
    docktinel add process routes --phase startup -- /usr/bin/routes "$@"
}

wait_initialized() {
    docktinel add check initialized --phase after-start --deadline "${2:-3m}" \
        --interval 1s --timeout 5s -- test -f "$1"
}
