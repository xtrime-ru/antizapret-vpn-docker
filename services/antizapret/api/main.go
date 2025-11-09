package main

import (
	"bufio"
	"compress/gzip"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"net/http"
	"os"
	"os/exec"
	"regexp"
	"runtime"
	"strings"
	"sync"
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

	cmd := exec.Command("/root/antizapret/doall.sh")

	output, err := cmd.CombinedOutput()

	if err != nil {
		http.Error(w, fmt.Sprintf("Failed to execute script: %s", err.Error()), http.StatusInternalServerError)
		return
	}

	w.WriteHeader(http.StatusOK)
	_, _ = w.Write(output)
}

var decoder = schema.NewDecoder()

type ListRequest struct {
	Url    string `schema:"url"`
	File   string `schema:"file"`
	Format string `schema:"format"`
	Client string `schema:"client"` //$client=xxx
	Filter bool   `schema:"filter"` //skip lines with rules from exclude-hosts-{dist,custom}.txt
	Allow  bool   `schema:"allow"`  //add @@ at the start of rule
}

// RegexMatcher holds compiled regex rules
type RegexMatcher struct {
	substrs []string
	regexes []*regexp.Regexp
}

var excludeMatcher *RegexMatcher

// MatchString returns true if the input matches any of the compiled regex rules
func (rm *RegexMatcher) MatchString(s string) bool {
	for _, sub := range rm.substrs {
		if strings.Contains(strings.ToLower(s), sub) {
			return true
		}
	}
	for _, re := range rm.regexes {
		if re.MatchString(s) {
			return true
		}
	}
	return false
}

// NewRegexMatcher creates a new RegexMatcher from a list of file paths
func NewRegexMatcher(files []string) *RegexMatcher {
	var compiled []*regexp.Regexp
	var substrs []string

	for _, file := range files {
		f, err := os.Open(file)
		if err != nil {
			log.Printf("Failed to open file %s: %v", file, err)
			continue
		}

		scanner := bufio.NewScanner(f)
		for scanner.Scan() {
			line := strings.TrimSpace(scanner.Text())
			// Skip empty lines and comment lines
			if line == "" || strings.HasPrefix(line, "#") || strings.HasPrefix(line, "!") {
				continue
			}

			if !strings.ContainsAny(line, ".^$[]*+(){}|\\") {
				substrs = append(substrs, strings.ToLower(line))
				continue
			}

			re, err := regexp.Compile("(?i)" + line)
			if err != nil {
				log.Printf("Invalid regex '%s' in file %s: %v", line, file, err)
				continue
			}
			compiled = append(compiled, re)
		}

		if err := scanner.Err(); err != nil {
			log.Printf("Error reading file %s: %v", file, err)
		}

		f.Close()
	}

	return &RegexMatcher{regexes: compiled, substrs: substrs}
}

var DefaultClient string

func adaptList(w http.ResponseWriter, r *http.Request) {
	req := ListRequest{
		Client: DefaultClient,
		Filter: true, //
		Allow:  true, // default (adds @@)
	}

	if err := decoder.Decode(&req, r.URL.Query()); err != nil {
		http.Error(w, fmt.Sprintf("Invalid request body: %v", err), http.StatusBadRequest)
		return
	}

	var reader io.ReadCloser
	if req.Url != "" {
		// Create a new HTTP request
		reqRemote, err := http.NewRequest("GET", req.Url, nil)
		if err != nil {
			http.Error(w, fmt.Sprintf("Failed to create request: %v", err), http.StatusInternalServerError)
			return
		}

		// Forward all headers from the original request
		for name, values := range r.Header {
			for _, value := range values {
				reqRemote.Header.Add(name, value)
			}
		}

		// Perform the request
		client := &http.Client{}
		resp, err := client.Do(reqRemote)
		if err != nil {
			http.Error(w, fmt.Sprintf("Failed to download list: %v", err), http.StatusInternalServerError)
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
		file, err := os.Open(req.File)
		if err != nil {
			http.Error(w, fmt.Sprintf("Failed to open local file: %v", err), http.StatusInternalServerError)
			return
		}
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

	w.Header().Set("Content-Type", "text/plain; charset=utf-8")
	w.WriteHeader(http.StatusOK)

	// Helper to process and write each line
	processLine := func(line string) {
		line = strings.TrimSpace(line)
		// Skip empty lines or comments
		if line == "" || strings.HasPrefix(line, "!") || strings.HasPrefix(line, "#") {
			return
		}

		// Skip if line matches any exclude regex
		if req.Filter && excludeMatcher.MatchString(line) {
			return
		}

		var out string
		if strings.HasPrefix(line, "/") {
			out = fmt.Sprintf("%s$dnsrewrite,client=%s", line, req.Client)
		} else {
			out = fmt.Sprintf("||%s^$dnsrewrite,client=%s", line, req.Client)
		}
		if req.Allow {
			out = "@@" + out
		}

		fmt.Fprintln(w, out)
		flusher.Flush()
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
			fmt.Fprintf(w, "# Error reading list: %v\n", err)
		}
	case "json":
		// Stream JSON array one element at a time
		dec := json.NewDecoder(reader)

		// Expect start of array
		t, err := dec.Token()
		if err != nil {
			http.Error(w, fmt.Sprintf("Invalid JSON: %v", err), http.StatusBadRequest)
			return
		}
		if delim, ok := t.(json.Delim); !ok || delim != '[' {
			http.Error(w, "Expected JSON array", http.StatusBadRequest)
			return
		}

		// Decode each element until end of array
		for dec.More() {
			var item string
			if err := dec.Decode(&item); err != nil {
				fmt.Fprintf(w, "# Error decoding JSON item: %v\n", err)
				break
			}
			processLine(item)
		}

		// Consume closing bracket
		_, _ = dec.Token()
	default:
		http.Error(w, "Unsupported format (use 'json' or 'list')", http.StatusBadRequest)
		return
	}
}

func update(w http.ResponseWriter, r *http.Request) {
	excludeMatcher = NewRegexMatcher([]string{
		"/root/antizapret/config/exclude-hosts-dist.txt",
		"/root/antizapret/config/custom/exclude-hosts-custom.txt",
	})
	w.WriteHeader(http.StatusOK)
	_, _ = w.Write([]byte("ok"))
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

	excludeMatcher = NewRegexMatcher([]string{
		"/root/antizapret/config/exclude-hosts-dist.txt",
		"/root/antizapret/config/custom/exclude-hosts-custom.txt",
	})
	// Create a mux so we can wrap all handlers with logging
	r := http.NewServeMux()

	// Optional trailing slash via regex
	r.HandleFunc(`/list/`, adaptList)
	r.HandleFunc(`/doall/`, doallHandler)
	r.HandleFunc(`/update/`, update)

	fmt.Println("Starting server on http://localhost:80")
	log.Fatal(http.ListenAndServe(":80", loggingMiddleware(r)))
}
