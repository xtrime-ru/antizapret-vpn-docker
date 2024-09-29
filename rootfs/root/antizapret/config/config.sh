#!/bin/bash

# HTTPS (TLS) proxy address
PACHTTPSHOST='proxy-ssl.antizapret.prostovpn.org:3143'

# Regular proxy address
PACPROXYHOST='proxy-nossl.antizapret.prostovpn.org:29976'

# Facebook and Twitter proxy address
PACFBTWHOST='proxy-fbtw-ssl.antizapret.prostovpn.org:3143'

PACFILE="result/proxy-host-ssl.pac"
PACFILE_NOSSL="result/proxy-host-nossl.pac"

# Perform DNS resolving to detect and filter non-existent domains
RESOLVE_NXDOMAIN="no"
