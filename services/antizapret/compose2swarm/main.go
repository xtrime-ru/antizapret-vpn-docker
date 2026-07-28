package main

import (
	"fmt"
	"io"
	"os"
	"regexp"
	"strings"
	"time"

	"github.com/compose-spec/compose-go/loader"
	"github.com/compose-spec/compose-go/types"
	"gopkg.in/yaml.v3"
)

func main() {
	// Read docker-compose.yml from stdin
	data, err := io.ReadAll(os.Stdin)
	if err != nil {
		fmt.Fprintf(os.Stderr, "Error reading stdin: %v\n", err)
		os.Exit(1)
	}

	// Unmarshal YAML to generic map
	var compose map[string]interface{}
	if err := yaml.Unmarshal(data, &compose); err != nil {
		fmt.Fprintf(os.Stderr, "Error unmarshaling YAML: %v\n", err)
		os.Exit(1)
	}

	// Find services and process devices
	services, ok := compose["services"].(map[string]interface{})
	if ok {
		for _, svc := range services {
			serviceMap, ok := svc.(map[string]interface{})
			if !ok {
				continue
			}
			devices, ok := serviceMap["devices"].([]interface{})
			if !ok {
				continue
			}
			var newDevices []interface{}
			for _, dev := range devices {
				devMap, ok := dev.(map[string]interface{})
				if ok {
					src, _ := devMap["source"].(string)
					tgt, _ := devMap["target"].(string)
					perm, _ := devMap["permissions"].(string)
					newDevices = append(newDevices, fmt.Sprintf("%s:%s:%s", src, tgt, perm))
				} else if str, ok := dev.(string); ok {
					newDevices = append(newDevices, str)
				}
			}
			serviceMap["devices"] = newDevices
		}
	}

	// Parse Compose YAML
	project, err := loader.Load(types.ConfigDetails{
		WorkingDir:  ".",
		ConfigFiles: []types.ConfigFile{{Config: compose}},
	})
	if err != nil {
		fmt.Fprintf(os.Stderr, "Error parsing Compose YAML: %v\n", err)
		os.Exit(1)
	}

	for key, network := range project.Networks {
		if network.Driver == "bridge" {
			network.Driver = "overlay"
			project.Networks[key] = network
		}
	}
	if _, exists := project.Networks["host"]; !exists {
		project.Networks["host"] = types.NetworkConfig{Name: "host", External: types.External{External: true}}
	}

	// Remove unsupported fields for Swarm
	for name, service := range project.Services {
		service.Build = nil     // Swarm does not build images
		service.DependsOn = nil // Swarm ignores depends_on
		service.Devices = nil   // Swarm does not support devices

		// Convert restart to deploy.restart_policy
		if service.Restart != "" {
			var condition string
			switch service.Restart {
			case "no":
				condition = "none"
			case "always", "unless-stopped":
				condition = "any"
			case "on-failure":
				condition = "on-failure"
			default:
				fmt.Fprintf(os.Stderr, "Unknown restart value: %s in service %s\n", service.Restart, service.Name)
				os.Exit(1)
			}
			service.Restart = "" // clear old field
			if service.Deploy == nil {
				service.Deploy = &types.DeployConfig{}
			}
			var maxAttempts uint64 = 10
			var delay types.Duration = types.Duration(1 * time.Second)
			var window types.Duration = types.Duration(5 * time.Second)
			service.Deploy.RestartPolicy = &types.RestartPolicy{
				Condition:   condition,
				MaxAttempts: &maxAttempts,
				Delay:       &delay,
				Window:      &window,
			}
		}

		if service.NetworkMode == "host" {
			service.NetworkMode = ""
			service.Networks = make(map[string]*types.ServiceNetworkConfig)
			service.Networks["host"] = nil

		} else if service.NetworkMode != "" {
			panic(fmt.Sprintf("Unsupported network_mode: %s in service %s", service.NetworkMode, service.Name))
		}

		if len(service.ExtraHosts) > 0 {
			newExtraHosts := make(map[string]string, len(service.ExtraHosts))

			for host, val := range service.ExtraHosts {
				if val == "" && strings.Contains(host, "=") {
					parts := strings.SplitN(host, "=", 2)
					if len(parts) == 2 {
						newExtraHosts[parts[0]] = parts[1]
					}
				}
			}

			service.ExtraHosts = newExtraHosts
		}

		project.Services[name] = service
	}

	// Marshal back to YAML
	out, err := yaml.Marshal(project)
	if err != nil {
		fmt.Fprintf(os.Stderr, "Error marshaling YAML: %v\n", err)
		os.Exit(1)
	}

	// Regex: match published: "123" or published: '123'
	ports := regexp.MustCompile(`(?m)published: "(\d+)"`)
	name := regexp.MustCompile(`(?m)^name:.*\n`)

	// Replace with integer
	fixed := ports.ReplaceAllString(string(out), "published: $1")
	fixed = name.ReplaceAllString(fixed, "")
	fixed = strings.ReplaceAll(fixed, "mode: ingress", "mode: host")

	fmt.Println(string(fixed))
}
