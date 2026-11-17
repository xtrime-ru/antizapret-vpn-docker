package integration

import (
	"bytes"
	"context"
	"encoding/json/v2"
	"errors"
	"fmt"
	"strings"
	"testing"
	"time"
	"uuid"

	"github.com/moby/moby/api/pkg/stdcopy"
	"github.com/moby/moby/api/types/container"
	"github.com/moby/moby/client"
)

type sentinelStatus struct {
	Phase string `json:"phase"`
	State string `json:"state"`
	Main  struct {
		PID int `json:"pid"`
	} `json:"main"`
	Tasks []struct {
		Name    string `json:"name"`
		Process *struct {
			PID int `json:"pid"`
		} `json:"process"`
	} `json:"tasks"`
	Daemon struct {
		PID int `json:"pid"`
	} `json:"daemon"`
}

type fixture struct {
	t   *testing.T
	cli *client.Client
	id  string
}

func newFixture(t *testing.T, service, script string, env ...string) *fixture {
	t.Helper()
	return fixtureWithHost(t, service, script, nil, env...)
}

func fixtureWithHost(t *testing.T, service, script string, host *container.HostConfig, env ...string) *fixture {
	t.Helper()
	requireService(t, service)
	if host == nil {
		host = &container.HostConfig{}
	}
	host.Init = new(true)
	if !strings.HasPrefix(string(host.NetworkMode), "container:") {
		host.ExtraHosts = append(host.ExtraHosts, "az-local.antizapret:192.0.2.10", "az-world.antizapret:192.0.2.10", "adguard.antizapret:192.0.2.30", "coredns.antizapret:192.0.2.20")
	}
	cfg := &container.Config{Image: serviceImage(service), Env: env}
	if script == "" {
		launch := loadComposeLaunch(t, service)
		cfg.Entrypoint, cfg.Cmd = launch.Entrypoint, launch.Command
	} else {
		cfg.Env = append(cfg.Env, "DOCKTINEL_CONFIG=/tmp/test-config.json")
		script = "printf '%s' '{\"tasks\":[]}' > /tmp/test-config.json\n" + script
		cfg.Entrypoint = []string{"bash", "-ec", script}
		cfg.Cmd = []string{}
	}
	return startContainer(t, cfg, host)
}

func startContainer(t *testing.T, cfg *container.Config, host *container.HostConfig) *fixture {
	t.Helper()
	cli, err := client.New(client.FromEnv)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = cli.Close() })
	ctx, cancel := context.WithTimeout(t.Context(), 30*time.Second)
	defer cancel()
	created, err := cli.ContainerCreate(ctx, client.ContainerCreateOptions{
		Name: "antizapret-integration-" + uuid.New().String(), Config: cfg,
		HostConfig: host,
	})
	if err != nil {
		t.Fatal(err)
	}
	f := &fixture{t: t, cli: cli, id: created.ID}
	t.Cleanup(f.cleanup)
	if _, err := cli.ContainerStart(ctx, f.id, client.ContainerStartOptions{}); err != nil {
		t.Fatal(err)
	}
	return f
}

func (f *fixture) cleanup() {
	// t.Context is canceled before cleanup; cleanup must have its own budget.
	ctx, cancel := context.WithTimeout(context.Background(), 45*time.Second)
	defer cancel()
	if f.t.Failed() {
		logs, err := f.cli.ContainerLogs(ctx, f.id, client.ContainerLogsOptions{
			ShowStdout: true, ShowStderr: true, Tail: "100",
		})
		if err == nil {
			var output bytes.Buffer
			_, _ = stdcopy.StdCopy(&output, &output, logs)
			_ = logs.Close()
			f.t.Logf("container %s logs:\n%s", f.id, &output)
		}
	}
	if _, err := f.cli.ContainerRemove(ctx, f.id, client.ContainerRemoveOptions{Force: true, RemoveVolumes: true}); err != nil {
		f.t.Errorf("remove test container %s: %v", f.id, err)
	}
}

func (f *fixture) exec(ctx context.Context, args ...string) (string, int, error) {
	created, err := f.cli.ExecCreate(ctx, f.id, client.ExecCreateOptions{
		Cmd: args, AttachStdout: true, AttachStderr: true,
	})
	if err != nil {
		return "", 0, err
	}
	stream, err := f.cli.ExecAttach(ctx, created.ID, client.ExecAttachOptions{})
	if err != nil {
		return "", 0, err
	}
	defer stream.Close()
	// Hijacked streams need an explicit close when the test/deadline expires.
	stop := context.AfterFunc(ctx, func() { stream.Close() })
	defer stop()
	var stdout, stderr bytes.Buffer
	if _, err := stdcopy.StdCopy(&stdout, &stderr, stream.Reader); err != nil {
		return stdout.String(), 0, err
	}
	for {
		state, err := f.cli.ExecInspect(ctx, created.ID, client.ExecInspectOptions{})
		if err != nil {
			return stdout.String(), 0, err
		}
		if !state.Running {
			if state.ExitCode != 0 {
				return stdout.String(), state.ExitCode, fmt.Errorf("exec %q exited %d: %s", args, state.ExitCode, &stderr)
			}
			return stdout.String(), 0, nil
		}
		select {
		case <-ctx.Done():
			return stdout.String(), 0, ctx.Err()
		case <-time.After(20 * time.Millisecond):
		}
	}
}

func (f *fixture) mustExec(args ...string) string {
	f.t.Helper()
	ctx, cancel := context.WithTimeout(f.t.Context(), 5*time.Second)
	defer cancel()
	output, _, err := f.exec(ctx, args...)
	if err != nil {
		f.t.Fatal(err)
	}
	return output
}

func (f *fixture) status(ctx context.Context) (sentinelStatus, error) {
	output, _, err := f.exec(ctx, "docktinel", "status", "--json")
	if err != nil {
		return sentinelStatus{}, err
	}
	var s sentinelStatus
	err = json.Unmarshal([]byte(output), &s)
	return s, err
}

// eventually returns the typed successful observation, retaining the last error
// for diagnostics. Every poll shares the same bounded context.
func eventually[T any](t *testing.T, observe func(context.Context) (T, error), ready func(T) bool) T {
	t.Helper()
	ctx, cancel := context.WithTimeout(t.Context(), 45*time.Second)
	defer cancel()
	ticker := time.NewTicker(100 * time.Millisecond)
	defer ticker.Stop()
	var value T
	var err error
	for {
		value, err = observe(ctx)
		if err == nil && ready(value) {
			return value
		}
		select {
		case <-ctx.Done():
			t.Fatalf("condition timed out: last value=%+v, last error=%v", value, err)
			return value
		case <-ticker.C:
		}
	}
}

func (f *fixture) waitState(state string) {
	f.t.Helper()
	eventually(f.t, f.status, func(s sentinelStatus) bool { return s.State == state })
}

func (f *fixture) runtime() sentinelStatus {
	f.t.Helper()
	state := eventually(f.t, f.status, func(s sentinelStatus) bool { return s.Phase == "runtime" })
	// The live HTTP API can lead the independent published health snapshot.
	eventually(f.t, func(ctx context.Context) (bool, error) {
		_, code, err := f.exec(ctx, "docktinel", "healthcheck", "--no-kill")
		if code != 0 {
			return false, nil
		}
		return err == nil, err
	}, func(healthy bool) bool { return healthy })
	return state
}

func (f *fixture) waitExit() int64 {
	f.t.Helper()
	ctx, cancel := context.WithTimeout(f.t.Context(), 45*time.Second)
	defer cancel()
	wait := f.cli.ContainerWait(ctx, f.id, client.ContainerWaitOptions{Condition: container.WaitConditionNotRunning})
	select {
	case result := <-wait.Result:
		if result.Error != nil {
			f.t.Fatal(result.Error.Message)
		}
		return result.StatusCode
	case err := <-wait.Error:
		f.t.Fatal(err)
	case <-ctx.Done():
		f.t.Fatal(ctx.Err())
	}
	return 0
}

func (f *fixture) expectUnhealthy(args ...string) {
	f.t.Helper()
	ctx, cancel := context.WithTimeout(f.t.Context(), 5*time.Second)
	defer cancel()
	_, code, err := f.exec(ctx, append([]string{"docktinel", "healthcheck"}, args...)...)
	if err == nil && code != 0 {
		return
	}
	// If healthcheck terminates the main process, Docker may close the exec
	// stream before it can report a code. Require container exit in that case.
	if err != nil && !errors.Is(err, context.DeadlineExceeded) {
		f.waitExit()
		return
	}
	f.t.Fatalf("expected healthcheck failure: exit=%d, error=%v", code, err)
}
