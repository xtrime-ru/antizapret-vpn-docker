#!/bin/sh

STAGE_1=false
STAGE_2=true
STAGE_3=true

curl -X POST http://az-local.antizapret:80/doall \
     -H "Content-Type: application/json" \
     -d '{ "stage_1": '"$STAGE_1"', "stage_2": '"$STAGE_2"', "stage_3": '"$STAGE_3"' }'
