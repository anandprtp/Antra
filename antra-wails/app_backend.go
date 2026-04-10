package main

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io/fs"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"time"

	wailsRuntime "github.com/wailsapp/wails/v2/pkg/runtime"
)

type Config struct {
	DownloadPath     string `json:"download_path"`
	SoulseekEnabled  bool   `json:"soulseek_enabled"`
	SoulseekUsername string `json:"soulseek_username,omitempty"`
	SoulseekPassword string `json:"soulseek_password,omitempty"`
	FirstRunComplete bool   `json:"first_run_complete"`
	OutputFormat     string `json:"output_format,omitempty"`
}

type HistoryItem struct {
	Date       string         `json:"date"`
	URL        string         `json:"url"`
	Total      int            `json:"total"`
	Downloaded int            `json:"downloaded"`
	Failed     int            `json:"failed"`
	Skipped    int            `json:"skipped"`
	Sources    map[string]int `json:"sources"`
}

func getAppDataDir() string {
	switch runtime.GOOS {
	case "windows":
		localAppData := os.Getenv("LOCALAPPDATA")
		return filepath.Join(localAppData, "Antra")
	case "darwin":
		home := os.Getenv("HOME")
		return filepath.Join(home, "Library", "Application Support", "Antra")
	default:
		home := os.Getenv("HOME")
		return filepath.Join(home, ".local", "share", "Antra")
	}
}

func getConfigPath() string {
	return filepath.Join(getAppDataDir(), "config.json")
}

func getHistoryPath() string {
	return filepath.Join(getAppDataDir(), "history.json")
}

// GetConfig returns the application configuration
func (a *App) GetConfig() Config {
	var cfg Config
	cfgPath := getConfigPath()
	if _, err := os.Stat(cfgPath); os.IsNotExist(err) {
		userProfile := os.Getenv("USERPROFILE")
		if userProfile == "" {
			userProfile = os.Getenv("HOME")
		}
		cfg.DownloadPath = filepath.Join(userProfile, "Music")
		return cfg
	}

	data, err := os.ReadFile(cfgPath)
	if err != nil {
		wailsRuntime.LogErrorf(a.ctx, "Failed to read config: %v", err)
		cfg.DownloadPath = "./Music"
		return cfg
	}

	json.Unmarshal(data, &cfg)
	if cfg.DownloadPath == "" {
		userProfile := os.Getenv("USERPROFILE")
		if userProfile == "" {
			userProfile = os.Getenv("HOME")
		}
		cfg.DownloadPath = filepath.Join(userProfile, "Music")
	}
	return cfg
}

// SaveConfig saves the configuration and marks first run as complete
func (a *App) SaveConfig(cfg Config) error {
	cfg.FirstRunComplete = true
	dir := getAppDataDir()
	if err := os.MkdirAll(dir, 0755); err != nil {
		return err
	}

	data, err := json.MarshalIndent(cfg, "", "  ")
	if err != nil {
		return err
	}

	return os.WriteFile(getConfigPath(), data, 0644)
}

// GetHistory returns the application history
func (a *App) GetHistory() []HistoryItem {
	var history []HistoryItem
	historyPath := getHistoryPath()

	if _, err := os.Stat(historyPath); os.IsNotExist(err) {
		return history
	}

	data, err := os.ReadFile(historyPath)
	if err != nil {
		wailsRuntime.LogErrorf(a.ctx, "Failed to read history: %v", err)
		return history
	}

	json.Unmarshal(data, &history)
	return history
}

// AddHistory appends a new run to the history file
func (a *App) AddHistory(item HistoryItem) error {
	history := a.GetHistory()
	history = append([]HistoryItem{item}, history...) // prepend

	// Keep history bounded if needed, here keeping all for now.
	data, err := json.MarshalIndent(history, "", "  ")
	if err != nil {
		return err
	}

	dir := getAppDataDir()
	os.MkdirAll(dir, 0755)
	return os.WriteFile(getHistoryPath(), data, 0644)
}

// ClearHistory deletes history
func (a *App) ClearHistory() error {
	path := getHistoryPath()
	if _, err := os.Stat(path); err == nil {
		return os.Remove(path)
	}
	return nil
}

// PickDirectory opens a folder selection dialog for the user
func (a *App) PickDirectory() string {
	dir, err := wailsRuntime.OpenDirectoryDialog(a.ctx, wailsRuntime.OpenDialogOptions{
		Title: "Select Download Folder",
	})
	if err != nil {
		return ""
	}
	return dir
}

// CancelDownload cancels the active download session
func (a *App) CancelDownload() {
	a.mu.Lock()
	a.isStopping = true
	a.mu.Unlock()

	cancel, cmd := a.detachActiveDownload()
	// Kill the process tree BEFORE cancelling the context.
	// If we cancel() first, Go kills the parent PID, which breaks the
	// tree relationship and taskkill /T can no longer find children.
	if err := killCommandTree(cmd); err != nil {
		wailsRuntime.LogErrorf(a.ctx, "Failed to stop library engine: %v", err)
	}
	if cancel != nil {
		cancel()
	}
	wailsRuntime.LogInfof(a.ctx, "Download cancelled by user")
}

// StartDownload starts the Python backend process and streams output
func (a *App) StartDownload(playlists []string) error {
	wailsRuntime.LogInfof(a.ctx, "Starting download for: %v", playlists)

	if cancel, cmd := a.detachActiveDownload(); cancel != nil || cmd != nil {
		if cancel != nil {
			cancel()
		}
		if err := killCommandTree(cmd); err != nil {
			wailsRuntime.LogWarningf(a.ctx, "Failed to stop previous library engine: %v", err)
		}
	}

	a.mu.Lock()
	a.isStopping = false
	a.mu.Unlock()

	ctx, cancel := context.WithCancel(a.ctx)

	command, args, workDir, env, err := a.resolveBackendCommand(playlists)
	if err != nil {
		cancel()
		wailsRuntime.LogErrorf(a.ctx, err.Error())
		return err
	}

	cmd := exec.CommandContext(ctx, command, args...)
	hideProcess(cmd)
	cmd.Dir = workDir
	cmd.Env = env

	stdout, err := cmd.StdoutPipe()
	if err != nil {
		cancel()
		return err
	}
	cmd.Stderr = cmd.Stdout // merge stderr into stdout for parsing

	if err := cmd.Start(); err != nil {
		cancel()
		return err
	}
	a.attachActiveDownload(cancel, cmd)

	go func() {
		scanner := bufio.NewScanner(stdout)
		for scanner.Scan() {
			a.mu.Lock()
			stopping := a.isStopping
			a.mu.Unlock()

			// Stop emitting events once the context has been cancelled or a stop was requested.
			// Use break (not return) so we still fall through to cmd.Wait() and process_ended.
			if ctx.Err() != nil || stopping {
				break
			}
			line := scanner.Text()

			// Filter out noisy yt-dlp warnings and progress updates
			lowerLine := strings.ToLower(line)
			if strings.Contains(line, "No supported JavaScript runtime") ||
				strings.Contains(line, "YouTube extraction without a JS runtime") ||
				strings.Contains(lowerLine, "deno is enabled by default") ||
				strings.Contains(lowerLine, "js-runtimes") ||
				strings.HasPrefix(line, "[download]") ||
				strings.Contains(line, "% of ") {
				continue
			}

			// Try to parse as JSON first — apply message-level filtering only to plain log messages
			if json.Valid([]byte(line)) {
				var probe map[string]interface{}
				if json.Unmarshal([]byte(line), &probe) == nil && probe["type"] == "log" {
					msg, _ := probe["message"].(string)
					if shouldHideLogMessage(msg) {
						continue
					}
				}
			}

			// Parse JSON line and re-emit via Wails
			var payload map[string]interface{}
			if err := json.Unmarshal([]byte(line), &payload); err == nil {
				wailsRuntime.EventsEmit(a.ctx, "backend-event", payload)
			} else {
				// If it's not JSON, just send it as a raw log
				fallback := map[string]interface{}{
					"type":    "log",
					"level":   "info",
					"message": line,
				}
				wailsRuntime.EventsEmit(a.ctx, "backend-event", fallback)
			}
		}

		scanErr := scanner.Err()
		err := cmd.Wait()
		a.clearActiveDownload(cmd)

		status := "completed"
		if ctx.Err() == context.Canceled {
			status = "cancelled"
		} else if scanErr != nil || err != nil {
			status = "failed"
		}

		if scanErr != nil && ctx.Err() != context.Canceled {
			wailsRuntime.EventsEmit(a.ctx, "backend-event", map[string]interface{}{
				"type":    "log",
				"level":   "error",
				"message": fmt.Sprintf("Library engine stream failed: %v", scanErr),
			})
		}
		if err != nil && ctx.Err() != context.Canceled {
			wailsRuntime.EventsEmit(a.ctx, "backend-event", map[string]interface{}{
				"type":    "log",
				"level":   "error",
				"message": fmt.Sprintf("Library engine exited with error: %v", err),
			})
		}
		wailsRuntime.EventsEmit(a.ctx, "backend-event", map[string]interface{}{
			"type":   "process_ended",
			"status": status,
		})
	}()

	return nil
}

func (a *App) attachActiveDownload(cancel context.CancelFunc, cmd *exec.Cmd) {
	a.mu.Lock()
	defer a.mu.Unlock()
	a.cancelDownload = cancel
	a.activeCmd = cmd
}

func (a *App) detachActiveDownload() (context.CancelFunc, *exec.Cmd) {
	a.mu.Lock()
	defer a.mu.Unlock()

	cancel := a.cancelDownload
	cmd := a.activeCmd
	a.cancelDownload = nil
	a.activeCmd = nil
	return cancel, cmd
}

func (a *App) clearActiveDownload(cmd *exec.Cmd) {
	a.mu.Lock()
	defer a.mu.Unlock()

	if a.activeCmd == cmd {
		a.activeCmd = nil
		a.cancelDownload = nil
	}
}

// shouldHideLogMessage returns true for internal/noisy log lines that the
// desktop UI should not surface to the user.
func shouldHideLogMessage(msg string) bool {
	noisePrefixes := []string{
		"[OK] HiFi adapter",
		"[OK] Amazon adapter",
		"[OK] Apple Music adapter",
		"[OK] JioSaavn adapter",
		"[OK] Qobuz adapter",
		"[OK] Deezer adapter",
		"[OK] Tidal adapter",
		"[OK] YAMS adapter",
		"[OK] Soulseek adapter",
		"[OK] Source preference",
		"[Gate]",
		"[HiFi]",
		"[Resolver]",
		"[DL]",
		"[OK] Done:",
		"[Qobuz]",
		"[Yams]",
		"[Apple]",
		"[Amazon]",
		"[Soulseek]",
		"[LinkResolver]",
		"[Songwhip]",
		"[Odesli]",
		"[QobuzCreds]",
	}
	for _, prefix := range noisePrefixes {
		if strings.HasPrefix(msg, prefix) {
			return true
		}
	}
	return false
}

func killCommandTree(cmd *exec.Cmd) error {
	if cmd == nil || cmd.Process == nil {
		return nil
	}

	if runtime.GOOS == "windows" {
		killer := exec.Command("taskkill", "/PID", fmt.Sprintf("%d", cmd.Process.Pid), "/T", "/F")
		hideProcess(killer)
		output, err := killer.CombinedOutput()
		if err != nil {
			text := strings.ToLower(string(output))
			if strings.Contains(text, "not found") || strings.Contains(text, "no running instance") {
				return nil
			}
			return fmt.Errorf("taskkill failed: %v (%s)", err, strings.TrimSpace(string(output)))
		}
		return nil
	}

	if err := cmd.Process.Kill(); err != nil && !errors.Is(err, os.ErrProcessDone) {
		return err
	}
	return nil
}

func (a *App) runPythonCommand(args []string) (string, error) {
	pythonExe, _, workDir, env, err := a.resolveBackendCommand([]string{})
	if err != nil {
		return "", err
	}

	// We want to run python -m antra <args>
	// resolveBackendCommand returns ['json_cli.py', '--config', '...']
	// We need to swap json_cli.py with -m antra

	finalArgs := []string{"-m", "antra"}
	finalArgs = append(finalArgs, args...)

	cmd := exec.Command(pythonExe, finalArgs...)
	cmd.Dir = workDir
	cmd.Env = env
	hideProcess(cmd)

	output, err := cmd.CombinedOutput()
	if err != nil {
		return string(output), err
	}
	return string(output), nil
}

// Spotify Auth & Management

// GetArtistDiscography fetches the full release list for an artist URL.
// Returns a JSON string: {"artist_id","artist_name","artwork_url","albums":[...]}
// On error returns: {"error":"..."}
func (a *App) GetArtistDiscography(artistUrl string) string {
	ctx, cancel := context.WithTimeout(context.Background(), 60*time.Second)
	defer cancel()

	backend, err := ensureBundledBackend()
	if err != nil {
		// Dev fallback: run via python source
		return a.getArtistDiscographyViaPython(ctx, artistUrl)
	}

	cmd := exec.CommandContext(ctx, backend, "--discography", artistUrl, "--config", getConfigPath())
	hideProcess(cmd)
	out, err := cmd.Output()
	if err != nil {
		if ctx.Err() == context.DeadlineExceeded {
			return `{"error":"timed out fetching discography (60s)"}`
		}
		return `{"error":"` + strings.ReplaceAll(err.Error(), `"`, `'`) + `"}`
	}
	return unwrapDiscographyJSON(out)
}

func (a *App) getArtistDiscographyViaPython(ctx context.Context, artistUrl string) string {
	pythonExe, _, workDir, env, err := a.resolveBackendCommand([]string{})
	if err != nil {
		return `{"error":"could not resolve backend"}`
	}
	cmd := exec.CommandContext(ctx, pythonExe, "-m", "antra.json_cli", "--discography", artistUrl, "--config", getConfigPath())
	cmd.Dir = workDir
	cmd.Env = env
	hideProcess(cmd)
	out, err := cmd.Output()
	if err != nil {
		if ctx.Err() == context.DeadlineExceeded {
			return `{"error":"timed out fetching discography (60s)"}`
		}
		return `{"error":"` + strings.ReplaceAll(err.Error(), `"`, `'`) + `"}`
	}
	return unwrapDiscographyJSON(out)
}

// unwrapDiscographyJSON unpacks {"type":"discography","data":{...}} → just the data object.
func unwrapDiscographyJSON(out []byte) string {
	var wrapper map[string]interface{}
	if jsonErr := json.Unmarshal(bytes.TrimSpace(out), &wrapper); jsonErr != nil {
		return string(out)
	}
	if wrapper["type"] == "error" {
		msg, _ := wrapper["message"].(string)
		return `{"error":"` + strings.ReplaceAll(msg, `"`, `'`) + `"}`
	}
	result, _ := json.Marshal(wrapper["data"])
	return string(result)
}

func (a *App) GetSpotifyStatus() string {
	output, err := a.runPythonCommand([]string{"spotify", "status", "--json"})
	if err != nil {
		return `{"authenticated": false, "error": "` + err.Error() + `"}`
	}
	return output
}

func (a *App) LoginSpotify() string {
	// This opens the browser and waits for the automated capture
	output, err := a.runPythonCommand([]string{"spotify", "login"})
	if err != nil {
		return `{"success": false, "error": "` + err.Error() + `"}`
	}
	return output
}

func (a *App) LogoutSpotify() string {
	output, err := a.runPythonCommand([]string{"spotify", "logout", "--json"})
	if err != nil {
		return `{"success": false, "error": "` + err.Error() + `"}`
	}
	return output
}

func (a *App) GetSpotifyPlaylists() string {
	output, err := a.runPythonCommand([]string{"spotify", "playlists", "--json"})
	if err != nil {
		return `{"error": "` + err.Error() + `"}`
	}
	return output
}

func (a *App) SetSpotifyCookie(spDc string) string {
	output, err := a.runPythonCommand([]string{"spotify", "set-cookie", spDc})
	if err != nil {
		return `{"success": false, "error": "` + err.Error() + `"}`
	}
	return `{"success": true, "message": "` + strings.TrimSpace(output) + `"}`
}

func (a *App) SetSpotifyToken(token string) string {
	output, err := a.runPythonCommand([]string{"spotify", "set-token", token})
	if err != nil {
		return `{"success": false, "error": "` + err.Error() + `"}`
	}
	return `{"success": true, "message": "` + strings.TrimSpace(output) + `"}`
}

func (a *App) resolveBackendCommand(playlists []string) (string, []string, string, []string, error) {
	if bundledBackend, err := ensureBundledBackend(); err == nil {
		args := append([]string{}, playlists...)
		args = append(args, "--config", getConfigPath())
		return bundledBackend, args, filepath.Dir(bundledBackend), os.Environ(), nil
	} else if !errors.Is(err, fs.ErrNotExist) {
		return "", nil, "", nil, fmt.Errorf("failed to prepare bundled backend: %w", err)
	}

	// Dev fallback: run the Python backend directly from source.
	pythonExe := "python"
	exePath, _ := os.Executable()
	exeDir := filepath.Dir(exePath)
	currentDir, _ := os.Getwd()

	candidates := uniqueCleanPaths([]string{
		exeDir,
		filepath.Join(exeDir, "resources"),
		filepath.Join(exeDir, ".."),
		filepath.Join(exeDir, "..", ".."),
		filepath.Join(exeDir, "..", "..", ".."),
		currentDir,
		filepath.Join(currentDir, ".."),
	})

	var parentDir string
	var jsonCliScript string
	for _, dir := range candidates {
		testPath := filepath.Join(dir, "antra", "json_cli.py")
		if _, err := os.Stat(testPath); err == nil {
			parentDir = dir
			jsonCliScript = testPath
			break
		}
	}

	if jsonCliScript == "" {
		return "", nil, "", nil, fmt.Errorf(
			"could not find bundled backend or antra/json_cli.py; checked: %s",
			strings.Join(candidates, ", "),
		)
	}

	args := []string{jsonCliScript}
	args = append(args, playlists...)
	args = append(args, "--config", getConfigPath())
	env := append(os.Environ(), fmt.Sprintf("PYTHONPATH=%s", parentDir))
	return pythonExe, args, parentDir, env, nil
}

func uniqueCleanPaths(paths []string) []string {
	seen := make(map[string]struct{}, len(paths))
	result := make([]string, 0, len(paths))
	for _, path := range paths {
		clean := filepath.Clean(path)
		if _, ok := seen[clean]; ok {
			continue
		}
		seen[clean] = struct{}{}
		result = append(result, clean)
	}
	return result
}
