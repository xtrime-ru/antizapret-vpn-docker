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
)

func TestV2flyIncludesAndMatchTypes(t *testing.T) {
	root := useListRoot(t)
	files := map[string]string{
		"main":       "# comment\ninclude:child @cn @-ads\ndomain:example.org @test &other # inline\nfull:exact.example.org\nkeyword:game.store\nregexp:^cdn[0-9]+\\.example\\.org$\ninclude:child @ads\n",
		"child":      "include:grandchild\ndrop.example @cn @ads\nkeep.example @cn\nother.example\n",
		"grandchild": "full:nested.example @cn\n",
	}
	for name, data := range files {
		if err := os.WriteFile(filepath.Join(root, name), []byte(data), 0600); err != nil {
			t.Fatal(err)
		}
	}
	response := httptest.NewRecorder()
	adaptList(response, httptest.NewRequest("GET", "/list/?format=v2fly&exclude=1&file="+url.QueryEscape(filepath.Join(root, "main")), nil))
	patterns := []string{"|nested.example|", "||keep.example^", "||example.org^", "|exact.example.org|", `/game\.store/`, `/^cdn[0-9]+\.example\.org$/`, "||drop.example^"}
	var want string
	for _, pattern := range patterns {
		want += pattern + "$dnsrewrite=SERVFAIL,client=az-local|az-world|az-resolver,important\n"
	}
	if response.Code != 200 || response.Body.String() != want {
		t.Fatalf("status=%d\ngot %q\nwant %q", response.Code, response.Body.String(), want)
	}
}

func TestV2flyRejectsInvalidIncludesAndRules(t *testing.T) {
	for _, data := range []string{"include:main", "include:../secret", "include:file:///etc/passwd", "regexp:[", "unknown:example.org", "domain:", "example.org invalid"} {
		t.Run(data, func(t *testing.T) {
			root := useListRoot(t)
			path := filepath.Join(root, "main")
			if err := os.WriteFile(path, []byte(data), 0600); err != nil {
				t.Fatal(err)
			}
			response := httptest.NewRecorder()
			adaptList(response, httptest.NewRequest("GET", "/list/?format=v2fly&filter_custom=0&file="+url.QueryEscape(path), nil))
			if response.Code != http.StatusBadGateway {
				t.Fatalf("status=%d body=%s", response.Code, response.Body)
			}
		})
	}
}

func TestV2flyRemoteIncludes(t *testing.T) {
	var server *httptest.Server
	server = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/data/main":
			fmt.Fprintln(w, "include:child @cn")
			fmt.Fprintln(w, "include:"+server.URL+"/external")
		case "/data/child":
			fmt.Fprintln(w, "full:cdn.example @cn\nignored.example")
		case "/external":
			fmt.Fprintln(w, "game.example")
		default:
			http.NotFound(w, r)
		}
	}))
	defer server.Close()
	response := httptest.NewRecorder()
	adaptList(response, httptest.NewRequest("GET", "/list/?format=v2fly&filter_custom=0&url="+url.QueryEscape(server.URL+"/data/main"), nil))
	want := "@@|cdn.example|$dnsrewrite\n@@||game.example^$dnsrewrite\n"
	if response.Code != 200 || response.Body.String() != want {
		t.Fatalf("status=%d body=%q", response.Code, response.Body.String())
	}
}

func TestV2flyBatchFilteringPreservesFullMatches(t *testing.T) {
	useExclusions(t, "^drop\\.example$")
	root := useListRoot(t)
	path := filepath.Join(root, "main")
	data := strings.Repeat("full:drop.example\nfull:keep.example\ndomain:keep.example\n", 1100)
	if err := os.WriteFile(path, []byte(data), 0600); err != nil {
		t.Fatal(err)
	}
	response := httptest.NewRecorder()
	adaptList(response, httptest.NewRequest("GET", "/list/?format=v2fly&file="+url.QueryEscape(path), nil))
	want := strings.Repeat("@@|keep.example|$dnsrewrite\n@@||keep.example^$dnsrewrite\n", 1100)
	if response.Code != 200 || response.Body.String() != want {
		t.Fatalf("status=%d got %d bytes want %d", response.Code, response.Body.Len(), len(want))
	}
}

// Optional network smoke test for the production include graph.
func TestBundledDirectListLive(t *testing.T) {
	path := os.Getenv("V2FLY_LIVE_LIST")
	if path == "" {
		t.Skip("set V2FLY_LIVE_LIST to check upstream game lists")
	}
	file, err := os.Open(path)
	if err != nil {
		t.Fatal(err)
	}
	defer file.Close()
	parser := v2flyListParser{ctx: t.Context(), active: make(map[string]bool)}
	var rules []v2flyRule
	err = parser.parseSource(path, file, false, func(rule v2flyRule) { rules = append(rules, rule) })
	if err != nil {
		t.Fatal(err)
	}
	got := make(map[string]bool)
	for _, rule := range rules {
		got[rule.pattern] = true
	}
	for _, expected := range []string{"||steampowered.com^", "|steampipe.akamaized.net|", "||playstation.net^", "||rockstargames.com^", "|gamedownloads-rockstargames-com.akamaized.net|"} {
		if !got[expected] {
			t.Errorf("missing %s", expected)
		}
	}
	t.Logf("loaded %d game rules", len(rules))
}
