package integration

import (
	"bytes"
	"context"
	"strconv"
	"strings"
	"testing"
	"time"

	"github.com/moby/moby/api/pkg/stdcopy"
	"github.com/moby/moby/api/types/container"
	"github.com/moby/moby/client"
)

func TestCoreDNSMaintenanceFailureAndRecovery(t *testing.T) {
	f := newFixture(t, "coredns", "")
	f.runtime()
	original := f.mustExec("cat", "/Corefile")
	if !strings.Contains(original, "forward . 192.0.2.10 192.0.2.30") {
		t.Fatalf("aliases not deduplicated or wrong priority: %s", original)
	}
	// There are no working upstreams. That must not restart CoreDNS: it owns
	// its own upstream failover and AdGuard has an independent fallback.
	f.mustExec("curl", "-fsS", "http://127.0.0.1:8080/health")
	// Simulate all Docker DNS records disappearing. Preserve the last valid
	// config and expose the failed maintenance job as degraded, not fatal.
	f.mustExec("sh", "-c", "printf '#!/bin/sh\nexit 2\n' > /usr/local/bin/getent; chmod +x /usr/local/bin/getent")
	f.waitState("degraded")
	if got := f.mustExec("cat", "/Corefile"); got != original {
		t.Fatalf("lost working config during discovery failure: %s", got)
	}
	f.mustExec("docktinel", "healthcheck")
	// A changed address is picked up without restarting the main process.
	f.mustExec("sh", "-c", "printf '#!/bin/sh\nprintf \"192.0.2.40 host\\n\"\n' > /usr/local/bin/getent")
	eventually(t, func(ctx context.Context) (string, error) {
		out, _, err := f.exec(ctx, "cat", "/Corefile")
		return out, err
	}, func(s string) bool { return strings.Contains(s, "forward . 192.0.2.40 {") })
	f.waitState("healthy")
	f.mustExec("docktinel", "healthcheck")
}

func TestEntrypointWaitsForStartupAndChecksRuntime(t *testing.T) {
	f := newFixture(t, "coredns", `cat > /etc/docktinel/tasks.sh <<'SH'
docktinel add check dependency --phase both --deadline 30s --interval 100ms --timeout 1s --failures 2 -- test -f /tmp/dependency
SH
exec /usr/local/lib/docktinel/entrypoint sh -c 'touch /tmp/application-started; exec sleep infinity'`)
	eventually(t, f.status, func(s sentinelStatus) bool { return s.Phase == "startup" })
	f.mustExec("test", "!", "-e", "/tmp/application-started")
	f.mustExec("touch", "/tmp/dependency")
	f.runtime()
	eventually(t, func(ctx context.Context) (int, error) {
		_, code, err := f.exec(ctx, "test", "-e", "/tmp/application-started")
		return code, err
	}, func(code int) bool { return code == 0 })
	f.mustExec("rm", "/tmp/dependency")
	f.waitExit()
}

func TestContainerExitsAfterDaemonDies(t *testing.T) {
	f := newFixture(t, "coredns", `echo ':' > /etc/docktinel/tasks.sh
exec /usr/local/lib/docktinel/entrypoint sleep infinity`)
	state := f.runtime()
	ctx, cancel := context.WithTimeout(t.Context(), 5*time.Second)
	defer cancel()
	// Docker can close the exec stream before reporting the signal command's result.
	_, _, _ = f.exec(ctx, "kill", "-KILL", strconv.Itoa(state.Daemon.PID))
	if code := f.waitExit(); code != 137 {
		t.Fatalf("container exit code = %d, want 137", code)
	}
}

func TestAdGuardReadinessWithUpstreamUnavailable(t *testing.T) {
	requireService(t, "adguard")
	// Swarm publishes the service name after healthcheck succeeds. Keep the
	// name absent and the DNS upstream unreachable while readiness runs.
	f := startContainer(t, &container.Config{
		Image: serviceImage("adguard"),
		Env:   []string{"DOCKTINEL_CONFIG=/tmp/test-config.json"},
		Entrypoint: []string{"bash", "-ec", `printf '%s' '{"tasks":[]}' > /tmp/test-config.json
mkdir -p /opt/adguardhome/conf /opt/adguardhome/work
yq '.filters = [] | .whitelist_filters = [] | .users = [] |
    .dns.upstream_dns = ["127.0.0.1:9"] | .dns.fallback_dns = [] |
    .dns.upstream_timeout = "1s"' /root/AdGuardHome.yaml > /opt/adguardhome/conf/AdGuardHome.yaml
printf '#!/bin/sh\nexec sleep 600\n' > /usr/bin/routes
chmod +x /usr/bin/routes
touch /dev/shm/.inited
exec /usr/local/lib/docktinel/entrypoint sh -ec '
    until test -f /tmp/start-dns; do sleep 0.1; done
    exec /opt/adguardhome/AdGuardHome --no-check-update -c /opt/adguardhome/conf/AdGuardHome.yaml -w /opt/adguardhome/work
'`},
	}, &container.HostConfig{Init: new(true), NetworkMode: "none"})

	eventually(t, f.status, func(s sentinelStatus) bool {
		return s.Phase == "after-start" && s.State == "starting"
	})
	f.mustExec("sh", "-ec", "! timeout 2 getent hosts adguard.antizapret")
	f.mustExec("touch", "/tmp/start-dns")
	f.runtime()

	ctx, cancel := context.WithTimeout(t.Context(), 5*time.Second)
	defer cancel()
	reply, code, err := f.exec(ctx, "busybox", "nslookup", "-type=a", "-timeout=3", "-retry=1", "example.org", "127.0.0.1")
	if code == 0 || !strings.Contains(reply, "SERVFAIL") {
		t.Fatalf("expected SERVFAIL from the unavailable upstream, got exit %d: %s (%v)", code, reply, err)
	}
	f.mustExec("docktinel", "healthcheck")
}

func TestAdGuardReinitializesDNSWithoutRestartingOnMaintenanceFailure(t *testing.T) {
	// Avoid public DNS/filter dependencies: a local rewrite exercises the real
	// resolver while unreachable exit HTTP endpoints make maintenance fail.
	f := newFixture(t, "adguard", `mkdir -p /opt/adguardhome/conf /opt/adguardhome/work
yq '.filters = [] | .users = [] | .dns.upstream_dns = ["127.0.0.1:9"] | .dns.fallback_dns = [] | .filtering.rewrites = [{"domain":"example.org", "answer":"192.0.2.1", "enabled":true}]' /root/AdGuardHome.yaml > /opt/adguardhome/conf/AdGuardHome.yaml
printf '#!/bin/sh\nexec sleep 600\n' > /usr/bin/routes
chmod +x /usr/bin/routes
touch /dev/shm/.inited
exec /usr/local/lib/docktinel/entrypoint /opt/adguardhome/AdGuardHome --no-check-update -c /opt/adguardhome/conf/AdGuardHome.yaml -w /opt/adguardhome/work`)
	f.runtime()
	eventually(t, func(ctx context.Context) (string, error) {
		out, _, err := f.exec(ctx, "cat", "/dev/shm/.dns_dependencies")
		return out, err
	}, func(s string) bool { return strings.Contains(s, `"coredns":`) })
	f.waitState("degraded")
	f.mustExec("docktinel", "healthcheck")
	f.mustExec("sh", "-c", `cat > /usr/local/bin/getent <<'SH'
#!/bin/sh
case "$2" in
    coredns.antizapret) echo '192.0.2.20 host' ;;
    *) echo '192.0.2.40 host' ;;
esac
SH
chmod +x /usr/local/bin/getent`)

	eventually(t, func(ctx context.Context) (string, error) {
		out, _, err := f.exec(ctx, "cat", "/dev/shm/.dns_dependencies")
		return out, err
	}, func(s string) bool { return strings.Contains(s, `"local":"192.0.2.40"`) })
	if reply := f.mustExec("busybox", "nslookup", "-type=a", "-timeout=12", "example.org", "127.0.0.1"); !strings.Contains(reply, "192.0.2.1") {
		t.Fatalf("resolver stopped answering after DNS reinitialization: %s", reply)
	}
	f.mustExec("docktinel", "healthcheck")
}

func TestAntizapretTaskRegistration(t *testing.T) {
	f := newFixture(t, "antizapret", `. /usr/local/lib/docktinel/tasks.sh
. /etc/docktinel/tasks.sh
docktinel config validate`)
	if code := f.waitExit(); code != 0 {
		t.Fatalf("task registration exited %d", code)
	}
}

func TestFileBrowserReadiness(t *testing.T) {
	f := newFixture(t, "filebrowser", "", "FILEBROWSER_PASSWORD=Resilience-test-only-827364")
	f.runtime()
	f.mustExec("wget", "-q", "--spider", "http://127.0.0.1:80/health")
	f.mustExec("docktinel", "healthcheck")
}

func TestAntizapretShutdownTasks(t *testing.T) {
	f := newFixture(t, "antizapret", `mkdir -p /root/antizapret/result /root/antizapret/iptables
touch /dev/shm/.zapret_started
echo 'export ZAPRET_CONFIG=/tmp/active-zapret.conf' > /etc/default/antizapret
cat > /opt/zapret2/init.d/sysv/zapret2 <<'SH'
#!/bin/sh
test "$1" = stop && test "$ZAPRET_CONFIG" = /tmp/active-zapret.conf && touch /tmp/zapret-stopped
SH
cat > /usr/local/bin/iptables-save <<'SH'
#!/bin/sh
printf '%s\n' '-A dnsmap -j ACCEPT' '-A OUTPUT -j ACCEPT'
SH
chmod +x /opt/zapret2/init.d/sysv/zapret2 /usr/local/bin/iptables-save
. /usr/local/lib/docktinel/tasks.sh
. /etc/docktinel/tasks.sh
python3 -c 'import json, sys; p = sys.argv[1]; d = json.load(open(p)); d["tasks"] = [t for t in d["tasks"] if t["phase"] == "shutdown"]; json.dump(d, open(p, "w"))' "$DOCKTINEL_CONFIG"
docktinel add job verify-cleanup --phase shutdown -- sh -ec '
    test -f /tmp/zapret-stopped
    test "$(cat "/root/antizapret/iptables/$(hostname -s).rules")" = "-A dnsmap -j ACCEPT"
    echo cleanup-verified
'
exec docktinel daemon`)
	f.runtime()
	f.stop()

	ctx, cancel := context.WithTimeout(t.Context(), 5*time.Second)
	defer cancel()
	logs, err := f.cli.ContainerLogs(ctx, f.id, client.ContainerLogsOptions{ShowStdout: true})
	if err != nil {
		t.Fatal(err)
	}
	defer logs.Close()
	var output bytes.Buffer
	if _, err := stdcopy.StdCopy(&output, &output, logs); err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(output.String(), "cleanup-verified") {
		t.Fatalf("shutdown did not complete all cleanup steps: %s", &output)
	}
}

func TestVPNStartupAndHealthDoNotDependOnAdGuard(t *testing.T) {
	for _, service := range []string{"wireguard", "openvpn", "ocserv"} {
		t.Run(service, func(t *testing.T) {
			// Use the actual VPN registrations with stand-ins for application
			// health and route management. Any DNS probe fails and leaves a
			// marker, so both startup gates and runtime probes are detected.
			f := newFixture(t, service, `mkdir -p /opt/app
printf '#!/bin/sh\ntouch /tmp/dns-probed\nexit 1\n' > /usr/local/lib/docktinel/check-dns
printf '#!/bin/sh\nexec sleep infinity\n' > /usr/bin/routes
printf '#!/bin/sh\nexit 0\n' > /healthcheck.sh
cp /healthcheck.sh /opt/app/healthcheck.sh
chmod +x /usr/local/lib/docktinel/check-dns /usr/bin/routes /healthcheck.sh /opt/app/healthcheck.sh
exec /usr/local/lib/docktinel/entrypoint sh -c 'touch /dev/shm/.wireguard_started /dev/shm/.inited /dev/shm/.ocserv_started; exec sleep infinity'
`)
			f.runtime()
			f.mustExec("test", "!", "-e", "/tmp/dns-probed")
			if status := f.mustExec("docktinel", "status", "--json"); strings.Contains(status, `"adguard-dns"`) {
				t.Fatal("VPN still registers an AdGuard dependency")
			}
			f.mustExec("docktinel", "healthcheck")
		})
	}
}
