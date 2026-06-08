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
	dnsTimeout        = 1 * time.Second
	httpClientTimeout = 3 * time.Second
)

var routeListClient = &http.Client{Timeout: httpClientTimeout}
var routeReplace = netlink.RouteReplace

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
	routes        []routeSpec
	routeGateways map[string]string
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
	cfg.updateAddresses()
	ticker := time.NewTicker(interval)
	defer ticker.Stop()

	for {
		select {
		case <-ticker.C:
			cfg.updateAddresses()
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
	flag.Float64Var(&intervalSeconds, "interval", 10., "route check interval in seconds")
	flag.Parse()

	if flag.NArg() > 0 {
		return nil, 0, fmt.Errorf("unknown option: %s", flag.Arg(0))
	}
	if cfg.self == "" {
		return nil, 0, errors.New("Error: --self option required")
	}
	if intervalSeconds < 0.1 {
		intervalSeconds = 0.1
	}

	cfg.routes = parseRoutes(os.Getenv("ROUTES"), cfg.self)
	cfg.routeGateways = make(map[string]string, len(cfg.routes))
	return cfg, time.Duration(intervalSeconds * float64(time.Second)), nil
}

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
		if !ok || host == "" || subnet == "" || host == self {
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
		if route.host == "adguard" {
			hasError := false
			for _, protocol := range []string{"tcp", "udp"} {
				for _, ruleset := range []string{"PREROUTING", "OUTPUT"} {
					cmd := exec.Command(
						"iptables",
						"-t", "nat",
						"-A", ruleset,
						"-p", protocol,
						"--dport", "53",
						"-j", "DNAT",
						"--to-destination", route.subnet,
					)
					err := cmd.Run()
					if err != nil {
						hasError = true
						fmt.Fprintf(os.Stdout, "failed to add iptables DNS redirect for %s: %v\n", route.subnet, err)
					}
				}
			}
			if !hasError {
				fmt.Printf("ROUTES: added iptables DNS redirect destination=%s\n", route.subnet)
			}
		}
	}

}

func (a *app) updateAddresses() {
	start := time.Now()
	a.logVerbose("route update started")
	for _, route := range a.routes {
		gateway := a.resolve(route.host)
		if gateway == "" {
			a.logVerbose("route skipped: host=%s subnet=%s reason=unresolved", route.host, route.subnet)
			continue
		}

		currentGateway := a.routeGateways[route.host]
		a.logVerbose("route state: host=%s subnet=%s current_gateway=%q resolved_gateway=%s", route.host, route.subnet, currentGateway, gateway)
		switch {
		case currentGateway == gateway:
			a.logVerbose("route unchanged: %s via %s", route.subnet, gateway)
			continue
		case currentGateway == "":
			if a.replaceRoute(route, gateway) == nil {
				fmt.Printf("Route added: %s via %s\n", route.subnet, gateway)
			}
		default:
			if a.replaceRoute(route, gateway) == nil {
				fmt.Printf("Route changed: %s via %s\n", route.subnet, gateway)
			}
		}
		if err := a.applyVPNRoutes(route.host, gateway); err != nil {
			delete(a.routeGateways, route.host)
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

	ips, err := udpResolver.LookupIP(ctx, "ip4", host)
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

func isIPv4(value string) bool {
	ip := net.ParseIP(strings.TrimSpace(value))
	return ip != nil && ip.To4() != nil
}

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

func routeSpecFor(subnet, gateway string) (netlink.Route, error) {
	dst, err := parseRouteDst(subnet)
	if err != nil {
		return netlink.Route{}, err
	}
	gw, err := parseIPv4(gateway)
	if err != nil {
		return netlink.Route{}, fmt.Errorf("invalid IPv4 gateway: %s", gateway)
	}
	return netlink.Route{Dst: dst, Gw: gw}, nil
}

func (a *app) replaceRoute(route routeSpec, gateway string) error {
	start := time.Now()
	routeNetLink, err := routeSpecFor(route.subnet, gateway)
	if err == nil {
		err = routeReplace(&routeNetLink)
	}
	if err == nil {
		a.routeGateways[route.host] = gateway
	}
	a.logVerbose("netlink RouteReplace dst=%s gateway=%s duration=%s err=%v", route.subnet, gateway, time.Since(start), err)
	return err
}

func (a *app) applyVPNRoutes(host, gateway string) error {
	if !a.vpn {
		return nil
	}
	switch host {
	case "az-local":
		return a.replaceRoutesFromFile(azLocalListPath, host, gateway)
	case "az-world":
		return a.replaceRoutesFromFile(azWorldListPath, host, gateway)
	}
	return nil
}

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

func (a *app) replaceRoutesFromFile(path, host, gateway string) error {
	return forEachRouteLine(path, func(subnet string) {
		a.replaceRoute(routeSpec{subnet: subnet, host: host}, gateway)
	})
}

func forEachRouteLine(path string, fn func(string)) error {
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
		fn(line)
	}

	if err := scanner.Err(); err != nil {
		fmt.Fprintf(os.Stderr, "reading routes list %s: %v\n", path, err)
		return err
	}
	return nil
}

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
