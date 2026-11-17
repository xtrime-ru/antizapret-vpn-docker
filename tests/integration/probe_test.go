package integration

import (
	"net"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"testing"
	"time"
)

func runDNSProbe(t *testing.T, rcode byte, pause time.Duration, reply bool) error {
	t.Helper()
	if _, err := exec.LookPath("busybox"); err != nil {
		t.Skip("BusyBox required; run DNS probe tests in Linux with BusyBox installed")
	}
	server, err := net.ListenPacket("udp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer server.Close()
	go func() {
		packet := make([]byte, 4096)
		for {
			n, peer, err := server.ReadFrom(packet)
			if err != nil {
				return
			}
			if !reply {
				continue
			}
			if pause > 0 {
				timer := time.NewTimer(pause)
				select {
				case <-timer.C:
				case <-t.Context().Done():
					timer.Stop()
					return
				}
			}
			packet[2], packet[3] = 0x81, 0x80|rcode
			if _, err := server.WriteTo(packet[:n], peer); err != nil {
				return
			}
		}
	}()
	probe, err := filepath.Abs("../../services/adguard/check-dns")
	if err != nil {
		t.Fatal(err)
	}
	cmd := exec.CommandContext(t.Context(), "sh", probe)
	cmd.Env = append(os.Environ(), "DNS_PROBE_PORT="+strconv.Itoa(server.LocalAddr().(*net.UDPAddr).Port))
	output, err := cmd.CombinedOutput()
	if err != nil {
		t.Logf("DNS probe: %s", output)
	}
	return err
}

func TestDNSProbeRequiresSuccessfulResponse(t *testing.T) {
	for _, tc := range []struct {
		name  string
		rcode byte
	}{{"NOERROR", 0}, {"SERVFAIL", 2}, {"REFUSED", 5}, {"NXDOMAIN", 3}} {
		t.Run(tc.name, func(t *testing.T) {
			err := runDNSProbe(t, tc.rcode, 0, true)
			if (err == nil) != (tc.rcode == 0) {
				t.Fatalf("status %s: %v", tc.name, err)
			}
		})
	}
}

func TestDNSProbeAllowsDelayedFallbackResponse(t *testing.T) {
	// Upstream timeout is 5s; a fallback response after that must still pass.
	if err := runDNSProbe(t, 0, 5500*time.Millisecond, true); err != nil {
		t.Fatalf("delayed fallback rejected: %v", err)
	}
}

func TestDNSProbeTimeoutFails(t *testing.T) {
	if err := runDNSProbe(t, 0, 0, false); err == nil {
		t.Fatal("unresponsive DNS passed")
	}
}
