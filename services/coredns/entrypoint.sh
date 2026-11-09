#!/usr/bin/env bash
set -e

/root/config.sh

exec /coredns -conf /Corefile
