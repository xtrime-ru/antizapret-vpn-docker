package main

import (
	"reflect"
	"testing"
)

func TestEnableDNSRedirectSkipsWhenVPNDisabled(t *testing.T) {
	commands := captureIPTablesCommands(t, func() {
		(&app{
			vpn:    false,
			routes: []routeSpec{{host: "adguard", subnet: "14.16.0.1"}},
		}).enableDNSRedirect()
	})

	if len(commands) != 0 {
		t.Fatalf("iptables commands = %v, want none", commands)
	}
}

func TestEnableDNSRedirectSkipsNonAdguardRoutes(t *testing.T) {
	commands := captureIPTablesCommands(t, func() {
		(&app{
			vpn: true,
			routes: []routeSpec{
				{host: "az-local", subnet: "14.16.0.0/15"},
				{host: "az-world", subnet: "0.0.0.0/0"},
			},
		}).enableDNSRedirect()
	})

	if len(commands) != 0 {
		t.Fatalf("iptables commands = %v, want none", commands)
	}
}

func TestEnableDNSRedirectAddsDNATAndLocalMasqueradeRules(t *testing.T) {
	commands := captureIPTablesCommands(t, func() {
		(&app{
			vpn:    true,
			routes: []routeSpec{{host: "adguard", subnet: "14.16.0.1"}},
		}).enableDNSRedirect()
	})

	want := [][]string{
		{"-t", "nat", "-A", "PREROUTING", "-p", "tcp", "--dport", "53", "-j", "DNAT", "--to-destination", "14.16.0.1"},
		{"-t", "nat", "-A", "OUTPUT", "-p", "tcp", "--dport", "53", "-j", "DNAT", "--to-destination", "14.16.0.1"},
		{"-t", "nat", "-A", "PREROUTING", "-p", "udp", "--dport", "53", "-j", "DNAT", "--to-destination", "14.16.0.1"},
		{"-t", "nat", "-A", "OUTPUT", "-p", "udp", "--dport", "53", "-j", "DNAT", "--to-destination", "14.16.0.1"},
		{"-t", "nat", "-A", "POSTROUTING", "-m", "addrtype", "--src-type", "LOCAL", "-p", "tcp", "-d", "14.16.0.1", "--dport", "53", "-j", "MASQUERADE"},
		{"-t", "nat", "-A", "POSTROUTING", "-m", "addrtype", "--src-type", "LOCAL", "-p", "udp", "-d", "14.16.0.1", "--dport", "53", "-j", "MASQUERADE"},
	}

	if !reflect.DeepEqual(commands, want) {
		t.Fatalf("iptables commands = %#v, want %#v", commands, want)
	}

}

func captureIPTablesCommands(t *testing.T, fn func()) [][]string {
	t.Helper()

	original := iptablesRun
	t.Cleanup(func() {
		iptablesRun = original
	})

	var commands [][]string
	iptablesRun = func(args ...string) error {
		commands = append(commands, append([]string(nil), args...))
		return nil
	}

	fn()
	return commands
}
