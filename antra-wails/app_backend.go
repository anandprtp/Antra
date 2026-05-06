package main

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"runtime"
	"strings"
	"sync"
	"time"

	wailsRuntime "github.com/wailsapp/wails/v2/pkg/runtime"
)

type Config struct {
	DownloadPath            string   `json:"download_path"`
	AppleEnabled            bool     `json:"apple_enabled"`
	AppleAuthorizationToken string   `json:"apple_authorization_token,omitempty"`
	AppleMusicUserToken     string   `json:"apple_music_user_token,omitempty"`
	AppleStorefront         string   `json:"apple_storefront,omitempty"`
	AppleWVDPath            string   `json:"apple_wvd_path,omitempty"`
	AmazonEnabled           bool     `json:"amazon_enabled"`
	AmazonDirectCredsJSON   string   `json:"amazon_direct_creds_json,omitempty"`
	AmazonWVDPath           string   `json:"amazon_wvd_path,omitempty"`
	AmazonRegion            string   `json:"amazon_region,omitempty"`
	QobuzEnabled            bool     `json:"qobuz_enabled"`
	QobuzEmail              string   `json:"qobuz_email,omitempty"`
	QobuzPassword           string   `json:"qobuz_password,omitempty"`
	QobuzAppID              string   `json:"qobuz_app_id,omitempty"`
	QobuzAppSecret          string   `json:"qobuz_app_secret,omitempty"`
	QobuzUserAuthToken      string   `json:"qobuz_user_auth_token,omitempty"`
	DeezerARLToken          string   `json:"deezer_arl_token,omitempty"`
	DeezerBFSecret          string   `json:"deezer_bf_secret,omitempty"`
	SoulseekEnabled         bool     `json:"soulseek_enabled"`
	SoulseekUsername        string   `json:"soulseek_username,omitempty"`
	SoulseekPassword        string   `json:"soulseek_password,omitempty"`
	SoulseekSeedAfterDL     bool     `json:"soulseek_seed_after_download"`
	SourcesEnabled          []string `json:"sources_enabled,omitempty"`
	FirstRunComplete        bool     `json:"first_run_complete"`
	OutputFormat            string   `json:"output_format,omitempty"`
	MaxRetries              int      `json:"max_retries,omitempty"`
	LibraryMode             string   `json:"library_mode,omitempty"`
	PreferExplicit          *bool    `json:"prefer_explicit,omitempty"`
	FolderStructure         string   `json:"folder_structure,omitempty"`
	AlbumFolderStructure    string   `json:"album_folder_structure,omitempty"`
	PlaylistFolderStructure string   `json:"playlist_folder_structure,omitempty"`
	SingleTrackStructure    string   `json:"single_track_structure,omitempty"`
	FilenameFormat          string   `json:"filename_format,omitempty"`
	SpotifySpDc             string   `json:"spotify_sp_dc,omitempty"`
	TidalEnabled            bool     `json:"tidal_enabled"`
	TidalAuthMode           string   `json:"tidal_auth_mode,omitempty"`
	TidalSessionJSON        string   `json:"tidal_session_json,omitempty"`
	TidalAccessToken        string   `json:"tidal_access_token,omitempty"`
	TidalRefreshToken       string   `json:"tidal_refresh_token,omitempty"`
	TidalSessionID          string   `json:"tidal_session_id,omitempty"`
	TidalTokenType          string   `json:"tidal_token_type,omitempty"`
	TidalCountryCode        string   `json:"tidal_country_code,omitempty"`
	AntraApiKey             string   `json:"antra_api_key,omitempty"`
}

type HistoryItem struct {
	Date       string         `json:"date"`
	URL        string         `json:"url"`
	Title      string         `json:"title,omitempty"`
	ArtworkUrl string         `json:"artwork_url,omitempty"`
	Total      int            `json:"total"`
	Downloaded int            `json:"downloaded"`
	Failed     int            `json:"failed"`
	Skipped    int            `json:"skipped"`
	Error      string         `json:"error,omitempty"`
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
		cfg.MaxRetries = 3
		cfg.AppleStorefront = "us"
		cfg.QobuzAppID = "285473059"
		cfg.DeezerBFSecret = "g4el58wc0zvf9na1"
		cfg.TidalAuthMode = "session_json"
		cfg.TidalTokenType = "Bearer"
		cfg.FolderStructure = "standard"
		cfg.AlbumFolderStructure = "standard"
		cfg.PlaylistFolderStructure = "standard"
		cfg.SingleTrackStructure = "album_numbered"
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
	if cfg.MaxRetries <= 0 {
		cfg.MaxRetries = 3
	}
	if cfg.AppleStorefront == "" {
		cfg.AppleStorefront = "us"
	}
	if cfg.QobuzAppID == "" {
		cfg.QobuzAppID = "285473059"
	}
	if cfg.DeezerBFSecret == "" {
		cfg.DeezerBFSecret = "g4el58wc0zvf9na1"
	}
	if cfg.TidalAuthMode == "" {
		cfg.TidalAuthMode = "session_json"
	}
	if cfg.TidalTokenType == "" {
		cfg.TidalTokenType = "Bearer"
	}
	if cfg.FolderStructure == "" {
		cfg.FolderStructure = "standard"
	}
	if cfg.AlbumFolderStructure == "" {
		cfg.AlbumFolderStructure = cfg.FolderStructure
	}
	if cfg.PlaylistFolderStructure == "" {
		cfg.PlaylistFolderStructure = cfg.FolderStructure
	}
	if cfg.SingleTrackStructure == "" {
		cfg.SingleTrackStructure = "album_numbered"
	}
	return cfg
}

// SaveConfig saves the configuration and marks first run as complete
func (a *App) SaveConfig(cfg Config) error {
	cfg.FirstRunComplete = true
	if cfg.MaxRetries <= 0 {
		cfg.MaxRetries = 3
	}
	if cfg.AppleStorefront == "" {
		cfg.AppleStorefront = "us"
	}
	if cfg.QobuzAppID == "" {
		cfg.QobuzAppID = "285473059"
	}
	if cfg.DeezerBFSecret == "" {
		cfg.DeezerBFSecret = "g4el58wc0zvf9na1"
	}
	if cfg.TidalAuthMode == "" {
		cfg.TidalAuthMode = "session_json"
	}
	if cfg.TidalTokenType == "" {
		cfg.TidalTokenType = "Bearer"
	}
	if cfg.FolderStructure == "" {
		cfg.FolderStructure = "standard"
	}
	if cfg.AlbumFolderStructure == "" {
		cfg.AlbumFolderStructure = cfg.FolderStructure
	}
	if cfg.PlaylistFolderStructure == "" {
		cfg.PlaylistFolderStructure = cfg.FolderStructure
	}
	if cfg.SingleTrackStructure == "" {
		cfg.SingleTrackStructure = "album_numbered"
	}
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

	return a.startBackendProcess(ctx, cancel, command, args, workDir, env)
}

func (a *App) RetryTrackDownload(trackJSON string) error {
	if strings.TrimSpace(trackJSON) == "" {
		return fmt.Errorf("retry track payload is empty")
	}

	wailsRuntime.LogInfof(a.ctx, "Retrying failed track")

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
	command, baseArgs, workDir, env, err := a.resolveBackendCommand([]string{})
	if err != nil {
		cancel()
		wailsRuntime.LogErrorf(a.ctx, err.Error())
		return err
	}

	args := append([]string{}, baseArgs...)
	args = append(args, "--retry-track-json", trackJSON)
	return a.startBackendProcess(ctx, cancel, command, args, workDir, env)
}

func (a *App) startBackendProcess(
	ctx context.Context,
	cancel context.CancelFunc,
	command string,
	args []string,
	workDir string,
	env []string,
) error {

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
	finalArgs = append(finalArgs, "--config", getConfigPath())

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

func (a *App) ValidateTidalAuth() string {
	output, err := a.runPythonCommand([]string{"--tidal-validate"})
	if err != nil {
		msg := strings.TrimSpace(output)
		if msg == "" {
			msg = err.Error()
		}
		resp := map[string]interface{}{
			"ok":      false,
			"message": msg,
		}
		if b, marshalErr := json.Marshal(resp); marshalErr == nil {
			return string(b)
		}
		return `{"ok":false,"message":"Internal error marshaling validation failure"}`
	}
	return strings.TrimSpace(output)
}

// StartTidalOAuthLogin initiates the TIDAL OAuth device-code login flow.
// It spawns the Python backend with --tidal-oauth-login and streams all JSON events
// to the frontend via "tidal-oauth-event" events. The flow is long-running (waits
// for user to visit URL in browser), so it runs asynchronously.
func (a *App) StartTidalOAuthLogin() error {
	command, baseArgs, workDir, env, err := a.resolveBackendCommand([]string{})
	if err != nil {
		return err
	}

	// Build args: insert --tidal-oauth-login after the script/module args
	args := append([]string{}, baseArgs...)
	// If dev mode (python json_cli.py ...), insert after the script path
	// If bundled mode (exe ...), just append
	oauthArgs := []string{}
	for _, arg := range args {
		oauthArgs = append(oauthArgs, arg)
		if strings.HasSuffix(arg, "json_cli.py") {
			// After script, insert our flag
			oauthArgs = append(oauthArgs, "--tidal-oauth-login")
		}
	}
	// Bundled backend: just append if not already added
	if !containsStr(oauthArgs, "--tidal-oauth-login") {
		// Find where --config starts and insert before it
		newArgs := []string{}
		inserted := false
		for _, arg := range oauthArgs {
			if arg == "--config" && !inserted {
				newArgs = append(newArgs, "--tidal-oauth-login")
				inserted = true
			}
			newArgs = append(newArgs, arg)
		}
		if !inserted {
			newArgs = append(newArgs, "--tidal-oauth-login")
		}
		oauthArgs = newArgs
	}

	ctx, cancel := context.WithTimeout(a.ctx, 10*time.Minute)

	cmd := exec.CommandContext(ctx, command, oauthArgs...)
	hideProcess(cmd)
	cmd.Dir = workDir
	cmd.Env = env

	stdout, err := cmd.StdoutPipe()
	if err != nil {
		cancel()
		return err
	}
	cmd.Stderr = cmd.Stdout

	if err := cmd.Start(); err != nil {
		cancel()
		return err
	}

	go func() {
		defer cancel()
		scanner := bufio.NewScanner(stdout)
		for scanner.Scan() {
			line := scanner.Text()
			var payload map[string]interface{}
			if err := json.Unmarshal([]byte(line), &payload); err == nil {
				wailsRuntime.EventsEmit(a.ctx, "tidal-oauth-event", payload)
			}
		}
		cmd.Wait()
		wailsRuntime.EventsEmit(a.ctx, "tidal-oauth-event", map[string]interface{}{
			"type": "tidal_oauth_done",
		})
	}()

	return nil
}

func containsStr(slice []string, s string) bool {
	for _, v := range slice {
		if v == s {
			return true
		}
	}
	return false
}

func (a *App) startBrowserLoginFlow(flag string, eventName string, doneType string) error {
	command, baseArgs, workDir, env, err := a.resolveBackendCommand([]string{})
	if err != nil {
		return err
	}

	args := append([]string{}, baseArgs...)
	loginArgs := []string{}
	for _, arg := range args {
		loginArgs = append(loginArgs, arg)
		if strings.HasSuffix(arg, "json_cli.py") {
			loginArgs = append(loginArgs, flag)
		}
	}
	if !containsStr(loginArgs, flag) {
		newArgs := []string{}
		inserted := false
		for _, arg := range loginArgs {
			if arg == "--config" && !inserted {
				newArgs = append(newArgs, flag)
				inserted = true
			}
			newArgs = append(newArgs, arg)
		}
		if !inserted {
			newArgs = append(newArgs, flag)
		}
		loginArgs = newArgs
	}

	ctx, cancel := context.WithTimeout(a.ctx, 10*time.Minute)
	cmd := exec.CommandContext(ctx, command, loginArgs...)
	hideProcess(cmd)
	cmd.Dir = workDir
	cmd.Env = env

	stdout, err := cmd.StdoutPipe()
	if err != nil {
		cancel()
		return err
	}
	cmd.Stderr = cmd.Stdout

	if err := cmd.Start(); err != nil {
		cancel()
		return err
	}

	go func() {
		defer cancel()
		scanner := bufio.NewScanner(stdout)
		for scanner.Scan() {
			line := scanner.Text()
			var payload map[string]interface{}
			if err := json.Unmarshal([]byte(line), &payload); err == nil {
				wailsRuntime.EventsEmit(a.ctx, eventName, payload)
				if eventType, _ := payload["type"].(string); strings.HasSuffix(eventType, "_success") {
					wailsRuntime.WindowShow(a.ctx)
				}
			}
		}
		cmd.Wait()
		wailsRuntime.EventsEmit(a.ctx, eventName, map[string]interface{}{"type": doneType})
	}()

	return nil
}

func (a *App) StartAppleBrowserLogin() error {
	return a.startBrowserLoginFlow("--apple-browser-login", "apple-login-event", "apple_login_done")
}

func (a *App) StartAmazonBrowserLogin() error {
	return a.startBrowserLoginFlow("--amazon-browser-login", "amazon-login-event", "amazon_login_done")
}

// ConfirmAmazonLogin is called by the frontend when the user has signed into
// Amazon Music in their real browser and is ready for Antra to capture the session.
// It writes a sentinel file that the Python --amazon-browser-login process polls for.
func (a *App) ConfirmAmazonLogin() error {
	sentinelPath := filepath.Join(os.TempDir(), "antra_amazon_login_confirm.tmp")
	return os.WriteFile(sentinelPath, []byte("ok"), 0644)
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

// SearchArtists searches for artists by name using the given source ("spotify" or "apple").
// Returns a JSON string: {"type":"artist_search","data":[...]} or {"error":"..."}
func (a *App) SearchArtists(query string, source string) string {
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()

	if source == "" {
		source = "spotify"
	}

	backend, err := ensureBundledBackend()
	var out []byte
	if err == nil {
		cmd := exec.CommandContext(ctx, backend, "--search-artists", query, "--search-source", source, "--config", getConfigPath())
		hideProcess(cmd)
		out, err = cmd.Output()
	} else {
		// Dev fallback
		pythonExe, _, workDir, env, resolveErr := a.resolveBackendCommand([]string{})
		if resolveErr != nil {
			return `{"error":"could not resolve backend"}`
		}
		cmd := exec.CommandContext(ctx, pythonExe, "-m", "antra.json_cli", "--search-artists", query, "--search-source", source, "--config", getConfigPath())
		cmd.Dir = workDir
		cmd.Env = env
		hideProcess(cmd)
		out, err = cmd.Output()
	}

	if err != nil {
		if ctx.Err() == context.DeadlineExceeded {
			return `{"error":"artist search timed out"}`
		}
		return `{"error":"` + strings.ReplaceAll(err.Error(), `"`, `'`) + `"}`
	}

	// Unwrap {"type":"artist_search","data":[...]} → just the data array as JSON string
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

func (a *App) GetDiscoveryData(region string, genreId string, genreName string) string {
	ctx, cancel := context.WithTimeout(context.Background(), 60*time.Second)
	defer cancel()

	if region == "" {
		region = "us"
	}

	backend, err := ensureBundledBackend()
	var out []byte
	if err == nil {
		args := []string{"--discovery-json", "--discovery-region", region}
		if genreId != "" {
			args = append(args, "--discovery-genre-id", genreId)
		}
		if genreName != "" {
			args = append(args, "--discovery-genre-name", genreName)
		}
		args = append(args, "--config", getConfigPath())
		cmd := exec.CommandContext(ctx, backend, args...)
		hideProcess(cmd)
		out, err = cmd.Output()
	} else {
		pythonExe, _, workDir, env, resolveErr := a.resolveBackendCommand([]string{})
		if resolveErr != nil {
			return `{"error":"could not resolve backend"}`
		}
		args := []string{"-m", "antra.json_cli", "--discovery-json", "--discovery-region", region}
		if genreId != "" {
			args = append(args, "--discovery-genre-id", genreId)
		}
		if genreName != "" {
			args = append(args, "--discovery-genre-name", genreName)
		}
		args = append(args, "--config", getConfigPath())
		cmd := exec.CommandContext(ctx, pythonExe, args...)
		cmd.Dir = workDir
		cmd.Env = env
		hideProcess(cmd)
		out, err = cmd.Output()
	}

	if err != nil {
		if ctx.Err() == context.DeadlineExceeded {
			return `{"error":"discovery fetch timed out"}`
		}
		return `{"error":"` + strings.ReplaceAll(err.Error(), `"`, `'`) + `"}`
	}

	var wrapper map[string]interface{}
	if jsonErr := json.Unmarshal(bytes.TrimSpace(out), &wrapper); jsonErr != nil {
		return string(out)
	}
	if wrapper["type"] == "error" {
		msg, _ := wrapper["message"].(string)
		return `{"error":"` + strings.ReplaceAll(msg, `"`, `'`) + `"}`
	}
	return string(bytes.TrimSpace(out))
}

func (a *App) GetDiscoveryGenres(region string) string {
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()

	if region == "" {
		region = "us"
	}

	backend, err := ensureBundledBackend()
	var out []byte
	if err == nil {
		cmd := exec.CommandContext(ctx, backend, "--discovery-genres-only", "--discovery-region", region, "--config", getConfigPath())
		hideProcess(cmd)
		out, err = cmd.Output()
	} else {
		pythonExe, _, workDir, env, resolveErr := a.resolveBackendCommand([]string{})
		if resolveErr != nil {
			return `{"error":"could not resolve backend"}`
		}
		cmd := exec.CommandContext(ctx, pythonExe, "-m", "antra.json_cli", "--discovery-genres-only", "--discovery-region", region, "--config", getConfigPath())
		cmd.Dir = workDir
		cmd.Env = env
		hideProcess(cmd)
		out, err = cmd.Output()
	}

	if err != nil {
		if ctx.Err() == context.DeadlineExceeded {
			return `{"error":"genres fetch timed out"}`
		}
		return `{"error":"` + strings.ReplaceAll(err.Error(), `"`, `'`) + `"}`
	}

	var wrapper map[string]interface{}
	if jsonErr := json.Unmarshal(bytes.TrimSpace(out), &wrapper); jsonErr != nil {
		return string(out)
	}
	if wrapper["type"] == "error" {
		msg, _ := wrapper["message"].(string)
		return `{"error":"` + strings.ReplaceAll(msg, `"`, `'`) + `"}`
	}
	return string(bytes.TrimSpace(out))
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
		return bundledBackend, args, filepath.Dir(bundledBackend), append(os.Environ(), "PYTHONUTF8=1"), nil
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
	env := append(os.Environ(), fmt.Sprintf("PYTHONPATH=%s", parentDir), "PYTHONUTF8=1")
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

// ── Source health check ───────────────────────────────────────────────────────

type EndpointStatus struct {
	URL       string `json:"url"`
	Alive     bool   `json:"alive"`
	LatencyMs int64  `json:"latency_ms"`
}

type SourceHealthResult struct {
	Source    string           `json:"source"`
	Total     int              `json:"total"`
	Live      int              `json:"live"`
	Endpoints []EndpointStatus `json:"endpoints"`
}

const defaultEndpointManifestURL = "https://gist.githubusercontent.com/anandprtp/fdc2c16b7bfdc2d337fbc86161b79371/raw"

var gistIDPattern = regexp.MustCompile(`(?i)([0-9a-f]{32})`)

type endpointManifestDab struct {
	Search []string `json:"search"`
	Stream []string `json:"stream"`
}

type endpointManifestMirrors struct {
	Tidal  string `json:"tidal"`
	Qobuz  string `json:"qobuz"`
	Deezer string `json:"deezer"`
	Amazon string `json:"amazon"`
	Apple  string `json:"apple"`
}

type endpointManifest struct {
	Hifi    []string                `json:"hifi"`
	Amazon  []string                `json:"amazon"`
	Apple   []string                `json:"apple"`
	Dab     endpointManifestDab     `json:"dab"`
	Mirrors endpointManifestMirrors `json:"mirrors"`
	ApiKey  string                  `json:"api_key"`
}

func getEndpointManifestCachePaths() []string {
	paths := []string{filepath.Join(getAppDataDir(), "endpoint_manifest_cache.json")}

	switch runtime.GOOS {
	case "windows":
		localAppData := os.Getenv("LOCALAPPDATA")
		if localAppData != "" {
			paths = append(paths, filepath.Join(localAppData, "Antra", "Antra", "endpoint_manifest_cache.json"))
		}
	case "darwin":
		home := os.Getenv("HOME")
		if home != "" {
			paths = append(paths, filepath.Join(home, "Library", "Application Support", "Antra", "Antra", "endpoint_manifest_cache.json"))
		}
	default:
		home := os.Getenv("HOME")
		if home != "" {
			paths = append(paths, filepath.Join(home, ".local", "share", "Antra", "Antra", "endpoint_manifest_cache.json"))
		}
	}

	return uniqueCleanPaths(paths)
}

func loadEndpointManifest() endpointManifest {
	manifestURL := strings.TrimSpace(os.Getenv("ANTRA_ENDPOINT_MANIFEST_URL"))
	if manifestURL == "" {
		manifestURL = defaultEndpointManifestURL
	}

	client := &http.Client{
		Timeout: 5 * time.Second,
		Transport: &http.Transport{
			Proxy: nil,
		},
	}
	if manifest, ok := fetchManifestFromURL(client, manifestURL); ok {
		manifest.normalize()
		writeEndpointManifestCache(manifest)
		return manifest
	}

	if gistID := extractGistID(manifestURL); gistID != "" {
		if manifest, ok := fetchManifestFromGistAPI(client, gistID); ok {
			manifest.normalize()
			writeEndpointManifestCache(manifest)
			return manifest
		}
	}

	if cached, ok := readEndpointManifestCache(); ok {
		return cached
	}
	return endpointManifest{}
}

func readEndpointManifestCache() (endpointManifest, bool) {
	for _, cachePath := range getEndpointManifestCachePaths() {
		data, err := os.ReadFile(cachePath)
		if err != nil {
			continue
		}
		var manifest endpointManifest
		if err := unmarshalEndpointManifest(data, &manifest); err != nil {
			continue
		}
		manifest.normalize()
		return manifest, true
	}
	return endpointManifest{}, false
}

func fetchManifestFromURL(client *http.Client, manifestURL string) (endpointManifest, bool) {
	req, err := http.NewRequest(http.MethodGet, manifestURL, nil)
	if err != nil {
		return endpointManifest{}, false
	}
	req.Header.Set("User-Agent", "Antra/1.0 (+https://github.com/anandprtp/Antra)")
	req.Header.Set("Accept", "application/json, text/plain, */*")
	req.Header.Set("Cache-Control", "no-cache")

	resp, err := client.Do(req)
	if err != nil {
		return endpointManifest{}, false
	}
	defer resp.Body.Close()

	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return endpointManifest{}, false
	}

	data, err := io.ReadAll(resp.Body)
	if err != nil {
		return endpointManifest{}, false
	}

	var manifest endpointManifest
	if err := unmarshalEndpointManifest(data, &manifest); err != nil {
		return endpointManifest{}, false
	}
	return manifest, true
}

func extractGistID(manifestURL string) string {
	match := gistIDPattern.FindStringSubmatch(manifestURL)
	if len(match) < 2 {
		return ""
	}
	return match[1]
}

func fetchManifestFromGistAPI(client *http.Client, gistID string) (endpointManifest, bool) {
	req, err := http.NewRequest(http.MethodGet, "https://api.github.com/gists/"+gistID, nil)
	if err != nil {
		return endpointManifest{}, false
	}
	req.Header.Set("User-Agent", "Antra/1.0 (+https://github.com/anandprtp/Antra)")
	req.Header.Set("Accept", "application/vnd.github+json")

	resp, err := client.Do(req)
	if err != nil {
		return endpointManifest{}, false
	}
	defer resp.Body.Close()

	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return endpointManifest{}, false
	}

	var payload struct {
		Files map[string]struct {
			Content string `json:"content"`
		} `json:"files"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&payload); err != nil {
		return endpointManifest{}, false
	}

	for _, file := range payload.Files {
		if strings.TrimSpace(file.Content) == "" {
			continue
		}
		var manifest endpointManifest
		if err := unmarshalEndpointManifest([]byte(file.Content), &manifest); err == nil {
			return manifest, true
		}
	}

	return endpointManifest{}, false
}

func writeEndpointManifestCache(manifest endpointManifest) {
	data, err := json.MarshalIndent(manifest, "", "  ")
	if err != nil {
		return
	}
	for _, cachePath := range getEndpointManifestCachePaths() {
		if err := os.MkdirAll(filepath.Dir(cachePath), 0755); err != nil {
			continue
		}
		_ = os.WriteFile(cachePath, data, 0644)
	}
}

func unmarshalEndpointManifest(data []byte, manifest *endpointManifest) error {
	if err := json.Unmarshal(data, manifest); err == nil {
		return nil
	}
	var legacyHifi []string
	if err := json.Unmarshal(data, &legacyHifi); err == nil {
		manifest.Hifi = legacyHifi
		return nil
	}
	return fmt.Errorf("unsupported endpoint manifest payload")
}

func (m *endpointManifest) normalize() {
	m.Hifi = normalizeURLList(m.Hifi)
	m.Amazon = normalizeURLList(m.Amazon)
	m.Apple = normalizeURLList(m.Apple)
	m.Dab.Search = normalizeURLList(m.Dab.Search)
	m.Dab.Stream = normalizeURLList(m.Dab.Stream)
}

func normalizeURLList(urls []string) []string {
	seen := make(map[string]struct{}, len(urls))
	result := make([]string, 0, len(urls))
	for _, raw := range urls {
		clean := strings.TrimSpace(strings.TrimRight(raw, "/"))
		if clean == "" {
			continue
		}
		if _, ok := seen[clean]; ok {
			continue
		}
		seen[clean] = struct{}{}
		result = append(result, clean)
	}
	return result
}

func endpointsForHealthSource(manifest endpointManifest, source string) []string {
	switch source {
	case "hifi":
		eps := append([]string{}, manifest.Hifi...)
		if manifest.Mirrors.Tidal != "" {
			eps = append([]string{manifest.Mirrors.Tidal}, eps...)
		}
		return eps
	case "amazon":
		eps := append([]string{}, manifest.Amazon...)
		if manifest.Mirrors.Amazon != "" {
			eps = append([]string{manifest.Mirrors.Amazon}, eps...)
		}
		return eps
	case "apple":
		eps := append([]string{}, manifest.Apple...)
		if manifest.Mirrors.Apple != "" {
			eps = append([]string{manifest.Mirrors.Apple}, eps...)
		}
		return eps
	case "qobuz":
		if manifest.Mirrors.Qobuz != "" {
			return []string{manifest.Mirrors.Qobuz}
		}
		return nil
	case "dab":
		return append([]string{}, manifest.Dab.Search...)
	case "deezer":
		if manifest.Mirrors.Deezer != "" {
			return []string{manifest.Mirrors.Deezer}
		}
		return nil
	default:
		return nil
	}
}

// CheckSourceHealth probes all known endpoints for a given source ("hifi", "amazon",
// "apple", "qobuz", "deezer", "dab") in parallel and returns a JSON-encoded
// SourceHealthResult.
//
// Health check URLs mirror the adapters' own liveness checks:
// GetSlskdWebUIInfo returns the slskd web UI URL, username, and generated password
// from the managed instance's state.json. Returns an empty JSON object if slskd
// has not been bootstrapped yet (no state file).
func (a *App) GetSlskdWebUIInfo() string {
	statePath := getSlskdStatePath()
	data, err := os.ReadFile(statePath)
	if err != nil {
		return "{}"
	}
	var state map[string]interface{}
	if err := json.Unmarshal(data, &state); err != nil {
		return "{}"
	}
	baseURL, _ := state["base_url"].(string)
	webPassword, _ := state["web_password"].(string)
	if baseURL == "" || webPassword == "" {
		return "{}"
	}
	result, _ := json.Marshal(map[string]string{
		"url":      baseURL,
		"username": "slskd",
		"password": webPassword,
	})
	return string(result)
}

func getSlskdStatePath() string {
	switch runtime.GOOS {
	case "windows":
		local := os.Getenv("LOCALAPPDATA")
		return filepath.Join(local, "antra", "slskd", "runtime", "state.json")
	default:
		home := os.Getenv("HOME")
		return filepath.Join(home, ".cache", "antra", "slskd", "runtime", "state.json")
	}
}

func probeHifiEndpoint(client *http.Client, base string) (bool, error) {
	// Use the public health endpoint (GET /) — no API key needed
	resp, err := client.Get(base + "/")
	if err != nil {
		return false, err
	}
	defer resp.Body.Close()
	return resp.StatusCode == 200, nil
}

// - HiFi:   search + track manifest probe must both succeed
// - Amazon: GET {mirror}/           → 200 or 404 (server reachable)
// - Apple:  GET {mirror}/           → 200 or 404 (server reachable)
// - DAB:    GET {ep}/search?q=test  → 200
func (a *App) CheckSourceHealth(source string) string {
	manifest := loadEndpointManifest()
	endpoints := endpointsForHealthSource(manifest, source)
	if endpoints == nil {
		res := SourceHealthResult{Source: source, Total: 0, Live: 0, Endpoints: []EndpointStatus{}}
		b, _ := json.Marshal(res)
		return string(b)
	}

	type probeResult struct {
		alive     bool
		latencyMs int64
	}

	results := make([]probeResult, len(endpoints))
	client := &http.Client{Timeout: 7 * time.Second}

	var wg sync.WaitGroup
	for i, ep := range endpoints {
		wg.Add(1)
		go func(idx int, base string) {
			defer wg.Done()
			start := time.Now()
			alive := false
			switch source {
			case "hifi":
				ok, err := probeHifiEndpoint(client, base)
				alive = err == nil && ok
			default:
				var checkURL string
				switch source {
				case "amazon", "apple", "qobuz", "deezer":
					checkURL = base + "/"
				case "dab":
					checkURL = base + "/search?q=test"
				default:
					checkURL = base
				}
				resp, err := client.Get(checkURL)
				if err == nil {
					resp.Body.Close()
					switch source {
					case "amazon", "apple", "qobuz", "deezer":
						alive = resp.StatusCode == 200 || resp.StatusCode == 404
					default:
						alive = resp.StatusCode == 200
					}
				}
			}
			elapsed := time.Since(start).Milliseconds()
			results[idx] = probeResult{alive: alive, latencyMs: elapsed}
		}(i, ep)
	}
	wg.Wait()

	statuses := make([]EndpointStatus, len(endpoints))
	live := 0
	for i, ep := range endpoints {
		statuses[i] = EndpointStatus{
			URL:       ep,
			Alive:     results[i].alive,
			LatencyMs: results[i].latencyMs,
		}
		if results[i].alive {
			live++
		}
	}

	res := SourceHealthResult{
		Source:    source,
		Total:     len(endpoints),
		Live:      live,
		Endpoints: statuses,
	}
	b, _ := json.Marshal(res)
	return string(b)
}

// ── Self-serve key generation ─────────────────────────────────────────────────

// KeyGenResult is returned by RequestAccessKey to the Svelte frontend.
type KeyGenResult struct {
	OK            bool   `json:"ok"`
	Key           string `json:"key,omitempty"`
	ExpiresAt     string `json:"expires_at,omitempty"`
	DownloadLimit int    `json:"download_limit,omitempty"`
	Error         string `json:"error,omitempty"`
}

// RequestAccessKey calls the VPS key-generation endpoint, saves the returned
// key into the local config, and returns the result to the frontend.
//
// The VPS URL is read from the endpoint manifest (mirrors.tidal field).
// If the manifest is unavailable, the function returns an error.
func (a *App) RequestAccessKey() KeyGenResult {
	// 1. Load the manifest to discover the Tidal mirror URL.
	manifest := loadEndpointManifest()
	tidalURL := strings.TrimRight(manifest.Mirrors.Tidal, "/")
	if tidalURL == "" {
		return KeyGenResult{
			OK:    false,
			Error: "Could not reach Antra servers. Check your internet connection and try again.",
		}
	}

	// 2. POST to the key-generation endpoint.
	client := &http.Client{Timeout: 15 * time.Second}
	resp, err := client.Post(tidalURL+"/api/keys/generate", "application/json", nil)
	if err != nil {
		return KeyGenResult{
			OK:    false,
			Error: "Could not reach Antra servers. Check your internet connection and try again.",
		}
	}
	defer resp.Body.Close()

	body, _ := io.ReadAll(resp.Body)

	if resp.StatusCode == 429 {
		// Rate-limited — parse the detail message from the VPS response.
		var detail struct {
			Detail string `json:"detail"`
		}
		if json.Unmarshal(body, &detail) == nil && detail.Detail != "" {
			return KeyGenResult{OK: false, Error: detail.Detail}
		}
		return KeyGenResult{OK: false, Error: "You already have an active key. Try again in 24 hours."}
	}

	if resp.StatusCode != 200 {
		return KeyGenResult{
			OK:    false,
			Error: fmt.Sprintf("Server returned an error (%d). Try again later.", resp.StatusCode),
		}
	}

	// 3. Parse the response.
	var result struct {
		Key           string `json:"key"`
		ExpiresAt     string `json:"expires_at"`
		DownloadLimit int    `json:"download_limit"`
	}
	if err := json.Unmarshal(body, &result); err != nil || result.Key == "" {
		return KeyGenResult{OK: false, Error: "Unexpected response from server. Try again later."}
	}

	// 4. Save the key into the local config so it takes effect immediately.
	cfg := a.GetConfig()
	cfg.AntraApiKey = result.Key
	if err := a.SaveConfig(cfg); err != nil {
		wailsRuntime.LogErrorf(a.ctx, "RequestAccessKey: failed to save config: %v", err)
		// Still return the key — user can paste it manually.
	}

	return KeyGenResult{
		OK:            true,
		Key:           result.Key,
		ExpiresAt:     result.ExpiresAt,
		DownloadLimit: result.DownloadLimit,
	}
}
