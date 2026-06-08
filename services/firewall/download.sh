#!/bin/bash
set -ex

curl --max-time 60 -f "$1" -g -o "$2"
