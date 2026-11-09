package main

import (
	"net/http/httptest"
	"net/url"
	"os"
	"path/filepath"
	"testing"
)

func TestListRoutingPriorities(t *testing.T) {
	useExclusions(t, `^example\.org$`)
	path := filepath.Join(useListRoot(t), "include-hosts-custom.txt")
	if err := os.WriteFile(path, []byte("example.org\n"), 0600); err != nil {
		t.Fatal(err)
	}
	for _, tc := range []struct{ name, query, want string }{
		{"dist", "filter_custom=0&client=az-local", "@@||example.org^$dnsrewrite,client=az-local\n"},
		{"custom", "filter_custom=0&client=az-local", "@@||example.org^$dnsrewrite,client=az-local\n"},
		{"custom world", "filter_custom=0&client=az-world", "@@||example.org^$dnsrewrite,client=az-world\n"},
		{"exclude", "exclude=1", "||example.org^$dnsrewrite=SERVFAIL,client=az-local|az-world|az-resolver,important\n"},
		{"exclude preset", "exclude=1&client=coredns&suffix=0&allow=1", "||example.org^$dnsrewrite=SERVFAIL,client=az-local|az-world|az-resolver,important\n"},
		{"ordinary list respects custom exclusions", "", ""},
	} {
		t.Run(tc.name, func(t *testing.T) {
			response := httptest.NewRecorder()
			adaptList(response, httptest.NewRequest("GET", "/list/?"+tc.query+"&file="+url.QueryEscape(path), nil))
			if response.Code != 200 || response.Body.String() != tc.want {
				t.Fatalf("status=%d got %q want %q", response.Code, response.Body.String(), tc.want)
			}
		})
	}
}

func TestExcludeRegexUsesAllExitClients(t *testing.T) {
	path := filepath.Join(useListRoot(t), "exclude")
	if err := os.WriteFile(path, []byte(`(^|\.)example\.org$`+"\n"), 0600); err != nil {
		t.Fatal(err)
	}
	response := httptest.NewRecorder()
	adaptList(response, httptest.NewRequest("GET", "/list/?exclude=1&regex=1&file="+url.QueryEscape(path), nil))
	want := `/(^|\.)example\.org$/$dnsrewrite=SERVFAIL,client=az-local|az-world|az-resolver,important` + "\n"
	if response.Code != 200 || response.Body.String() != want {
		t.Fatalf("status=%d got %q want %q", response.Code, response.Body.String(), want)
	}
}
