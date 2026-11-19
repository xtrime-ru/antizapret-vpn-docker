package fallbackttl

import (
	"context"
	"slices"
	"strconv"
	"strings"

	"github.com/coredns/caddy"
	"github.com/coredns/caddy/caddyfile"
	"github.com/coredns/coredns/core/dnsserver"
	"github.com/coredns/coredns/plugin"
	"github.com/coredns/coredns/plugin/forward"
	"github.com/miekg/dns"
)

const name = "fallbackttl"

func init() { plugin.Register(name, setup) }

func setup(c *caddy.Controller) error {
	c.Next()
	args := c.RemainingArgs()
	if len(args) < 1 || len(args) > 2 {
		return plugin.Error(name, c.ArgErr())
	}
	expectedExits, err := strconv.Atoi(args[0])
	if err != nil || expectedExits < 1 {
		return plugin.Error(name, c.Err("expected a positive number of az-* upstreams"))
	}
	config := fallbackTTL{expectedExits: expectedExits}
	if len(args) == 2 {
		config.fallback = args[1]
	}

	if !c.NextArg() || c.Val() != "{" || !c.Next() || c.Val() != "forward" {
		return plugin.Error(name, c.Err("expected a forward block"))
	}
	forwardTokens, err := config.splitForward(c)
	if err != nil {
		return plugin.Error(name, err)
	}
	setupForward, err := caddy.DirectiveAction("dns", "forward")
	if err != nil {
		return plugin.Error(name, err)
	}
	dnsserver.GetConfig(c).AddPlugin(func(next plugin.Handler) plugin.Handler {
		handler := config
		handler.next = next
		return handler
	})
	forwardController := *c
	forwardController.Dispenser = caddyfile.NewDispenserTokens(c.File(), forwardTokens)
	return setupForward(&forwardController)
}

// Split the destinations and take ownership of failover. Forward parses the other options.
func (f *fallbackTTL) splitForward(c *caddy.Controller) ([]caddyfile.Token, error) {
	directive := caddyfile.Token{Text: c.Val(), File: c.File(), Line: c.Line()}
	args := c.RemainingArgs()
	if len(args) < 2 || args[0] != "." {
		return nil, c.Err("expected forward . followed by upstream addresses")
	}
	open, close := directive, directive
	open.Text, close.Text = "{", "}"
	options := []caddyfile.Token{open}
	for c.NextBlock() {
		option := caddyfile.Token{Text: c.Val(), File: c.File(), Line: c.Line()}
		values := c.RemainingArgs()
		switch option.Text {
		case "failover":
			if len(values) == 0 {
				return nil, c.ArgErr()
			}
			for _, value := range values {
				code, ok := dns.StringToRcode[strings.ToUpper(value)]
				if !ok || code == dns.RcodeSuccess {
					return nil, c.Errf("invalid failover response code %q", value)
				}
				f.failover = append(f.failover, code)
			}
		case "next", "next_on_nodata":
			return nil, c.Errf("%s would bypass fallbackttl", option.Text)
		default:
			options = append(options, option)
			for _, value := range values {
				option.Text = value
				options = append(options, option)
			}
		}
	}
	if !c.Next() || c.Val() != "}" || c.Next() {
		return nil, c.Err("expected end of fallbackttl block")
	}

	close.Line = c.Line()
	options = append(options, close)
	zone, target := directive, directive
	zone.Text = "."
	var tokens []caddyfile.Token
	for _, address := range args[1:] {
		target.Text = address
		tokens = append(tokens, directive, zone, target)
		tokens = append(tokens, options...)
	}
	return tokens, nil
}

type fallbackTTL struct {
	next          plugin.Handler
	expectedExits int
	fallback      string
	failover      []int
}

func (fallbackTTL) Name() string { return name }

func (f fallbackTTL) ServeDNS(ctx context.Context, w dns.ResponseWriter, req *dns.Msg) (int, error) {
	refused := 0
	var lastErr error
	writer := responseWriter{ResponseWriter: w}
	for upstream, ok := f.next.(*forward.Forward); ok; upstream, ok = upstream.Next.(*forward.Forward) {
		writer.reply = nil
		_, lastErr = upstream.ServeDNS(ctx, &writer, req)
		reply := writer.reply
		if lastErr != nil || reply == nil {
			continue
		}

		isFallback := f.fallback != "" && upstream.List()[0].Addr() == f.fallback
		_, hasNext := upstream.Next.(*forward.Forward)
		if !isFallback && hasNext && slices.Contains(f.failover, reply.Rcode) {
			if reply.Rcode == dns.RcodeRefused {
				refused++
			}
			continue
		}
		if isFallback && refused != f.expectedExits && reply.IsTsig() == nil {
			zeroTTL(reply)
		}
		return dns.RcodeSuccess, w.WriteMsg(reply)
	}
	return dns.RcodeServerFailure, lastErr
}

type responseWriter struct {
	dns.ResponseWriter
	reply *dns.Msg
}

func (w *responseWriter) WriteMsg(msg *dns.Msg) error {
	w.reply = msg
	return nil
}

func zeroTTL(msg *dns.Msg) {
	for _, section := range [][]dns.RR{msg.Answer, msg.Ns, msg.Extra} {
		for _, record := range section {
			header := record.Header()
			// OPT stores EDNS flags and extended RCODE here, outside normal TTL semantics.
			if header.Rrtype != dns.TypeOPT {
				header.Ttl = 0
			}
		}
	}
}
