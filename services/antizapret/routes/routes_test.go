package main

import (
	"net"
	"reflect"
	"testing"

	"github.com/vishvananda/netlink"
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

func TestUpdateAddressesUsesDefaultRouteForSelectedVPNHost(t *testing.T) {
	routes := captureRouteReplace(t, func() {
		(&app{
			vpn:           true,
			defaultRoute:  "10.200.0.2",
			routes:        []routeSpec{{host: "10.200.0.2", subnet: "14.16.0.0/15"}},
			routeGateways: map[string]string{},
		}).updateAddresses()
	})

	if len(routes) != 1 {
		t.Fatalf("routes = %#v, want 1 route", routes)
	}
	if routes[0].Dst != nil {
		t.Fatalf("routes[0].Dst = %v, want nil default route", routes[0].Dst)
	}
	if !routes[0].Gw.Equal(net.ParseIP("10.200.0.2")) {
		t.Fatalf("routes[0].Gw = %v, want 10.200.0.2", routes[0].Gw)
	}
}

func TestApplyVPNRoutesSkipsSelectedDefaultRouteHost(t *testing.T) {
	routes := captureRouteReplace(t, func() {
		err := (&app{
			vpn:           true,
			defaultRoute:  "az-local",
			routeGateways: map[string]string{},
		}).applyVPNRoutes("az-local", "10.200.0.2")
		if err != nil {
			t.Fatalf("applyVPNRoutes() error = %v", err)
		}
	})

	if len(routes) != 0 {
		t.Fatalf("routes = %#v, want none", routes)
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

func captureRouteReplace(t *testing.T, fn func()) []netlink.Route {
	t.Helper()

	original := routeReplace
	t.Cleanup(func() {
		routeReplace = original
	})

	var routes []netlink.Route
	routeReplace = func(route *netlink.Route) error {
		routes = append(routes, *route)
		return nil
	}

	fn()
	return routes
}
