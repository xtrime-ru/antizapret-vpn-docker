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

func TestAdaptListDetectsV2flyAndTrimsDomainAnnotations(t *testing.T) {
	root := useListRoot(t)
	path := filepath.Join(root, "list")
	if err := os.WriteFile(filepath.Join(root, "child"), []byte("keep.example @cn\ndrop.example @ads\n"), 0600); err != nil {
		t.Fatal(err)
	}
	for _, tc := range []struct{ name, query, input, want string }{
		{"plain attributes", "", "example.org @cn ignored\n", "@@||example.org^$dnsrewrite\n"},
		{"plain comments", "", "# domain:comment\n example.org\t# full:comment\n", "# domain:comment\n@@||example.org^$dnsrewrite\n"},
		{"domain", "", "\tdomain:example.org @cn\n", "@@||example.org^$dnsrewrite\n"},
		{"full", "", "full:example.org\n", "@@|example.org|$dnsrewrite\n"},
		{"keyword", "", "keyword:example\n", "@@/example/$dnsrewrite\n"},
		{"regexp", "", "regexp:^example\\.org$\n", "@@/^example\\.org$/$dnsrewrite\n"},
		{"include attributes", "", "example.org @ads\ninclude:child @cn @-ads\n", "@@||example.org^$dnsrewrite\n@@||keep.example^$dnsrewrite\n"},
		{"late directive", "", strings.Repeat("example.org @cn\n", 1100) + "full:cdn.example.org\n", strings.Repeat("@@||example.org^$dnsrewrite\n", 1100) + "@@|cdn.example.org|$dnsrewrite\n"},
		{"unicode whitespace", "", "\u2003example.org\u00a0@cn\n", "@@||example.org^$dnsrewrite\n"},
		{"regex spaces", "", "/foo bar/\n", "@@/foo bar/$dnsrewrite\n"},
		{"json", "&format=json", `["example.org","cdn.example.org"]`, "@@||example.org^$dnsrewrite\n@@||cdn.example.org^$dnsrewrite\n"},
		{"json raw", "&format=json&raw=1", `["||example.org^$important"]`, "||example.org^$important\n"},
		{"no suffix", "&suffix=0", "example.org\n", "@@||example.org^\n"},
		{"explicit list", "&format=list", "domain:example.org @cn\n", "@@||domain:example.org^$dnsrewrite\n"},
		{"raw", "&raw=1", "full:example.org some text\n", "full:example.org some text\n"},
		{"regex", "&regex=1", "keyword:example text\n", "@@/keyword:example text/$dnsrewrite\n"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			if err := os.WriteFile(path, []byte(tc.input), 0600); err != nil {
				t.Fatal(err)
			}
			response := httptest.NewRecorder()
			adaptList(response, httptest.NewRequest("GET", "/list/?filter_custom=0&file="+url.QueryEscape(path)+tc.query, nil))
			if response.Code != 200 || response.Body.String() != tc.want {
				t.Fatalf("status=%d got %q want %q", response.Code, response.Body.String(), tc.want)
			}
		})
	}
}

func TestAdaptListRejectsConflictingModesBeforeLoadingSource(t *testing.T) {
	for _, query := range []string{
		"format=v2fly&regex=1", "format=V2Fly&raw=1",
		"format=v2fly&exclude=1&raw=1", "raw=1&regex=1",
		"exclude=1&raw=1", "format=unsupported",
	} {
		t.Run(query, func(t *testing.T) {
			response := httptest.NewRecorder()
			adaptList(response, httptest.NewRequest("GET", "/list/?"+query+"&file=/etc/hostname", nil))
			if response.Code != http.StatusBadRequest {
				t.Fatalf("status=%d, want 400; body=%s", response.Code, response.Body)
			}
			if !strings.Contains(response.Body.String(), "format") && !strings.Contains(response.Body.String(), "cannot be combined") {
				t.Fatalf("missing validation message: %s", response.Body)
			}
		})
	}
}
