package main

import (
	"encoding/json"
	"fmt"
	"log"
	"net/http"
	"os"
	"os/exec"
	"sync"
)

var isScriptRunning bool
var mu sync.Mutex


type DoAllRequest struct {
	Stage1 bool `json:"stage_1"`
	Stage2 bool `json:"stage_2"`
	Stage3 bool `json:"stage_3"`
}

func doallHandler(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "Invalid request method", http.StatusMethodNotAllowed)
		return
	}

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

	var req DoAllRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		http.Error(w, "Invalid request body", http.StatusBadRequest)
		return
	}

	env := os.Environ()
	env = append(env, fmt.Sprintf("STAGE_1=%t", req.Stage1))
	env = append(env, fmt.Sprintf("STAGE_2=%t", req.Stage2))
	env = append(env, fmt.Sprintf("STAGE_3=%t", req.Stage3))

	cmd := exec.Command("/root/antizapret/doall.sh")
	cmd.Env = env

	output, err := cmd.CombinedOutput()

	if err != nil {
		http.Error(w, fmt.Sprintf("Failed to execute script: %s", err.Error()), http.StatusInternalServerError)
		return
	}

	w.WriteHeader(http.StatusOK)
	_, _ = w.Write(output)
}

func main() {
	http.HandleFunc("/doall", doallHandler)

	fmt.Println("Starting server on http://localhost:8080")
	log.Fatal(http.ListenAndServe(":8080", nil))
}