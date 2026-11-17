package integration

import (
	"testing"

	"github.com/moby/moby/api/types/container"
	"gopkg.in/yaml.v3"
)

func TestAntizapretUtilitiesDoNotStartWatchdog(t *testing.T) {
	requireService(t, "antizapret")
	// Intentionally omit Docker init: one-shot CLI tools must not need it.
	f := startContainer(t, &container.Config{
		Image: serviceImage("antizapret"),
		Cmd: []string{"sh", "-ec", `
printf 'services:\n  example:\n    image: alpine:3.22\n' | compose2swarm > /tmp/stack.yml 2>/tmp/stderr
test ! -e /run/docktinel/control.sock
! grep -q "docktinel:" /tmp/stderr || exit 1
grep -q "services:" /tmp/stack.yml
`},
	}, nil)
	if code := f.waitExit(); code != 0 {
		t.Fatalf("utility exited %d", code)
	}
}

func TestAntizapretSwarmRestartPolicy(t *testing.T) {
	f := newFixture(t, "antizapret", "exec sleep infinity")
	output := f.mustExec("sh", "-ec", `
compose2swarm <<'YAML'
services:
  default:
    image: alpine
    restart: unless-stopped
  explicit:
    image: alpine
    restart: unless-stopped
    deploy:
      restart_policy:
        condition: on-failure
        delay: 30s
        max_attempts: 2
        window: 1m
YAML
`)
	type restartPolicy struct {
		Condition   string
		Delay       string
		MaxAttempts *int `yaml:"max_attempts"`
		Window      string
	}
	var stack struct {
		Services map[string]struct {
			Restart string
			Deploy  struct {
				Policy restartPolicy `yaml:"restart_policy"`
			}
		}
	}
	if err := yaml.Unmarshal([]byte(output), &stack); err != nil {
		t.Fatal(err)
	}
	for name, service := range stack.Services {
		if service.Restart != "" {
			t.Errorf("%s retained Compose restart: %s", name, service.Restart)
		}
	}
	defaultPolicy := stack.Services["default"].Deploy.Policy
	if defaultPolicy.Condition != "any" || defaultPolicy.Delay != "1s" || defaultPolicy.MaxAttempts != nil {
		t.Errorf("default policy = %+v; want any, 1s, unlimited attempts", defaultPolicy)
	}
	explicit := stack.Services["explicit"].Deploy.Policy
	if explicit.Condition != "on-failure" || explicit.Delay != "30s" ||
		explicit.MaxAttempts == nil || *explicit.MaxAttempts != 2 || explicit.Window != "1m0s" {
		t.Errorf("explicit policy changed: %+v", explicit)
	}
}
