#!/usr/bin/env bash
docktinel add check ready --phase after-start --deadline 30s --interval 1s -- \
    wget -q --spider http://127.0.0.1:80/health
docktinel add check live --interval 5s -- \
    wget -q --spider http://127.0.0.1:80/health
