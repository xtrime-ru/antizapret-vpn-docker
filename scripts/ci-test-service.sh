#!/usr/bin/env bash
set -euo pipefail

image="antizapret-$SERVICE:ci"
test "$(docker image inspect --format '{{.Os}}/{{.Architecture}}' "$image")" = "linux/$BUILD_ARCH"
tests=
args=()
case "$SERVICE" in
    adguard) binary=/opt/adguardhome/AdGuardHome; args=(--version); tests='Test(AdGuard|DNSProbe)' ;;
    antizapret) binary=/usr/bin/dnsmap; args=(--help); tests=TestAntizapret ;;
    cloak) binary=/usr/local/bin/ck-server ;;
    coredns) binary=/coredns; args=(-version); tests='Test(CoreDNS|Entrypoint|Healthcheck)' ;;
    dashboard) binary=lighttpd; args=(-v); tests=TestDashboard ;;
    filebrowser) binary=/bin/filebrowser; args=(version); tests=TestFileBrowser ;;
    firewall) binary=ipset; tests=TestFirewall ;;
    https) binary=caddy; args=(version); tests=TestHTTPS ;;
    ocserv) binary=ocserv; args=(--version); tests=TestOcserv ;;
    openvpn) binary=/usr/local/sbin/openvpn; args=(--version) ;;
    openvpn-ui) binary=/opt/openvpn-ui/openvpn-ui; tests=TestOpenVPNUI ;;
    proxy) binary=/bin/3proxy; tests=TestProxy ;;
    wireguard) binary=wg; args=(--version) ;;
    *) binary=sh ;;
esac
if [[ -n "${args[0]:-}" ]]; then
    docker run --rm --pull=never --network none --entrypoint "$binary" "$image" "${args[@]}"
else
    docker run --rm --pull=never --network none --entrypoint /bin/sh "$image" -ec 'command -v "$1"' sh "$binary"
fi

if [[ "$SERVICE" == adguard && -f services/adguard/test_resilience.py ]]; then
    YQ=yq python3 -B -m unittest discover -s services/adguard -v
fi
if [[ "$SERVICE" == coredns && -f services/coredns/test_failover.py ]]; then
    docker run --rm --pull=never --entrypoint /bin/sh "$image" -ec 'cat /coredns' > "$RUNNER_TEMP/coredns"
    chmod +x "$RUNNER_TEMP/coredns"
    COREDNS="$RUNNER_TEMP/coredns" python3 -B services/coredns/test_failover.py -v
fi
if [[ ! -f tests/integration/go.mod || "$SERVICE" == cloak ]]; then
    exit 0
fi

CGO_ENABLED=0 go -C tests/integration test -c -o "$RUNNER_TEMP/integration.test"
test_command=("$RUNNER_TEMP/integration.test")
export DOCKER_HOST="$DOCKER_SOCKET"
if [[ "$SERVICE" == adguard ]]; then
    # Run the DNS probe tests with the BusyBox version shipped in this image.
    test_command=(docker run --rm --pull=never --entrypoint /integration.test
        -v "${DOCKER_SOCKET#unix://}:/var/run/docker.sock"
        -v "$PWD:/repo:ro" -w /repo/tests/integration
        -v "$RUNNER_TEMP/integration.test:/integration.test:ro" "$image")
fi

cd tests/integration
"${test_command[@]}" -test.v -test.timeout=10m "-test.run=^TestServiceLaunchPolicy$/^$SERVICE$"
if [[ -n "$tests" ]]; then
    "${test_command[@]}" -test.v -test.timeout=10m "-test.run=^$tests"
fi
case "$SERVICE" in
    wireguard|openvpn|ocserv)
        "${test_command[@]}" -test.v -test.timeout=10m "-test.run=^TestVPNStartupAndHealthDoNotDependOnAdGuard$/^$SERVICE$"
        ;;
esac
