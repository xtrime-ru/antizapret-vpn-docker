package main

import (
	"io"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"path/filepath"
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
