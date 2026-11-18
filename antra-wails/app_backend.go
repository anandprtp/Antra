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
	"log"
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
	StrictMatching          bool     `json:"strict_matching"`
	// v1.1.8 FEAT-3/2/4. Pointers, not plain bools: these default to TRUE, and a
	// plain bool would read as false whenever the key is absent from an existing
	// config.json — silently disabling them on upgrade. Same reason PreferExplicit
	// above is a pointer.
	StrictFormat                *bool  `json:"strict_format,omitempty"`
	PreventLossyTranscode       *bool  `json:"prevent_lossy_transcode,omitempty"`
	WriteAntraTags              *bool  `json:"write_antra_tags,omitempty"`
	FolderStructure             string `json:"folder_structure,omitempty"`
	AlbumFolderStructure        string `json:"album_folder_structure,omitempty"`
	PlaylistFolderStructure     string `json:"playlist_folder_structure,omitempty"`
	PlaylistStorageMode         string `json:"playlist_storage_mode,omitempty"`  // issue #78: "playlist_folder" (default) | "library"
	PlaylistRenumber            *bool  `json:"playlist_renumber,omitempty"`      // issue #75: pointer — default is true, so "" must not read as false
	PlaylistPruneOrphans        string `json:"playlist_prune_orphans,omitempty"` // issue #75: "ask" (default) | "auto" | "off"
	SingleTrackStructure        string `json:"single_track_structure,omitempty"`
	FilenameFormat              string `json:"filename_format,omitempty"`
	SingleTrackFilenameTemplate string `json:"single_track_filename_template,omitempty"`
	AlbumZipNameTemplate        string `json:"album_zip_name_template,omitempty"`
	AlbumTrackFilenameTemplate  string `json:"album_track_filename_template,omitempty"`
	// v1.1.8 FEAT-11 — pointer, not string: with a plain string, `omitempty` drops
	// "" from config.json, so a deliberately-cleared template is indistinguishable
	// from one that was never set and the default gets re-applied on next launch.
	// nil = never configured, "" = cleared on purpose (flat library root).
	// Same class of trap as the *bool fields above.
	FolderStructureTemplate     *string `json:"folder_structure_template,omitempty"`
	MultiDiscHandling           string  `json:"multi_disc_handling,omitempty"`
	TrackNumberPadding          int     `json:"track_number_padding,omitempty"`
	IllegalCharacterReplacement string  `json:"illegal_character_replacement,omitempty"`
	WhitespaceHandling          string  `json:"whitespace_handling,omitempty"`
	FilenameConflictBehavior    string  `json:"filename_conflict_behavior,omitempty"`
	FetchLyrics                 bool    `json:"fetch_lyrics"`
	SpotifySpDc                 string  `json:"spotify_sp_dc,omitempty"`
	TidalEnabled                bool    `json:"tidal_enabled"`
	TidalAuthMode               string  `json:"tidal_auth_mode,omitempty"`
	TidalSessionJSON            string  `json:"tidal_session_json,omitempty"`
	TidalAccessToken            string  `json:"tidal_access_token,omitempty"`
	TidalRefreshToken           string  `json:"tidal_refresh_token,omitempty"`
	TidalSessionID              string  `json:"tidal_session_id,omitempty"`
	TidalTokenType              string  `json:"tidal_token_type,omitempty"`
	TidalCountryCode            string  `json:"tidal_country_code,omitempty"`
	// AntraApiKey is the PASTED supporter/premium key (ak_ / pk_) — a credential
	// the user owns and types in. AntraDeviceToken is the device-code login token
	// (at_), minted per device by the website.
	//
	// These are deliberately SEPARATE fields. Until v1.1.8 the device token was
	// written into AntraApiKey, which is also what the Settings paste box binds
	// to — so signing in overwrote the user's supporter key with a token that
	// /api/keys/validate cannot recognise (it is not a keys.json entry). The UI
	// then reported "Key not recognized", isSupporter went false, and the user
	// was silently demoted to 1 worker with no FEAT-7 discount. Logging in must
	// never cost someone their supporter tier, so the two never share a field.
	AntraApiKey      string `json:"antra_api_key,omitempty"`
	AntraDeviceToken string `json:"antra_device_token,omitempty"`
	Theme            string `json:"theme,omitempty"`
	// "" = system (follow the OS), "light", "dark".  v2.0.0 THEME-2
	Mode             string `json:"mode,omitempty"`
	Font             string `json:"font,omitempty"`
	// UI language (v2.0.0 SF-3). "" = follow the OS, which the webview reports
	// via navigator.language on all three platforms — so this needs no Go binding.
	// A plain string like Theme/Mode, not a *string: "" is itself the meaningful
	// default (auto), so an absent key and an empty one mean the same thing and
	// there is no third state to tell apart.
	Language         string `json:"language,omitempty"`

	// Custom theme axes (v2.0.0 THEME-4). Both are plain strings, not pointers:
	// unlike the *bool settings, "" is itself the meaningful default here (the
	// pair the [data-theme="custom"] block declares), so an absent key and an
	// empty one mean the same thing and there is no third state to distinguish.
	// v2.0.0 PORT-5 — width of the draggable activity-log panel, in CSS px.
	// *int, NOT a plain int: the default is 380 and a plain int would read 0
	// from every config.json written before PORT-5, which renders as a
	// collapsed panel nobody can see. Same trap as NotifyVolume /
	// EmbeddedArtworkSize. Absent means "never dragged" — the frontend
	// supplies the default and omitempty keeps the key out of config.json
	// until the user actually resizes.
	LogPanelWidth *int `json:"log_panel_width,omitempty"`

	CustomBase   string `json:"custom_base,omitempty"`
	CustomAccent string `json:"custom_accent,omitempty"`
	// Notification sounds (v1.1.8 FEAT-13). Volume is a *int and BatchOnly a
	// *bool on purpose: a plain int reads 0 (= silent) and a plain bool reads
	// false when the key is absent from an existing config.json, which would
	// silently disable the feature for every current user. Same trap as
	// PreferExplicit / the FEAT-3/2/4 settings.
	NotifySound         string        `json:"notify_sound,omitempty"`
	NotifyVolume        *int          `json:"notify_volume,omitempty"`
	// v2.0.0 FR-5 — previews get their OWN volume, separate from both the
	// notification sounds and the library player. Pointer for the same reason
	// as NotifyVolume: a plain int reads 0 (silent) when the key is absent.
	PreviewVolume       *int          `json:"preview_volume,omitempty"`
	NotifyBatchOnly     *bool         `json:"notify_batch_only,omitempty"`
	DownloadSource      string        `json:"download_source,omitempty"`
	DownloadSources     []string      `json:"download_sources,omitempty"`
	SaveCoverArtSidecar bool          `json:"save_cover_art_sidecar"`

	// v2.0.0 FR-2 — cover art settings. The three ints are *int and NOT plain
	// ints on purpose: JpegImageQuality defaults to 90 and EmbeddedArtworkSize
	// to 1000, so a plain int would read 0 from any config.json written before
	// FR-2 and silently drop quality to the floor and uncap embedded artwork
	// for every existing user. Same trap as NotifyVolume / PreferExplicit.
	CoverImageTemplate  string        `json:"cover_image_template,omitempty"`
	LocalArtworkSize    *int          `json:"local_artwork_size,omitempty"`
	LocalArtworkFormat  string        `json:"local_artwork_format,omitempty"`
	JpegImageQuality    *int          `json:"jpeg_image_quality,omitempty"`
	EmbeddedArtworkSize *int          `json:"embedded_artwork_size,omitempty"`
	EmbeddedArtworkPng  bool          `json:"embedded_artwork_png"`
	SaveArtistImage     bool          `json:"save_artist_image"`
	ArtistImageTemplate string        `json:"artist_image_template,omitempty"`
	// v2.0.0 PORT-2 - sidecar files. Plain bools, NOT pointers: false IS the
	// default here, so an older config.json reading false is the correct
	// answer - not the silent-downgrade trap that made the FR-2 ints *int.
	// v2.0.0 DMX-1 - per-tag write toggles. DisabledTags is a []string of the
	// tags switched OFF, not twenty booleans: a nil slice (what every
	// pre-DMX-1 config.json produces) means nothing is disabled, so absent
	// naturally reads as the default. Twenty plain bools would each read
	// false there and strip every tag at once - the *bool trap, twentyfold.
	// WriteId3v1 IS a pointer, because that one defaults to true.
	DisabledTags        []string      `json:"disabled_tags,omitempty"`
	DateFormat          string        `json:"date_format,omitempty"`
	WriteId3v1          *bool         `json:"write_id3v1,omitempty"`
	// v2.0.0 DMX-2 - output-only text transforms. RemoveDuplicateArtists and
	// AlbumVariousArtists ARE pointers because both default to TRUE: a plain
	// bool would read false from every config.json written before DMX-2 and
	// silently turn them off - the NotifyVolume / PreferExplicit / FR-2 trap.
	// RemoveAlbumVersion is a plain bool because false IS its default, and the
	// three strings use "" to mean "use the default" (clamped in Python by
	// migrate_legacy_templates).
	TitleCasing            string `json:"title_casing,omitempty"`
	ArtistCasing           string `json:"artist_casing,omitempty"`
	RemoveAlbumVersion     bool   `json:"remove_album_version,omitempty"`
	RemoveDuplicateArtists *bool  `json:"remove_duplicate_artists,omitempty"`
	FeaturedArtistsMode    string `json:"featured_artists_mode,omitempty"`
	MultiArtistSeparator   string `json:"multi_artist_separator,omitempty"`
	AlbumVariousArtists    *bool  `json:"album_various_artists,omitempty"`
	// v2.0.0 DMX-6 - post-download command hook. All plain (non-pointer):
	// every default here is the SAFE value (off / empty), so an older
	// config.json reading false is the correct answer, not the silent-
	// downgrade trap that made the FR-2 ints *int. PostCommandTimeout is an
	// int whose zero IS invalid, but Python clamps 0 back to the 30s default
	// in normalize_timeout(), so an absent key still gets the real default.
	PostCommandEnabled   bool   `json:"post_command_enabled,omitempty"`
	PostCommandConfirmed bool   `json:"post_command_confirmed,omitempty"`
	PostCommand          string `json:"post_command,omitempty"`
	PostCommandMode      string `json:"post_command_mode,omitempty"`
	PostCommandTimeout   int    `json:"post_command_timeout,omitempty"`
	// v2.0.0 SF-2 - ReplayGain tags. Plain bool: false IS the default, so an
	// older config.json reading false is the correct answer.
	ReplaygainEnabled bool `json:"replaygain_enabled,omitempty"`
	// Audio fingerprint verification against the track's store preview.
	// *bool, not bool: this defaults to ON, and a plain bool reads false from
	// every config.json written before the setting existed -- silently
	// disabling it for every current user. Same trap as PreferExplicit.
	VerifyAudioEnabled *bool `json:"verify_audio_enabled,omitempty"`
	SidecarCue          bool          `json:"sidecar_cue"`
	SidecarNfo          bool          `json:"sidecar_nfo"`
	SidecarJson         bool          `json:"sidecar_json"`
	AutoSyncEnabled     bool          `json:"auto_sync_enabled"`
	AutoSyncHour        int           `json:"auto_sync_hour"`
	AutoSyncMinute      int           `json:"auto_sync_minute"`
	AutoSyncDays        int           `json:"auto_sync_days"` // bitmask Mon=0…Sun=6
	TrackedPlaylists    []interface{} `json:"tracked_playlists,omitempty"`
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
		cfg.PlaylistStorageMode = "playlist_folder"
		cfg.SingleTrackStructure = "album_numbered"
		cfg.MultiDiscHandling = "prefix"
		cfg.TrackNumberPadding = 2
		cfg.IllegalCharacterReplacement = "_"
		cfg.WhitespaceHandling = "preserve"
		cfg.FilenameConflictBehavior = "skip"
		cfg.FetchLyrics = true
		cfg.DownloadSource = "auto"
		cfg.DownloadSources = []string{"auto"}
		return cfg
	}

	data, err := os.ReadFile(cfgPath)
	if err != nil {
		wailsRuntime.LogErrorf(a.ctx, "Failed to read config: %v", err)
		cfg.DownloadPath = "./Music"
		return cfg
	}

	if err := json.Unmarshal(data, &cfg); err != nil {
		// A damaged config used to be silently replaced by defaults on the next
		// save, destroying every setting and credential. Keep the bad file.
		preserveCorruptConfig(cfgPath, data)
		log.Printf("config.json is not valid JSON, kept a copy: %v", err)
	}
	if !bytes.Contains(data, []byte(`"fetch_lyrics"`)) {
		cfg.FetchLyrics = true
	}
	// Migrate installs that already signed in while the device token shared a
	// field with the supporter key. Read-only and idempotent — persisted by the
	// next SaveConfig. The supporter key it displaced cannot be recovered; the
	// user has to re-paste it (it is in their email).
	if cfg.AntraDeviceToken == "" && strings.HasPrefix(strings.TrimSpace(cfg.AntraApiKey), "at_") {
		cfg.AntraDeviceToken = strings.TrimSpace(cfg.AntraApiKey)
		cfg.AntraApiKey = ""
	}
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
	if cfg.PlaylistStorageMode == "" {
		cfg.PlaylistStorageMode = "playlist_folder"
	}
	if cfg.SingleTrackStructure == "" {
		cfg.SingleTrackStructure = "album_numbered"
	}
	if cfg.MultiDiscHandling == "" {
		cfg.MultiDiscHandling = "prefix"
	}
	if cfg.TrackNumberPadding <= 0 {
		cfg.TrackNumberPadding = 2
	}
	if cfg.IllegalCharacterReplacement == "" {
		cfg.IllegalCharacterReplacement = "_"
	}
	if cfg.WhitespaceHandling == "" {
		cfg.WhitespaceHandling = "preserve"
	}
	if cfg.FilenameConflictBehavior == "" {
		cfg.FilenameConflictBehavior = "skip"
	}
	if cfg.DownloadSource == "" {
		cfg.DownloadSource = "auto"
	}
	if len(cfg.DownloadSources) == 0 {
		cfg.DownloadSources = []string{cfg.DownloadSource}
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
	if cfg.PlaylistStorageMode == "" {
		cfg.PlaylistStorageMode = "playlist_folder"
	}
	if cfg.SingleTrackStructure == "" {
		cfg.SingleTrackStructure = "album_numbered"
	}
	if cfg.MultiDiscHandling == "" {
		cfg.MultiDiscHandling = "prefix"
	}
	if cfg.TrackNumberPadding <= 0 {
		cfg.TrackNumberPadding = 2
	}
	if cfg.IllegalCharacterReplacement == "" {
		cfg.IllegalCharacterReplacement = "_"
	}
	if cfg.WhitespaceHandling == "" {
		cfg.WhitespaceHandling = "preserve"
	}
	if cfg.FilenameConflictBehavior == "" {
		cfg.FilenameConflictBehavior = "skip"
	}
	if cfg.DownloadSource == "" {
		cfg.DownloadSource = "auto"
	}
	if len(cfg.DownloadSources) == 0 {
		cfg.DownloadSources = []string{cfg.DownloadSource}
	}
	dir := getAppDataDir()
	if err := os.MkdirAll(dir, 0755); err != nil {
		return err
	}

	data, err := json.MarshalIndent(cfg, "", "  ")
	if err != nil {
		return err
	}

	// config.json holds API keys, the device token and cookies: owner-only, and
	// atomic so a crash mid-save cannot leave a truncated file.
	return writeFileAtomic(getConfigPath(), data, 0o600)
}

// writeFileAtomic writes data to a temp file beside path, fsyncs it and renames it
// into place. The temp file is created 0600, so the result is owner-only even when
// an older, more permissive file is being replaced.
func writeFileAtomic(path string, data []byte, perm os.FileMode) error {
	tmp, err := os.CreateTemp(filepath.Dir(path), filepath.Base(path)+".*.tmp")
	if err != nil {
		return err
	}
	tmpName := tmp.Name()
	fail := func(err error) error {
		tmp.Close()
		os.Remove(tmpName)
		return err
	}
	if _, err := tmp.Write(data); err != nil {
		return fail(err)
	}
	if err := tmp.Chmod(perm); err != nil && runtime.GOOS != "windows" {
		return fail(err)
	}
	if err := tmp.Sync(); err != nil {
		return fail(err)
	}
	if err := tmp.Close(); err != nil {
		os.Remove(tmpName)
		return err
	}
	// Windows can refuse a rename-over briefly while another rename/scan holds the target.
	var rerr error
	for i := 0; i < 20; i++ {
		if rerr = os.Rename(tmpName, path); rerr == nil || runtime.GOOS != "windows" {
			break
		}
		time.Sleep(10 * time.Millisecond)
	}
	if rerr != nil {
		os.Remove(tmpName)
		return rerr
	}
	return nil
}

var historyMu sync.Mutex

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
	historyMu.Lock() // read-modify-write: two finishing jobs must not drop each other's entry
	defer historyMu.Unlock()
	history := a.GetHistory()
	history = append([]HistoryItem{item}, history...) // prepend

	// Keep history bounded if needed, here keeping all for now.
	data, err := json.MarshalIndent(history, "", "  ")
	if err != nil {
		return err
	}

	dir := getAppDataDir()
	os.MkdirAll(dir, 0755)
	return writeFileAtomic(getHistoryPath(), data, 0o600)
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
	if err != nil || dir == "" {
		return ""
	}
	a.mu.Lock()
	a.writeGrants = append(a.writeGrants, dir)
	a.mu.Unlock()
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
		wailsRuntime.LogErrorf(a.ctx, "%s", err.Error())
		return err
	}

	return a.startBackendProcess(ctx, cancel, command, args, workDir, env, nil)
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
		wailsRuntime.LogErrorf(a.ctx, "%s", err.Error())
		return err
	}

	args := append([]string{}, baseArgs...)
	args = append(args, "--retry-track-json", trackJSON)
	return a.startBackendProcess(ctx, cancel, command, args, workDir, env, nil)
}

func (a *App) startBackendProcess(
	ctx context.Context,
	cancel context.CancelFunc,
	command string,
	args []string,
	workDir string,
	env []string,
	onEnd func(status string), // SF-1: runs before process_ended is emitted; nil for non-queue jobs
) error {

	cmd := exec.CommandContext(ctx, command, args...)
	hideProcess(cmd)
	// Stop must reach the whole tree, not just the PID we hold: the bundled
	// backend is a PyInstaller onefile binary that forks its real engine.
	setProcessGroup(cmd)
	cmd.Dir = workDir
	cmd.Env = env
	// CommandContext's default cancel kills only cmd.Process, which would
	// leave that forked engine running. Route it through the same tree kill.
	cmd.Cancel = func() error { return killProcessTree(cmd) }

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
		// Large playlist metadata events can exceed Scanner's default 64 KiB cap.
		// Raise it so 1000+ track payloads still reach the frontend intact.
		scanner.Buffer(make([]byte, 64*1024), 16*1024*1024)
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
				// issue #75 — swap the filesystem paths for an opaque id before
				// this reaches the webview. See playlist_orphans.go.
				if payload["type"] == "playlist_orphans" {
					safe := a.interceptPlaylistOrphans(payload)
					if safe == nil {
						// Malformed or empty report. Dropping it is right: the
						// alternative is relaying the original, which is the one
						// message in the stream that carries raw paths.
						continue
					}
					payload = safe
				}
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
		// SF-1 — update the queue BEFORE process_ended, because the frontend
		// asks for the next job the moment it sees that event.
		if onEnd != nil {
			onEnd(status)
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

// validateBackendLinks refuses link arguments the backend's argparse would read as
// flags. The links are passed positionally, so "--export-path" pasted as a "URL"
// would otherwise switch the backend into another mode.
func validateBackendLinks(links []string) error {
	for _, l := range links {
		if strings.HasPrefix(strings.TrimSpace(l), "-") {
			return fmt.Errorf("refused: %q is not a link", l)
		}
	}
	return nil
}

// killCommandTree stops cmd and everything it spawned. The per-platform work
// lives in proc_windows.go / proc_unix.go.
func killCommandTree(cmd *exec.Cmd) error {
	return killProcessTree(cmd)
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

func (a *App) CaptureSpDC() error {
	return a.startBrowserLoginFlow("--capture-sp-dc", "sp-dc-event", "sp_dc_done")
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
	if err := validateBackendLinks([]string{artistUrl}); err != nil {
		return `{"error":"` + strings.ReplaceAll(err.Error(), `"`, `'`) + `"}`
	}
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

// GetSpotifyLibrary returns the user's Spotify library (Liked Songs + playlists).
// Requires spotify_sp_dc to be configured.
// Returns a JSON string: the library object or {"error":"..."}.
func (a *App) GetSpotifyLibrary() string {
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()

	backend, err := ensureBundledBackend()
	if err != nil {
		return a.getSpotifyLibraryViaPython(ctx)
	}

	cmd := exec.CommandContext(ctx, backend, "--spotify-library", "--config", getConfigPath())
	hideProcess(cmd)
	out, err := cmd.Output()
	if err != nil {
		if ctx.Err() == context.DeadlineExceeded {
			return `{"error":"timed out fetching Spotify library (30s)"}`
		}
		return `{"error":"` + strings.ReplaceAll(err.Error(), `"`, `'`) + `"}`
	}
	return parseLibraryOutput(out, "spotify_library")
}

func (a *App) getSpotifyLibraryViaPython(ctx context.Context) string {
	pythonExe, _, workDir, env, err := a.resolveBackendCommand([]string{})
	if err != nil {
		return `{"error":"could not resolve backend"}`
	}
	cmd := exec.CommandContext(ctx, pythonExe, "-m", "antra.json_cli", "--spotify-library", "--config", getConfigPath())
	cmd.Dir = workDir
	cmd.Env = env
	hideProcess(cmd)
	out, err := cmd.Output()
	if err != nil {
		if ctx.Err() == context.DeadlineExceeded {
			return `{"error":"timed out fetching Spotify library (30s)"}`
		}
		return `{"error":"` + strings.ReplaceAll(err.Error(), `"`, `'`) + `"}`
	}
	return parseLibraryOutput(out, "spotify_library")
}

// GetAppleMusicLibrary returns the user's Apple Music library (saved songs + playlists).
// Requires the Apple Music web session fields to be configured.
// Returns a JSON string: the library object or {"error":"..."}.
func (a *App) GetAppleMusicLibrary() string {
	ctx, cancel := context.WithTimeout(context.Background(), 35*time.Second)
	defer cancel()

	backend, err := ensureBundledBackend()
	if err != nil {
		return a.getAppleMusicLibraryViaPython(ctx)
	}

	cmd := exec.CommandContext(ctx, backend, "--apple-library", "--config", getConfigPath())
	hideProcess(cmd)
	out, err := cmd.Output()
	if err != nil {
		if ctx.Err() == context.DeadlineExceeded {
			return `{"error":"timed out fetching Apple Music library (35s)"}`
		}
		return `{"error":"` + strings.ReplaceAll(err.Error(), `"`, `'`) + `"}`
	}
	return parseLibraryOutput(out, "apple_library")
}

func (a *App) getAppleMusicLibraryViaPython(ctx context.Context) string {
	pythonExe, _, workDir, env, err := a.resolveBackendCommand([]string{})
	if err != nil {
		return `{"error":"could not resolve backend"}`
	}
	cmd := exec.CommandContext(ctx, pythonExe, "-m", "antra.json_cli", "--apple-library", "--config", getConfigPath())
	cmd.Dir = workDir
	cmd.Env = env
	hideProcess(cmd)
	out, err := cmd.Output()
	if err != nil {
		if ctx.Err() == context.DeadlineExceeded {
			return `{"error":"timed out fetching Apple Music library (35s)"}`
		}
		return `{"error":"` + strings.ReplaceAll(err.Error(), `"`, `'`) + `"}`
	}
	return parseLibraryOutput(out, "apple_library")
}

// parseLibraryOutput scans newline-delimited JSON output and extracts a single
// library event. Other lines (log events, etc.) are ignored so stray config
// messages do not break JSON parsing.
func parseLibraryOutput(out []byte, eventType string) string {
	for _, line := range bytes.Split(bytes.TrimSpace(out), []byte("\n")) {
		line = bytes.TrimSpace(line)
		if len(line) == 0 {
			continue
		}
		var wrapper map[string]interface{}
		if err := json.Unmarshal(line, &wrapper); err != nil {
			continue
		}
		switch wrapper["type"] {
		case eventType:
			result, _ := json.Marshal(wrapper["data"])
			return string(result)
		case "error":
			msg, _ := wrapper["message"].(string)
			return `{"error":"` + strings.ReplaceAll(msg, `"`, `'`) + `"}`
		}
	}
	return `{"error":"no ` + eventType + ` event in backend output"}`
}

// exportFormatExtensions mirrors antra/utils/playlist_export.py's EXPORT_EXTENSIONS.
var exportFormatExtensions = map[string]string{
	"csv":  ".csv",
	"m3u8": ".m3u8",
	"xspf": ".xspf",
	"json": ".json",
	"txt":  ".txt",
}

var exportFormatFilters = map[string]string{
	"csv":  "Comma-separated values (*.csv)",
	"m3u8": "Playlist (*.m3u8)",
	"xspf": "XSPF playlist (*.xspf)",
	"json": "JSON (*.json)",
	"txt":  "Text file (*.txt)",
}

// ExportPlaylist resolves `url` and writes its tracklist to a file the user
// picks (v2.0.0 FR-3). Returns the backend's playlist_exported summary as JSON,
// {"cancelled":true} if the save dialog is dismissed, or {"error":"..."}.
//
// The metadata is re-fetched rather than taken from the frontend: the
// playlist_loaded event carries only artist/title/duration, which is far short
// of an Exportify CSV, and widening it would put a per-track metadata blob on
// every resolve for a feature most runs never use.
func (a *App) ExportPlaylist(url string, format string, suggestedName string) string {
	format = strings.ToLower(strings.TrimSpace(format))
	if _, ok := exportFormatExtensions[format]; !ok {
		format = "csv"
	}
	if strings.TrimSpace(url) == "" {
		return `{"error":"No link to export - resolve a release first."}`
	}
	// exportTracklist() has no http-prefix filter on inputUrl; refuse here.
	if err := validateBackendLinks([]string{url}); err != nil {
		return `{"error":"` + strings.ReplaceAll(err.Error(), `"`, `'`) + `"}`
	}

	ext := exportFormatExtensions[format]
	defaultName := strings.TrimSpace(suggestedName)
	if defaultName == "" {
		defaultName = "playlist"
	}
	if !strings.HasSuffix(strings.ToLower(defaultName), ext) {
		defaultName += ext
	}

	target, dlgErr := wailsRuntime.SaveFileDialog(a.ctx, wailsRuntime.SaveDialogOptions{
		Title:                "Export tracklist",
		DefaultFilename:      defaultName,
		CanCreateDirectories: true,
		Filters: []wailsRuntime.FileFilter{
			{DisplayName: exportFormatFilters[format], Pattern: "*" + ext},
		},
	})
	if dlgErr != nil {
		return `{"error":"` + strings.ReplaceAll(dlgErr.Error(), `"`, `'`) + `"}`
	}
	if strings.TrimSpace(target) == "" {
		return `{"cancelled":true}`
	}

	// A large playlist genuinely takes minutes to fetch, so this is generous
	// rather than the 30s used by the small one-shot lookups.
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Minute)
	defer cancel()

	args := []string{
		"--export-playlist", url,
		"--export-format", format,
		"--export-path", target,
		"--config", getConfigPath(),
	}

	backend, err := ensureBundledBackend()
	var out []byte
	if err == nil {
		cmd := exec.CommandContext(ctx, backend, args...)
		hideProcess(cmd)
		out, err = cmd.Output()
	} else {
		pythonExe, _, workDir, env, resolveErr := a.resolveBackendCommand([]string{})
		if resolveErr != nil {
			return `{"error":"could not resolve backend"}`
		}
		cmd := exec.CommandContext(ctx, pythonExe, append([]string{"-m", "antra.json_cli"}, args...)...)
		cmd.Dir = workDir
		cmd.Env = env
		hideProcess(cmd)
		out, err = cmd.Output()
	}

	// A non-zero exit still carries a JSON error line on stdout, so parse first
	// and only fall back to the process error when nothing usable came back.
	parsed := parseExportOutput(out)
	if parsed != "" {
		return parsed
	}
	if err != nil {
		if ctx.Err() == context.DeadlineExceeded {
			return `{"error":"Export timed out while fetching the tracklist."}`
		}
		return `{"error":"` + strings.ReplaceAll(err.Error(), `"`, `'`) + `"}`
	}
	return `{"error":"The backend produced no export result."}`
}

// parseExportOutput scans newline-delimited JSON for the playlist_exported
// event. Log lines precede it, so the whole buffer cannot simply be unmarshalled.
func parseExportOutput(out []byte) string {
	for _, line := range bytes.Split(bytes.TrimSpace(out), []byte("\n")) {
		line = bytes.TrimSpace(line)
		if len(line) == 0 {
			continue
		}
		var wrapper map[string]interface{}
		if err := json.Unmarshal(line, &wrapper); err != nil {
			continue
		}
		switch wrapper["type"] {
		case "playlist_exported":
			result, marshalErr := json.Marshal(wrapper)
			if marshalErr != nil {
				return `{"error":"could not encode export result"}`
			}
			return string(result)
		case "error":
			msg, _ := wrapper["message"].(string)
			return `{"error":"` + strings.ReplaceAll(msg, `"`, `'`) + `"}`
		}
	}
	return ""
}

// RunAutoSync triggers an immediate auto-sync of all tracked playlists.
// Streams newline-delimited JSON events on stdout (same format as StartDownload).
// Returns empty string on success or an error string.
func (a *App) RunAutoSync() string {
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Minute)
	defer cancel()

	backend, err := ensureBundledBackend()
	var cmd *exec.Cmd
	if err != nil {
		pythonExe, _, workDir, env, resolveErr := a.resolveBackendCommand([]string{})
		if resolveErr != nil {
			return `error:could not resolve backend`
		}
		cmd = exec.CommandContext(ctx, pythonExe, "-m", "antra.json_cli", "--auto-sync", "--config", getConfigPath())
		cmd.Dir = workDir
		cmd.Env = env
	} else {
		cmd = exec.CommandContext(ctx, backend, "--auto-sync", "--config", getConfigPath())
	}
	hideProcess(cmd)

	stdout, pipeErr := cmd.StdoutPipe()
	if pipeErr != nil {
		return `error:` + pipeErr.Error()
	}
	if startErr := cmd.Start(); startErr != nil {
		return `error:` + startErr.Error()
	}

	scanner := bufio.NewScanner(stdout)
	scanner.Buffer(make([]byte, 16*1024*1024), 16*1024*1024)
	for scanner.Scan() {
		line := scanner.Text()
		wailsRuntime.EventsEmit(a.ctx, "auto_sync_event", line)
	}
	_ = cmd.Wait()
	return ""
}

// SearchArtists searches for artists by name using the given source ("spotify" or "apple").
// searchArtistsArgs builds the backend argv for an artist search. The query is
// joined with "=" because a query that begins with a dash (the real artist
// "-M-", say) is otherwise read by argparse as the next flag and the CLI exits
// with "expected one argument" (measured 2026-10-26).
func searchArtistsArgs(query, source, configPath string) []string {
	return []string{"--search-artists=" + query, "--search-source", source, "--config", configPath}
}

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
		cmd := exec.CommandContext(ctx, backend, searchArtistsArgs(query, source, getConfigPath())...)
		hideProcess(cmd)
		out, err = cmd.Output()
	} else {
		// Dev fallback
		pythonExe, _, workDir, env, resolveErr := a.resolveBackendCommand([]string{})
		if resolveErr != nil {
			return `{"error":"could not resolve backend"}`
		}
		cmd := exec.CommandContext(ctx, pythonExe, append([]string{"-m", "antra.json_cli"}, searchArtistsArgs(query, source, getConfigPath())...)...)
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

// searchCatalogArgs builds the backend argv for a catalogue search.
//
// kind and source are normalised HERE rather than passed through, because
// --search-kind and --search-source are argparse `choices=`: an unknown value
// makes the CLI exit 2 with the reason on stderr, which reaches the user as a
// bare "exit status 2". Split out from SearchCatalog so it can be tested
// without spawning a process.
func searchCatalogArgs(query, kind, source, configPath string) []string {
	switch kind {
	case "song", "album", "artist":
	default:
		kind = "song"
	}
	switch source {
	case "apple", "spotify":
	default:
		source = "apple"
	}
	// `--search-catalog=<query>` rather than two argv elements: a query that
	// begins with a dash ("-", "--config", a song actually titled "--") is
	// otherwise read by argparse as the NEXT FLAG, and the CLI exits 2 with
	// "expected one argument". Measured 2026-09-26: the separate-argument form
	// exits 2 on `--config`, the `=` form returns results. The other three are
	// fixed strings we control, so they stay as plain pairs.
	return []string{
		"--search-catalog=" + query,
		"--search-kind", kind,
		"--search-source", source,
		"--config", configPath,
	}
}

// SearchCatalog searches for songs, albums or artists (issue #58 - the app
// could only ever search for an artist, so finding one song meant opening the
// artist and reading the whole discography).
//
// kind is "song", "album" or "artist"; source is "apple" or "spotify". Both are
// normalised here rather than passed through, because --search-kind and
// --search-source are argparse `choices=` and an unknown value makes the CLI
// exit 2 with the error on stderr, which surfaces as a bare "exit status 2".
//
// Returns a JSON string: the data array, or {"error":"..."}.
func (a *App) SearchCatalog(query string, kind string, source string) string {
	ctx, cancel := context.WithTimeout(context.Background(), 45*time.Second)
	defer cancel()

	args := searchCatalogArgs(query, kind, source, getConfigPath())

	backend, err := ensureBundledBackend()
	var out []byte
	if err == nil {
		cmd := exec.CommandContext(ctx, backend, args...)
		hideProcess(cmd)
		out, err = cmd.Output()
	} else {
		// Dev fallback
		pythonExe, _, workDir, env, resolveErr := a.resolveBackendCommand([]string{})
		if resolveErr != nil {
			return `{"error":"could not resolve backend"}`
		}
		cmd := exec.CommandContext(ctx, pythonExe, append([]string{"-m", "antra.json_cli"}, args...)...)
		cmd.Dir = workDir
		cmd.Env = env
		hideProcess(cmd)
		out, err = cmd.Output()
	}

	if err != nil {
		if ctx.Err() == context.DeadlineExceeded {
			return `{"error":"search timed out"}`
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
	result, _ := json.Marshal(wrapper["data"])
	return string(result)
}

func (a *App) GetDiscoveryData(region string, genreId string, genreName string, source string) string {
	ctx, cancel := context.WithTimeout(context.Background(), 60*time.Second)
	defer cancel()

	if region == "" {
		region = "us"
	}
	// Normalise before it reaches the CLI: --discovery-source is a choices=
	// argument, so an empty or unexpected value fails the whole call rather
	// than degrading. Spotify's Discover is the personalised home feed, and the
	// backend falls back to Apple by itself when no account is connected.
	if source != "spotify" {
		source = "apple"
	}

	backend, err := ensureBundledBackend()
	var out []byte
	if err == nil {
		args := []string{"--discovery-json", "--discovery-region", region, "--discovery-source", source}
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
		args := []string{"-m", "antra.json_cli", "--discovery-json", "--discovery-region", region, "--discovery-source", source}
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

func (a *App) GetDiscoveryGenres(region string, source string) string {
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()

	if region == "" {
		region = "us"
	}
	if source != "spotify" {
		source = "apple"
	}

	backend, err := ensureBundledBackend()
	var out []byte
	if err == nil {
		cmd := exec.CommandContext(ctx, backend, "--discovery-genres-only", "--discovery-region", region, "--discovery-source", source, "--config", getConfigPath())
		hideProcess(cmd)
		out, err = cmd.Output()
	} else {
		pythonExe, _, workDir, env, resolveErr := a.resolveBackendCommand([]string{})
		if resolveErr != nil {
			return `{"error":"could not resolve backend"}`
		}
		cmd := exec.CommandContext(ctx, pythonExe, "-m", "antra.json_cli", "--discovery-genres-only", "--discovery-region", region, "--discovery-source", source, "--config", getConfigPath())
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

// GetAlbumAvailability fetches country-by-country availability for a Spotify or
// Deezer album URL. Returns a JSON string with service, segments, stats, and
// artwork. Takes ~5-30s for Spotify (probes every market in parallel).
func (a *App) GetAlbumAvailability(url string) string {
	// The availability box has no http-prefix filter in the frontend; refuse
	// here rather than let argparse read a dash-leading paste as another flag.
	if err := validateBackendLinks([]string{url}); err != nil {
		return `{"error":"` + strings.ReplaceAll(err.Error(), `"`, `'`) + `"}`
	}
	ctx, cancel := context.WithTimeout(context.Background(), 120*time.Second)
	defer cancel()

	backend, err := ensureBundledBackend()
	var out []byte
	if err == nil {
		cmd := exec.CommandContext(ctx, backend, "--availability-url", url, "--config", getConfigPath())
		hideProcess(cmd)
		out, err = cmd.Output()
	} else {
		pythonExe, _, workDir, env, resolveErr := a.resolveBackendCommand([]string{})
		if resolveErr != nil {
			return `{"error":"could not resolve backend"}`
		}
		cmd := exec.CommandContext(ctx, pythonExe, "-m", "antra.json_cli", "--availability-url", url, "--config", getConfigPath())
		cmd.Dir = workDir
		cmd.Env = env
		hideProcess(cmd)
		out, err = cmd.Output()
	}

	if err != nil {
		if ctx.Err() == context.DeadlineExceeded {
			return `{"error":"availability lookup timed out (120s)"}`
		}
		errMsg := strings.ReplaceAll(strings.TrimSpace(string(out)), `"`, `'`)
		if errMsg == "" {
			errMsg = err.Error()
		}
		return `{"error":"` + errMsg + `"}`
	}

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

// supporterContext resolves, in a single /api/keys/validate round-trip, the two
// things a download run needs from the user's key: whether they get supporter
// concurrency, and whether their own key may be used to authenticate to the
// mirrors.
//
// The second return value is the load-bearing one and it is deliberately empty
// unless the server confirmed a supporter key.  Mirror adapters otherwise use
// the shared manifest key (see service.py `mirror_api_key`), and v1.1.7 proved
// that sending an unregistered personal key there 401s and silently kills
// whole-album resolution.  So an unverified guess is never sent — only a key the
// server has just vouched for.
//
// Why it matters: FEAT-7's politeness-delay discount is chosen from the key on
// the incoming request.  Because every desktop download authenticated with the
// shared manifest key (key_type "regular"), no desktop supporter has ever
// received the discount — the FEAT-7 measurements were taken server-side against
// production keys directly, never through the adapter path.
// There are now two possible personal credentials — a device-login token and a
// pasted supporter key — and the choice between them is deliberately made on
// TIER, not on precedence. Preferring the token unconditionally would price a
// supporter who signed in with a free website account as free, which is exactly
// the demotion this whole area got wrong before; preferring the key
// unconditionally would throw away FEAT-8's per-user attribution. So: whichever
// credential the server confirms as supporter wins, and the token is used at
// free tier when neither is.
func (a *App) supporterContext() (isSupporter bool, mirrorKey string) {
	cfg := a.GetConfig()
	deviceToken := storedDeviceToken(cfg)
	personal := strings.TrimSpace(cfg.AntraApiKey)
	if strings.HasPrefix(personal, "at_") {
		personal = "" // a token in the legacy shared field is not a supporter key
	}

	var status DeviceAccountStatus
	if deviceToken != "" {
		status = a.GetDeviceAccountStatus()
	}
	var keyInfo KeyInfoResult
	if personal != "" {
		// "Server unreachable" leaves Valid false even though the key may well be
		// fine — the conservative rule is deliberate, because v1.1.7 showed an
		// unvouched key 401ing at the mirrors kills whole-album resolution.
		keyInfo = a.validateKeyAgainstServer(personal)
	}

	decided, key, needManifestCheck := chooseCredential(deviceToken, status, personal, keyInfo)
	if needManifestCheck {
		// No personal credential at all. Validate the manifest key purely for the
		// supporter check, exactly as the previous isSupporterKey() did. It is
		// never returned as a mirror key: the adapters already default to it, so
		// passing it would be a no-op.
		info := a.GetKeyInfo()
		return info.Valid && info.IsSupporter, ""
	}
	return decided, key
}

// chooseCredential is the whole decision, kept pure so the table can be tested
// without a server: which credential authenticates mirror requests, and whether
// this user counts as a supporter.
//
// The third return says "neither credential exists, fall back to checking the
// shared manifest key" — kept out of here because it needs a network call.
func chooseCredential(
	deviceToken string, status DeviceAccountStatus,
	personal string, keyInfo KeyInfoResult,
) (isSupporter bool, mirrorKey string, needManifestCheck bool) {
	// A token the server has positively refused (revoked, or past the renewal
	// chain cap) must not authenticate anything. An UNREACHABLE server is not a
	// refusal — signing someone out over an outage is the exact failure FEAT-12
	// exists to prevent — so the token is kept and simply carries no tier.
	if deviceToken != "" && status.Reachable && !status.Valid {
		deviceToken = ""
	}
	deviceSupporter := deviceToken != "" && status.IsSupporter
	keySupporter := personal != "" && keyInfo.Valid && keyInfo.IsSupporter

	switch {
	case deviceSupporter:
		return true, deviceToken, false
	case keySupporter:
		// The key wins here rather than the token: preferring the token
		// unconditionally would price a supporter who signed in with a free
		// website account as free, which is the demotion this area got wrong.
		return true, personal, false
	case deviceToken != "":
		// Signed in at free tier: still authenticate as this user, which is the
		// point of FEAT-8 — per-account attribution instead of one shared key.
		return false, deviceToken, false
	case personal == "":
		return false, "", true
	default:
		return false, "", false
	}
}

// endpointEnv hands the resolved mirror URLs to the Python backend as env vars.
//
// Go is the single fetcher: it is the side that holds the device token, so it is
// the only side that can ask the account for endpoints. Python's config reads
// these env vars ahead of any manifest, so with them set the backend never needs
// to fetch anything — and ANTRA_ENDPOINT_MANIFEST_DISABLED stops it falling back
// to the public gist behind our back.
//
// Empty values are omitted rather than exported blank, so a partial manifest
// degrades to the existing behaviour for the missing entries instead of
// blanking a URL Python would otherwise have resolved.
func endpointEnv() []string {
	return endpointEnvFrom(loadEndpointManifest())
}

func endpointEnvFrom(m endpointManifest) []string {
	var env []string
	add := func(name, value string) {
		v := strings.TrimRight(strings.TrimSpace(value), "/")
		if v == "" {
			return
		}
		// The backend is handed the account credential alongside these URLs, so
		// a host the manifest invented would receive it. Anything not on the
		// allowlist is dropped rather than exported, and Python then resolves
		// that mirror the old way from its own built-in defaults — the same
		// degradation an absent entry already produced.
		if !isTrustedAntraHost(v) {
			return
		}
		env = append(env, name+"="+v)
	}
	add("TIDAL_MIRROR_URL", m.Mirrors.Tidal)
	add("QOBUZ_MIRROR_URL", m.Mirrors.Qobuz)
	add("DEEZER_MIRROR_URL", m.Mirrors.Deezer)
	add("APPLE_MIRRORS", m.Mirrors.Apple)
	add("AMAZON_MIRRORS", m.Mirrors.Amazon)
	// Only claim the backend needs no manifest when we actually gave it the
	// endpoints; otherwise let it resolve them the old way. Counted BEFORE the
	// key is appended, so a manifest carrying a key but no URLs cannot make the
	// backend believe it was handed endpoints it never received.
	gaveEndpoints := len(env) > 0
	// Hand over the manifest's key alongside its URLs.
	//
	// Setting ANTRA_ENDPOINT_MANIFEST_DISABLED stops Python fetching the gist,
	// after which its ONLY remaining source for the shared key is the on-disk
	// cache. On any machine where that cache is missing or unreadable both the
	// manifest key and the personal key came back empty, the X-API-Key header
	// was omitted, and every Tidal/Qobuz metadata call got
	// `401 API key required`. Exporting the key with the URLs it belongs to
	// removes that dependency entirely.
	//
	// Kept separate from ANTRA_MIRROR_API_KEY on purpose: that one means "a key
	// the server confirmed is a SUPPORTER key" and it outranks this for FEAT-7's
	// pacing discount. Overloading it would price every free user as a supporter.
	if k := strings.TrimSpace(m.ApiKey); k != "" {
		env = append(env, "ANTRA_MANIFEST_API_KEY="+k)
	}
	if gaveEndpoints {
		env = append(env, "ANTRA_ENDPOINT_MANIFEST_DISABLED=1")
	}
	return env
}

func (a *App) resolveBackendCommand(playlists []string) (string, []string, string, []string, error) {
	if err := validateBackendLinks(playlists); err != nil {
		return "", nil, "", nil, err
	}
	// Determine concurrent-worker count: 2 for supporter keys, 1 for everyone else.
	// v1.1.8 FEAT-7 — concurrency is the SAFE supporter speed lever. The mirrors'
	// politeness delay is per streaming account, so N workers use N *different*
	// accounts in parallel and each keeps its own spacing: throughput scales
	// without raising the per-account request rate that risks bans. Raised 2 -> 4.
	// Deliberately not higher: the Tidal pool has a documented thundering-herd
	// history (v1.1.7), and 4 stays well inside it.
	isSupporter, mirrorKey := a.supporterContext()
	maxWorkers := "1"
	if isSupporter {
		maxWorkers = "4"
	}
	extraEnv := []string{"PYTHONUTF8=1", "ANTRA_MAX_WORKERS=" + maxWorkers}
	if mirrorKey != "" {
		extraEnv = append(extraEnv, "ANTRA_MIRROR_API_KEY="+mirrorKey)
	}
	extraEnv = append(extraEnv, endpointEnv()...)

	if bundledBackend, err := ensureBundledBackend(); err == nil {
		args := append([]string{}, playlists...)
		args = append(args, "--config", getConfigPath())
		return bundledBackend, args, filepath.Dir(bundledBackend), append(os.Environ(), extraEnv...), nil
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
	// Reuse extraEnv rather than re-listing the variables: the two paths had
	// already drifted once, and a variable added above must not silently be
	// missing when running from source.
	env := append(os.Environ(), fmt.Sprintf("PYTHONPATH=%s", parentDir))
	env = append(env, extraEnv...)
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

// fetchEndpointsFromAccount asks the website for the mirror endpoints, using the
// signed-in device token (v1.1.8 FEAT-8 Phase A).
//
// This is what lets the public gist stop carrying a working credential. It does
// NOT make the hostnames secret — they are in public DNS and in the TLS SNI of
// every request, so anyone can find them. What it changes is that finding them
// is no longer the same as being able to USE them.
//
// Returns ok=false for any problem, including not being signed in, so every
// caller falls back to the gist exactly as before.
func fetchEndpointsFromAccount() (endpointManifest, bool) {
	token := storedDeviceToken((&App{}).GetConfig())
	if token == "" {
		return endpointManifest{}, false
	}

	client := &http.Client{Timeout: 10 * time.Second}
	req, err := http.NewRequest(http.MethodGet, deviceAuthURL("/api/desktop/endpoints"), nil)
	if err != nil {
		return endpointManifest{}, false
	}
	req.Header.Set("X-API-Key", token)
	resp, err := client.Do(req)
	if err != nil {
		return endpointManifest{}, false
	}
	defer resp.Body.Close()
	if resp.StatusCode != 200 {
		return endpointManifest{}, false
	}

	var body struct {
		Mirrors endpointManifestMirrors `json:"mirrors"`
	}
	if json.NewDecoder(resp.Body).Decode(&body) != nil {
		return endpointManifest{}, false
	}
	m := endpointManifest{Mirrors: body.Mirrors}
	m.normalize()
	// Deliberately no ApiKey: the device token IS the credential now, and it is
	// already sent as ANTRA_MIRROR_API_KEY.
	if m.Mirrors.Tidal == "" && m.Mirrors.Qobuz == "" && m.Mirrors.Apple == "" &&
		m.Mirrors.Amazon == "" && m.Mirrors.Deezer == "" {
		return endpointManifest{}, false
	}
	return m, true
}

// loadEndpointManifest is called on many paths (health checks, key validation,
// every download start), and each call used to make a network fetch. With the
// account lookup added in front of it that would be two, so the result is cached
// for the process. invalidateEndpointManifest() is called whenever sign-in state
// changes, which is the only thing that can change the answer.
var (
	manifestCacheMu  sync.Mutex
	manifestCacheVal endpointManifest
	manifestCacheAt  time.Time
	manifestCacheOK  bool
	manifestCacheTTL = 5 * time.Minute
)

func invalidateEndpointManifest() {
	manifestCacheMu.Lock()
	manifestCacheOK = false
	manifestCacheMu.Unlock()
}

func loadEndpointManifest() endpointManifest {
	manifestCacheMu.Lock()
	if manifestCacheOK && time.Since(manifestCacheAt) < manifestCacheTTL {
		cached := manifestCacheVal
		manifestCacheMu.Unlock()
		return cached
	}
	manifestCacheMu.Unlock()

	result := resolveEndpointManifest()

	manifestCacheMu.Lock()
	manifestCacheVal, manifestCacheAt, manifestCacheOK = result, time.Now(), true
	manifestCacheMu.Unlock()
	return result
}

func resolveEndpointManifest() endpointManifest {
	// Signed-in clients get their endpoints from the account, not the gist.
	if manifest, ok := fetchEndpointsFromAccount(); ok {
		writeEndpointManifestCache(manifest)
		return manifest
	}

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
// "apple", "qobuz", "deezer") in parallel and returns a JSON-encoded
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

// ── Key info ──────────────────────────────────────────────────────────────────

// KeyInfoResult is returned by GetKeyInfo to the Svelte frontend.
// It tells the app whether the current key is a supporter (admin) key and
// exposes the daily usage so the UI can show remaining quota.
type KeyInfoResult struct {
	Valid         bool                   `json:"valid"`
	IsSupporter   bool                   `json:"is_supporter"`
	KeyType       string                 `json:"key_type,omitempty"`
	ExpiresAt     string                 `json:"expires_at,omitempty"`
	DownloadLimit int                    `json:"download_limit,omitempty"`
	DownloadCount int                    `json:"download_count,omitempty"`
	UsageToday    map[string]interface{} `json:"usage_today,omitempty"`
	Error         string                 `json:"error,omitempty"`
	// Reachable distinguishes "the server told us this key is bad" from "we could
	// not ask" (v1.1.8 BUG-5). Both used to collapse into Valid=false, which gave
	// the UI no way to avoid rejecting a genuinely valid key just because the
	// user was offline. Only Reachable=true && Valid=false means "key is bad".
	Reachable bool `json:"reachable"`
}

// ValidateKey checks an arbitrary key against the server WITHOUT persisting it,
// so the Settings panel can refuse to save a key that does not work (BUG-5).
// GetKeyInfo() validates whatever is already in config; this validates a
// candidate the user just pasted.
func (a *App) ValidateKey(key string) KeyInfoResult {
	key = strings.TrimSpace(key)
	if key == "" {
		return KeyInfoResult{Valid: false, Reachable: true, Error: "Enter a key first."}
	}
	return a.validateKeyAgainstServer(key)
}

// validateKeyAgainstServer performs the actual /api/keys/validate round-trip.
func (a *App) validateKeyAgainstServer(key string) KeyInfoResult {
	manifest := loadEndpointManifest()
	tidalURL := strings.TrimRight(manifest.Mirrors.Tidal, "/")
	if tidalURL == "" {
		return KeyInfoResult{Valid: false, Reachable: false, Error: "Could not reach Antra servers."}
	}

	client := &http.Client{Timeout: 8 * time.Second}
	req, err := http.NewRequest(http.MethodGet, tidalURL+"/api/keys/validate", nil)
	if err != nil {
		return KeyInfoResult{Valid: false, Reachable: false, Error: err.Error()}
	}
	req.Header.Set("X-API-Key", key)

	resp, err := client.Do(req)
	if err != nil {
		return KeyInfoResult{Valid: false, Reachable: false, Error: "Could not reach Antra servers."}
	}
	defer resp.Body.Close()

	if resp.StatusCode == 403 || resp.StatusCode == 401 {
		// The server answered — this key is genuinely not recognised.
		return KeyInfoResult{Valid: false, Reachable: true, Error: "Key not recognized."}
	}
	if resp.StatusCode != 200 {
		// Server reachable but unhealthy — treat as "could not verify", not "bad key".
		return KeyInfoResult{Valid: false, Reachable: false,
			Error: fmt.Sprintf("Server returned %d.", resp.StatusCode)}
	}

	var result KeyInfoResult
	if err := json.NewDecoder(resp.Body).Decode(&result); err != nil {
		return KeyInfoResult{Valid: false, Reachable: false, Error: "Unexpected response from server."}
	}
	result.Reachable = true
	return result
}

// GetKeyInfo validates the currently configured Antra API key against the VPS
// and returns supporter status + daily usage.  The result is used by the
// frontend to show quota info and by resolveBackendCommand to enable 2-worker
// mode for supporter keys.
func (a *App) GetKeyInfo() KeyInfoResult {
	cfg := a.GetConfig()
	key := strings.TrimSpace(cfg.AntraApiKey)
	if strings.HasPrefix(key, "at_") {
		// A device token is not a keys.json entry, so /api/keys/validate would
		// answer 403 and the UI would report "Key not recognized" — the BUG this
		// separation fixes. Device tokens are checked via /api/device/status.
		key = ""
	}
	if key == "" {
		// Try manifest key as fallback
		manifest := loadEndpointManifest()
		key = strings.TrimSpace(manifest.ApiKey)
	}
	if key == "" {
		return KeyInfoResult{Valid: false, Reachable: true, Error: "No API key configured."}
	}
	return a.validateKeyAgainstServer(key)
}

// SaveCoverArt downloads the album/playlist cover art at maximum resolution
// and saves it to the library root folder.
func (a *App) SaveCoverArt(artworkUrl string, title string) string {
	cfg := a.GetConfig()
	libraryRoot := strings.TrimSpace(cfg.DownloadPath)
	if libraryRoot == "" {
		return "error: no music library folder configured"
	}

	// Upgrade URL to 3000x3000 hi-res
	// Apple Music: {w}x{h}bb → 3000x3000bb
	// Spotify: no dimension tokens — use as-is (largest available)
	hiResURL := regexp.MustCompile(`\{\w\}x\{\w\}bb`).ReplaceAllString(artworkUrl, "3000x3000bb")
	hiResURL = regexp.MustCompile(`\d+x\d+bb`).ReplaceAllString(hiResURL, "3000x3000bb")

	// Sanitize title for filename
	safeTitle := strings.Map(func(r rune) rune {
		if strings.ContainsRune(`<>:"/\|?*`, r) {
			return '_'
		}
		return r
	}, strings.TrimSpace(title))
	if safeTitle == "" {
		safeTitle = "cover"
	}
	safeTitle = strings.TrimRight(safeTitle, ". ")

	client := &http.Client{Timeout: 30 * time.Second}
	resp, err := client.Get(hiResURL)
	if err != nil {
		return fmt.Sprintf("error: failed to download cover art — %v", err)
	}

	if resp.StatusCode != 200 {
		resp.Body.Close()
		resp2, err2 := client.Get(artworkUrl)
		if err2 != nil {
			return fmt.Sprintf("error: failed to download cover art — %v", err2)
		}
		resp = resp2
		if resp.StatusCode != 200 {
			resp.Body.Close()
			return fmt.Sprintf("error: server returned %d", resp.StatusCode)
		}
		hiResURL = artworkUrl
	}
	defer resp.Body.Close()

	// Determine extension from content-type or URL
	contentType := resp.Header.Get("Content-Type")
	ext := ".jpg"
	if strings.Contains(contentType, "png") {
		ext = ".png"
	} else if strings.Contains(contentType, "webp") {
		ext = ".webp"
	}

	outPath := filepath.Join(libraryRoot, safeTitle+ext)
	data, err := io.ReadAll(resp.Body)
	if err != nil {
		return fmt.Sprintf("error: failed to read cover art — %v", err)
	}

	if err := os.MkdirAll(filepath.Dir(outPath), 0755); err != nil {
		return fmt.Sprintf("error: %v", err)
	}
	if err := os.WriteFile(outPath, data, 0644); err != nil {
		return fmt.Sprintf("error: failed to save cover art — %v", err)
	}

	return fmt.Sprintf("ok: %s", outPath)
}

// preserveCorruptConfig copies an unreadable config.json aside once, so the
// defaults that replace it cannot overwrite the only copy of the user's settings.
func preserveCorruptConfig(path string, data []byte) {
	bak := path + ".corrupt"
	if _, err := os.Stat(bak); err == nil {
		return // keep the first bad copy; later ones are usually the same file
	}
	_ = os.WriteFile(bak, data, 0o600)
}
