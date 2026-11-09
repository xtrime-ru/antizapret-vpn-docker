package main

import (
	"bufio"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"testing"
	"time"
)

func TestListStreamsBeforeSourceEnds(t *testing.T) {
	for _, tc := range []struct {
		name, query string
		include     bool
	}{
		{"auto", "", false}, {"v2fly", "&format=v2fly", false}, {"include with attributes", "", true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			release := make(chan struct{})
			source := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if tc.include && r.URL.Path == "/main" {
					fmt.Fprintln(w, "include:child @cn @-ads")
					return
				}
				io.WriteString(w, strings.Repeat("keep.example @cn\ndrop.example @cn @ads\n", 1100))
				w.(http.Flusher).Flush()
				<-release
				// The directive arrives only after the first batch has been delivered.
				fmt.Fprintln(w, "full:cdn.example")
			}))
			api := httptest.NewServer(http.HandlerFunc(adaptList))
			defer func() { close(release); api.Close(); source.Close() }()
			result := make(chan error, 1)
			go func() {
				response, err := (&http.Client{Timeout: 5 * time.Second}).Get(api.URL + "/list/?filter_custom=0&url=" + url.QueryEscape(source.URL+"/main") + tc.query)
				if err != nil {
					result <- err
					return
				}
				defer response.Body.Close()
				reader := bufio.NewReader(response.Body)
				line, err := reader.ReadString('\n')
				if err == nil && line != "@@||keep.example^$dnsrewrite\n" {
					err = fmt.Errorf("first line = %q", line)
				}
				result <- err
			}()
			select {
			case err := <-result:
				if err != nil {
					t.Fatal(err)
				}
			case <-time.After(3 * time.Second):
				t.Fatal("no rules received before the source finished")
			}
		})
	}
}

func TestListAbortsOnErrorAfterStreaming(t *testing.T) {
	source := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		io.WriteString(w, strings.Repeat("keep.example\n", 1100)+"regexp:[\n")
	}))
	defer source.Close()
	api := httptest.NewServer(http.HandlerFunc(adaptList))
	defer api.Close()
	response, err := api.Client().Get(api.URL + "/list/?filter_custom=0&url=" + url.QueryEscape(source.URL))
	if err != nil {
		t.Fatal(err)
	}
	defer response.Body.Close()
	body, err := io.ReadAll(response.Body)
	if err == nil {
		t.Fatal("expected an interrupted response for an invalid late directive")
	}
	if !strings.HasPrefix(string(body), "@@||keep.example^$dnsrewrite\n") {
		t.Fatalf("missing streamed rules: %q", body)
	}
	if strings.Contains(string(body), "Failed to parse") {
		t.Fatal("error text leaked into a partial rule list")
	}
}
