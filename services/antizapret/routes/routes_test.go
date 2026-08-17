package main

import (
	"context"
	"errors"
	"net"
	"os"
	"path/filepath"
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
		{"-t", "nat", "-A", "OUTPUT", "!", "-d", "127.0.0.0/8", "-p", "tcp", "--dport", "53", "-j", "DNAT", "--to-destination", "14.16.0.1"},
		{"-t", "nat", "-A", "PREROUTING", "-p", "udp", "--dport", "53", "-j", "DNAT", "--to-destination", "14.16.0.1"},
		{"-t", "nat", "-A", "OUTPUT", "!", "-d", "127.0.0.0/8", "-p", "udp", "--dport", "53", "-j", "DNAT", "--to-destination", "14.16.0.1"},
		{"-t", "nat", "-A", "POSTROUTING", "-m", "addrtype", "--src-type", "LOCAL", "-p", "tcp", "-d", "14.16.0.1", "--dport", "53", "-j", "MASQUERADE"},
		{"-t", "nat", "-A", "POSTROUTING", "-m", "addrtype", "--src-type", "LOCAL", "-p", "udp", "-d", "14.16.0.1", "--dport", "53", "-j", "MASQUERADE"},
	}

	if !reflect.DeepEqual(commands, want) {
		t.Fatalf("iptables commands = %#v, want %#v", commands, want)
	}

}

func TestUpdateRoutesKeepsConfiguredRouteInMainTable(t *testing.T) {
	routes := captureRouteReplace(t, func() {
		(&app{
			vpn:           true,
			defaultRoute:  "10.200.0.2",
			routes:        []routeSpec{{host: "10.200.0.2", subnet: "14.16.0.0/15"}},
			routeGateways: map[string]string{},
		}).updateRoutes()
	})

	if len(routes) != 1 {
		t.Fatalf("routes = %#v, want 1 route", routes)
	}
	if routes[0].Dst == nil || routes[0].Dst.String() != "14.16.0.0/15" {
		t.Fatalf("routes[0].Dst = %v, want 14.16.0.0/15", routes[0].Dst)
	}
	if routes[0].Table != 0 {
		t.Fatalf("routes[0].Table = %v, want main table", routes[0].Table)
	}
}

func TestUpdateRoutesAddsAzRouteToMainAndClientTraffic(t *testing.T) {
	restoreLookup := stubLookupIP(t, map[string]string{"az-local": "10.200.0.2"})
	defer restoreLookup()

	routes := captureRouteReplace(t, func() {
		(&app{
			vpn:           true,
			defaultRoute:  "az-local",
			routes:        []routeSpec{{host: "az-local", subnet: "14.16.0.0/15"}},
			routeGateways: map[string]string{},
		}).updateRoutes()
	})

	if len(routes) != 2 {
		t.Fatalf("routes = %#v, want 2 routes", routes)
	}
	if routes[0].Dst == nil || routes[0].Dst.String() != "14.16.0.0/15" {
		t.Fatalf("routes[0].Dst = %v, want 14.16.0.0/15", routes[0].Dst)
	}
	if routes[0].Table != 0 {
		t.Fatalf("routes[0].Table = %v, want main table", routes[0].Table)
	}
	if routes[1].Dst != nil {
		t.Fatalf("routes[1].Dst = %v, want nil default route", routes[1].Dst)
	}
	if routes[1].Table != vpnRouteTable {
		t.Fatalf("routes[1].Table = %v, want %v", routes[1].Table, vpnRouteTable)
	}
}

func TestUpdateRoutesAddsAdguardRouteToMainAndVPNPolicyTable(t *testing.T) {
	restoreLookup := stubLookupIP(t, map[string]string{"adguard": "10.200.0.6"})
	defer restoreLookup()

	routes := captureRouteReplace(t, func() {
		(&app{
			vpn:           true,
			defaultRoute:  "az-local",
			routes:        []routeSpec{{host: "adguard", subnet: "14.16.0.1"}},
			routeGateways: map[string]string{},
			vpnGateways:   map[string]string{},
		}).updateRoutes()
	})

	if len(routes) != 2 {
		t.Fatalf("routes = %#v, want 2 routes", routes)
	}
	if routes[0].Dst == nil || routes[0].Dst.String() != "14.16.0.1/32" {
		t.Fatalf("routes[0].Dst = %v, want 14.16.0.1/32", routes[0].Dst)
	}
	if routes[0].Table != 0 {
		t.Fatalf("routes[0].Table = %v, want main table", routes[0].Table)
	}
	if routes[1].Dst == nil || routes[1].Dst.String() != "14.16.0.1/32" {
		t.Fatalf("routes[1].Dst = %v, want 14.16.0.1/32", routes[1].Dst)
	}
	if routes[1].Table != vpnRouteTable {
		t.Fatalf("routes[1].Table = %v, want %v", routes[1].Table, vpnRouteTable)
	}
}

func TestUpdateRoutesAppliesVPNRouteWhenMainRouteUnchanged(t *testing.T) {
	restoreLookup := stubLookupIP(t, map[string]string{"az-local": "10.200.0.2"})
	defer restoreLookup()

	routes := captureRouteReplace(t, func() {
		(&app{
			vpn:           true,
			defaultRoute:  "az-local",
			routes:        []routeSpec{{host: "az-local", subnet: "14.16.0.0/15"}},
			routeGateways: map[string]string{"az-local": "10.200.0.2"},
			vpnGateways:   map[string]string{},
		}).updateRoutes()
	})

	if len(routes) != 1 {
		t.Fatalf("routes = %#v, want 1 VPN route", routes)
	}
	if routes[0].Dst != nil {
		t.Fatalf("routes[0].Dst = %v, want nil default route", routes[0].Dst)
	}
	if routes[0].Table != vpnRouteTable {
		t.Fatalf("routes[0].Table = %v, want %v", routes[0].Table, vpnRouteTable)
	}
}

func TestUpdateRoutesSkipsVPNRouteWhenVPNRouteUnchanged(t *testing.T) {
	restoreLookup := stubLookupIP(t, map[string]string{"az-local": "10.200.0.2"})
	defer restoreLookup()

	routes := captureRouteReplace(t, func() {
		(&app{
			vpn:           true,
			defaultRoute:  "az-local",
			routes:        []routeSpec{{host: "az-local", subnet: "14.16.0.0/15"}},
			routeGateways: map[string]string{"az-local": "10.200.0.2"},
			vpnGateways:   map[string]string{"az-local": "10.200.0.2"},
		}).updateRoutes()
	})

	if len(routes) != 0 {
		t.Fatalf("routes = %#v, want none", routes)
	}
}

func TestUpdateRoutesSkipsAliasResolvingToLocalAddress(t *testing.T) {
	restoreLookup := stubLookupIP(t, map[string]string{
		"az-local": "10.200.0.2",
		"az-world": "10.200.0.2",
	})
	defer restoreLookup()

	routes := captureRouteReplace(t, func() {
		(&app{
			self:          "az-local",
			routes:        []routeSpec{{host: "az-world", subnet: "14.18.0.0/15"}},
			routeGateways: map[string]string{},
		}).updateRoutes()
	})

	if len(routes) != 0 {
		t.Fatalf("routes = %#v, want none", routes)
	}
}

func TestUpdateRoutesKeepsAliasResolvingToRemoteAddress(t *testing.T) {
	restoreLookup := stubLookupIP(t, map[string]string{
		"az-local": "10.200.0.2",
		"az-world": "10.200.0.3",
	})
	defer restoreLookup()

	routes := captureRouteReplace(t, func() {
		(&app{
			self:          "az-local",
			routes:        []routeSpec{{host: "az-world", subnet: "14.18.0.0/15"}},
			routeGateways: map[string]string{},
		}).updateRoutes()
	})

	if len(routes) != 1 {
		t.Fatalf("routes = %#v, want one", routes)
	}
}

func TestUpdateRoutesChecksNonDefaultMainRoutesBeforeVPNPolicyTable(t *testing.T) {
	var routes []netlink.Route
	rules := captureRuleAdd(t, func() {
		routes = captureRouteReplace(t, func() {
			(&app{
				self:          "wireguard",
				vpn:           true,
				defaultRoute:  "az-local",
				routes:        []routeSpec{{host: "wireguard", subnet: "10.1.166.0/24"}},
				routeGateways: map[string]string{},
			}).updateRoutes()
		})
	})

	if len(routes) != 0 {
		t.Fatalf("routes = %#v, want none", routes)
	}
	if len(rules) != 2 {
		t.Fatalf("rules = %#v, want 2 rules", rules)
	}
	if rules[0].Src == nil || rules[0].Src.String() != "10.1.166.0/24" {
		t.Fatalf("rules[0].Src = %v, want 10.1.166.0/24", rules[0].Src)
	}
	if rules[0].Dst != nil {
		t.Fatalf("rules[0].Dst = %v, want any destination", rules[0].Dst)
	}
	if rules[0].Table != mainRouteTable || rules[0].Priority != vpnLocalPriority {
		t.Fatalf("rules[0] table/priority = %v/%v, want %v/%v", rules[0].Table, rules[0].Priority, mainRouteTable, vpnLocalPriority)
	}
	if rules[0].SuppressPrefixlen != 0 {
		t.Fatalf("rules[0].SuppressPrefixlen = %v, want 0", rules[0].SuppressPrefixlen)
	}
	if rules[1].Src == nil || rules[1].Src.String() != "10.1.166.0/24" {
		t.Fatalf("rules[1].Src = %v, want 10.1.166.0/24", rules[1].Src)
	}
	if rules[1].Dst != nil {
		t.Fatalf("rules[1].Dst = %v, want any destination", rules[1].Dst)
	}
	if rules[1].Table != vpnRouteTable || rules[1].Priority != vpnRulePriority {
		t.Fatalf("rules[1] table/priority = %v/%v, want %v/%v", rules[1].Table, rules[1].Priority, vpnRouteTable, vpnRulePriority)
	}
}

func TestApplyVPNRoutesAddsDefaultForSelectedDefaultRouteHost(t *testing.T) {
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

	if len(routes) != 1 {
		t.Fatalf("routes = %#v, want 1 route", routes)
	}
	if routes[0].Dst != nil {
		t.Fatalf("routes[0].Dst = %v, want nil default route", routes[0].Dst)
	}
	if routes[0].Table != vpnRouteTable {
		t.Fatalf("routes[0].Table = %v, want %v", routes[0].Table, vpnRouteTable)
	}
	if routes[0].LinkIndex != 42 {
		t.Fatalf("routes[0].LinkIndex = %v, want 42", routes[0].LinkIndex)
	}
	if routes[0].Flags&int(netlink.FLAG_ONLINK) == 0 {
		t.Fatalf("routes[0].Flags = %v, want onlink", routes[0].Flags)
	}
	if !routes[0].Gw.Equal(net.ParseIP("10.200.0.2")) {
		t.Fatalf("routes[0].Gw = %v, want 10.200.0.2", routes[0].Gw)
	}
}

func TestReplaceRoutesFromFileReturnsRouteReplaceError(t *testing.T) {
	stubRouteGet(t, 42)
	path := filepath.Join(t.TempDir(), "routes.txt")
	if err := os.WriteFile(path, []byte("1.2.3.0/24\n"), 0644); err != nil {
		t.Fatalf("WriteFile() error = %v", err)
	}

	original := routeReplace
	t.Cleanup(func() {
		routeReplace = original
	})
	routeReplace = func(*netlink.Route) error {
		return errors.New("route replace failed")
	}

	err := (&app{}).replaceRoutesFromFile(path, "az-world", "10.200.0.3")
	if err == nil {
		t.Fatal("replaceRoutesFromFile() error = nil, want error")
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
	stubRouteGet(t, 42)

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

func stubRouteGet(t *testing.T, linkIndex int) {
	t.Helper()

	original := routeGet
	t.Cleanup(func() {
		routeGet = original
	})
	routeGet = func(net.IP) ([]netlink.Route, error) {
		return []netlink.Route{{LinkIndex: linkIndex}}, nil
	}
}

func captureRuleAdd(t *testing.T, fn func()) []netlink.Rule {
	t.Helper()

	original := ruleAdd
	t.Cleanup(func() {
		ruleAdd = original
	})

	var rules []netlink.Rule
	ruleAdd = func(rule *netlink.Rule) error {
		rules = append(rules, *rule)
		return nil
	}

	fn()
	return rules
}

func stubLookupIP(t *testing.T, responses map[string]string) func() {
	t.Helper()

	original := lookupIP
	lookupIP = func(_ context.Context, host string) ([]net.IP, error) {
		ip, ok := responses[host]
		if !ok {
			return nil, &net.DNSError{Name: host, IsNotFound: true}
		}
		return []net.IP{net.ParseIP(ip)}, nil
	}
	return func() {
		lookupIP = original
	}
}
