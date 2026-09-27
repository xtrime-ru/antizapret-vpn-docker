package main

import (
	"bufio"
	"bytes"
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
	"sync/atomic"
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

// RegexFilter holds sanitized exclude patterns (GNU grep ERE, one per line).
// It is immutable after creation, so concurrent requests need no locking.
// GNU grep matches hundreds of patterns far faster than Go's regexp: on
// ~420 patterns and 300k domains, 0.2s against 46s.
type RegexFilter struct {
	patterns []byte // newline-terminated patterns, empty when nothing to filter
	count    int
}

var excludeMatcherDist atomic.Pointer[RegexFilter]
var excludeMatcherCustom atomic.Pointer[RegexFilter]

// startGrep starts cmd with patterns readable at /dev/fd/3. A pipe keeps the
// filter in memory: no temporary file can be removed under a running request,
// and the pattern list has no command-line length limit.
func startGrep(cmd *exec.Cmd, patterns []byte) error {
	patternsReader, patternsWriter, err := os.Pipe()
	if err != nil {
		return err
	}
	cmd.ExtraFiles = []*os.File{patternsReader}
	err = cmd.Start()
	_ = patternsReader.Close()
	if err != nil {
		_ = patternsWriter.Close()
		return err
	}
	go func() {
		// grep reads all patterns before its input; an early exit ends the write.
		_, _ = patternsWriter.Write(patterns)
		_ = patternsWriter.Close()
	}()
	return nil
}

// grepAccepts reports whether GNU grep -E compiles every pattern.
func grepAccepts(patterns []byte) (bool, string, error) {
	cmd := exec.Command("grep", "-E", "-f", "/dev/fd/3", "/dev/null")
	var stderr bytes.Buffer
	cmd.Stderr = &stderr
	if err := startGrep(cmd, patterns); err != nil {
		return false, "", err
	}
	err := cmd.Wait()
	var exitError *exec.ExitError
	switch {
	case err == nil:
		return true, "", nil
	case errors.As(err, &exitError) && exitError.ExitCode() == 1:
		return true, "", nil // valid patterns, no match in /dev/null
	case errors.As(err, &exitError):
		return false, strings.TrimSpace(stderr.String()), nil
	default:
		return false, "", err
	}
}

// NewRegexFilter reads one ERE per line. Empty lines and full-line comments
// are ignored (an empty pattern would match every line). Patterns wrapped in
// /.../ are unwrapped, as they are for AdGuard. A pattern grep cannot compile
// is skipped with a warning instead of disabling the whole filter. The source
// file is never modified.
func NewRegexFilter(file string) (*RegexFilter, error) {
	data, err := os.ReadFile(file)
	if err != nil {
		return nil, err
	}

	type pattern struct {
		line  int
		value string
	}
	var patterns []pattern
	for number, line := range strings.Split(string(data), "\n") {
		line = strings.TrimSpace(line)
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		if len(line) > 2 && strings.HasPrefix(line, "/") && strings.HasSuffix(line, "/") {
			line = line[1 : len(line)-1]
		}
		patterns = append(patterns, pattern{number + 1, line})
	}

	join := func(items []pattern) []byte {
		var buffer bytes.Buffer
		for _, item := range items {
			buffer.WriteString(item.value)
			buffer.WriteByte('\n')
		}
		return buffer.Bytes()
	}

	all := join(patterns)
	if len(patterns) == 0 {
		return &RegexFilter{}, nil
	}
	ok, _, err := grepAccepts(all)
	if err != nil {
		return nil, fmt.Errorf("validate %s: %w", file, err)
	}
	if ok {
		return &RegexFilter{patterns: all, count: len(patterns)}, nil
	}

	// Rare path: find the offending lines one by one.
	valid := patterns[:0:0]
	for _, item := range patterns {
		ok, message, err := grepAccepts([]byte(item.value + "\n"))
		if err != nil {
			return nil, fmt.Errorf("validate %s: %w", file, err)
		}
		if !ok {
			log.Printf("[WARN] %s:%d: skipping invalid pattern %q: %s", file, item.line, item.value, message)
			continue
		}
		valid = append(valid, item)
	}
	return &RegexFilter{patterns: join(valid), count: len(valid)}, nil
}

var DefaultClient string

// formatRule converts one list line into an AdGuard rule for the request.
func formatRule(req *ListRequest, line string) string {
	out := strings.TrimSpace(line)
	if req.Raw || out == "" || strings.HasPrefix(out, "!") || strings.HasPrefix(out, "#") {
		return out
	}
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
	return out
}

// abortResponse drops the connection after the 200 status was sent. The
// client sees an incomplete download (AdGuard keeps its cached filter)
// instead of a truncated list that looks complete.
func abortResponse(format string, args ...any) {
	log.Printf("[ERROR] "+format+"; aborting response", args...)
	panic(http.ErrAbortHandler)
}

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

	// Create a flusher to stream output
	flusher, ok := w.(http.Flusher)
	if !ok {
		http.Error(w, "Streaming not supported", http.StatusInternalServerError)
		return
	}

	// produce streams the source lines; errors after the status was sent abort the response.
	var produce func(yield func(string) error) error
	if req.Format == "" {
		req.Format = "list"
	}
	switch strings.ToLower(req.Format) {
	case "list":
		produce = func(yield func(string) error) error {
			scanner := bufio.NewScanner(reader)
			for scanner.Scan() {
				if err := yield(scanner.Text()); err != nil {
					return err
				}
			}
			return scanner.Err()
		}
	case "json":
		// Stream JSON array one element at a time; the opening bracket is
		// checked before the status is sent.
		dec := json.NewDecoder(reader)
		t, err := dec.Token()
		if err != nil {
			http.Error(w, fmt.Sprintf("Invalid JSON: %v", err), http.StatusBadRequest)
			return
		}
		if delim, ok := t.(json.Delim); !ok || delim != '[' {
			http.Error(w, "Expected JSON array", http.StatusBadRequest)
			return
		}
		produce = func(yield func(string) error) error {
			for dec.More() {
				var item string
				if err := dec.Decode(&item); err != nil {
					return fmt.Errorf("decoding JSON item: %w", err)
				}
				if err := yield(item); err != nil {
					return err
				}
			}
			_, err := dec.Token() // closing bracket
			return err
		}
	default:
		http.Error(w, "Unsupported format (use 'json' or 'list')", http.StatusBadRequest)
		return
	}

	// Take one snapshot of the filters before the status is sent, so a
	// missing filter is reported as an error and never as a partial list.
	var patterns []byte
	if req.FilterDist {
		distFilter := excludeMatcherDist.Load()
		if distFilter == nil {
			log.Println("[ERROR] Exclude filter not initialized: dist")
			http.Error(w, "Exclude filter not initialized: dist", http.StatusServiceUnavailable)
			return
		}
		patterns = append(patterns, distFilter.patterns...)
	}
	if req.FilterCustom {
		customFilter := excludeMatcherCustom.Load()
		if customFilter == nil {
			log.Println("[ERROR] Exclude filter not initialized: custom")
			http.Error(w, "Exclude filter not initialized: custom", http.StatusServiceUnavailable)
			return
		}
		patterns = append(patterns, customFilter.patterns...)
	}

	writeRule := func(line string) error {
		_, err := fmt.Fprintln(w, formatRule(&req, line))
		return err
	}

	if len(patterns) == 0 {
		w.Header().Set("Content-Type", "text/plain; charset=utf-8")
		w.WriteHeader(http.StatusOK)
		if err := produce(writeRule); err != nil {
			abortResponse("list %s: %v", r.URL.RawQuery, err)
		}
		flusher.Flush()
		return
	}

	// One grep per request for both filters: the process ends with the
	// request, so there is no delimiter protocol and no shared state.
	// -a keeps lines with invalid UTF-8 as text instead of "binary file matches".
	cmd := exec.CommandContext(r.Context(), "grep", "-a", "-v", "-E", "-f", "/dev/fd/3")
	cmd.Stderr = os.Stderr
	stdin, err := cmd.StdinPipe()
	if err != nil {
		http.Error(w, fmt.Sprintf("Failed to start exclude filter: %v", err), http.StatusInternalServerError)
		return
	}
	stdout, err := cmd.StdoutPipe()
	if err != nil {
		http.Error(w, fmt.Sprintf("Failed to start exclude filter: %v", err), http.StatusInternalServerError)
		return
	}
	if err := startGrep(cmd, patterns); err != nil {
		http.Error(w, fmt.Sprintf("Failed to start exclude filter: %v", err), http.StatusInternalServerError)
		return
	}

	produced := make(chan error, 1)
	go func() {
		input := bufio.NewWriter(stdin)
		err := produce(func(line string) error {
			if _, err := input.WriteString(line); err != nil {
				return err
			}
			return input.WriteByte('\n')
		})
		if flushErr := input.Flush(); err == nil {
			err = flushErr
		}
		_ = stdin.Close()
		produced <- err
	}()

	w.Header().Set("Content-Type", "text/plain; charset=utf-8")
	w.WriteHeader(http.StatusOK)

	scanner := bufio.NewScanner(stdout)
	scanner.Buffer(make([]byte, 0, 64*1024), 1024*1024) // allow long lines
	var writeErr error
	for scanner.Scan() {
		if writeErr = writeRule(scanner.Text()); writeErr != nil {
			break
		}
	}
	scanErr := scanner.Err()
	if writeErr != nil || scanErr != nil {
		// Unblock the producer and grep before waiting for them.
		_ = cmd.Process.Kill()
	}
	produceErr := <-produced
	waitErr := cmd.Wait()
	var exitError *exec.ExitError
	if errors.As(waitErr, &exitError) && exitError.ExitCode() == 1 {
		waitErr = nil // grep -v selected no lines: everything was excluded
	}

	switch {
	case writeErr != nil:
		abortResponse("writing list %s: %v", r.URL.RawQuery, writeErr)
	case produceErr != nil:
		abortResponse("reading list %s: %v", r.URL.RawQuery, produceErr)
	case scanErr != nil:
		abortResponse("reading exclude filter output for %s: %v", r.URL.RawQuery, scanErr)
	case waitErr != nil:
		abortResponse("exclude filter for %s: %v", r.URL.RawQuery, waitErr)
	}
	flusher.Flush()
}

var excludeDistPath = "/root/antizapret/config/exclude-hosts-dist.txt"
var excludeCustomPath = "/root/antizapret/config/custom/exclude-hosts-custom.txt"

// updateRegexFilter swaps both filters only after both compiled successfully;
// on error the previous filters stay active.
func updateRegexFilter() error {
	newDist, err := NewRegexFilter(excludeDistPath)
	if err != nil {
		return err
	}
	newCustom, err := NewRegexFilter(excludeCustomPath)
	if err != nil {
		return err
	}
	excludeMatcherDist.Store(newDist)
	excludeMatcherCustom.Store(newCustom)
	log.Printf("Exclude filters loaded: dist=%d custom=%d patterns", newDist.count, newCustom.count)
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
	}
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
