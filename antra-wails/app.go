package main

import (
	"context"
	"encoding/json"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"sync"
	"time"

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
	ffmpegOnce     sync.Once
	mediaServer    *http.Server
	mediaBaseURL   string
	mediaToken     string
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
	go a.ensureFfmpegPaths()
	go a.startAutoSyncTicker(ctx)
}

// startAutoSyncTicker checks every minute whether the auto-sync schedule has
// been met and, if so, spawns the Python backend with --auto-sync.
// Schedule is read from config.json each tick so changes take effect without restart.
func (a *App) startAutoSyncTicker(ctx context.Context) {
	ticker := time.NewTicker(60 * time.Second)
	defer ticker.Stop()

	// Align to the next whole minute so we don't double-fire near startup.
	time.Sleep(time.Until(time.Now().Truncate(time.Minute).Add(time.Minute)))

	for {
		select {
		case <-ctx.Done():
			return
		case now := <-ticker.C:
			a.maybeRunAutoSync(now)
		}
	}
}

// maybeRunAutoSync reads config.json, checks whether the current time matches
// the configured auto-sync schedule, and spawns the backend if it does.
func (a *App) maybeRunAutoSync(now time.Time) {
	cfgPath := getConfigPath()
	data, err := os.ReadFile(cfgPath)
	if err != nil {
		return
	}
	var cfg struct {
		AutoSyncEnabled  bool          `json:"auto_sync_enabled"`
		AutoSyncHour     int           `json:"auto_sync_hour"`
		AutoSyncMinute   int           `json:"auto_sync_minute"`
		AutoSyncDays     int           `json:"auto_sync_days"` // bitmask: Mon=bit0 … Sun=bit6
		TrackedPlaylists []interface{} `json:"tracked_playlists"`
	}
	if err := json.Unmarshal(data, &cfg); err != nil {
		return
	}
	if !cfg.AutoSyncEnabled || len(cfg.TrackedPlaylists) == 0 {
		return
	}

	// Check day-of-week bitmask (Go: Sunday=0, but we use Monday=bit0)
	dow := int(now.Weekday()) // 0=Sunday … 6=Saturday
	bit := (dow + 6) % 7      // Monday=0 … Sunday=6
	if cfg.AutoSyncDays&(1<<bit) == 0 {
		return
	}

	// Check hour and minute
	if now.Hour() != cfg.AutoSyncHour || now.Minute() != cfg.AutoSyncMinute {
		return
	}

	// Schedule matched — spawn auto-sync in background.
	// Delegate to RunAutoSync() rather than spawning our own process here:
	// RunAutoSync() registers itself as the App's active command (a.mu /
	// a.activeCmd), which is what lets shutdown() and a later manual
	// StartDownload() find and stop it. A bare exec.Command call here (the
	// old code) was invisible to both, so this scheduled sync could outlive
	// the GUI as an orphan, or run concurrently with a manual sync against
	// the same download folder.
	go func() {
		result := a.RunAutoSync()
		wailsRuntime.EventsEmit(a.ctx, "auto_sync_complete", result)
	}()
}

// ensureFfmpegPaths resolves the bundled ffmpeg/ffprobe exactly once per
// process. It is kicked off in the background at startup so the cost is hidden,
// but every consumer calls it too: the resolution spawns the PyInstaller
// backend, which takes seconds on a cold machine, and an analyzer opened inside
// that window would otherwise read an empty path and fall back to bare
// "ffmpeg" on PATH — i.e. exactly the failure this whole path exists to avoid.
func (a *App) ensureFfmpegPaths() {
	a.ffmpegOnce.Do(a.cacheFfmpegPaths)
}

// cacheFfmpegPaths asks the bundled Python backend where its ffmpeg lives so
// the Go analyzer can use a full path rather than relying on system PATH.
//
// It uses --export-ffmpeg, NOT --get-ffmpeg-dir. Inside the PyInstaller bundle
// the ffmpeg binary lives in sys._MEIPASS, which is deleted the instant that
// backend process exits — so --get-ffmpeg-dir hands back a path that is already
// gone by the time we stat it. On a developer machine this was invisible,
// because get_ffmpeg_exe() checks PATH first and returns a stable system path;
// on a clean install it meant the analyzer's stats and FFT panels could never
// work. --export-ffmpeg copies the binary somewhere persistent first.
func (a *App) cacheFfmpegPaths() {
	backend, err := ensureBundledBackend()
	if err != nil {
		return
	}
	binDir := filepath.Join(getAppDataDir(), "runtime", "ffmpeg")
	cmd := exec.Command(backend, "--export-ffmpeg", binDir)
	hideProcess(cmd)
	out, err := cmd.Output()
	if err != nil {
		// An older backend predates --export-ffmpeg and argparse exits non-zero.
		// Fall back so a mismatched dev tree still resolves something usable.
		cmd = exec.Command(backend, "--get-ffmpeg-dir")
		hideProcess(cmd)
		if out, err = cmd.Output(); err != nil {
			return
		}
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

	a.mu.Lock()
	mediaServer := a.mediaServer
	a.mediaServer = nil
	a.mediaBaseURL = ""
	a.mediaToken = ""
	a.mu.Unlock()
	if mediaServer != nil {
		_ = mediaServer.Close()
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
