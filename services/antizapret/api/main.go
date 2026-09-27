package main

import (
	"bufio"
	"compress/gzip"
	"context"
	"crypto/md5"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"log"
	"net/http"
	"os"
	"os/exec"
	"os/signal"
	"path/filepath"
	"runtime"
	"strings"
	"sync"
	"syscall"
	"time"

	"github.com/gorilla/schema"
)

var isScriptRunning bool
var mu sync.Mutex

func doallHandler(w http.ResponseWriter, r *http.Request) {
	mu.Lock()
	if isScriptRunning {
		mu.Unlock()
		http.Error(w, "Script is still running", http.StatusTooEarly)
		return
	}
	isScriptRunning = true
	mu.Unlock()

	defer func() {
		mu.Lock()
		isScriptRunning = false
		mu.Unlock()
	}()

	cmd := exec.Command(
		"timeout",
		"--kill-after=5s",
		"10m",
		"/root/antizapret/doall.sh",
	)

	output, err := cmd.CombinedOutput()

	if err != nil {
		http.Error(w, fmt.Sprintf("Failed to execute script: %s", err.Error()), http.StatusInternalServerError)
		return
	}

	w.WriteHeader(http.StatusOK)
	_, _ = w.Write(output)
}

var decoder = schema.NewDecoder()

var errListFileNotAllowed = errors.New("file is outside /root/antizapret")
var allowedListRoot = "/root/antizapret"

var listHTTPClient = &http.Client{Timeout: 60 * time.Second}

func openAllowedListFile(path string) (*os.File, error) {
	prefix := allowedListRoot + string(filepath.Separator)
	if !strings.HasPrefix(path, prefix) {
		return nil, errListFileNotAllowed
	}

	relativePath := strings.TrimPrefix(path, prefix)
	if relativePath == "" || filepath.Clean(relativePath) != relativePath {
		return nil, errListFileNotAllowed
	}

	root, err := os.OpenRoot(allowedListRoot)
	if err != nil {
		return nil, err
	}
	defer root.Close()

	file, err := root.Open(relativePath)
	if err != nil {
		return nil, err
	}
	info, err := file.Stat()
	if err != nil || !info.Mode().IsRegular() {
		_ = file.Close()
		if err != nil {
			return nil, err
		}
		return nil, errors.New("list source is not a regular file")
	}
	return file, nil
}

type ListRequest struct {
	Url          string `schema:"url"`
	File         string `schema:"file"`
	Format       string `schema:"format"`
	Client       string `schema:"client"`        //$client=xxx
	FilterCustom bool   `schema:"filter_custom"` //skip lines with rules from exclude-hosts-custom.txt
	FilterDist   bool   `schema:"filter_dist"`   //skip lines with rules from exclude-hosts-dist.txt
	Allow        bool   `schema:"allow"`         //add @@ at the start of rule
	Raw          bool   `schema:"raw"`           //dont modify rules
	Suffix       bool   `schema:"suffix"`        //add $dnsrewrite,client=xxx to rules
	DnsRewrite   string `schema:"dnsrewrite"`    //value for $dnsrewrite
	Regex        bool   `schema:"regex"`         //convert each line from an ERE to an AdGuard regex rule
}

type RegexFilter struct {
	cmd         *exec.Cmd
	stdin       io.WriteCloser
	reader      *bufio.Reader
	lock        sync.Mutex
	patternFile string
}

var excludeMatcherDist *RegexFilter
var excludeMatcherCustom *RegexFilter
var excludeMatchersLock sync.RWMutex

const delim = "\x1e" // Reserved record separator; cannot be a list item.

func (rf *RegexFilter) start() error {
	// Positive matches report exclusions; the extra pattern always returns
	// the batch terminator, regardless of the user patterns.
	rf.cmd = exec.Command("grep", "--line-buffered", "-a", "-E", "-f", rf.patternFile, "-e", "^"+delim+"$")
	var err error
	if rf.stdin, err = rf.cmd.StdinPipe(); err != nil {
		return err
	}
	stdout, err := rf.cmd.StdoutPipe()
	if err != nil {
		return err
	}
	rf.cmd.Stderr = os.Stderr
	if err := rf.cmd.Start(); err != nil {
		_ = rf.stdin.Close()
		_ = stdout.Close()
		return err
	}
	rf.reader = bufio.NewReader(stdout)
	return nil
}

func (rf *RegexFilter) Filter(lines []string) (result []string, err error) {
	rf.lock.Lock()
	defer rf.lock.Unlock()
	for _, line := range lines {
		if strings.ContainsRune(line, '\n') || line == delim {
			return nil, errors.New("list item contains a reserved separator")
		}
	}
	if rf.cmd == nil {
		if err = rf.start(); err != nil {
			_ = rf.Close()
			return nil, err
		}
	}
	// Drain stdout while writing the batch, so neither pipe can fill and stall.
	written := make(chan error, 1)
	go func() {
		_, err := io.WriteString(rf.stdin, strings.Join(lines, "\n")+"\n"+delim+"\n")
		written <- err
	}()
	defer func() {
		if err != nil {
			_ = rf.cmd.Process.Kill()
		}
		if writeErr := <-written; err == nil {
			err = writeErr
		}
		if err != nil {
			_ = rf.Close()
		}
	}()
	excluded := make(map[string]bool)
	for {
		line, readErr := rf.reader.ReadString('\n')
		if readErr != nil {
			return nil, readErr
		}
		line = strings.TrimSuffix(line, "\n")
		if line == delim {
			break
		}
		excluded[line] = true
	}
	for _, line := range lines {
		if !excluded[line] {
			result = append(result, line)
		}
	}
	return result, nil
}

// Close is called under excludeMatchersLock, or after a failed batch.
func (rf *RegexFilter) Close() error {
	if rf.cmd != nil && rf.cmd.Process != nil {
		_ = rf.cmd.Process.Kill()
		_ = rf.cmd.Wait()
	}
	if rf.stdin != nil {
		_ = rf.stdin.Close()
	}
	rf.cmd = nil
	return nil
}

func NewRegexFilter(file string) (*RegexFilter, error) {
	if out, err := exec.Command("sed", "-i", `s/\s*$//; /^$/d`, file).CombinedOutput(); err != nil {
		return nil, fmt.Errorf("failed to normalize exclude filter: %w: %s", err, out)
	}
	// Validate on reload so an invalid pattern keeps the old filter active.
	cmd := exec.Command("grep", "-a", "-E", "-f", file, "/dev/null")
	output, err := cmd.CombinedOutput()
	var exitErr *exec.ExitError
	if err != nil && !(errors.As(err, &exitErr) && exitErr.ExitCode() == 1) {
		return nil, fmt.Errorf("invalid exclude filter %s: %w: %s", file, err, output)
	}
	rf := &RegexFilter{patternFile: file}
	if err := rf.start(); err != nil {
		_ = rf.Close()
		return nil, err
	}
	return rf, nil
}

var DefaultClient string

func adaptList(w http.ResponseWriter, r *http.Request) {
	req := ListRequest{
		Client:       DefaultClient,
		FilterCustom: true, //
		FilterDist:   false,
		Allow:        true, // default (adds @@)
		Suffix:       true,
		Raw:          false,
		DnsRewrite:   "SERVFAIL",
	}

	if err := decoder.Decode(&req, r.URL.Query()); err != nil {
		http.Error(w, fmt.Sprintf("Invalid request body: %v", err), http.StatusBadRequest)
		return
	}

	var reader io.ReadCloser
	if req.Url != "" {
		// Create a new HTTP request
		reqRemote, err := http.NewRequestWithContext(r.Context(), http.MethodGet, req.Url, nil)
		if err != nil {
			http.Error(w, fmt.Sprintf("Failed to create request: %v", err), http.StatusBadRequest)
			return
		}

		// Keep a useful User-Agent without forwarding credentials or internal headers.
		if userAgent := r.Header.Get("User-Agent"); userAgent != "" {
			reqRemote.Header.Set("User-Agent", userAgent)
		}

		// Perform the request
		resp, err := listHTTPClient.Do(reqRemote)
		if err != nil {
			http.Error(w, fmt.Sprintf("Failed to download list: %v", err), http.StatusBadGateway)
			return
		}
		defer resp.Body.Close()

		if resp.StatusCode != http.StatusOK {
			http.Error(w, fmt.Sprintf("Remote server returned %d", resp.StatusCode), http.StatusBadGateway)
			return
		}

		if resp.Header.Get("Content-Encoding") == "gzip" {
			gz, err := gzip.NewReader(resp.Body)
			if err != nil {
				http.Error(w, fmt.Sprintf("Cant uncompress response: %v", err), http.StatusInternalServerError)
				return
			}
			defer gz.Close()
			reader = gz
		} else {
			reader = resp.Body
		}

		if resp.Header.Get("Content-Type") == "application/json" && req.Format == "" {
			req.Format = "json"
		}

	} else if req.File != "" {
		file, err := openAllowedListFile(req.File)
		if err != nil {
			status := http.StatusInternalServerError
			if errors.Is(err, errListFileNotAllowed) {
				status = http.StatusForbidden
			}
			http.Error(w, fmt.Sprintf("Failed to open local file: %v", err), status)
			return
		}
		defer file.Close()
		reader = file
	} else {
		http.Error(w, "Url or File required", http.StatusBadRequest)
		return
	}

	if req.Format != "" && !strings.EqualFold(req.Format, "list") && !strings.EqualFold(req.Format, "json") {
		http.Error(w, "Unsupported format (use 'json' or 'list')", http.StatusBadRequest)
		return
	}

	// Create a flusher to stream output
	flusher, ok := w.(http.Flusher)
	if !ok {
		http.Error(w, "Streaming not supported", http.StatusInternalServerError)
		return
	}

	abort := func(err error) {
		log.Printf("[ERROR] list response aborted: %v", err)
		panic(http.ErrAbortHandler)
	}

	w.Header().Set("Content-Type", "text/plain; charset=utf-8")
	w.WriteHeader(http.StatusOK)

	var buffer []string
	// Helper to process and write each line
	processBuffer := func() {
		excludeMatchersLock.RLock()
		defer excludeMatchersLock.RUnlock()
		filtered := buffer
		buffer = nil
		var err error
		if req.FilterDist {
			filtered, err = excludeMatcherDist.Filter(filtered)
			if err != nil {
				abort(err)
			}
		}
		if req.FilterCustom {
			filtered, err = excludeMatcherCustom.Filter(filtered)
			if err != nil {
				abort(err)
			}
		}

		for _, line := range filtered {
			out := strings.TrimSpace(line)
			if req.Raw || out == "" || strings.HasPrefix(out, "!") || strings.HasPrefix(out, "#") {
				//
			} else {
				if req.Regex && !(strings.HasPrefix(out, "/") && strings.HasSuffix(out, "/")) {
					out = "/" + strings.ReplaceAll(out, "/", `\/`) + "/"
				} else if !req.Regex && !strings.HasPrefix(out, "/") {
					out = "||" + out + "^"
				}
				if req.Allow {
					out = "@@" + out
				}
				if req.Suffix {
					suffix := ""

					if len(req.DnsRewrite) > 0 {
						if strings.HasPrefix(out, "@@") {
							suffix = "$dnsrewrite"
						} else {
							suffix = fmt.Sprintf("$dnsrewrite=%s", req.DnsRewrite)
						}
					}

					if len(req.Client) > 0 {
						if len(suffix) > 0 {
							suffix += ","
						}
						suffix += fmt.Sprintf("client=%s", req.Client)
					}
					out += suffix
				}
			}

			if _, err := fmt.Fprintln(w, out); err != nil {
				abort(err)
			}
		}

	}

	processLine := func(line string) {
		buffer = append(buffer, line)
		if len(buffer) > 1000 {
			processBuffer()
		}
	}

	if req.Format == "" {
		req.Format = "list"
	}
	// Handle format types
	switch strings.ToLower(req.Format) {
	case "list":
		// Stream line-by-line
		scanner := bufio.NewScanner(reader)
		for scanner.Scan() {
			processLine(scanner.Text())
		}
		if err := scanner.Err(); err != nil {
			abort(err)
		}
	case "json":
		// Stream JSON array one element at a time
		dec := json.NewDecoder(reader)

		// Expect start of array
		t, err := dec.Token()
		if err != nil {
			abort(err)
		}
		if delim, ok := t.(json.Delim); !ok || delim != '[' {
			abort(errors.New("expected JSON array"))
		}

		// Decode each element until end of array
		for dec.More() {
			var item string
			if err := dec.Decode(&item); err != nil {
				abort(err)
			}
			processLine(item)
		}

		// Consume closing bracket
		if _, err := dec.Token(); err != nil {
			abort(err)
		}
	default:
		abort(errors.New("unsupported format (use 'json' or 'list')"))
	}
	processBuffer()
	flusher.Flush()
}

func updateRegexFilter() error {
	excludeMatchersLock.Lock()
	defer excludeMatchersLock.Unlock()

	newDist, err := NewRegexFilter(
		"/root/antizapret/config/exclude-hosts-dist.txt",
	)
	if err != nil {
		return err
	}

	newCustom, err := NewRegexFilter(
		"/root/antizapret/config/custom/exclude-hosts-custom.txt",
	)
	if err != nil {
		_ = newDist.Close()
		return err
	}

	oldDist := excludeMatcherDist
	oldCustom := excludeMatcherCustom
	excludeMatcherDist = newDist
	excludeMatcherCustom = newCustom

	if oldDist != nil {
		_ = oldDist.Close()
	}
	if oldCustom != nil {
		_ = oldCustom.Close()
	}
	return nil
}

func update(w http.ResponseWriter, r *http.Request) {
	error := updateRegexFilter()
	if error != nil {
		http.Error(w, fmt.Sprintf("Failed to update exclude lists: %v", error), http.StatusInternalServerError)
		return
	}
	w.WriteHeader(http.StatusOK)
	fmt.Fprintln(w, "ok")
}

func configMd5Handler(w http.ResponseWriter, r *http.Request) {
	configPaths := []string{"/root/antizapret/config/", "/root/antizapret/result/"}
	md5s := []string{}

	for _, configPath := range configPaths {
		err := filepath.WalkDir(configPath, func(path string, d fs.DirEntry, err error) error {
			if err != nil {
				return err
			}
			if d.IsDir() {
				return nil
			}

			f, err := os.Open(path)
			if err != nil {
				return err
			}
			defer f.Close()

			h := md5.New()
			if _, err := io.Copy(h, f); err != nil {
				return err
			}

			md5s = append(md5s, hex.EncodeToString(h.Sum(nil)))
			return nil
		})

		if err != nil && !os.IsNotExist(err) {
			http.Error(w, fmt.Sprintf("Failed to calculate md5 for %s: %v", configPath, err), http.StatusInternalServerError)
			return
		}
	}

	if len(md5s) == 0 {
		http.Error(w, "No config or result files found", http.StatusNotFound)
		return
	}

	finalMd5 := md5.New()
	for _, md5 := range md5s {
		io.WriteString(finalMd5, md5)
	}

	w.WriteHeader(http.StatusOK)
	fmt.Fprintln(w, hex.EncodeToString(finalMd5.Sum(nil)))
}

// responseWriterWrapper captures the status code and bytes written
type responseWriterWrapper struct {
	http.ResponseWriter
	statusCode int
	bytesSent  int
}

func (rw *responseWriterWrapper) WriteHeader(code int) {
	if rw.statusCode != 0 {
		// Already written
		return
	}
	rw.statusCode = code
	rw.ResponseWriter.WriteHeader(code)
}

func (rw *responseWriterWrapper) Write(b []byte) (int, error) {
	// Ensure status code is set (in case WriteHeader wasn’t called explicitly)
	if rw.statusCode == 0 {
		rw.WriteHeader(http.StatusOK)
	}
	n, err := rw.ResponseWriter.Write(b)
	rw.bytesSent += n
	return n, err
}

// Implement http.Flusher by forwarding
func (rw *responseWriterWrapper) Flush() {
	if flusher, ok := rw.ResponseWriter.(http.Flusher); ok {
		flusher.Flush()
	}
}

func loggingMiddleware(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		start := time.Now()

		ip := r.RemoteAddr
		if forwarded := r.Header.Get("X-Forwarded-For"); forwarded != "" {
			ip = forwarded
		}

		// Wrap the ResponseWriter
		wrapped := &responseWriterWrapper{ResponseWriter: w}

		// Log request start
		log.Printf("[REQ] %s %s?%s from %s", r.Method, r.URL.Path, r.URL.RawQuery, ip)

		next.ServeHTTP(wrapped, r)

		// Log request end with status and duration
		duration := time.Since(start)
		log.Printf("[RES] %s %s?%s -> %d (%d bytes, %v)", r.Method, r.URL.Path, r.URL.RawQuery, wrapped.statusCode, wrapped.bytesSent, duration)
	})
}

func main() {
	DefaultClient = os.Getenv("CLIENT")
	runtime.GOMAXPROCS(runtime.NumCPU())

	err := updateRegexFilter()
	if err != nil {
		log.Fatalf("Failed to initialize regex filters: %v", err)
		panic(err)
	}
	defer func() {
		if excludeMatcherDist != nil {
			excludeMatcherDist.Close()
		}
		if excludeMatcherCustom != nil {
			excludeMatcherCustom.Close()
		}
	}()
	// Create a mux so we can wrap all handlers with logging
	r := http.NewServeMux()

	// Optional trailing slash via regex
	r.HandleFunc(`/list/`, adaptList)
	r.HandleFunc(`/doall/`, doallHandler)
	r.HandleFunc(`/update/`, update)
	r.HandleFunc(`/config-md5/`, configMd5Handler)

	server := &http.Server{
		Addr:    ":80",
		Handler: loggingMiddleware(r),
	}

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	go func() {
		fmt.Println("Starting server on http://localhost" + server.Addr)
		log.Fatal(server.ListenAndServe())
	}()

	// Block main execution until a termination signal is caught
	<-ctx.Done()
	log.Println("Shutting down server gracefully...")

	// Create a deadline context for the shutdown process (e.g., 1 seconds)
	shutdownCtx, cancel := context.WithTimeout(context.Background(), 1*time.Second)
	defer cancel()

	// Trigger the graceful shutdown
	if err := server.Shutdown(shutdownCtx); err != nil {
		log.Fatalf("Server graceful shutdown failed: %v", err)
	}

	log.Println("Server exited cleanly.")

}
