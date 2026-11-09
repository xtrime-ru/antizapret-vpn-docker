package main

import (
	"bufio"
	"crypto/sha256"
	"fmt"
	"hash"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
)

var benchmarkLine string

func BenchmarkPlainListLine(b *testing.B) {
	for _, line := range []string{"example.com", "example.com @cn # extra annotation", "example.com\t@cn\textra\tannotation"} {
		b.Run(line, func(b *testing.B) {
			b.ReportAllocs()
			for b.Loop() {
				benchmarkLine = plainListLine(line)
			}
		})
	}
}

type discardListResponse struct {
	header        http.Header
	status, bytes int
}

func (w *discardListResponse) Header() http.Header    { return w.header }
func (w *discardListResponse) WriteHeader(status int) { w.status = status }
func (w *discardListResponse) Write(data []byte) (int, error) {
	w.bytes += len(data)
	return len(data), nil
}
func (w *discardListResponse) Flush() {}

// BENCH_LIST and BENCH_EXCLUDE point to local copies so downloads and DNS do
// not obscure parser, exclusion-filter and rendering costs.
func useLargeList(b testing.TB) (*http.Request, string, string) {
	b.Helper()
	path, exclude := os.Getenv("BENCH_LIST"), os.Getenv("BENCH_EXCLUDE")
	if path == "" || exclude == "" {
		b.Skip("set BENCH_LIST and BENCH_EXCLUDE")
	}
	data, err := os.ReadFile(exclude)
	if err != nil {
		b.Fatal(err)
	}
	patterns := filepath.Join(b.TempDir(), "exclude")
	if err := os.WriteFile(patterns, data, 0600); err != nil {
		b.Fatal(err)
	}
	matcher, err := NewRegexFilter(patterns)
	if err != nil {
		b.Fatal(err)
	}
	customPath := patterns + ".custom"
	if err := os.WriteFile(customPath, nil, 0600); err != nil {
		b.Fatal(err)
	}
	custom, err := NewRegexFilter(customPath)
	if err != nil {
		b.Fatal(err)
	}
	oldRoot, oldMatcher, oldCustom := allowedListRoot, excludeMatcherDist, excludeMatcherCustom
	allowedListRoot, excludeMatcherDist, excludeMatcherCustom = filepath.Dir(path), matcher, custom
	b.Cleanup(func() {
		allowedListRoot, excludeMatcherDist, excludeMatcherCustom = oldRoot, oldMatcher, oldCustom
		matcher.Close()
		custom.Close()
	})
	request := httptest.NewRequest("GET", "/list/?filter_dist=1&filter_custom=0&client=az-local&file="+url.QueryEscape(path), nil)
	return request, path, patterns
}

func BenchmarkAdaptLargeList(b *testing.B)            { benchmarkAdaptLargeList(b, false) }
func BenchmarkAdaptLargeListEmptyCustom(b *testing.B) { benchmarkAdaptLargeList(b, true) }

func benchmarkAdaptLargeList(b *testing.B, custom bool) {
	request, path, _ := useLargeList(b)
	if custom {
		query := request.URL.Query()
		query.Del("filter_custom")
		request.URL.RawQuery = query.Encode()
	}

	info, err := os.Stat(path)
	if err != nil {
		b.Fatal(err)
	}
	b.SetBytes(info.Size())
	b.ReportAllocs()
	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		response := &discardListResponse{header: make(http.Header)}
		adaptList(response, request)
		if response.status != 0 && response.status != 200 {
			b.Fatalf("status=%d", response.status)
		}
		if response.bytes == 0 {
			b.Fatal("empty response")
		}
	}
}

func BenchmarkLargeListDomainExtraction(b *testing.B) {
	path := os.Getenv("BENCH_LIST")
	if path == "" {
		b.Skip("set BENCH_LIST")
	}
	data, err := os.ReadFile(path)
	if err != nil {
		b.Fatal(err)
	}
	lines := strings.Split(strings.TrimSpace(string(data)), "\n")
	b.SetBytes(int64(len(data)))
	b.ReportAllocs()
	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		for _, line := range lines {
			benchmarkLine = plainListLine(line)
		}
	}
}

type checksumListResponse struct {
	discardListResponse
	digest hash.Hash
}

func (w *checksumListResponse) Write(data []byte) (int, error) { return w.digest.Write(data) }

// Verify all output, including batch boundaries and in-place filtering, against
// an independent awk/grep pipeline (domain extraction before exclusions).
func TestLargeListOutputMatchesGrep(t *testing.T) {
	request, path, patterns := useLargeList(t)
	response := &checksumListResponse{discardListResponse: discardListResponse{header: make(http.Header)}, digest: sha256.New()}
	adaptList(response, request)
	normalizer := exec.Command("awk", "{print $1}", path)
	normalized, err := normalizer.StdoutPipe()
	if err != nil {
		t.Fatal(err)
	}
	command := exec.Command("grep", "-a", "-v", "-E", "-f", patterns)
	command.Stdin = normalized
	stdout, err := command.StdoutPipe()
	if err != nil {
		t.Fatal(err)
	}
	if err := command.Start(); err != nil {
		t.Fatal(err)
	}
	if err := normalizer.Start(); err != nil {
		t.Fatal(err)
	}
	expected := sha256.New()
	scanner := bufio.NewScanner(stdout)
	for scanner.Scan() {
		fmt.Fprintf(expected, "@@||%s^$dnsrewrite,client=az-local\n", scanner.Text())
	}
	if err := scanner.Err(); err != nil {
		t.Fatal(err)
	}
	if err := command.Wait(); err != nil {
		t.Fatal(err)
	}
	if err := normalizer.Wait(); err != nil {
		t.Fatal(err)
	}
	actualHash, expectedHash := fmt.Sprintf("%x", response.digest.Sum(nil)), fmt.Sprintf("%x", expected.Sum(nil))
	if actualHash != expectedHash {
		t.Fatalf("output hash=%s, want %s", actualHash, expectedHash)
	}
	t.Logf("complete output SHA-256: %s", actualHash)
}
