package main

import (
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func useConfigMD5Root(t *testing.T) string {
	t.Helper()
	oldRoot, oldFile := configMD5Root, configMD5File
	configMD5Root = t.TempDir()
	configMD5File = filepath.Join(t.TempDir(), "checksum")
	t.Cleanup(func() { configMD5Root, configMD5File = oldRoot, oldFile })
	for _, directory := range []string{"config", "result"} {
		if err := os.Mkdir(filepath.Join(configMD5Root, directory), 0o755); err != nil {
			t.Fatal(err)
		}
	}
	return configMD5Root
}

func writeConfigInput(t *testing.T, path, content string) {
	t.Helper()
	if err := os.WriteFile(path, []byte(content), 0o600); err != nil {
		t.Fatal(err)
	}
}

func requestConfigMD5(t *testing.T) string {
	t.Helper()
	response := httptest.NewRecorder()
	configMd5Handler(response, httptest.NewRequest(http.MethodGet, "/config-md5/", nil))
	if response.Code != http.StatusOK {
		t.Fatalf("status %d: %s", response.Code, response.Body.String())
	}
	return strings.TrimSpace(response.Body.String())
}

func TestConfigMD5ServesPublishedFileUntilRepublished(t *testing.T) {
	root := useConfigMD5Root(t)
	input := filepath.Join(root, "config", "rules.txt")
	writeConfigInput(t, input, "original")
	if err := publishConfigMD5(); err != nil {
		t.Fatal(err)
	}
	original := requestConfigMD5(t)
	if len(original) != 32 {
		t.Fatalf("invalid checksum %q", original)
	}
	writeConfigInput(t, input, "changed")
	if got := requestConfigMD5(t); got != original {
		t.Fatalf("request rehashed inputs: %s != %s", got, original)
	}
	if err := publishConfigMD5(); err != nil {
		t.Fatal(err)
	}
	if got := requestConfigMD5(t); got == original {
		t.Fatal("publication did not update checksum")
	}
	// Existing published state can be served even while inputs are unavailable.
	if err := os.RemoveAll(root); err != nil {
		t.Fatal(err)
	}
	requestConfigMD5(t)
}

func TestConfigMD5MissingCacheIsRebuilt(t *testing.T) {
	root := useConfigMD5Root(t)
	writeConfigInput(t, filepath.Join(root, "result", "ips.txt"), "192.0.2.0/24")
	original := requestConfigMD5(t)
	if err := os.Remove(configMD5File); err != nil {
		t.Fatal(err)
	}
	if got := requestConfigMD5(t); got != original {
		t.Fatalf("rebuilt checksum %s != %s", got, original)
	}
}

func TestConfigMD5IgnoresMarkersAndUnfinishedDownloads(t *testing.T) {
	root := useConfigMD5Root(t)
	writeConfigInput(t, filepath.Join(root, "config", "rules.txt"), "original")
	original, err := calculateConfigMD5()
	if err != nil {
		t.Fatal(err)
	}
	for _, path := range []string{"result/.ips-md5", "result/.doall_owner", "config/rules.txt.tmp"} {
		writeConfigInput(t, filepath.Join(root, path), "temporary")
	}
	if got, err := calculateConfigMD5(); err != nil || got != original {
		t.Fatalf("service files affected checksum: %q, %v", got, err)
	}
}

func TestConfigMD5FailedPublicationKeepsPreviousValue(t *testing.T) {
	root := useConfigMD5Root(t)
	input := filepath.Join(root, "config", "rules.txt")
	writeConfigInput(t, input, "original")
	if err := publishConfigMD5(); err != nil {
		t.Fatal(err)
	}
	original := requestConfigMD5(t)
	if err := os.Remove(input); err != nil {
		t.Fatal(err)
	}
	if err := publishConfigMD5(); err == nil {
		t.Fatal("empty inputs accepted")
	}
	if got := requestConfigMD5(t); got != original {
		t.Fatalf("failed publication replaced previous checksum: %q", got)
	}
	writeConfigInput(t, input, "recovered")
	if err := publishConfigMD5(); err != nil {
		t.Fatal(err)
	}
	if requestConfigMD5(t) == original {
		t.Fatal("publication did not recover")
	}
}
