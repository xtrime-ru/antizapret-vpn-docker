package main

import (
	"fmt"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func useExclusions(t *testing.T, pattern string) {
	t.Helper()
	path := filepath.Join(t.TempDir(), "exclude")
	if err := os.WriteFile(path, []byte(pattern+"\n"), 0600); err != nil {
		t.Fatal(err)
	}
	filter, err := NewRegexFilter(path)
	if err != nil {
		t.Fatal(err)
	}
	old := excludeMatcherCustom
	excludeMatcherCustom = filter
	t.Cleanup(func() { excludeMatcherCustom = old; _ = filter.Close() })
}

func TestListExcludesMatchingLines(t *testing.T) {
	domain := strings.Repeat("label.", 30) + "example.org\n"
	keep, drop, upper := "keep."+domain, "drop."+domain, "UPPER."+domain
	input := strings.Repeat(keep+drop+upper, 2000)
	path := filepath.Join(useListRoot(t), "list")
	if err := os.WriteFile(path, []byte(input), 0600); err != nil {
		t.Fatal(err)
	}
	for _, tc := range []struct{ pattern, want string }{
		{".*", ""}, {"[A-Z]|_", strings.Repeat(keep+drop, 2000)},
		{"^drop", strings.Repeat(keep+upper, 2000)}, {"", input},
	} {
		t.Run(tc.pattern, func(t *testing.T) {
			useExclusions(t, tc.pattern)
			response := httptest.NewRecorder()
			adaptList(response, httptest.NewRequest("GET", "/list/?raw=1&file="+url.QueryEscape(path), nil))
			if response.Code != http.StatusOK || response.Body.String() != tc.want {
				t.Fatalf("status=%d, got %d bytes, want %d", response.Code, response.Body.Len(), len(tc.want))
			}
		})
	}
}

func TestListSlowSourceDoesNotBlockAnotherRequest(t *testing.T) {
	useExclusions(t, "^drop")
	started, release := make(chan struct{}), make(chan struct{})
	source := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		_, _ = w.Write([]byte("drop.org\nkeep.org\n"))
		w.(http.Flusher).Flush()
		close(started)
		<-release
	}))
	defer source.Close()
	first := httptest.NewRecorder()
	firstDone := make(chan struct{})
	go func() {
		adaptList(first, httptest.NewRequest("GET", "/list/?raw=1&url="+url.QueryEscape(source.URL), nil))
		close(firstDone)
	}()
	var secondDone chan struct{}
	defer func() {
		close(release)
		<-firstDone
		if secondDone != nil {
			<-secondDone
		}
		if first.Code != http.StatusOK || first.Body.String() != "keep.org\n" {
			t.Errorf("first response: %d %q", first.Code, first.Body.String())
		}
	}()
	<-started
	path := filepath.Join(useListRoot(t), "list")
	if err := os.WriteFile(path, []byte("drop.org\nother.org\n"), 0600); err != nil {
		t.Fatal(err)
	}
	second := httptest.NewRecorder()
	secondDone = make(chan struct{})
	go func() {
		adaptList(second, httptest.NewRequest("GET", "/list/?raw=1&file="+url.QueryEscape(path), nil))
		close(secondDone)
	}()
	select {
	case <-secondDone:
		if second.Code != http.StatusOK || second.Body.String() != "other.org\n" {
			t.Fatalf("second response: %d %q", second.Code, second.Body.String())
		}
	case <-time.After(time.Second):
		t.Fatal("second request waited for the slow source")
	}
}

func TestEmptyFilterDoesNotStartGrep(t *testing.T) {
	path := filepath.Join(t.TempDir(), "exclude")
	if err := os.WriteFile(path, []byte(" \n\t\n"), 0600); err != nil {
		t.Fatal(err)
	}
	filter, err := NewRegexFilter(path)
	if err != nil {
		t.Fatal(err)
	}
	defer filter.Close()
	input := []listRule{{value: "example.org", pattern: "|example.org|"}}
	output, err := filter.Filter(input)
	if err != nil || len(output) != 1 || output[0] != input[0] {
		t.Fatalf("output=%v err=%v", output, err)
	}
	if filter.cmd != nil {
		t.Fatal("empty filter started grep")
	}
	if _, err := filter.Filter([]listRule{{value: "embedded\nnewline"}}); err == nil {
		t.Fatal("reserved separator was accepted")
	}
}

func TestFilterReusesBuffersAcrossConcurrentBatches(t *testing.T) {
	path := filepath.Join(t.TempDir(), "exclude")
	if err := os.WriteFile(path, []byte("^drop\\.\n"), 0600); err != nil {
		t.Fatal(err)
	}
	filter, err := NewRegexFilter(path)
	if err != nil {
		t.Fatal(err)
	}
	defer filter.Close()
	errors := make(chan error, 8)
	for worker := 0; worker < 8; worker++ {
		go func() {
			for batch := 0; batch < 4; batch++ {
				keep, drop := fmt.Sprintf("keep.%d.%d.example", worker, batch), fmt.Sprintf("drop.%d.%d.example", worker, batch)
				input := []listRule{{value: keep, pattern: "|" + keep + "|"}, {value: drop}, {value: keep}}
				output, err := filter.Filter(input)
				if err != nil {
					errors <- err
					return
				}
				if len(output) != 2 || output[0].value != keep || output[1].value != keep || output[0].pattern != "|"+keep+"|" {
					errors <- fmt.Errorf("unexpected batch output: %v", output)
					return
				}
			}
			errors <- nil
		}()
	}
	for worker := 0; worker < 8; worker++ {
		if err := <-errors; err != nil {
			t.Error(err)
		}
	}
}
