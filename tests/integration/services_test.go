package integration

import (
	"context"
	"os"
	"strconv"
	"strings"
	"testing"

	"github.com/moby/moby/api/types/container"
	"github.com/moby/moby/api/types/mount"
)

func serviceImage(service string) string {
	if image := os.Getenv(strings.ToUpper(strings.ReplaceAll(service, "-", "_")) + "_TEST_IMAGE"); image != "" {
		return image
	}
	return "antizapret-" + service + ":ci"
}

func TestHTTPSCertificateMaintenanceAndLocalHealth(t *testing.T) {
	f := newFixture(t, "https", "", "DOCKTINEL_RUNTIME_DIR=/tmp/https-watchdog", "PROXY_DOMAIN=sentinel.example", "PROXY_CERT_MODE=auto", "PROXY_ACME_CA=http://127.0.0.1:9", "PROXY_SERVICE_1=Missing:444:127.0.0.1:9")
	f.runtime()
	// Parsed certificates are reused, but expiry still advances without file changes.
	f.mustExec("sh", "-ec", `
. /usr/local/lib/docktinel/certificate-cache.sh
cp /data/ocserv/fallback.crt /tmp/cold.crt
cp /data/ocserv/fallback.key /tmp/cold.key
openssl() { return 99; }
if certificate_cached /tmp/cold.crt /tmp/cold.key; then exit 1; fi
unset -f openssl
certificate_cached /tmp/cold.crt /tmp/cold.key
# Previously poisoned cache entries must also recover after an upgrade.
cache="${DOCKTINEL_RUNTIME_DIR}/certificates/$(printf '%s' /tmp/cold.crt | cksum)"
sed -i '1s/v1-valid/v1-invalid/' "$cache"
certificate_cached /tmp/cold.crt /tmp/cold.key
certificate_cached /data/ocserv/fallback.crt /data/ocserv/fallback.key
expires=$CERT_EXPIRES
openssl() { echo unexpected-openssl >&2; return 99; }
certificate_cached /data/ocserv/fallback.crt /data/ocserv/fallback.key
# Simulate the clock passing notAfter, keeping both files and cache intact.
date() { echo "$expires"; }
! certificate_cached /data/ocserv/fallback.crt /data/ocserv/fallback.key
`)

	f.mustExec("curl", "-fsS", "http://127.0.0.1:2018/healthz")
	// A dead backend and unavailable ACME issuer must not make Caddy unhealthy.
	if code := f.mustExec("curl", "-ksS", "--max-time", "3", "--resolve", "sentinel.example:444:127.0.0.1", "-o", "/dev/null", "-w", "%{http_code}", "https://sentinel.example:444/"); code != "502" {
		t.Fatalf("unexpected backend response: %q", code)
	}
	f.mustExec("docktinel", "healthcheck")
	// Make the next reload fail while the serving Caddy process stays alive.
	f.mustExec("sh", "-ec", `mv /usr/bin/caddy /usr/bin/caddy.real
printf '#!/bin/sh\nexit 42\n' > /usr/bin/caddy
chmod +x /usr/bin/caddy
mkdir -p /data/caddy/certificates/test-ca/sentinel.example
openssl req -x509 -newkey rsa:2048 -nodes -days 2 -subj /CN=sentinel.example -addext subjectAltName=DNS:sentinel.example -keyout /data/caddy/certificates/test-ca/sentinel.example/sentinel.example.key -out /data/caddy/certificates/test-ca/sentinel.example/sentinel.example.crt 2>/dev/null`)
	f.waitState("degraded")
	f.mustExec("docktinel", "healthcheck")
	f.mustExec("mv", "/usr/bin/caddy.real", "/usr/bin/caddy")
	f.waitState("healthy")
	f.mustExec("cmp", "-s", "/data/ocserv/certificate.crt", "/data/caddy/certificates/test-ca/sentinel.example/sentinel.example.crt")
	// Losing the managed certificate must reactivate fallback without a restart.
	f.mustExec("sh", "-ec", `printf invalid > /data/ocserv/fallback.key; rm -rf /data/caddy/certificates/test-ca`)
	eventually(t, func(ctx context.Context) (bool, error) {
		_, code, err := f.exec(ctx, "cmp", "-s", "/data/ocserv/certificate.crt", "/data/ocserv/fallback.crt")
		return code == 0, err
	}, func(ok bool) bool { return ok })
	f.mustExec("docktinel", "healthcheck")
}

func TestProxyListenersAndRoutesRecovery(t *testing.T) {
	f := newFixture(t, "proxy", `printf '#!/bin/sh\nexec sleep 600\n' > /usr/bin/routes
chmod +x /usr/bin/routes
exec /usr/local/lib/docktinel/entrypoint /init.sh`, "PROXY_PORT=8180", "SOCKS_PORT=8118", "PRIMARY_RESOLVER=127.0.0.1:9")
	state := f.runtime()
	f.mustExec("/healthcheck.sh")
	var pid int
	for _, task := range state.Tasks {
		if task.Name == "routes" && task.Process != nil {
			pid = task.Process.PID
		}
	}
	if pid == 0 {
		t.Fatal("routes helper was not supervised")
	}
	f.mustExec("kill", "-KILL", strconv.Itoa(pid))
	eventually(t, f.status, func(s sentinelStatus) bool {
		for _, task := range s.Tasks {
			if task.Name == "routes" && task.Process != nil && task.Process.PID != pid {
				return s.State == "healthy"
			}
		}
		return false
	})
	// Each listener is necessary; overriding either probe port must fail locally.
	for _, setting := range []string{"PROXY_PORT=9", "SOCKS_PORT=9"} {
		ctx, cancel := context.WithCancel(t.Context())
		_, code, err := f.exec(ctx, "env", setting, "/healthcheck.sh")
		cancel()
		if code == 0 || err == nil {
			t.Fatalf("missing listener passed: %s", setting)
		}
	}
	f.mustExec("docktinel", "healthcheck")
}

func TestDashboardStartupAndLocalHealth(t *testing.T) {
	f := newFixture(t, "dashboard", "", "DASHBOARD_USERNAME=admin", "DASHBOARD_PASSWORD=sentinel-test", "DASHBOARD_SERVICE_1=Missing:444:missing.invalid:80")
	f.runtime()
	f.mustExec("/healthcheck.sh")
	// Generated JSON is validated at startup; runtime only probes local HTTP.
	f.mustExec("sh", "-ec", `printf invalid > "$SERVER_ROOT/config.json"`)
	f.mustExec("/healthcheck.sh")
	f.mustExec("sh", "-ec", `rm "$SERVER_ROOT/config.json"`)
	f.waitExit()
}

func TestOpenVPNUIStartupAndLocalHealth(t *testing.T) {
	f := newFixture(t, "openvpn-ui", "", "OPENVPN_EXTERNAL_IP=192.0.2.10", "DOCKER_SUBNET=10.200.0.0/24")
	f.runtime()
	f.mustExec("/opt/healthcheck.sh")
	f.mustExec("sqlite3", "/opt/openvpn-ui/db/data.db", "PRAGMA integrity_check;")
	f.mustExec("docktinel", "healthcheck")
}

func TestFirewallPreservesRulesAndRecovers(t *testing.T) {
	// A separate holder keeps the isolated network namespace alive after firewall
	// shutdown, allowing us to verify cleanup without touching the host network.
	holder := fixtureWithHost(t, "firewall", "exec sleep 600", &container.HostConfig{
		CapAdd: []string{"NET_ADMIN"},
		Mounts: []mount.Mount{{Type: mount.TypeVolume, Target: "/var/lib/antizapret-firewall"}},
	})
	firewallHost := func() *container.HostConfig {
		return &container.HostConfig{
			CapAdd:      []string{"NET_ADMIN"},
			NetworkMode: container.NetworkMode("container:" + holder.id),
			PidMode:     container.PidMode("container:" + holder.id),
			VolumesFrom: []string{holder.id},
		}
	}
	f := fixtureWithHost(t, "firewall", `iptables -N DOCKER-USER
ip6tables -N DOCKER-USER
iptables -A DOCKER-USER -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
printf '192.0.2.0/24\n' > /root/blacklist-v4.txt
printf '2001:db8:1::/48\n' > /root/blacklist-v6.txt
printf '198.51.100.0/24\n' > /tmp/feed-v4
exec /root/entrypoint.sh`, firewallHost(), "V4_FILE=blacklist-v4.txt", "V6_FILE=blacklist-v6.txt", "V4_URL=file:///tmp/feed-v4", "V6_URL=file:///tmp/feed-v6", "INTERVAL=1s")
	f.runtime()
	f.waitState("degraded")
	f.mustExec("ipset", "test", "az_firewall_v4", "192.0.2.1")
	f.mustExec("grep", "192.0.2.0/24", "/root/blacklist-v4.txt")
	f.mustExec("docktinel", "healthcheck")
	for _, feed := range []string{" \t\n", "invalid"} {
		f.mustExec("sh", "-ec", `printf '%s' "$1" > /tmp/feed-v6
if /root/refresh.sh; then exit 1; fi`, "sh", feed)
		f.mustExec("ipset", "test", "az_firewall_v4", "192.0.2.1")
		f.mustExec("ipset", "test", "az_firewall_v6", "2001:db8:1::1")
	}
	f.mustExec("sh", "-ec", `printf '2001:db8:2::/48\n' > /tmp/feed-v6`)
	f.waitState("healthy")
	f.mustExec("ipset", "test", "az_firewall_v4", "198.51.100.1")
	f.mustExec("ipset", "test", "az_firewall_v6", "2001:db8:2::1")
	// Fail the second swap after IPv4 was applied: IPv4 must roll back as well.
	f.mustExec("sh", "-ec", `mv /usr/sbin/ipset /usr/sbin/ipset.real
cat > /usr/sbin/ipset <<'SH'
#!/bin/sh
if [ "$1" = swap ]; then
 case "$2" in az_firewall_v6_new) exit 1;; esac
fi
exec /usr/sbin/ipset.real "$@"
SH
chmod +x /usr/sbin/ipset
printf '203.0.113.0/24\n' > /tmp/feed-v4`)
	f.waitState("degraded")
	f.mustExec("ipset", "test", "az_firewall_v4", "198.51.100.1")
	f.mustExec("grep", "198.51.100.0/24", "/root/blacklist-v4.txt")
	f.mustExec("mv", "/usr/sbin/ipset.real", "/usr/sbin/ipset")
	f.waitState("healthy")
	f.mustExec("ipset", "test", "az_firewall_v4", "203.0.113.1")
	f.mustExec("docktinel", "healthcheck")
	replacement := fixtureWithHost(t, "firewall", "",
		firewallHost(), "V4_FILE=blacklist-v4.txt", "V6_FILE=blacklist-v6.txt",
		"V4_URL=file:///tmp/feed-v4", "V6_URL=file:///tmp/feed-v6")
	replacement.runtime()
	stop := func(f *fixture) {
		state := f.runtime()
		ctx, cancel := context.WithCancel(t.Context())
		defer cancel()
		_, _, _ = f.exec(ctx, "kill", "-TERM", strconv.Itoa(state.Main.PID))
		f.waitExit()
	}
	// The predecessor can neither take over nor clear the replacement's rules.
	f.mustExec("/root/refresh.sh", "--restore")
	f.mustExec("/root/refresh.sh", "--clear")
	replacement.mustExec("/root/block.sh", "check")
	replacement.mustExec("ipset", "test", "az_firewall_v4", "203.0.113.1")
	replacement.mustExec("ipset", "test", "az_firewall_v6", "2001:db8:2::1")
	replacement.mustExec("sh", "-ec", `mkdir -p /tmp/xtables-busy
printf '#!/bin/sh\nexit 4\n' > /tmp/xtables-busy/iptables
chmod +x /tmp/xtables-busy/iptables
if PATH=/tmp/xtables-busy:$PATH /root/refresh.sh --clear; then exit 1; fi
/root/block.sh check`)
	stop(replacement)
	f.mustExec("/root/refresh.sh", "--restore")
	for _, set := range []string{"az_firewall_v4", "az_firewall_v6"} {
		holder.mustExec("sh", "-ec", `if ipset list "$1"; then exit 1; fi`, "sh", set)
	}
	stop(f)
	holder.mustExec("iptables", "-C", "DOCKER-USER", "-m", "conntrack", "--ctstate", "ESTABLISHED,RELATED", "-j", "ACCEPT")
	holder.mustExec("sh", "-ec", `test -z "$(ip6tables -S DOCKER-USER | grep -- '-A DOCKER-USER')"`)

	recreated := fixtureWithHost(t, "firewall", "",
		firewallHost(), "V4_FILE=blacklist-v4.txt", "V6_FILE=blacklist-v6.txt",
		"V4_URL=file:///tmp/feed-v4", "V6_URL=file:///tmp/feed-v6", "INTERVAL=1s")
	recreated.runtime()
	recreated.waitState("degraded")
	recreated.mustExec("ipset", "test", "az_firewall_v4", "203.0.113.1")
	recreated.mustExec("ipset", "test", "az_firewall_v6", "2001:db8:2::1")
	recreated.mustExec("docktinel", "healthcheck")
}

func TestAntizapretZapretLoadsBundledRules(t *testing.T) {
	host := &container.HostConfig{NetworkMode: "none", CapAdd: []string{"NET_ADMIN"}}
	f := fixtureWithHost(t, "antizapret", "exec sleep infinity", host)
	f.mustExec("bash", "-ec", `
. /root/zapret2/config.default
exec /opt/zapret2/nfq2/nfqws2 --intercept=0 \
    --lua-init=@/opt/zapret2/lua/zapret-lib.lua \
    --lua-init=@/opt/zapret2/lua/zapret-antidpi.lua $NFQWS2_OPT
`)
}
