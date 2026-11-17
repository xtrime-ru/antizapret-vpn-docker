package integration

import (
	"os"
	"slices"
	"testing"

	"github.com/moby/moby/client"
	"gopkg.in/yaml.v3"
)

var composeServices = map[string][2]string{
	"antizapret":  {"antizapret/docker-compose.yml", "antizapret"},
	"adguard":     {"adguard/docker-compose.yml", "adguard"},
	"coredns":     {"coredns/docker-compose.yml", "coredns"},
	"filebrowser": {"filebrowser/docker-compose.yml", "filebrowser"},
	"https":       {"https/docker-compose.yml", "https"},
	"proxy":       {"proxy/compose.yml", "proxy"},
	"dashboard":   {"dashboard/docker-compose.yml", "dashboard"},
	"firewall":    {"firewall/docker-compose.yml", "firewall"},
	"openvpn":     {"openvpn/docker-compose.yml", "openvpn"},
	"openvpn-ui":  {"openvpn/docker-compose.yml", "openvpn-ui"},
	"ocserv":      {"ocserv/compose.yml", "ocserv"},
	"wireguard":   {"wireguard/docker-compose.base.yml", "base"},
}

type composeLaunch struct {
	Entrypoint  []string
	Command     []string
	Init        bool
	Healthcheck struct{ Test []string }
}

func loadComposeLaunch(t *testing.T, service string) composeLaunch {
	t.Helper()
	location := composeServices[service]
	data, err := os.ReadFile("../../services/" + location[0])
	if err != nil {
		t.Fatal(err)
	}
	var compose struct{ Services map[string]composeLaunch }
	if err := yaml.Unmarshal(data, &compose); err != nil {
		t.Fatal(err)
	}
	launch, ok := compose.Services[location[1]]
	if !ok {
		t.Fatalf("service %s absent in %s", location[1], location[0])
	}
	return launch
}

func TestServiceLaunchPolicy(t *testing.T) {
	cli, err := client.New(client.FromEnv)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = cli.Close() })
	for service := range composeServices {
		t.Run(service, func(t *testing.T) {
			launch := loadComposeLaunch(t, service)
			if !launch.Init || !slices.Equal(launch.Entrypoint, []string{"/usr/local/lib/docktinel/entrypoint"}) || len(launch.Command) == 0 {
				t.Fatalf("incomplete Compose launch policy: %+v", launch)
			}
			if !slices.Contains(launch.Healthcheck.Test, "docktinel") {
				t.Fatal("Compose does not configure watchdog healthcheck")
			}
			image, err := cli.ImageInspect(t.Context(), serviceImage(service))
			if err != nil {
				t.Fatal(err)
			}
			if len(image.Config.Entrypoint) != 0 {
				t.Fatalf("image intercepts commands: %v", image.Config.Entrypoint)
			}
			if check := image.Config.Healthcheck; check != nil && len(check.Test) != 0 && check.Test[0] != "NONE" {
				t.Fatalf("image applies a service healthcheck to utility commands: %v", check.Test)
			}
		})
	}
}
