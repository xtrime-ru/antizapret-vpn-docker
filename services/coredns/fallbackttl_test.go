package fallbackttl

import (
	"context"
	"fmt"
	"net"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"sync/atomic"
	"syscall"
	"testing"
	"time"

	"github.com/miekg/dns"
)

const (
	testName    = "example.com."
	upstreamTTL = 1800
	dropQuery   = -1
)

type testUpstream struct {
	address string
	rcode   atomic.Int32
	queries atomic.Int32
	probes  atomic.Int32
}

func requireNoError(t *testing.T, err error) {
	t.Helper()
	if err != nil {
		t.Fatal(err)
	}
}

func startUpstream(t *testing.T, answer string) *testUpstream {
	t.Helper()
	conn, err := net.ListenPacket("udp4", "127.0.0.1:0")
	requireNoError(t, err)
	upstream := &testUpstream{address: conn.LocalAddr().String()}
	server := &dns.Server{
		PacketConn: conn,
		Handler: dns.HandlerFunc(func(w dns.ResponseWriter, request *dns.Msg) {
			q := request.Question[0]
			switch q.Qtype {
			case dns.TypeA:
				upstream.queries.Add(1)
			case dns.TypeNS:
				upstream.probes.Add(1)
			}
			rcode := int(upstream.rcode.Load())
			if rcode == dropQuery {
				return
			}
			reply := new(dns.Msg).SetRcode(request, rcode)
			reply.RecursionAvailable = true
			if rcode == dns.RcodeSuccess && q.Qtype == dns.TypeA {
				reply.Answer = []dns.RR{&dns.A{
					Hdr: dns.RR_Header{Name: q.Name, Rrtype: dns.TypeA, Class: q.Qclass, Ttl: upstreamTTL},
					A:   net.ParseIP(answer),
				}}
			}
			if opt := request.IsEdns0(); opt != nil {
				reply.Extra = append(reply.Extra, opt)
			}
			_ = w.WriteMsg(reply)
		}),
	}
	started := make(chan struct{})
	server.NotifyStartedFunc = func() { close(started) }
	done := make(chan error, 1)
	go func() { done <- server.ActivateAndServe() }()
	select {
	case <-started:
	case err := <-done:
		_ = conn.Close()
		t.Fatalf("start DNS upstream: %v", err)
	}
	t.Cleanup(func() {
		ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
		defer cancel()
		if err := server.ShutdownContext(ctx); err != nil {
			t.Errorf("stop DNS upstream: %v", err)
		}
		if err := <-done; err != nil {
			t.Errorf("serve DNS upstream: %v", err)
		}
	})
	return upstream
}

type testAnswer struct {
	address string
	ttl     uint32
	rcode   int
}

func TestFailoverAndRecovery(t *testing.T) {
	binary := os.Getenv("COREDNS")
	if binary == "" {
		t.Fatal("COREDNS must point to the built binary")
	}
	template, err := os.ReadFile("Corefile.template")
	requireNoError(t, err)
	world := startUpstream(t, "192.0.2.1")
	local := startUpstream(t, "192.0.2.2")
	adguard := startUpstream(t, "192.0.2.3")
	destinations := []string{world.address, local.address, adguard.address}

	reservation, err := net.ListenPacket("udp4", "127.0.0.1:0")
	requireNoError(t, err)
	address := reservation.LocalAddr().String()
	port := strconv.Itoa(reservation.LocalAddr().(*net.UDPAddr).Port)
	requireNoError(t, reservation.Close())
	corefile := filepath.Join(t.TempDir(), "Corefile")
	render := func(exits int, hosts []string, fallback, failover string) []byte {
		return []byte(strings.NewReplacer(
			".:53 {", ".:"+port+" {\n    bind 127.0.0.1",
			"    health :8080\n", "",
			"    log\n", "",
			"prefer_udp", "prefer_udp\n            read_timeout 200ms",
			"${AZ_EXPECTED_EXITS}", strconv.Itoa(exits),
			"${AZ_FALLBACK_HOST}", fallback,
			"${AZ_FORWARD_HOSTS}", strings.Join(hosts, " "),
			"failover SERVFAIL REFUSED", failover,
		).Replace(string(template)))
	}
	writeConfig := func(exits int, hosts []string, fallback, failover string) {
		t.Helper()
		requireNoError(t, os.WriteFile(corefile+".tmp", render(exits, hosts, fallback, failover), 0o600))
		requireNoError(t, os.Rename(corefile+".tmp", corefile))
	}

	writeConfig(2, destinations, adguard.address, "failover SERVFAIL REFUSED")
	process := exec.CommandContext(t.Context(), binary, "-conf", corefile)
	process.Cancel = func() error { return process.Process.Signal(syscall.SIGTERM) }
	process.WaitDelay = 3 * time.Second
	process.Stdout, process.Stderr = os.Stdout, os.Stderr
	requireNoError(t, process.Start())
	exited := make(chan struct{})
	go func() {
		_ = process.Wait()
		close(exited)
	}()
	t.Cleanup(func() { <-exited })

	query := func() (testAnswer, error) {
		request := new(dns.Msg).SetQuestion(testName, dns.TypeA).SetEdns0(1232, true)
		reply, _, err := (&dns.Client{Timeout: 2 * time.Second}).Exchange(request, address)
		if err != nil {
			return testAnswer{}, err
		}
		answer := testAnswer{rcode: reply.Rcode}
		if len(reply.Answer) == 0 {
			return answer, nil
		}
		record, ok := reply.Answer[0].(*dns.A)
		if !ok || len(reply.Answer) != 1 {
			return answer, fmt.Errorf("expected one A record, got %v", reply.Answer)
		}
		if opt := reply.IsEdns0(); opt == nil || opt.Hdr.Ttl != request.IsEdns0().Hdr.Ttl {
			return answer, fmt.Errorf("EDNS flags changed: %v", opt)
		}
		answer.address, answer.ttl = record.A.String(), record.Hdr.Ttl
		return answer, nil
	}
	check := func(want testAnswer, retry bool) {
		t.Helper()
		deadline := time.Now().Add(8 * time.Second)
		for {
			select {
			case <-exited:
				t.Fatalf("CoreDNS exited before answering: %v", process.ProcessState)
			default:
			}
			got, err := query()
			if err == nil && got == want {
				return
			}
			if !retry || time.Now().After(deadline) {
				t.Fatalf("DNS response = %+v, %v, want %+v", got, err, want)
			}
			time.Sleep(50 * time.Millisecond)
		}
	}
	expect := func(want testAnswer) { t.Helper(); check(want, false) }
	eventually := func(want testAnswer) { t.Helper(); check(want, true) }

	worldAnswer := testAnswer{address: "192.0.2.1", ttl: upstreamTTL}
	localAnswer := testAnswer{address: "192.0.2.2", ttl: upstreamTTL}
	fallback := testAnswer{address: "192.0.2.3", ttl: upstreamTTL}
	uncached := testAnswer{address: fallback.address}
	refused := testAnswer{rcode: dns.RcodeRefused}
	eventually(worldAnswer)

	local.rcode.Store(dns.RcodeServerFailure)
	world.rcode.Store(dropQuery)
	before := world.queries.Load()
	expect(uncached)
	if got := world.queries.Load(); got != before+1 {
		t.Fatalf("initial request made %d attempts to the timing-out upstream, want 1", got-before)
	}
	deadline := time.Now().Add(6 * time.Second)
	// The third probe starts after two previous probes have timed out.
	for world.probes.Load() < 3 && time.Now().Before(deadline) {
		time.Sleep(50 * time.Millisecond)
	}
	if got := world.probes.Load(); got < 3 {
		t.Fatalf("health probes = %d, want at least 3", got)
	}
	before = world.queries.Load()
	expect(uncached)
	local.rcode.Store(dns.RcodeSuccess)
	expect(localAnswer)
	if got := world.queries.Load(); got != before {
		t.Fatalf("CoreDNS queried an unhealthy upstream %d times", got-before)
	}

	local.rcode.Store(dns.RcodeServerFailure)
	world.rcode.Store(dns.RcodeRefused)
	expect(uncached)
	local.rcode.Store(dns.RcodeRefused)
	eventually(fallback)
	local.rcode.Store(dns.RcodeServerFailure)
	expect(uncached)
	local.rcode.Store(dns.RcodeSuccess)
	expect(localAnswer)
	world.rcode.Store(dns.RcodeSuccess)
	expect(worldAnswer)

	writeConfig(2, destinations[1:], adguard.address, "failover SERVFAIL REFUSED")
	eventually(localAnswer)
	local.rcode.Store(dns.RcodeRefused)
	expect(uncached)
	writeConfig(1, destinations[1:], adguard.address, "failover SERVFAIL REFUSED")
	eventually(fallback)
	writeConfig(2, destinations[2:], adguard.address, "failover SERVFAIL REFUSED")
	eventually(uncached)
	writeConfig(2, destinations[:2], "", "failover SERVFAIL REFUSED")
	eventually(worldAnswer)

	writeConfig(2, destinations[:2], "", "failover SERVFAIL")
	world.rcode.Store(dns.RcodeRefused)
	eventually(refused)
	local.rcode.Store(dns.RcodeSuccess)
	writeConfig(2, destinations[:2], "", "failover REFUSED")
	eventually(localAnswer)
	local.rcode.Store(dns.RcodeRefused)
	expect(refused)

	invalid := corefile + ".invalid"
	requireNoError(t, os.WriteFile(invalid, render(2, destinations[:2], "", "next REFUSED"), 0o600))
	ctx, cancel := context.WithTimeout(t.Context(), 5*time.Second)
	defer cancel()
	output, err := exec.CommandContext(ctx, binary, "-conf", invalid).CombinedOutput()
	if err == nil || !strings.Contains(string(output), "next would bypass fallbackttl") {
		t.Fatalf("expected invalid next directive error, got %v: %s", err, output)
	}
}
