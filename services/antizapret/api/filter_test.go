package main

import (
	"fmt"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"sync"
	"testing"
	"time"
)

func requireGrep(t *testing.T) {
	t.Helper()
	// Patterns are passed through /dev/fd/3, which needs Linux.
	if runtime.GOOS != "linux" {
		t.Skip("requires Linux")
	}
	if _, err := exec.LookPath("grep"); err != nil {
		t.Skip("requires grep")
	}
}

func writeExclude(t *testing.T, contents string) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), "exclude")
	if err := os.WriteFile(path, []byte(contents), 0600); err != nil {
		t.Fatal(err)
	}
	return path
}

func newFilter(t *testing.T, contents string) *RegexFilter {
	t.Helper()
	rf, err := NewRegexFilter(writeExclude(t, contents))
	if err != nil {
		t.Fatal(err)
	}
	return rf
}

func useFilters(t *testing.T, dist, custom *RegexFilter) {
	t.Helper()
	oldDist, oldCustom := excludeMatcherDist.Load(), excludeMatcherCustom.Load()
	excludeMatcherDist.Store(dist)
	excludeMatcherCustom.Store(custom)
	t.Cleanup(func() { excludeMatcherDist.Store(oldDist); excludeMatcherCustom.Store(oldCustom) })
}

// filterList runs lines through /list/ with raw output and the custom filter.
func filterList(t *testing.T, exclude string, lines []string) (int, string) {
	t.Helper()
	useFilters(t, &RegexFilter{}, newFilter(t, exclude))
	root := useListRoot(t)
	path := filepath.Join(root, "list")
	if err := os.WriteFile(path, []byte(strings.Join(lines, "\n")+"\n"), 0600); err != nil {
		t.Fatal(err)
	}
	request := httptest.NewRequest("GET", "/list/?raw=1&filter_custom=1&file="+url.QueryEscape(path), nil)
	response := httptest.NewRecorder()
	done := make(chan struct{})
	go func() { adaptList(response, request); close(done) }()
	select {
	case <-done:
	case <-time.After(10 * time.Second):
		t.Fatal("adaptList did not finish")
	}
	return response.Code, response.Body.String()
}

func TestFilterLargeList(t *testing.T) {
	requireGrep(t)
	lines := make([]string, 20000)
	for i := range lines {
		lines[i] = fmt.Sprintf("host%d.example.org", i)
	}
	code, body := filterList(t, "^host1[0-9]*\\.example\\.org$\n", lines)
	var want []string
	for _, line := range lines {
		if !strings.HasPrefix(line, "host1") {
			want = append(want, line)
		}
	}
	if code != http.StatusOK || body != strings.Join(want, "\n")+"\n" {
		t.Fatalf("status = %d, got %d bytes, want %d lines", code, len(body), len(want))
	}
}

// The persistent grep hung forever when a pattern matched its delimiter.
func TestFilterPatternsMatchingFormerDelimiter(t *testing.T) {
	requireGrep(t)
	code, body := filterList(t, "[A-Z]\n_\n", []string{"__DELIM__", "example.org", "Upper.org"})
	if code != http.StatusOK || body != "example.org\n" {
		t.Fatalf("status = %d, body = %q", code, body)
	}
}

// An empty pattern matches every line, so blank lines must never become one.
func TestFilterIgnoresBlankLinesAndComments(t *testing.T) {
	requireGrep(t)
	code, body := filterList(t, "\r\n   \n# comment\nexample\\.com  \r\n\n",
		[]string{"example.com", "keep.org"})
	if code != http.StatusOK || body != "keep.org\n" {
		t.Fatalf("status = %d, body = %q", code, body)
	}
}

func TestFilterEmptyFileKeepsEverything(t *testing.T) {
	rf, err := NewRegexFilter(writeExclude(t, "\n# only a comment\n"))
	if err != nil || rf.count != 0 || len(rf.patterns) != 0 {
		t.Fatalf("filter = %+v, err = %v", rf, err)
	}
}

func TestFilterSkipsInvalidPatterns(t *testing.T) {
	requireGrep(t)
	code, body := filterList(t, "(unclosed\nbad\\.org\n", []string{"bad.org", "(unclosed", "good.org"})
	if code != http.StatusOK || body != "(unclosed\ngood.org\n" {
		t.Fatalf("status = %d, body = %q", code, body)
	}
}

func TestFilterSlashWrappedAndGNUWordBoundary(t *testing.T) {
	requireGrep(t)
	code, body := filterList(t, "/foo[0-9]+\\.net/\n\\<vk\\>\n",
		[]string{"foo12.net", "vk.com", "vkontakte.ru", "other.net"})
	if code != http.StatusOK || body != "vkontakte.ru\nother.net\n" {
		t.Fatalf("status = %d, body = %q", code, body)
	}
}

func TestFilterExcludingEverythingIsSuccessful(t *testing.T) {
	requireGrep(t)
	code, body := filterList(t, ".*\n", []string{"a.org", "b.org"})
	if code != http.StatusOK || body != "" {
		t.Fatalf("status = %d, body = %q", code, body)
	}
}

func TestFilterDoesNotModifySourceFile(t *testing.T) {
	requireGrep(t)
	contents := "a  \r\n\n# c\n"
	path := writeExclude(t, contents)
	if _, err := NewRegexFilter(path); err != nil {
		t.Fatal(err)
	}
	data, err := os.ReadFile(path)
	if err != nil || string(data) != contents {
		t.Fatalf("file changed: %q, %v", data, err)
	}
}

func TestUpdateRegexFilterKeepsPreviousOnError(t *testing.T) {
	requireGrep(t)
	previous := newFilter(t, "old\n")
	useFilters(t, previous, previous)
	oldDistPath, oldCustomPath := excludeDistPath, excludeCustomPath
	t.Cleanup(func() { excludeDistPath, excludeCustomPath = oldDistPath, oldCustomPath })
	excludeDistPath = writeExclude(t, "new\n")
	excludeCustomPath = filepath.Join(t.TempDir(), "missing")

	if err := updateRegexFilter(); err == nil {
		t.Fatal("expected an error for a missing custom file")
	}
	if excludeMatcherDist.Load() != previous || excludeMatcherCustom.Load() != previous {
		t.Fatal("filters were replaced after a failed update")
	}
}

func TestAdaptListFiltersDistAndCustom(t *testing.T) {
	requireGrep(t)
	useFilters(t, newFilter(t, "^dist\\.org$\n"), newFilter(t, "custom\n"))
	root := useListRoot(t)
	path := filepath.Join(root, "list")
	if err := os.WriteFile(path, []byte("dist.org\ncustom.org\nexample.org\n"), 0600); err != nil {
		t.Fatal(err)
	}
	for _, query := range []string{"filter_dist=1&filter_custom=1", "filter_dist=0&filter_custom=0"} {
		request := httptest.NewRequest("GET", "/list/?raw=1&"+query+"&file="+url.QueryEscape(path), nil)
		response := httptest.NewRecorder()
		adaptList(response, request)
		want := "example.org\n"
		if query == "filter_dist=0&filter_custom=0" {
			want = "dist.org\ncustom.org\nexample.org\n"
		}
		if response.Code != http.StatusOK || response.Body.String() != want {
			t.Fatalf("%s: status = %d, body = %q", query, response.Code, response.Body.String())
		}
	}
}

func TestAdaptListConcurrentRequestsAreIndependent(t *testing.T) {
	requireGrep(t)
	useFilters(t, &RegexFilter{}, newFilter(t, "^drop\n"))
	root := useListRoot(t)
	var wg sync.WaitGroup
	errs := make(chan error, 8)
	for i := 0; i < 8; i++ {
		path := filepath.Join(root, fmt.Sprintf("list%d", i))
		var lines []string
		for j := 0; j < 2000; j++ {
			lines = append(lines, fmt.Sprintf("keep%d-%d.org", i, j), fmt.Sprintf("drop%d-%d.org", i, j))
		}
		if err := os.WriteFile(path, []byte(strings.Join(lines, "\n")), 0600); err != nil {
			t.Fatal(err)
		}
		wg.Add(1)
		go func(i int, path string) {
			defer wg.Done()
			request := httptest.NewRequest("GET", "/list/?raw=1&file="+url.QueryEscape(path), nil)
			response := httptest.NewRecorder()
			adaptList(response, request)
			body := response.Body.String()
			if strings.Count(body, "\n") != 2000 || strings.Contains(body, "drop") ||
				!strings.HasPrefix(body, fmt.Sprintf("keep%d-0.org\n", i)) {
				errs <- fmt.Errorf("request %d: %d lines", i, strings.Count(body, "\n"))
			}
		}(i, path)
	}
	wg.Wait()
	close(errs)
	for err := range errs {
		t.Error(err)
	}
}

// A truncated source must drop the connection, not look like a complete list.
func TestAdaptListAbortsOnTruncatedSource(t *testing.T) {
	requireGrep(t)
	useFilters(t, &RegexFilter{}, newFilter(t, "^drop\n"))
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Length", "1000")
		_, _ = w.Write([]byte("a.org\nb.org\n"))
	}))
	defer server.Close()

	request := httptest.NewRequest("GET", "/list/?raw=1&url="+url.QueryEscape(server.URL), nil)
	response := httptest.NewRecorder()
	defer func() {
		if recovered := recover(); recovered != http.ErrAbortHandler {
			t.Fatalf("recovered %v, want http.ErrAbortHandler", recovered)
		}
	}()
	adaptList(response, request)
	t.Fatal("adaptList returned normally for a truncated source")
}

// A missing filter must be an error status, never a 200 with a partial list.
func TestAdaptListFailsBeforeStreamingWithoutFilter(t *testing.T) {
	useFilters(t, nil, nil)
	root := useListRoot(t)
	path := filepath.Join(root, "list")
	if err := os.WriteFile(path, []byte("example.org\n"), 0600); err != nil {
		t.Fatal(err)
	}
	request := httptest.NewRequest("GET", "/list/?raw=1&filter_custom=1&file="+url.QueryEscape(path), nil)
	response := httptest.NewRecorder()
	adaptList(response, request)
	if response.Code != http.StatusServiceUnavailable {
		t.Fatalf("status = %d, body = %q", response.Code, response.Body.String())
	}
}
