package main

import (
	"fmt"
	"io"
	"os"
	"regexp"
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

	// Marshal back to YAML
	preprocessed, err := yaml.Marshal(compose)
	if err != nil {
		fmt.Fprintf(os.Stderr, "Error marshaling preprocessed YAML: %v\n", err)
		os.Exit(1)
	}

	// Parse Compose YAML
	project, err := loader.Load(types.ConfigDetails{
		WorkingDir:  ".",
		ConfigFiles: []types.ConfigFile{{Content: preprocessed}},
	})
	if err != nil {
		fmt.Fprintf(os.Stderr, "Error parsing Compose YAML: %v\n", err)
		os.Exit(1)
	}

	for name, network := range project.Networks {
		if network.Driver != "overlay" {
			network.Driver = "overlay"
			project.Networks[name] = network
		}
	}

	// Remove unsupported fields for Swarm
	for name, service := range project.Services {
		service.Build = nil        // Swarm does not build images
		service.DependsOn = nil    // Swarm ignores depends_on
		service.Privileged = false // Swarm does not support privileged mode
		service.Devices = nil // Swarm does not support devices

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

	fmt.Println(string(fixed))
}
