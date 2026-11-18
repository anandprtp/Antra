package main

// FR-4 (v2.0.0) — save the activity log to a .txt file.
//
// The log lives entirely in the frontend (`logs[]` in App.svelte), so the
// frontend renders it to plain text and this file does the two things it
// cannot: stamp a context header, and strip credentials before anything
// reaches disk.
//
// Redaction is deliberately NOT the frontend's job. Users paste these files
// into public issue trackers, and the frontend only knows the credentials it
// happens to have bound to inputs — it has no idea about the shared manifest
// key, which arrives from a remote gist and never touches a Svelte variable.
// Doing it here means one place owns the safety property, with the config as
// the source of truth for exact values and a pattern pass for everything else.

import (
	_ "embed"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"runtime"
	"sort"
	"strings"
	"time"

	wailsRuntime "github.com/wailsapp/wails/v2/pkg/runtime"
)

// wails.json is embedded rather than the version being copied into a Go
// constant. There are already three copies of the version string in this repo
// (wails.json, antra/__init__.py, the Settings footer) and a fourth that could
// drift would make the log header actively misleading — it would tell the
// maintainer the wrong version, which is worse than omitting it. Embedding
// reads the real value at compile time, so the Go side cannot drift at all;
// TestAppVersionMatchesPythonPackage pins the remaining two together.
//
//go:embed wails.json
var wailsProjectJSON []byte

func appVersion() string {
	var probe struct {
		Info struct {
			ProductVersion string `json:"productVersion"`
		} `json:"info"`
	}
	if err := json.Unmarshal(wailsProjectJSON, &probe); err == nil {
		if v := strings.TrimSpace(probe.Info.ProductVersion); v != "" {
			return v
		}
	}
	return "unknown"
}

// ── Redaction ───────────────────────────────────────────────────────────────

// logSecretMinLen is the shortest value the exact-match pass will replace.
//
// Every token-shaped credential is far longer than this (ak_/pk_ are 43 chars,
// ARLs 192, sp_dc 150+), so the floor only ever excludes short *passwords* —
// and a global search-and-replace of a short string is genuinely destructive:
// a Qobuz password of "download" would blank that word out of every line and
// wreck the log's diagnostic value. Passwords are also provably never logged
// (nothing in antra/ writes one), so the leak risk this trades away is
// theoretical while the mangling risk is not.
const logSecretMinLen = 12

// logRedactFields maps a Config json tag to the label written in its place.
// TestEveryCredentialFieldIsClassified asserts every credential-shaped Config
// field appears here or in logRedactExempt, so adding a new token to Config
// fails the tests until someone decides which it is.
var logRedactFields = map[string]string{
	"apple_authorization_token": "apple_authorization_token",
	"apple_music_user_token":    "apple_music_user_token",
	"amazon_direct_creds_json":  "amazon_direct_creds_json",
	"qobuz_password":            "qobuz_password",
	"qobuz_app_secret":          "qobuz_app_secret",
	"qobuz_user_auth_token":     "qobuz_user_auth_token",
	"deezer_arl_token":          "deezer_arl_token",
	"deezer_bf_secret":          "deezer_bf_secret",
	"soulseek_password":         "soulseek_password",
	"spotify_sp_dc":             "spotify_sp_dc",
	"tidal_session_json":        "tidal_session_json",
	"tidal_access_token":        "tidal_access_token",
	"tidal_refresh_token":       "tidal_refresh_token",
	"tidal_session_id":          "tidal_session_id",
	"antra_api_key":             "antra_api_key",
	"antra_device_token":        "antra_device_token",
}

// logRedactExempt: fields whose names look like credentials but are not, each
// with the reason. Redacting these would be actively harmful.
var logRedactExempt = map[string]string{
	// Literally "Bearer" — replacing it globally would blank a common word.
	"tidal_token_type": "constant scheme name, not a secret",
	// The public web-player application id, shared by every install.
	"qobuz_app_id": "public application id, not per-user",
	// Filesystem paths to Widevine device files, not the key material itself.
	"apple_wvd_path":  "path, not a credential",
	"amazon_wvd_path": "path, not a credential",
}

var (
	// ak_/pk_ + 40 lowercase hex. Kept at {16,} so a format change still
	// matches. This pass is the one that matters most: it catches the SHARED
	// manifest key, which arrives from a remote gist and is therefore absent
	// from config — exact-value redaction can never see it.
	reLegacyKey = regexp.MustCompile(`\b[ap]k_[0-9a-fA-F]{16,}\b`)
	// at_<base64url>.<base64url>, unpadded (device_tokens.py rstrips "=").
	reDeviceToken = regexp.MustCompile(`\bat_[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}`)
)

type secretValue struct {
	label string
	value string
}

// configSecrets returns the credential values actually present in cfg, longest
// first so that a secret containing another as a substring is replaced whole.
func configSecrets(cfg Config) []secretValue {
	raw, err := json.Marshal(cfg)
	if err != nil {
		return nil
	}
	var flat map[string]interface{}
	if json.Unmarshal(raw, &flat) != nil {
		return nil
	}

	seen := make(map[string]bool)
	out := make([]secretValue, 0, len(logRedactFields))
	for tag, label := range logRedactFields {
		s, _ := flat[tag].(string)
		s = strings.TrimSpace(s)
		if len([]rune(s)) < logSecretMinLen || seen[s] {
			continue
		}
		seen[s] = true
		out = append(out, secretValue{label: label, value: s})
	}
	sort.Slice(out, func(i, j int) bool {
		if len(out[i].value) != len(out[j].value) {
			return len(out[i].value) > len(out[j].value)
		}
		return out[i].label < out[j].label
	})
	return out
}

// redactSecrets strips credentials from text and reports how many it removed.
func redactSecrets(text string, cfg Config) (string, int) {
	count := 0

	for _, s := range configSecrets(cfg) {
		if n := strings.Count(text, s.value); n > 0 {
			text = strings.ReplaceAll(text, s.value, "[REDACTED:"+s.label+"]")
			count += n
		}
	}

	for _, re := range []*regexp.Regexp{reLegacyKey, reDeviceToken} {
		text = re.ReplaceAllStringFunc(text, func(string) string {
			count++
			return "[REDACTED:api-key]"
		})
	}

	return text, count
}

// ── Header ──────────────────────────────────────────────────────────────────

func boolLabel(v bool) string {
	if v {
		return "on"
	}
	return "off"
}

// boolPtrLabel resolves the *bool settings, which are pointers precisely so an
// absent key can be told from a deliberate false — see the Config comments.
func boolPtrLabel(p *bool, dflt bool) string {
	if p != nil {
		return boolLabel(*p)
	}
	return boolLabel(dflt)
}

func orNotSet(s, fallback string) string {
	if strings.TrimSpace(s) == "" {
		return fallback
	}
	return s
}

// accountLabel reports only PRESENCE and kind — never any part of the value.
// It is deliberately offline: a log save must work with no network, and
// validating the key here would make saving a log slow and fail when a mirror
// is down, which is exactly when someone needs the log.
func accountLabel(cfg Config) string {
	var parts []string
	if strings.TrimSpace(cfg.AntraDeviceToken) != "" {
		parts = append(parts, "device token (signed in)")
	}
	if strings.TrimSpace(cfg.AntraApiKey) != "" {
		parts = append(parts, "pasted supporter key")
	}
	if len(parts) == 0 {
		return "none (free tier)"
	}
	return strings.Join(parts, " + ")
}

func sourcesLabel(cfg Config) string {
	if len(cfg.DownloadSources) > 0 {
		return strings.Join(cfg.DownloadSources, ", ")
	}
	return orNotSet(cfg.DownloadSource, "auto (full chain)")
}

func buildLogHeader(cfg Config, lineCount int, now time.Time) string {
	row := func(k, v string) string { return fmt.Sprintf("  %-18s: %s\n", k, v) }
	rule := strings.Repeat("=", 72)

	var b strings.Builder
	b.WriteString(rule + "\n")
	b.WriteString("  Antra activity log\n")
	b.WriteString(rule + "\n")
	b.WriteString(row("Saved", now.Format("2006-01-02 15:04:05 -0700")+
		"  ("+now.UTC().Format("2006-01-02T15:04:05Z")+")"))
	b.WriteString(row("Antra version", appVersion()))
	b.WriteString(row("Platform", runtime.GOOS+"/"+runtime.GOARCH))
	b.WriteString(row("Output format", orNotSet(cfg.OutputFormat, "(unset - backend default)")))
	b.WriteString(row("Download sources", sourcesLabel(cfg)))
	if len(cfg.SourcesEnabled) > 0 {
		b.WriteString(row("Source groups", strings.Join(cfg.SourcesEnabled, ", ")))
	}
	b.WriteString(row("Library mode", orNotSet(cfg.LibraryMode, "smart_dedup")))
	// The three resolver gates change which sources are even eligible, so a
	// "why did this fall through to X" report is unreadable without them.
	b.WriteString(row("Strict matching", boolLabel(cfg.StrictMatching)))
	b.WriteString(row("Strict format", boolPtrLabel(cfg.StrictFormat, true)))
	b.WriteString(row("No lossy re-encode", boolPtrLabel(cfg.PreventLossyTranscode, true)))
	// Kept verbatim rather than shortened to "~": path LENGTH is itself the
	// diagnostic in the Windows MAX_PATH failures this project has hit, and a
	// username is not a credential.
	b.WriteString(row("Library folder", orNotSet(cfg.DownloadPath, "(unset)")))
	b.WriteString(row("Account", accountLabel(cfg)))
	b.WriteString(row("Log lines", fmt.Sprintf("%d", lineCount)))
	b.WriteString(rule + "\n\n")
	return b.String()
}

// ── Public binding ──────────────────────────────────────────────────────────

// composeLogFile builds the exact bytes that get written, so everything except
// the native dialog and the os.WriteFile call is reachable from a test.
//
// Redaction runs AFTER the header is prepended, not before: the header is built
// to contain no secret, but a later edit that added one would then still be
// covered rather than silently bypassing the one part of this feature the spec
// calls non-optional.
func composeLogFile(cfg Config, logText string, lineCount int, now time.Time) (string, int) {
	return redactSecrets(buildLogHeader(cfg, lineCount, now)+logText+"\n", cfg)
}

// normalizeLogTargetPath appends .txt when the user cleared the extension in
// the save dialog. A dotted name (Antra 2.0 run) keeps its own extension rather
// than being rewritten, so only a genuinely extensionless path is touched.
func normalizeLogTargetPath(target string) string {
	if filepath.Ext(target) == "" {
		return target + ".txt"
	}
	return target
}

// SaveActivityLog prompts for a path and writes the activity log there.
// Returns {"saved":true,...} | {"cancelled":true} | {"error":"..."}.
func (a *App) SaveActivityLog(logText string, lineCount int) string {
	if strings.TrimSpace(logText) == "" {
		return jsonError("The activity log is empty - there is nothing to save yet.")
	}

	now := time.Now()
	cfg := a.GetConfig()
	body, redacted := composeLogFile(cfg, logText, lineCount, now)

	target, dlgErr := wailsRuntime.SaveFileDialog(a.ctx, wailsRuntime.SaveDialogOptions{
		Title:                "Save activity log",
		DefaultFilename:      "antra-log-" + now.Format("20060102-150405") + ".txt",
		CanCreateDirectories: true,
		Filters: []wailsRuntime.FileFilter{
			{DisplayName: "Text file (*.txt)", Pattern: "*.txt"},
		},
	})
	if dlgErr != nil {
		return jsonError(dlgErr.Error())
	}
	if strings.TrimSpace(target) == "" {
		return `{"cancelled":true}`
	}
	target = normalizeLogTargetPath(target)

	// 0600, not 0644: the whole point of this file is that it may still hold
	// something sensitive we failed to recognise.
	if err := os.WriteFile(target, []byte(body), 0o600); err != nil {
		return jsonError(err.Error())
	}

	out, _ := json.Marshal(map[string]interface{}{
		"saved":    true,
		"path":     target,
		"bytes":    len(body),
		"redacted": redacted,
	})
	return string(out)
}

func jsonError(msg string) string {
	out, _ := json.Marshal(map[string]string{"error": msg})
	return string(out)
}

// GetAppVersion exposes the embedded wails.json version to the UI, so the
// Settings footer cannot drift from the real release again.
func (a *App) GetAppVersion() string { return appVersion() }
