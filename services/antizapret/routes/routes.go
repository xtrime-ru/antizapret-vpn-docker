package main

import (
	"bufio"
	"bytes"
	"context"
	"errors"
	"flag"
	"fmt"
	"io"
	"net"
	"net/http"
	"os"
	"os/exec"
	"os/signal"
	"strings"
	"syscall"
	"time"

	"github.com/vishvananda/netlink"
)

const (
	azLocalListPath   = "http://az-local.antizapret/list/?raw=1&file=/root/antizapret/result/ips.txt"
	azWorldListPath   = "http://az-local.antizapret/list/?raw=1&file=/root/antizapret/result/ips-world.txt"
	vpnDefaultRoute   = "default"
	mainRouteTable    = 254
	vpnRouteTable     = 100
	vpnRulePriority   = 10000
	vpnLocalPriority  = vpnRulePriority - 1
	dnsTimeout        = 1 * time.Second
	httpClientTimeout = 3 * time.Second
)

var routeListClient = &http.Client{Timeout: httpClientTimeout}
var routeReplace = netlink.RouteReplace
var routeGet = netlink.RouteGet
var ruleAdd = netlink.RuleAdd
var lookupIP = func(ctx context.Context, host string) ([]net.IP, error) {
	return udpResolver.LookupIP(ctx, "ip4", host)
}
var iptablesRun = func(args ...string) error {
	return exec.Command("iptables", args...).Run()
}

var udpResolver = &net.Resolver{
	PreferGo: true,
	Dial: func(ctx context.Context, network, address string) (net.Conn, error) {
		var d net.Dialer
		d.Timeout = dnsTimeout
		return d.DialContext(ctx, "udp", address)
	},
}

type routeSpec struct {
	host   string
	subnet string
}

type app struct {
	self          string
	vpn           bool
	verbose       bool
	defaultRoute  string
	routes        []routeSpec
	routeGateways map[string]string
	vpnGateways   map[string]string
	gatewayLinks  map[string]int
}

func main() {
	cfg, interval, err := parseArgs()
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}

	cfg.logVerbose("starting route updater: self=%s vpn=%v interval=%s routes=%d", cfg.self, cfg.vpn, interval, len(cfg.routes))

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	cfg.enableDNSRedirect()
	cfg.updateRoutes()
	ticker := time.NewTicker(interval)
	defer ticker.Stop()

	for {
		select {
		case <-ticker.C:
			cfg.updateRoutes()
		case <-ctx.Done():
			fmt.Println("routes: Gracefully shutting down...")
			return
		}
	}
}

func parseArgs() (*app, time.Duration, error) {
	self, _ := os.Hostname()
	self = strings.SplitN(self, ".", 2)[0]

	var intervalSeconds float64
	cfg := &app{}
	flag.StringVar(&cfg.self, "self", self, "current host name")
	flag.BoolVar(&cfg.vpn, "vpn", false, "manage VPN-specific routes")
	flag.BoolVar(&cfg.verbose, "verbose", false, "enable verbose route logs")
	flag.StringVar(&cfg.defaultRoute, "default-route", "az-local", "default VPN route host")
	flag.Float64Var(&intervalSeconds, "interval", 10., "route check interval in seconds")
	flag.Parse()

	if flag.NArg() > 0 {
		return nil, 0, fmt.Errorf("unknown option: %s", flag.Arg(0))
	}
	if cfg.self == "" {
		return nil, 0, errors.New("Error: --self option required")
	}
	if cfg.defaultRoute != "az-local" && cfg.defaultRoute != "az-world" {
		return nil, 0, fmt.Errorf("invalid --default-route: %s", cfg.defaultRoute)
	}
	if intervalSeconds < 0.1 {
		intervalSeconds = 0.1
	}

	cfg.routes = parseRoutes(os.Getenv("ROUTES"), cfg.self)
	cfg.routeGateways = make(map[string]string, len(cfg.routes))
	cfg.vpnGateways = make(map[string]string, len(cfg.routes))
	return cfg, time.Duration(intervalSeconds * float64(time.Second)), nil
}

// parseRoutes parses ROUTES entries in the form host:subnet;.
func parseRoutes(raw, self string) []routeSpec {
	parts := strings.FieldsFunc(raw, func(r rune) bool { return r == ';' })
	routes := make([]routeSpec, 0, len(parts))
	for _, part := range parts {
		part = strings.TrimSpace(part)
		if part == "" {
			continue
		}
		host, subnet, ok := strings.Cut(part, ":")
		host = strings.TrimSpace(host)
		subnet = strings.TrimSpace(subnet)
		if !ok || host == "" || subnet == "" {
			continue
		}
		routes = append(routes, routeSpec{host: host, subnet: subnet})
	}
	return routes
}

func (a *app) enableDNSRedirect() {
	if !a.vpn {
		return
	}

	for _, route := range a.routes {
		fmt.Fprintf(os.Stdout, "ROUTES: host=%s subnet=%s\n", route.host, route.subnet)
		if route.host != "adguard" {
			continue
		}

		hasError := false
		for _, rule := range dnsRedirectRules(route.subnet) {
			if err := iptablesRun(rule...); err != nil {
				hasError = true
				fmt.Fprintf(os.Stdout, "failed to add iptables DNS redirect for %s: %v\n", route.subnet, err)
			}
		}
		if !hasError {
			fmt.Printf("ROUTES: added iptables DNS redirect destination=%s\n", route.subnet)
		}
	}

}

func dnsRedirectRules(destination string) [][]string {
	rules := make([][]string, 0, 6)
	for _, protocol := range []string{"tcp", "udp"} {
		for _, ruleset := range []string{"PREROUTING", "OUTPUT"} {
			rule := []string{
				"-t", "nat",
				"-A", ruleset,
			}
			if ruleset == "OUTPUT" {
				rule = append(rule, "!", "-d", "127.0.0.0/8")
			}
			rule = append(rule,
				"-p", protocol,
				"--dport", "53",
				"-j", "DNAT",
				"--to-destination", destination,
			)
			rules = append(rules, rule)
		}
	}
	for _, protocol := range []string{"tcp", "udp"} {
		rules = append(rules, []string{
			"-t", "nat",
			"-A", "POSTROUTING",
			"-m", "addrtype",
			"--src-type", "LOCAL",
			"-p", protocol,
			"-d", destination,
			"--dport", "53",
			"-j", "MASQUERADE",
		})
	}
	return rules
}

// updateRoutes refreshes configured routes and policy rules.
func (a *app) updateRoutes() {
	start := time.Now()
	a.logVerbose("route update started")
	selfGateway := ""
	if a.self != "" {
		selfGateway = a.resolve(a.self)
	}
	if a.routeGateways == nil {
		a.routeGateways = make(map[string]string, len(a.routes))
	}
	if a.vpnGateways == nil {
		a.vpnGateways = make(map[string]string, len(a.routes))
	}
	if a.gatewayLinks == nil {
		a.gatewayLinks = make(map[string]int)
	}

	for _, route := range a.routes {
		isSelf := route.host == a.self
		if a.vpn && isSelf {
			if err := a.addVPNClientRules(route); err != nil {
				fmt.Fprintf(os.Stderr, "failed to add VPN client rules: host=%s subnet=%s error=%v\n", route.host, route.subnet, err)
			}
		}
		if isSelf {
			a.logVerbose("route skipped: host=%s subnet=%s reason=self", route.host, route.subnet)
			continue
		}

		gateway := a.resolve(route.host)
		if gateway == "" {
			a.logVerbose("route skipped: host=%s subnet=%s reason=unresolved", route.host, route.subnet)
			continue
		}
		if gateway == selfGateway {
			a.logVerbose("route skipped: host=%s subnet=%s reason=self-alias gateway=%s", route.host, route.subnet, gateway)
			continue
		}

		currentGateway := a.routeGateways[route.host]
		a.logVerbose("route state: host=%s subnet=%s current_gateway=%q resolved_gateway=%s", route.host, route.subnet, currentGateway, gateway)
		if currentGateway != gateway {
			if err := a.replaceRoute(route, gateway, false); err == nil {
				a.routeGateways[route.host] = gateway
				if currentGateway == "" {
					fmt.Printf("Route added: %s via %s\n", route.subnet, gateway)
				} else {
					fmt.Printf("Route changed: %s via %s\n", route.subnet, gateway)
				}
			} else {
				delete(a.routeGateways, route.host)
				fmt.Fprintf(os.Stderr, "failed to replace route: host=%s subnet=%s gateway=%s error=%v\n", route.host, route.subnet, gateway, err)
			}
		} else {
			a.logVerbose("route unchanged: %s via %s", route.subnet, gateway)
		}

		if a.vpn {
			if a.vpnGateways[route.host] == gateway {
				a.logVerbose("VPN route unchanged: host=%s gateway=%s", route.host, gateway)
				continue
			}
			if err := a.applyVPNRoutes(route.host, gateway); err != nil {
				delete(a.vpnGateways, route.host)
				fmt.Fprintf(os.Stderr, "failed to apply VPN routes: host=%s gateway=%s error=%v\n", route.host, gateway, err)
			} else {
				a.vpnGateways[route.host] = gateway
			}
		}
	}
	a.logVerbose("route update finished in %s", time.Since(start))
}

func (a *app) resolve(host string) string {
	if isIPv4(host) {
		a.logVerbose("LookupIP skipped for literal IPv4 host=%s", host)
		return host
	}
	start := time.Now()
	ctx, cancel := context.WithTimeout(context.Background(), dnsTimeout)
	defer cancel()

	ips, err := lookupIP(ctx, host)
	elapsed := time.Since(start)
	if err == nil {
		for _, ip := range ips {
			if v4 := ip.To4(); v4 != nil {
				a.logVerbose("LookupIP host=%s ip=%s duration=%s", host, v4.String(), elapsed)
				return v4.String()
			}
		}
	}
	if err != nil {
		a.logVerbose("LookupIP host=%s failed duration=%s error=%v", host, elapsed, err)
	} else {
		a.logVerbose("LookupIP host=%s returned no IPv4 duration=%s", host, elapsed)
	}
	return ""
}

// isIPv4 reports whether value is a literal IPv4 address.
func isIPv4(value string) bool {
	ip := net.ParseIP(strings.TrimSpace(value))
	return ip != nil && ip.To4() != nil
}

// parseRouteDst converts a route destination into netlink form; nil means default route.
func parseRouteDst(subnet string) (*net.IPNet, error) {
	subnet = strings.TrimSpace(subnet)
	if subnet == "" {
		return nil, errors.New("empty route destination")
	}
	if subnet == "default" || subnet == "0.0.0.0/0" {
		return nil, nil
	}
	if strings.Contains(subnet, "/") {
		_, dst, err := net.ParseCIDR(subnet)
		if err != nil {
			return nil, err
		}
		return dst, nil
	}
	ip := net.ParseIP(subnet)
	if ip == nil || ip.To4() == nil {
		return nil, fmt.Errorf("invalid IPv4 route destination: %s", subnet)
	}
	return &net.IPNet{IP: ip.To4(), Mask: net.CIDRMask(32, 32)}, nil
}

// routeSpecFor builds a netlink route for the requested routing table.
func routeSpecFor(subnet, gateway string, table int) (netlink.Route, error) {
	dst, err := parseRouteDst(subnet)
	if err != nil {
		return netlink.Route{}, err
	}
	gw, err := parseIPv4(gateway)
	if err != nil {
		return netlink.Route{}, fmt.Errorf("invalid IPv4 gateway: %s", gateway)
	}
	return netlink.Route{Dst: dst, Gw: gw, Table: table}, nil
}

// replaceRoute performs ip route replace, optionally in the VPN policy table.
func (a *app) replaceRoute(route routeSpec, gateway string, useVPNTable bool) error {
	start := time.Now()
	table := 0
	if useVPNTable {
		table = vpnRouteTable
	}
	routeNetLink, err := routeSpecFor(route.subnet, gateway, table)
	if err != nil {
		return err
	}
	linkIndex, err := a.linkIndexForGateway(gateway)
	if err != nil {
		return err
	}
	routeNetLink.LinkIndex = linkIndex
	routeNetLink.SetFlag(netlink.FLAG_ONLINK)
	err = routeReplace(&routeNetLink)
	a.logVerbose("netlink RouteReplace dst=%s gateway=%s duration=%s err=%v", route.subnet, gateway, time.Since(start), err)
	return err
}

// linkIndexForGateway resolves the output interface explicitly. Older kernels
// cannot always infer it when the first route is added to a policy table.
func (a *app) linkIndexForGateway(gateway string) (int, error) {
	if linkIndex := a.gatewayLinks[gateway]; linkIndex > 0 {
		return linkIndex, nil
	}

	gatewayIP, err := parseIPv4(gateway)
	if err != nil {
		return 0, err
	}
	routes, err := routeGet(gatewayIP)
	if err != nil {
		return 0, fmt.Errorf("failed to resolve interface for gateway %s: %w", gateway, err)
	}
	for _, route := range routes {
		if route.LinkIndex > 0 {
			if a.gatewayLinks == nil {
				a.gatewayLinks = make(map[string]int)
			}
			a.gatewayLinks[gateway] = route.LinkIndex
			return route.LinkIndex, nil
		}
	}
	return 0, fmt.Errorf("failed to resolve interface for gateway %s", gateway)
}

// addVPNClientRules checks non-default routes in the main table first, so
// connected networks stay local, and sends the remaining VPN client traffic
// to the VPN policy table.
func (a *app) addVPNClientRules(route routeSpec) error {
	vpnSubnet, err := parseRouteDst(route.subnet)
	if err != nil {
		return err
	}
	if vpnSubnet == nil {
		return errors.New("VPN client rule requires a source subnet")
	}

	localRule := netlink.NewRule()
	localRule.Family = netlink.FAMILY_V4
	localRule.Priority = vpnLocalPriority
	localRule.Table = mainRouteTable
	localRule.Src = vpnSubnet
	localRule.SuppressPrefixlen = 0
	if err := addRule(localRule); err != nil {
		return err
	}

	rule := netlink.NewRule()
	rule.Family = netlink.FAMILY_V4
	rule.Priority = vpnRulePriority
	rule.Table = vpnRouteTable
	rule.Src = vpnSubnet
	return addRule(rule)
}

func addRule(rule *netlink.Rule) error {
	err := ruleAdd(rule)
	if errors.Is(err, syscall.EEXIST) {
		return nil
	}
	return err
}

// applyVPNRoutes loads VPN policy-table egress routes for antizapret containers.
func (a *app) applyVPNRoutes(host, gateway string) error {
	switch host {
	case "adguard":
		return a.replaceRoute(routeSpec{host: host, subnet: a.subnetForHost(host)}, gateway, true)
	case "az-local":
		return a.applyAZRoutes(host, gateway, azLocalListPath)
	case "az-world":
		return a.applyAZRoutes(host, gateway, azWorldListPath)
	}
	return nil
}

// applyAZRoutes adds the AZ subnet/default and optional route list to the VPN policy table.
func (a *app) applyAZRoutes(host, gateway, listPath string) error {
	if host == a.defaultRoute {
		return a.replaceRoute(routeSpec{host: host, subnet: vpnDefaultRoute}, gateway, true)
	}
	if err := a.replaceRoute(routeSpec{host: host, subnet: a.subnetForHost(host)}, gateway, true); err != nil {
		return err
	}
	return a.replaceRoutesFromFile(listPath, host, gateway)
}

// subnetForHost returns the configured ROUTES subnet for host.
func (a *app) subnetForHost(host string) string {
	for _, route := range a.routes {
		if route.host == host {
			return route.subnet
		}
	}
	return ""
}

// parseIPv4 parses and copies a literal IPv4 address.
func parseIPv4(value string) (net.IP, error) {
	ip := net.ParseIP(strings.TrimSpace(value))
	if ip == nil {
		return nil, errors.New("invalid IPv4 address")
	}
	v4 := ip.To4()
	if v4 == nil {
		return nil, errors.New("invalid IPv4 address")
	}
	return append(net.IP(nil), v4...), nil
}

// replaceRoutesFromFile replaces every route from a downloaded list into the VPN policy table.
func (a *app) replaceRoutesFromFile(path, host, gateway string) error {
	return forEachRouteLine(path, func(subnet string) error {
		return a.replaceRoute(routeSpec{subnet: subnet, host: host}, gateway, true)
	})
}

// forEachRouteLine reads a route list and calls fn for every non-empty route line.
func forEachRouteLine(path string, fn func(string) error) error {
	file, err := openRouteList(path)
	if err != nil {
		return err
	}
	defer file.Close()

	scanner := bufio.NewScanner(file)
	for scanner.Scan() {
		line := strings.TrimSpace(scanner.Text())
		if line == "" {
			continue
		}
		if err := fn(line); err != nil {
			fmt.Fprintf(os.Stderr, "applying route from list %s: %v\n", path, err)
			return err
		}
	}

	if err := scanner.Err(); err != nil {
		fmt.Fprintf(os.Stderr, "reading routes list %s: %v\n", path, err)
		return err
	}
	return nil
}

// openRouteList opens a local route file or downloads and validates an HTTP route list.
func openRouteList(path string) (io.ReadCloser, error) {
	if strings.HasPrefix(path, "http://") || strings.HasPrefix(path, "https://") {
		resp, err := routeListClient.Get(path)
		if err != nil {
			fmt.Fprintf(os.Stderr, "fetching routes list %s: %v\n", path, err)
			return nil, err
		}
		defer resp.Body.Close()
		if resp.StatusCode != http.StatusOK {
			err := fmt.Errorf("unexpected HTTP status: %s", resp.Status)
			fmt.Fprintf(os.Stderr, "fetching routes list %s: %v\n", path, err)
			return nil, err
		}
		data, err := io.ReadAll(resp.Body)
		if err != nil {
			fmt.Fprintf(os.Stderr, "invalid routes list %s: %v\n", path, err)
			return nil, err
		}
		if !validRouteList(data) {
			err := errors.New("invalid routes list")
			fmt.Fprintf(os.Stderr, "invalid routes list %s: %v\n", path, err)
			return nil, err
		}
		return io.NopCloser(bytes.NewReader(data)), nil
	}
	return os.Open(path)
}

// validRouteList verifies that a downloaded list contains at least one valid route.
func validRouteList(data []byte) bool {
	scanner := bufio.NewScanner(bytes.NewReader(data))
	lines := 0
	for scanner.Scan() {
		line := strings.TrimSpace(scanner.Text())
		if line == "" {
			continue
		}
		if _, err := parseRouteDst(line); err != nil {
			return false
		}
		lines++
	}
	return lines > 0 && scanner.Err() == nil
}

func (a *app) logVerbose(format string, args ...any) {
	if a.verbose {
		fmt.Printf(format+"\n", args...)
	}
}
