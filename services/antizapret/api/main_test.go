package main

import (
	"io"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func useListRoot(t *testing.T) string {
	t.Helper()
	oldRoot := allowedListRoot
	allowedListRoot = t.TempDir()
	t.Cleanup(func() { allowedListRoot = oldRoot })
	return allowedListRoot
}

func TestOpenAllowedListFile(t *testing.T) {
	root := useListRoot(t)
	allowedPath := filepath.Join(root, "list.txt")
	if err := os.WriteFile(allowedPath, []byte("example.com\n"), 0o644); err != nil {
		t.Fatal(err)
	}

	file, err := openAllowedListFile(allowedPath)
	if err != nil {
		t.Fatalf("expected allowed file to open: %v", err)
	}
	contents, err := io.ReadAll(file)
	_ = file.Close()
	if err != nil {
		t.Fatal(err)
	}
	if string(contents) != "example.com\n" {
		t.Fatalf("unexpected contents: %q", contents)
	}
}

func TestOpenAllowedListFileRejectsOutsideRelativeAndSymlinkPaths(t *testing.T) {
	root := useListRoot(t)
	outsidePath := filepath.Join(filepath.Dir(root), "secret.txt")
	if err := os.WriteFile(outsidePath, []byte("secret"), 0o600); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = os.Remove(outsidePath) })

	if _, err := openAllowedListFile(outsidePath); err == nil {
		t.Fatal("expected an outside path to be rejected")
	}
	if _, err := openAllowedListFile(root + "/directory/../list.txt"); err == nil {
		t.Fatal("expected a path containing a relative component to be rejected")
	}

	symlinkPath := filepath.Join(root, "escape.txt")
	if err := os.Symlink(outsidePath, symlinkPath); err != nil {
		t.Fatal(err)
	}
	if _, err := openAllowedListFile(symlinkPath); err == nil {
		t.Fatal("expected a symlink escaping the allowed root to be rejected")
	}
}

func TestAdaptListRejectsFileOutsideAllowedRoots(t *testing.T) {
	request := httptest.NewRequest(http.MethodGet, "/list/?raw=1&filter_custom=0&file=/etc/hostname", nil)
	response := httptest.NewRecorder()

	adaptList(response, request)

	if response.Code != http.StatusForbidden {
		t.Fatalf("status = %d, want %d; body: %s", response.Code, http.StatusForbidden, response.Body)
	}
}

func TestAdaptListAllowsIPURLWithCredentials(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, request *http.Request) {
		username, password, ok := request.BasicAuth()
		if !ok || username != "user" || password != "password" {
			http.Error(w, "missing credentials", http.StatusUnauthorized)
			return
		}
		_, _ = io.WriteString(w, "example.com\n")
	}))
	defer server.Close()

	remoteURL, err := url.Parse(server.URL)
	if err != nil {
		t.Fatal(err)
	}
	remoteURL.User = url.UserPassword("user", "password")

	request := httptest.NewRequest(http.MethodGet, "/list/?raw=1&filter_custom=0&url="+url.QueryEscape(remoteURL.String()), nil)
	response := httptest.NewRecorder()

	adaptList(response, request)

	if response.Code != http.StatusOK || response.Body.String() != "example.com\n" {
		t.Fatalf("status = %d, body = %q", response.Code, response.Body.String())
	}
}

func TestAdaptListConvertsExcludePatternsToResolverServfailRules(t *testing.T) {
	root := useListRoot(t)
	listPath := filepath.Join(root, "exclude-hosts-custom.txt")
	contents := "example\\.com\n/foo[0-9]+\\.net/\npath/segment\n# comment\n"
	if err := os.WriteFile(listPath, []byte(contents), 0o644); err != nil {
		t.Fatal(err)
	}

	requestURL := "/list/?regex=1&allow=0&client=az-resolver&filter_custom=0&filter_dist=0&file=" + url.QueryEscape(listPath)
	request := httptest.NewRequest(http.MethodGet, requestURL, nil)
	response := httptest.NewRecorder()

	adaptList(response, request)

	want := "/example\\.com/$dnsrewrite=SERVFAIL,client=az-resolver\n" +
		"/foo[0-9]+\\.net/$dnsrewrite=SERVFAIL,client=az-resolver\n" +
		"/path\\/segment/$dnsrewrite=SERVFAIL,client=az-resolver\n" +
		"# comment\n"
	if response.Code != http.StatusOK || response.Body.String() != want {
		t.Fatalf("status = %d, body = %q, want %q", response.Code, response.Body.String(), want)
	}
}

func TestAdaptListIPv6EgressLocalAllowRule(t *testing.T) {
	root := useListRoot(t)
	listPath := filepath.Join(root, "ipv6-egress-domains-local.txt")
	if err := os.WriteFile(listPath, []byte("example.com\napi64.ipify.org\n# comment\n\n"), 0o644); err != nil {
		t.Fatal(err)
	}

	requestURL := "/list/?allow=1&client=az-ipv6-local&filter_custom=0&filter_dist=0&file=" + url.QueryEscape(listPath) + "&dnsrewrite=SERVFAIL&suffix=1"
	request := httptest.NewRequest(http.MethodGet, requestURL, nil)
	response := httptest.NewRecorder()

	adaptList(response, request)

	if response.Code != http.StatusOK {
		t.Fatalf("status = %d, body: %s", response.Code, response.Body.String())
	}

	body := response.Body.String()
	if !strings.Contains(body, "@@||example.com^$dnsrewrite,client=az-ipv6-local") {
		t.Fatalf("expected local allow rule for example.com, got: %q", body)
	}
	if !strings.Contains(body, "@@||api64.ipify.org^$dnsrewrite,client=az-ipv6-local") {
		t.Fatalf("expected local allow rule for api64.ipify.org, got: %q", body)
	}
	if strings.Contains(body, "dnsrewrite=SERVFAIL") {
		t.Fatalf("IPv6 Egress allowlist must not contain dnsrewrite=SERVFAIL, got: %q", body)
	}
	if strings.Contains(body, "dnsrewrite=127.0.0.1") {
		t.Fatalf("IPv6 Egress allowlist must not contain dnsrewrite=127.0.0.1, got: %q", body)
	}
	if strings.Contains(body, "client=az-ipv6-world") {
		t.Fatalf("local allowlist must not grant world client access, got: %q", body)
	}
}

func TestAdaptListIPv6EgressWorldAllowRule(t *testing.T) {
	root := useListRoot(t)
	listPath := filepath.Join(root, "ipv6-egress-domains-world.txt")
	if err := os.WriteFile(listPath, []byte("example.org\napi64.ipify.org\n"), 0o644); err != nil {
		t.Fatal(err)
	}

	requestURL := "/list/?allow=1&client=az-ipv6-world&filter_custom=0&filter_dist=0&file=" + url.QueryEscape(listPath) + "&dnsrewrite=SERVFAIL&suffix=1"
	request := httptest.NewRequest(http.MethodGet, requestURL, nil)
	response := httptest.NewRecorder()

	adaptList(response, request)

	if response.Code != http.StatusOK {
		t.Fatalf("status = %d, body: %s", response.Code, response.Body.String())
	}

	body := response.Body.String()
	if !strings.Contains(body, "@@||example.org^$dnsrewrite,client=az-ipv6-world") {
		t.Fatalf("expected world allow rule for example.org, got: %q", body)
	}
	if !strings.Contains(body, "@@||api64.ipify.org^$dnsrewrite,client=az-ipv6-world") {
		t.Fatalf("expected world allow rule for api64.ipify.org, got: %q", body)
	}
	if strings.Contains(body, "dnsrewrite=SERVFAIL") {
		t.Fatalf("IPv6 Egress allowlist must not contain dnsrewrite=SERVFAIL, got: %q", body)
	}
	if strings.Contains(body, "dnsrewrite=127.0.0.1") {
		t.Fatalf("IPv6 Egress allowlist must not contain dnsrewrite=127.0.0.1, got: %q", body)
	}
	if strings.Contains(body, "client=az-ipv6-local") {
		t.Fatalf("world allowlist must not grant local client access, got: %q", body)
	}
}

func TestAdaptListIPv6EgressLocalAndWorldIndependent(t *testing.T) {
	root := useListRoot(t)
	localPath := filepath.Join(root, "ipv6-egress-domains-local.txt")
	worldPath := filepath.Join(root, "ipv6-egress-domains-world.txt")
	if err := os.WriteFile(localPath, []byte("local-only.com\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(worldPath, []byte("world-only.com\n"), 0o644); err != nil {
		t.Fatal(err)
	}

	// Local request
	localReq := httptest.NewRequest(http.MethodGet,
		"/list/?allow=1&client=az-ipv6-local&filter_custom=0&filter_dist=0&file="+url.QueryEscape(localPath)+"&dnsrewrite=SERVFAIL&suffix=1", nil)
	localResp := httptest.NewRecorder()
	adaptList(localResp, localReq)

	// World request
	worldReq := httptest.NewRequest(http.MethodGet,
		"/list/?allow=1&client=az-ipv6-world&filter_custom=0&filter_dist=0&file="+url.QueryEscape(worldPath)+"&dnsrewrite=SERVFAIL&suffix=1", nil)
	worldResp := httptest.NewRecorder()
	adaptList(worldResp, worldReq)

	localBody := localResp.Body.String()
	worldBody := worldResp.Body.String()

	if !strings.Contains(localBody, "local-only.com") {
		t.Fatalf("local allowlist missing local-only.com, got: %q", localBody)
	}
	if strings.Contains(localBody, "world-only.com") {
		t.Fatalf("local allowlist must not contain world-only.com, got: %q", localBody)
	}
	if !strings.Contains(worldBody, "world-only.com") {
		t.Fatalf("world allowlist missing world-only.com, got: %q", worldBody)
	}
	if strings.Contains(worldBody, "local-only.com") {
		t.Fatalf("world allowlist must not contain local-only.com, got: %q", worldBody)
	}
}

func TestAdaptListIPv6EgressEmptyFile(t *testing.T) {
	root := useListRoot(t)
	listPath := filepath.Join(root, "ipv6-egress-domains-local.txt")
	if err := os.WriteFile(listPath, []byte(""), 0o644); err != nil {
		t.Fatal(err)
	}

	requestURL := "/list/?allow=1&client=az-ipv6-local&filter_custom=0&filter_dist=0&file=" + url.QueryEscape(listPath) + "&dnsrewrite=SERVFAIL&suffix=1"
	request := httptest.NewRequest(http.MethodGet, requestURL, nil)
	response := httptest.NewRecorder()

	adaptList(response, request)

	if response.Code != http.StatusOK {
		t.Fatalf("status = %d, body: %s", response.Code, response.Body.String())
	}
	if response.Body.String() != "" {
		t.Fatalf("expected empty body for empty file, got: %q", response.Body.String())
	}
}

func TestAdaptListIPv6EgressFileNotFound(t *testing.T) {
	root := useListRoot(t)
	missingPath := filepath.Join(root, "does-not-exist.txt")

	requestURL := "/list/?allow=1&client=az-ipv6-local&filter_custom=0&filter_dist=0&file=" + url.QueryEscape(missingPath) + "&dnsrewrite=SERVFAIL&suffix=1"
	request := httptest.NewRequest(http.MethodGet, requestURL, nil)
	response := httptest.NewRecorder()

	adaptList(response, request)

	if response.Code != http.StatusForbidden {
		t.Fatalf("status = %d, want %d; body: %s", response.Code, http.StatusForbidden, response.Body.String())
	}
}

func TestAdaptListIPv6EgressInvalidPath(t *testing.T) {
	requestURL := "/list/?allow=1&client=az-ipv6-local&filter_custom=0&filter_dist=0&file=/etc/passwd&dnsrewrite=SERVFAIL&suffix=1"
	request := httptest.NewRequest(http.MethodGet, requestURL, nil)
	response := httptest.NewRecorder()

	adaptList(response, request)

	if response.Code != http.StatusForbidden {
		t.Fatalf("status = %d, want %d; body: %s", response.Code, http.StatusForbidden, response.Body.String())
	}
}

func TestAdaptListIPv6EgressCommentsAndBlankLinesIgnored(t *testing.T) {
	root := useListRoot(t)
	listPath := filepath.Join(root, "ipv6-egress-domains-local.txt")
	if err := os.WriteFile(listPath, []byte("# this is a comment\n\n   \nexample.com\n#another comment\n"), 0o644); err != nil {
		t.Fatal(err)
	}

	requestURL := "/list/?allow=1&client=az-ipv6-local&filter_custom=0&filter_dist=0&file=" + url.QueryEscape(listPath) + "&dnsrewrite=SERVFAIL&suffix=1"
	request := httptest.NewRequest(http.MethodGet, requestURL, nil)
	response := httptest.NewRecorder()

	adaptList(response, request)

	if response.Code != http.StatusOK {
		t.Fatalf("status = %d, body: %s", response.Code, response.Body.String())
	}

	body := response.Body.String()
	// Only one rule should be generated (for example.com)
	count := strings.Count(body, "@@||example.com^$dnsrewrite,client=az-ipv6-local")
	if count != 1 {
		t.Fatalf("expected exactly 1 allow rule for example.com, got %d: %q", count, body)
	}
	if strings.Contains(body, "dnsrewrite=SERVFAIL") {
		t.Fatalf("IPv6 Egress allowlist must not contain dnsrewrite=SERVFAIL, got: %q", body)
	}
}
