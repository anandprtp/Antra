package main

import (
	"context"
	"encoding/json"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"sync"

	wailsRuntime "github.com/wailsapp/wails/v2/pkg/runtime"
)

// App struct
type App struct {
	ctx            context.Context
	mu             sync.Mutex
	cancelDownload context.CancelFunc
	activeCmd      *exec.Cmd
	isStopping     bool
	ffmpegExe      string // absolute path to bundled ffmpeg (empty = use PATH)
	ffprobeExe     string // absolute path to bundled ffprobe (empty = use PATH)
}

// NewApp creates a new App application struct
func NewApp() *App {
	return &App{}
}

// startup is called when the app starts. The context is saved
// so we can call the runtime methods
func (a *App) startup(ctx context.Context) {
	a.ctx = ctx
}

// domReady is called after the frontend DOM has finished loading.
// We reveal the window here to avoid the white/unstyled flash that
// occurs when the window is shown before the Svelte app has mounted.
func (a *App) domReady(ctx context.Context) {
	wailsRuntime.WindowShow(ctx)
	go a.cacheFfmpegPaths()
}

// cacheFfmpegPaths asks the bundled Python backend where its ffmpeg lives so
// the Go analyzer can use a full path rather than relying on system PATH.
func (a *App) cacheFfmpegPaths() {
	backend, err := ensureBundledBackend()
	if err != nil {
		return
	}
	cmd := exec.Command(backend, "--get-ffmpeg-dir")
	hideProcess(cmd)
	out, err := cmd.Output()
	if err != nil {
		return
	}
	// Output is two lines: ffmpeg path, ffprobe path (either may be empty)
	lines := strings.SplitN(strings.ReplaceAll(strings.TrimSpace(string(out)), "\r\n", "\n"), "\n", 2)
	ffmpegPath := ""
	ffprobePath := ""
	if len(lines) >= 1 {
		ffmpegPath = strings.TrimSpace(lines[0])
	}
	if len(lines) >= 2 {
		ffprobePath = strings.TrimSpace(lines[1])
	}

	a.mu.Lock()
	defer a.mu.Unlock()
	if ffmpegPath != "" {
		if _, err := os.Stat(ffmpegPath); err == nil {
			a.ffmpegExe = ffmpegPath
		}
	}
	if ffprobePath != "" {
		if _, err := os.Stat(ffprobePath); err == nil {
			a.ffprobeExe = ffprobePath
		}
	}
}

// shutdown is called when the application is closing.
// Clean up any running backend processes so we don't leave orphans.
func (a *App) shutdown(ctx context.Context) {
	_, cmd := a.detachActiveDownload()
	if cmd != nil {
		_ = killCommandTree(cmd)
	}

	if runtime.GOOS == "windows" {
		// Kill by process name to catch any orphaned children.
		for _, name := range []string{"AntraBackend.exe", "slskd.exe"} {
			killer := exec.Command("taskkill", "/IM", name, "/F")
			hideProcess(killer)
			_ = killer.Run()
		}
	} else {
		// macOS / Linux: kill slskd by the PID we saved in the state file,
		// then fall back to pkill in case the state file is missing or stale.
		killed := false
		if pid := readSlskdPID(); pid > 0 {
			if p, err := os.FindProcess(pid); err == nil {
				if err := p.Kill(); err == nil {
					killed = true
				}
			}
		}
		if !killed {
			_ = exec.Command("pkill", "-f", "slskd").Run()
		}
	}
}

// readSlskdPID returns the PID stored by SlskdBootstrapManager in its state
// file, or 0 if the file is absent / unreadable.
func readSlskdPID() int {
	stateFile := slskdStatePath()
	if stateFile == "" {
		return 0
	}
	data, err := os.ReadFile(stateFile)
	if err != nil {
		return 0
	}
	var state map[string]interface{}
	if err := json.Unmarshal(data, &state); err != nil {
		return 0
	}
	switch v := state["pid"].(type) {
	case float64:
		return int(v)
	case int:
		return v
	}
	return 0
}

// slskdStatePath returns the platform-specific path to slskd's state.json.
func slskdStatePath() string {
	switch runtime.GOOS {
	case "windows":
		local := os.Getenv("LOCALAPPDATA")
		if local == "" {
			return ""
		}
		return filepath.Join(local, "antra", "slskd", "runtime", "state.json")
	default:
		home, err := os.UserHomeDir()
		if err != nil {
			return ""
		}
		return filepath.Join(home, ".cache", "antra", "slskd", "runtime", "state.json")
	}
}
