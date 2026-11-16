package main

import (
	"crypto/md5"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"net/http"
	"os"
	"path/filepath"
	"strings"
)

var configMD5Root = "/root/antizapret"
var configMD5File = "/dev/shm/.api_config_md5"

func calculateConfigMD5() (string, error) {
	combined := md5.New()
	files := 0
	for _, directory := range []string{"config", "result"} {
		err := filepath.WalkDir(filepath.Join(configMD5Root, directory), func(path string, entry fs.DirEntry, err error) error {
			if err != nil {
				return err
			}
			// Ownership, checksum markers and unfinished downloads are not inputs.
			if strings.HasPrefix(entry.Name(), ".") || strings.HasSuffix(entry.Name(), ".tmp") {
				if entry.IsDir() {
					return filepath.SkipDir
				}
				return nil
			}
			if entry.IsDir() {
				return nil
			}
			file, err := os.Open(path)
			if err != nil {
				return err
			}
			defer file.Close()
			hash := md5.New()
			if _, err := io.Copy(hash, file); err != nil {
				return err
			}
			io.WriteString(combined, hex.EncodeToString(hash.Sum(nil)))
			files++
			return nil
		})
		if err != nil {
			return "", err
		}
	}
	if files == 0 {
		return "", errors.New("no config or result files found")
	}
	return hex.EncodeToString(combined.Sum(nil)), nil
}

func publishConfigMD5() error {
	checksum, err := calculateConfigMD5()
	if err != nil {
		return err
	}
	temporary, err := os.CreateTemp(filepath.Dir(configMD5File), ".api_config_md5-*")
	if err != nil {
		return err
	}
	defer os.Remove(temporary.Name())
	defer temporary.Close()
	if _, err := fmt.Fprintln(temporary, checksum); err != nil {
		return err
	}
	if err := temporary.Close(); err != nil {
		return err
	}
	return os.Rename(temporary.Name(), configMD5File)
}

func configMd5Handler(w http.ResponseWriter, r *http.Request) {
	checksum, err := os.ReadFile(configMD5File)
	if errors.Is(err, fs.ErrNotExist) {
		// Recover a missing cache once; ordinary requests only read the ready file.
		if err = publishConfigMD5(); err == nil {
			checksum, err = os.ReadFile(configMD5File)
		}
	}
	if err != nil {
		http.Error(w, fmt.Sprintf("Failed to read configuration checksum: %v", err), http.StatusInternalServerError)
		return
	}
	w.Header().Set("Content-Type", "text/plain; charset=utf-8")
	_, _ = w.Write(checksum)
}
