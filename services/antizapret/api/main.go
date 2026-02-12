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
	Url          string `schema:"url"`
	File         string `schema:"file"`
	Format       string `schema:"format"`
	Client       string `schema:"client"`        //$client=xxx
	FilterCustom bool   `schema:"filter_custom"` //skip lines with rules from exclude-hosts-custom.txt
	FilterDist   bool   `schema:"filter_dist"`   //skip lines with rules from exclude-hosts-dist.txt
	Allow        bool   `schema:"allow"`         //add @@ at the start of rule
	Raw          bool   `schema:"raw"`           //dont modify rules
	Suffix       bool   `schema:"suffix"`        //add $dnsrewrite,client=xxx to rules
}

type RegexFilter struct {
	cmd     *exec.Cmd
	stdin   io.WriteCloser
	scanner *bufio.Scanner
	lock    sync.Mutex
}

var excludeMatcherDist *RegexFilter
var excludeMatcherCustom *RegexFilter

const delim = "__DELIM__"

func (rf *RegexFilter) Filter(lines []string) ([]string, error) {
	rf.lock.Lock()
	defer rf.lock.Unlock()
	var result []string
	for _, line := range lines {
		if _, err := fmt.Fprintln(rf.stdin, line); err != nil {
			return result, err
		}
	}

	if _, err := fmt.Fprintln(rf.stdin, delim); err != nil {
		return result, err
	}

	for {
		if !rf.scanner.Scan() {
			return result, rf.scanner.Err()
		}
		text := rf.scanner.Text()
		if text == delim {
			break
		}
		result = append(result, text)
	}

	return result, nil
}

// Close terminates the subprocess cleanly
func (rf *RegexFilter) Close() error {
	if rf.stdin != nil {
		//Ensure at least one line is processed by grep to avoid exit code 1
		rf.Filter([]string{"example.com"})
		_ = rf.stdin.Close()
		rf.stdin = nil
	}
	if rf.cmd != nil {
		err := rf.cmd.Wait()
		rf.cmd = nil
		return err
	}
	return nil
}

func NewRegexFilter(file string) (*RegexFilter, error) {
	if out, err := exec.Command("sed", "-i", "s/\\s*$//", file).Output(); err != nil {
		return nil, fmt.Errorf("Failed to normalize line endings: %v, output: %s", err, string(out))
	}

	if out, err := exec.Command("gawk", "-i", "inplace", "NF", file).Output(); err != nil {
		return nil, fmt.Errorf("Failed to remove empty lines: %v, output: %s", err, string(out))
	}

	cmd := exec.Command(
		"grep",
		"--line-buffered",
		"-v",
		"-E",
		"-f",
		file,
	)

	stdin, err := cmd.StdinPipe()
	if err != nil {
		return nil, err
	}

	stdout, err := cmd.StdoutPipe()
	if err != nil {
		return nil, err
	}

	cmd.Stderr = os.Stderr

	if err := cmd.Start(); err != nil {
		return nil, err
	}

	scanner := bufio.NewScanner(stdout)
	scanner.Buffer(make([]byte, 0, 64*1024), 1024*1024) // allow long lines

	return &RegexFilter{
		cmd:     cmd,
		stdin:   stdin,
		scanner: scanner,
		lock:    sync.Mutex{},
	}, nil
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

	var buffer []string
	// Helper to process and write each line
	processBuffer := func() {
		filtered := buffer
		buffer = nil
		if req.FilterDist {
			if excludeMatcherDist == nil {
				log.Println("[ERROR] Exclude filter not initialized: dist")
				http.Error(w, "Exclude filter not initialized: dist", http.StatusInternalServerError)
				return
			}
			filtered, _ = excludeMatcherDist.Filter(filtered)
		}
		if req.FilterCustom {
			if excludeMatcherCustom == nil {
				log.Println("[ERROR] Exclude filter not initialized: custom")
				http.Error(w, "Exclude filter not initialized: custom", http.StatusInternalServerError)
				return
			}
			filtered, _ = excludeMatcherCustom.Filter(filtered)
		}

		for _, line := range filtered {
			out := strings.TrimSpace(line)
			if req.Raw || out == "" || strings.HasPrefix(out, "!") || strings.HasPrefix(out, "#") {
				//
			} else {
				if !strings.HasPrefix(line, "/") {
					out = "||" + out + "^"
				}
				if req.Suffix {
					out = fmt.Sprintf("%s$dnsrewrite,client=%s", out, req.Client)
				}

				if req.Allow {
					out = "@@" + out
				}
			}

			fmt.Fprintln(w, out)
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
				http.Error(w, fmt.Sprintf("# Error decoding JSON item: %v\n", err), http.StatusBadRequest)
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
	processBuffer()
	flusher.Flush()
}

func updateRegexFilter() error {
	var error error
	if excludeMatcherDist != nil {
		error = excludeMatcherDist.Close()
	}
	if error != nil {
		return error
	}

	excludeMatcherDist, error = NewRegexFilter(
		"/root/antizapret/config/exclude-hosts-dist.txt",
	)
	if error != nil {
		return error
	}

	if excludeMatcherCustom != nil {
		error = excludeMatcherCustom.Close()
	}
	if error != nil {
		return error
	}

	excludeMatcherCustom, error = NewRegexFilter(
		"/root/antizapret/config/custom/exclude-hosts-custom.txt",
	)
	return error
}

func update(w http.ResponseWriter, r *http.Request) {
	error := updateRegexFilter()
	if error != nil {
		http.Error(w, fmt.Sprintf("Failed to update exclude lists: %v", error), http.StatusInternalServerError)
		return
	}
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

	err := updateRegexFilter()
	if err != nil {
		log.Fatalf("Failed to initialize regex filters: %v", err)
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

	fmt.Println("Starting server on http://localhost:80")
	log.Fatal(http.ListenAndServe(":80", loggingMiddleware(r)))
}
