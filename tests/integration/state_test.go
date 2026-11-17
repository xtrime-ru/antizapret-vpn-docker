package integration

import (
	"context"
	"testing"

	"github.com/moby/moby/api/types/container"
)

func TestAntizapretGeneratedListMarkers(t *testing.T) {
	f := newFixture(t, "antizapret", "exec sleep infinity")
	f.mustExec("bash", "-euo", "pipefail", "-c", `
. /usr/local/lib/docktinel/state.sh
mkdir -p /root/antizapret/result
cd /root/antizapret
printf '192.0.2.0/24\n' > result/ips-test.txt
printf 'route\n' > result/openvpn-blocked-ranges.txt
./scripts/publish-state.sh
expected=$(checksum_files result/ips*)
[[ "$(config_checksum result/.ips-md5 result/ips*)" == "$expected" ]]
metadata=$(files_metadata result/.ips-md5)
./scripts/publish-state.sh
[[ "$(files_metadata result/.ips-md5)" == "$metadata" ]]
# Consumers use the committed marker, with no reads of the large lists.
cat() { return 99; }
[[ "$(config_checksum result/.ips-md5 result/ips*)" == "$expected" ]]
unset -f cat
printf '203.0.113.0/24\n' >> result/ips-test.txt
./scripts/publish-state.sh
[[ "$(config_checksum result/.ips-md5 result/ips*)" != "$expected" ]]
# Old volumes still work; malformed markers must fail rather than pass health.
rm result/.ips-md5
[[ "$(config_checksum result/.ips-md5 result/ips*)" == "$(checksum_files result/ips*)" ]]
printf invalid > result/.ips-md5
if config_checksum result/.ips-md5 result/ips*; then exit 1; fi
`)
}

func TestAntizapretRefreshStateMetadataAndFailedUpdateRetry(t *testing.T) {
	f := newFixture(t, "antizapret", "exec sleep infinity")
	f.mustExec("bash", "-euo", "pipefail", "-c", `
mkdir -p /root/antizapret/result /root/antizapret/config/custom /etc/default /tmp/mock
: > /etc/default/antizapret
printf initial > /root/antizapret/result/test.txt
cat > /tmp/mock/doall <<'SH'
#!/bin/sh
echo called >> /tmp/generations
SH
cat > /tmp/mock/curl <<'SH'
#!/bin/sh
[ ! -f /tmp/fail-api ]
SH
chmod +x /tmp/mock/*
export PATH=/tmp/mock:$PATH
/refresh-state.sh
[[ "$(wc -l < /tmp/generations)" == 1 ]]
# An unchanged snapshot must not read list contents.
cat > /tmp/mock/cat <<'SH'
#!/bin/sh
case "$1" in /root/antizapret/result/*|/root/antizapret/config/custom/*) exit 99;; esac
exec /bin/cat "$@"
SH
chmod +x /tmp/mock/cat
/refresh-state.sh
rm /tmp/mock/cat
# Metadata-only changes do not trigger regeneration.
touch /root/antizapret/result/test.txt
/refresh-state.sh
[[ "$(wc -l < /tmp/generations)" == 1 ]]
cp /dev/shm/.config_metadata /tmp/snapshot
printf changed > /root/antizapret/config/custom/new.txt
touch /tmp/fail-api
if /refresh-state.sh; then exit 1; fi
cmp /dev/shm/.config_metadata /tmp/snapshot
rm /tmp/fail-api
/refresh-state.sh
[[ "$(wc -l < /tmp/generations)" == 3 ]]
/refresh-state.sh
[[ "$(wc -l < /tmp/generations)" == 3 ]]
rm /root/antizapret/config/custom/new.txt
/refresh-state.sh
[[ "$(wc -l < /tmp/generations)" == 4 ]]
`)
}

func TestAdGuardBatchedClientsAndCacheClearRetry(t *testing.T) {
	f := newFixture(t, "adguard", "exec sleep infinity", "AZ_WORLD_ENABLED=1")
	f.mustExec("bash", "-euo", "pipefail", "-c", `
mkdir -p /tmp/mock
touch /dev/shm/.inited
printf 'abc abc\n' > /dev/shm/.config_md5
printf '{"local":"192.0.2.10","world":"192.0.2.11","coredns":"192.0.2.20"}\n' > /dev/shm/.dns_dependencies
printf '{"clients":[{"name":"az-local","ids":["az-local"],"use_global_settings":false},{"name":"az-world","ids":["az-world"],"use_global_settings":true},{"name":"coredns","ids":["coredns"]}]}' > /tmp/clients
cat > /tmp/mock/getent <<'SH'
#!/bin/bash
case "$2" in
 az-local.antizapret) cat /tmp/local-address;;
 az-world.antizapret) cat /tmp/world-address;;
 *) exec /usr/bin/getent "$@";;
esac
SH
chmod +x /tmp/mock/getent
echo "192.0.2.10 az-local" > /tmp/local-address
echo "192.0.2.11 az-world" > /tmp/world-address
cat > /tmp/mock/curl <<'SH'
#!/bin/bash
while (($#)); do
 case "$1" in http://*) url=$1;; --data) shift; body=$1;; esac
 shift
done
case "$url" in
 */control/dns_info) echo '{"upstream_dns":["coredns.antizapret"]}';;
 */control/dns_config) :;;
 */control/clients) cat /tmp/clients;;
 */control/clients/update)
  jq -e --argjson update "$body" '
    [.clients[] | select(.name != $update.name) | .ids[]] as $taken |
    all($update.data.ids[]; . as $id | $taken | index($id) == null)
  ' /tmp/clients >/dev/null || exit 22
  printf '%s\n' "$body" >> /tmp/updates
  jq --argjson update "$body" '.clients |= map(if .name == $update.name then $update.data else . end)' /tmp/clients > /tmp/clients.new
  mv /tmp/clients.new /tmp/clients;;
 */control/cache_clear) echo called >> /tmp/clears; [ ! -f /tmp/fail-clear ];;
 */config-md5/) echo abc;;
 *) echo "unexpected API: $url" >&2; exit 22;;
esac
SH
chmod +x /tmp/mock/curl
export PATH=/tmp/mock:$PATH
touch /tmp/fail-clear
if /root/refresh-state.sh; then exit 1; fi
[[ -f /dev/shm/.clients_cache_dirty ]]
[[ "$(wc -l < /tmp/updates)" == 3 ]]
jq -s -e 'length == 3 and .[0].data.use_global_settings == false and .[1].data.use_global_settings == true and .[2].data.ids == ["coredns","192.0.2.20"]' /tmp/updates
rm /tmp/fail-clear
/root/refresh-state.sh
[[ ! -f /dev/shm/.clients_cache_dirty ]]
[[ "$(wc -l < /tmp/updates)" == 3 ]]
[[ "$(wc -l < /tmp/clears)" == 2 ]]

: > /tmp/world-address
/root/refresh-state.sh
jq -e '.clients[] | select(.name == "az-world") | .ids == ["az-world"] and .use_global_settings' /tmp/clients
/root/refresh-state.sh
[[ "$(wc -l < /tmp/updates)" == 4 ]]
echo "192.0.2.44 az-world" > /tmp/world-address
/root/refresh-state.sh
jq -e '.clients[] | select(.name == "az-world") | .ids == ["az-world","192.0.2.44"] and .use_global_settings' /tmp/clients

# Docker may reuse both addresses before the next maintenance run.
echo "192.0.2.44 az-local" > /tmp/local-address
echo "192.0.2.10 az-world" > /tmp/world-address
/root/refresh-state.sh
jq -e '.clients[] | select(.name == "az-local") | .ids == ["az-local","192.0.2.44"]' /tmp/clients
jq -e '.clients[] | select(.name == "az-world") | .ids == ["az-world","192.0.2.10"]' /tmp/clients
`)
}

func TestOcservCertificateCacheInvalidation(t *testing.T) {
	f := newFixture(t, "ocserv", "exec sleep infinity", "HTTPS_CERT_DIR=/tmp/certs")
	f.mustExec("bash", "-euo", "pipefail", "-c", `
mkdir -p /tmp/certs
printf test > /tmp/certs/identity
printf domain > /tmp/certs/identity.type
openssl req -x509 -newkey rsa:2048 -nodes -days 2 -subj /CN=test -keyout /tmp/certs/certificate.key -out /tmp/certs/certificate.crt 2>/dev/null
. /certificate.sh
# A cold-cache tool failure must not poison subsequent checks.
openssl() { return 99; }
if certificate_checksum; then exit 1; fi
unset -f openssl
original=$(certificate_checksum)
openssl() { return 99; }
[[ "$(certificate_checksum)" == "$original" ]]
unset -f openssl
cp /tmp/certs/certificate.key /tmp/key-backup
printf invalid > /tmp/certs/certificate.key
if certificate_checksum; then exit 1; fi
cp /tmp/key-backup /tmp/certs/certificate.key
[[ "$(certificate_checksum)" == "$original" ]]
# Changing identity must invalidate the application snapshot too.
printf changed > /tmp/certs/identity
[[ "$(certificate_checksum)" != "$original" ]]
`)
}

func TestOcservLoadsListsPublishedWhileWaitingForCertificate(t *testing.T) {
	f := fixtureWithHost(t, "ocserv", "exec sleep infinity", &container.HostConfig{
		NetworkMode: "none", CapAdd: []string{"NET_ADMIN"},
	}, "HTTPS_CERT_DIR=/tmp/certs", "CERT_WAIT_TIMEOUT=15")
	f.mustExec("bash", "-euo", "pipefail", "-c", `
result=/opt/antizapret/result
mkdir -p "$result" "$HTTPS_CERT_DIR" /tmp/probe-bin
openssl req -x509 -newkey rsa:2048 -nodes -days 2 -subj /CN=test.example -keyout /tmp/test.key -out /tmp/test.crt 2>/dev/null
printf 'dns\n' > "$HTTPS_CERT_DIR/identity.type"
printf 'test.example\n' > "$HTTPS_CERT_DIR/identity"
cat > /tmp/probe-bin/sleep <<'SH'
#!/bin/sh
touch /tmp/waiting-for-certificate
exec /bin/sleep "$@"
SH
chmod +x /tmp/probe-bin/sleep
PATH="/tmp/probe-bin:$PATH" /ocserv.sh true &
pid=$!
for i in {1..50}; do
    [[ -f /tmp/waiting-for-certificate ]] && break
    sleep 0.1
done
[[ -f /tmp/waiting-for-certificate ]]
printf '192.0.2.0/24\n' > "$result/ips.txt"
md5sum "$result/ips.txt" | cut -d ' ' -f1 > "$result/.ips-md5"
mv /tmp/test.key "$HTTPS_CERT_DIR/certificate.key"
mv /tmp/test.crt "$HTTPS_CERT_DIR/certificate.crt"
wait "$pid"
grep -Fx 'route = 192.0.2.0/24' /run/ocserv/config-per-group/az
cmp "$result/.ips-md5" /dev/shm/.config_md5
/healthcheck.sh
`)
}

func TestAntizapretConfigMD5HTTPUsesDoallPublication(t *testing.T) {
	f := newFixture(t, "antizapret", `
mkdir -p /root/antizapret/config/custom
: > /root/antizapret/config/custom/exclude-hosts-custom.txt
printf 'DOALL_DISABLED=1\n' > /etc/default/antizapret
# API startup must replace a checksum left by an earlier process.
printf stale > /dev/shm/.api_config_md5
exec /opt/api/app
`)
	original := eventually(t, func(ctx context.Context) (string, error) {
		output, _, err := f.exec(ctx, "curl", "-fsS", "http://127.0.0.1/config-md5/")
		return output, err
	}, func(value string) bool { return len(value) == 33 })
	f.mustExec("bash", "-ec", `printf 'test.example\n' > /root/antizapret/config/custom/include-hosts-custom.txt`)
	if got := f.mustExec("curl", "-fsS", "http://127.0.0.1/config-md5/"); got != original {
		t.Fatal("request rehashed unpublished changes")
	}
	f.mustExec("/usr/bin/doall")
	afterDoall := f.mustExec("curl", "-fsS", "http://127.0.0.1/config-md5/")
	if afterDoall == original {
		t.Fatal("doall did not publish new checksum")
	}
	f.mustExec("bash", "-ec", `printf 'another.example\n' >> /root/antizapret/config/custom/include-hosts-custom.txt`)
	f.mustExec("curl", "-fsS", "http://127.0.0.1/update/")
	if got := f.mustExec("curl", "-fsS", "http://127.0.0.1/config-md5/"); got == afterDoall {
		t.Fatal("update did not publish new checksum")
	}
}
