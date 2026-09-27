package main

import (
	"context"
	"errors"
	"fmt"
	"net"
	"os"
	"path/filepath"
	"reflect"
	"strings"
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
	stubKernelRoutes(t, []netlink.Route{kernelRoute("14.16.0.0/15", "10.200.0.2", mainRouteTable)})
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
	stubKernelRoutes(t, []netlink.Route{
		kernelRoute("14.16.0.0/15", "10.200.0.2", mainRouteTable),
		kernelRoute("default", "10.200.0.2", vpnRouteTable),
	})
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

func TestRouteListSkipsInvalidLinesAndContinuesAfterErrors(t *testing.T) {
	stubRouteGet(t, 42)
	path := filepath.Join(t.TempDir(), "routes.txt")
	list := "2001:db8::/32\n1.2.3.0/24\nnot-a-route\n5.6.7.0/24\n9.9.9.9\n"
	if err := os.WriteFile(path, []byte(list), 0644); err != nil {
		t.Fatalf("WriteFile() error = %v", err)
	}

	original := routeReplace
	t.Cleanup(func() {
		routeReplace = original
	})
	var applied []string
	routeReplace = func(route *netlink.Route) error {
		applied = append(applied, route.Dst.String())
		if route.Dst.String() == "1.2.3.0/24" {
			return errors.New("route replace failed")
		}
		return nil
	}

	err := (&app{}).replaceRoutesFromFile(path, "az-world", "10.200.0.3")
	if err == nil {
		t.Fatal("replaceRoutesFromFile() error = nil, want the failed route error")
	}
	want := []string{"1.2.3.0/24", "5.6.7.0/24", "9.9.9.9/32"}
	if strings.Join(applied, ",") != strings.Join(want, ",") {
		t.Fatalf("applied = %v, want %v", applied, want)
	}
}

func TestValidRouteListNeedsOneValidRoute(t *testing.T) {
	if !validRouteList([]byte("2001:db8::/32\n1.2.3.0/24\n")) {
		t.Fatal("a list with one valid route was rejected")
	}
	if validRouteList([]byte("<html>error</html>\n2001:db8::/32\n")) {
		t.Fatal("a list without valid IPv4 routes was accepted")
	}
}

func TestRouteListURLsDoNotApplyHostExclusions(t *testing.T) {
	for _, path := range []string{azLocalListPath, azWorldListPath} {
		if !strings.Contains(path, "filter_custom=0") {
			t.Fatalf("%s applies exclude-hosts-custom regexes to IP routes", path)
		}
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

// Unit tests model kernel state explicitly and never touch host routing tables.
func TestMain(m *testing.M) {
	routeListFiltered = func(int, *netlink.Route, uint64) ([]netlink.Route, error) { return nil, nil }
	os.Exit(m.Run())
}

func kernelRoute(subnet, gateway string, table int) netlink.Route {
	route, err := routeSpecFor(subnet, gateway, table)
	if err != nil {
		panic(err)
	}
	route.LinkIndex = 42
	route.Type = 1 // Linux RTN_UNICAST.
	return route
}

func stubKernelRoutes(t *testing.T, routes []netlink.Route) {
	t.Helper()
	original := routeListFiltered
	t.Cleanup(func() { routeListFiltered = original })
	routeListFiltered = func(_ int, filter *netlink.Route, _ uint64) ([]netlink.Route, error) {
		var result []netlink.Route
		for _, route := range routes {
			if route.Table == filter.Table {
				result = append(result, route)
			}
		}
		return result, nil
	}
}

func TestReconcileRoutesDespiteUnchangedGateway(t *testing.T) {
	for _, damage := range []string{"deleted", "wrong-gateway", "wrong-interface"} {
		t.Run(damage, func(t *testing.T) {
			restore := stubLookupIP(t, map[string]string{"az-local": "10.200.0.2"})
			defer restore()
			for _, table := range []int{mainRouteTable, vpnRouteTable} {
				t.Run(fmt.Sprint(table), func(t *testing.T) {
					main := kernelRoute("14.16.0.0/15", "10.200.0.2", mainRouteTable)
					vpn := kernelRoute("default", "10.200.0.2", vpnRouteTable)
					state := []netlink.Route{main, vpn}
					index := 0
					if table == vpnRouteTable {
						index = 1
					}
					switch damage {
					case "deleted":
						state = append(state[:index], state[index+1:]...)
					case "wrong-gateway":
						state[index].Gw = net.ParseIP("10.200.0.99")
					case "wrong-interface":
						state[index].LinkIndex = 99
					}
					stubKernelRoutes(t, state)
					changes := captureRouteReplace(t, func() {
						(&app{vpn: true, defaultRoute: "az-local",
							routes:        []routeSpec{{host: "az-local", subnet: "14.16.0.0/15"}},
							routeGateways: map[string]string{"az-local": "10.200.0.2"},
							vpnGateways:   map[string]string{"az-local": "10.200.0.2"},
						}).updateRoutes()
					})
					if len(changes) != 1 {
						t.Fatalf("changes = %v, want one repair", changes)
					}
					wantTable := table
					if table == mainRouteTable {
						wantTable = 0
					}
					if changes[0].Table != wantTable || changes[0].LinkIndex != 42 || !changes[0].Gw.Equal(net.ParseIP("10.200.0.2")) {
						t.Fatalf("unexpected repair: %v", changes[0])
					}
				})
			}
		})
	}
}

func TestRepairsCachedVPNListWithoutHTTP(t *testing.T) {
	restore := stubLookupIP(t, map[string]string{"az-world": "10.200.0.3"})
	defer restore()
	stubKernelRoutes(t, []netlink.Route{
		kernelRoute("14.18.0.0/15", "10.200.0.3", mainRouteTable),
		kernelRoute("14.18.0.0/15", "10.200.0.3", vpnRouteTable),
	})
	changes := captureRouteReplace(t, func() {
		(&app{vpn: true, defaultRoute: "az-local",
			routes:      []routeSpec{{host: "az-world", subnet: "14.18.0.0/15"}},
			vpnGateways: map[string]string{"az-world": "10.200.0.3"},
			vpnLists:    map[string][]string{"az-world": {"192.0.2.0/24"}},
		}).updateRoutes()
	})
	if len(changes) != 1 || changes[0].Dst.String() != "192.0.2.0/24" || changes[0].Table != vpnRouteTable {
		t.Fatalf("changes = %v, want missing cached list route", changes)
	}
}

func TestRouteSnapshotErrorDoesNotWriteRoutes(t *testing.T) {
	original := routeListFiltered
	t.Cleanup(func() { routeListFiltered = original })
	routeListFiltered = func(int, *netlink.Route, uint64) ([]netlink.Route, error) {
		return nil, errors.New("netlink unavailable")
	}
	changes := captureRouteReplace(t, func() {
		(&app{routes: []routeSpec{{host: "10.200.0.2", subnet: "14.16.0.0/15"}}}).updateRoutes()
	})
	if len(changes) != 0 {
		t.Fatalf("unexpected changes: %v", changes)
	}
}

func TestRepeatedCyclesRepairDeletionAndInterfaceChange(t *testing.T) {
	originalList, originalReplace, originalGet := routeListFiltered, routeReplace, routeGet
	t.Cleanup(func() {
		routeListFiltered, routeReplace, routeGet = originalList, originalReplace, originalGet
	})
	state := make(map[string]netlink.Route)
	writes, link := 0, 42
	routeListFiltered = func(_ int, filter *netlink.Route, _ uint64) ([]netlink.Route, error) {
		var routes []netlink.Route
		for _, route := range state {
			if route.Table == filter.Table {
				routes = append(routes, route)
			}
		}
		return routes, nil
	}
	routeGet = func(net.IP) ([]netlink.Route, error) { return []netlink.Route{{LinkIndex: link}}, nil }
	routeReplace = func(route *netlink.Route) error {
		writes++
		stored := *route
		if stored.Table == 0 {
			stored.Table = mainRouteTable
		}
		stored.Type = 1
		state[routeKey(stored.Table, stored.Dst)] = stored
		return nil
	}
	a := &app{routes: []routeSpec{{host: "10.200.0.2", subnet: "14.16.0.0/15"}}}
	a.updateRoutes()
	a.updateRoutes()
	if writes != 1 {
		t.Fatalf("healthy second cycle wrote routes: %d", writes)
	}
	clear(state)
	a.updateRoutes()
	if writes != 2 {
		t.Fatalf("deleted route was not repaired: %d", writes)
	}
	link = 43
	a.updateRoutes()
	if writes != 3 {
		t.Fatalf("changed interface was not repaired: %d", writes)
	}
	a.updateRoutes()
	if writes != 3 {
		t.Fatalf("repaired route rewritten: %d", writes)
	}
}
