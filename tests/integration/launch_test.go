package integration

import (
	"encoding/json/v2"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"

	"github.com/moby/moby/api/types/container"
	"github.com/moby/moby/client"
	"gopkg.in/yaml.v3"
)

var selectedServices = strings.Fields(os.Getenv("TEST_SERVICES"))

func requireService(t *testing.T, name string) {
	t.Helper()
	if len(selectedServices) > 0 && !slices.Contains(selectedServices, name) {
		t.Skipf("%s was not selected", name)
	}
}

type imageSpec struct {
	Name    string `json:"name"`
	Source  string `json:"source"`
	Compose string `json:"compose"`
	Target  string `json:"target"`
}

func loadImageSpecs(t *testing.T) []imageSpec {
	t.Helper()
	data, err := os.ReadFile("../../versions.json")
	if err != nil {
		t.Fatal(err)
	}
	var manifest struct {
		Images []imageSpec `json:"images"`
	}
	if err := json.Unmarshal(data, &manifest); err != nil {
		t.Fatal(err)
	}
	return manifest.Images
}

func TestPackagedImages(t *testing.T) {
	smoke := map[string][]string{
		"adguard":     {"/opt/adguardhome/AdGuardHome", "--version"},
		"antizapret":  {"/usr/bin/dnsmap", "--help"},
		"cloak":       {"/usr/local/bin/ck-server"},
		"coredns":     {"/coredns", "-version"},
		"dashboard":   {"lighttpd", "-v"},
		"filebrowser": {"/bin/filebrowser", "version"},
		"firewall":    {"ipset"},
		"https":       {"caddy", "version"},
		"ocserv":      {"ocserv", "--version"},
		"openvpn":     {"/usr/local/sbin/openvpn", "--version"},
		"openvpn-ui":  {"/opt/openvpn-ui/openvpn-ui"},
		"proxy":       {"/bin/3proxy"},
		"wireguard":   {"wg", "--version"},
	}
	for _, spec := range loadImageSpecs(t) {
		t.Run(spec.Name, func(t *testing.T) {
			requireService(t, spec.Name)
			cmd := smoke[spec.Name]
			if len(cmd) == 0 {
				cmd = []string{"sh"}
			}
			if len(cmd) == 1 {
				cmd = []string{"/bin/sh", "-ec", "command -v \"$1\"", "sh", cmd[0]}
			}
			f := startContainer(t, &container.Config{
				Image: serviceImage(spec.Name), Entrypoint: cmd[:1], Cmd: cmd[1:],
			}, &container.HostConfig{NetworkMode: "none"})
			if code := f.waitExit(); code != 0 {
				t.Fatalf("smoke check exited %d", code)
			}
			if arch := os.Getenv("BUILD_ARCH"); arch != "" {
				image, err := f.cli.ImageInspect(t.Context(), serviceImage(spec.Name))
				if err != nil {
					t.Fatal(err)
				}
				if image.Os != "linux" || image.Architecture != arch {
					t.Fatalf("image platform %s/%s, want linux/%s", image.Os, image.Architecture, arch)
				}
			}
		})
	}
}

type composeLaunch struct {
	Entrypoint  []string
	Command     []string
	Init        bool
	Healthcheck struct{ Test []string }
}

func loadComposeLaunch(t *testing.T, service string) composeLaunch {
	t.Helper()
	specs := loadImageSpecs(t)
	index := slices.IndexFunc(specs, func(spec imageSpec) bool { return spec.Name == service })
	if index == -1 {
		t.Fatalf("service %s absent in versions.json", service)
	}
	spec := specs[index]
	location := filepath.Join("../..", spec.Source, spec.Compose)
	data, err := os.ReadFile(location)
	if err != nil {
		t.Fatal(err)
	}
	var compose struct{ Services map[string]composeLaunch }
	if err := yaml.Unmarshal(data, &compose); err != nil {
		t.Fatal(err)
	}
	launch, ok := compose.Services[spec.Target]
	if !ok {
		t.Fatalf("service %s absent in %s", spec.Target, location)
	}
	return launch
}

func TestServiceLaunchPolicy(t *testing.T) {
	cli, err := client.New(client.FromEnv)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = cli.Close() })
	for _, spec := range loadImageSpecs(t) {
		service := spec.Name
		if service == "cloak" {
			continue
		}
		t.Run(service, func(t *testing.T) {
			requireService(t, service)
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
