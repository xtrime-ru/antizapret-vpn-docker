#!/bin/sh

STAGE_1=false
STAGE_2=true
STAGE_3=true

wget -qO- --header="Content-Type: application/json" \
     --post-data='{ "stage_1": '"$STAGE_1"', "stage_2": '"$STAGE_2"', "stage_3": '"$STAGE_3"' }' \
     http://az-local.antizapret:80/doall
