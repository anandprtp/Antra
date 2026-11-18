package main

// Tests for FR-4 (v2.0.0) — activity-log export.
//
// The save dialog itself cannot be driven headlessly, so everything either
// side of it is tested directly: the header builder, the redaction pass and
// the path normaliser. Assertions are on the bytes that would be written, not
// on source text.

import (
	"encoding/json"
	"os"
	"path/filepath"
	"reflect"
	"regexp"
	"strings"
	"testing"
	"time"
)

// Key-SHAPED fixtures for the redaction test — none of these is a real
// credential. They match the formats device_tokens.py / kofi_webhook.py mint
// (ak_ + 40 hex, at_<b64url>.<b64url>), which is all the redactor keys off.
//
// fakeSharedKey used to hold the REAL value of the public `antra-shared` gist
// key under a comment claiming it was fake. That was harmless (the key is
// published on purpose — see CLAUDE.md) but actively misleading: it invites
// someone to "rotate the leaked key", which takes every non-supporter user
// offline. Keep this a random value.
const (
	fakeSharedKey   = "ak_3d71b8e04f2a6c95d1e87b3f0a4c62d9e5178b0c"
	fakePremiumKey  = "pk_1cc4d0e6f8e3f1a7c2b5d9e4f0a6c3b8d1e5f2a9"
	fakeDeviceToken = "at_eyJ1aWQiOjQyLCJ0aWVyIjoic3VwcG9ydGVyIn0.c3VwZXJzZWNyZXRzaWduYXR1cmVoZXJl"
)

func fullySeededConfig() Config {
	tr := true
	return Config{
		DownloadPath:            `D:\Music`,
		OutputFormat:            "lossless-24",
		LibraryMode:             "smart_dedup",
		StrictMatching:          true,
		StrictFormat:            &tr,
		PreventLossyTranscode:   &tr,
		DownloadSources:         []string{"tidal", "qobuz"},
		SourcesEnabled:          []string{"hifi", "soulseek"},
		AppleAuthorizationToken: "apple-authorization-token-value-000000",
		AppleMusicUserToken:     "apple-music-user-token-value-11111111",
		AmazonDirectCredsJSON:   `{"amazon":"direct-creds-json-value-2222"}`,
		QobuzPassword:           "a-long-enough-qobuz-password",
		QobuzAppSecret:          "qobuzappsecret0123456789abcdef01",
		QobuzUserAuthToken:      "qobuz-user-auth-token-value-333333",
		DeezerARLToken:          strings.Repeat("d", 192),
		DeezerBFSecret:          "deezer-bf-secret-value-4444444",
		SoulseekPassword:        "soulseek-password-value-5555",
		SpotifySpDc:             "AQ" + strings.Repeat("s", 150),
		TidalSessionJSON:        `{"tidal":"session-json-value-666666"}`,
		TidalAccessToken:        "tidal-access-token-value-7777777",
		TidalRefreshToken:       "tidal-refresh-token-value-8888888",
		TidalSessionID:          "9f8e7d6c-5b4a-3210-9876-543210fedcba",
		TidalTokenType:          "Bearer",
		QobuzAppID:              "798273057",
		AntraApiKey:             fakePremiumKey,
		AntraDeviceToken:        fakeDeviceToken,
	}
}

// ── The version must not be able to drift ───────────────────────────────────

func TestAppVersionMatchesPythonPackage(t *testing.T) {
	got := appVersion()
	if got == "" || got == "unknown" {
		t.Fatalf("appVersion() did not resolve from the embedded wails.json: %q", got)
	}

	raw, err := os.ReadFile(filepath.Join("..", "antra", "__init__.py"))
	if err != nil {
		t.Fatalf("could not read antra/__init__.py: %v", err)
	}
	m := regexp.MustCompile(`__version__\s*=\s*"([^"]+)"`).FindSubmatch(raw)
	if m == nil {
		t.Fatalf("no __version__ in antra/__init__.py")
	}
	if want := string(m[1]); got != want {
		t.Fatalf("version drift: wails.json productVersion=%q but antra/__init__.py __version__=%q. "+
			"A stale version in the log header misreports the build to whoever reads the bug report.", got, want)
	}
}

// ── A new credential in Config must force a decision ────────────────────────

func TestEveryCredentialFieldIsClassified(t *testing.T) {
	credentialish := regexp.MustCompile(`token|password|secret|_key|arl|sp_dc|creds|session`)

	typ := reflect.TypeOf(Config{})
	checked := 0
	for i := 0; i < typ.NumField(); i++ {
		tag := strings.Split(typ.Field(i).Tag.Get("json"), ",")[0]
		if tag == "" || tag == "-" {
			continue
		}
		if !credentialish.MatchString(tag) {
			continue
		}
		checked++
		_, redacted := logRedactFields[tag]
		_, exempt := logRedactExempt[tag]
		if redacted && exempt {
			t.Errorf("%q is in BOTH logRedactFields and logRedactExempt", tag)
		}
		if !redacted && !exempt {
			t.Errorf("Config field %q looks like a credential but is neither redacted nor "+
				"explicitly exempt. Add it to logRedactFields, or to logRedactExempt with a reason.", tag)
		}
	}
	// Guard against the pattern silently matching nothing — a test that checks
	// zero fields would pass forever while proving nothing.
	if checked < 10 {
		t.Fatalf("only %d credential-shaped fields matched; the pattern is probably broken", checked)
	}
}

func TestRedactFieldsAllExistInConfig(t *testing.T) {
	tags := map[string]bool{}
	typ := reflect.TypeOf(Config{})
	for i := 0; i < typ.NumField(); i++ {
		tags[strings.Split(typ.Field(i).Tag.Get("json"), ",")[0]] = true
	}
	for tag := range logRedactFields {
		if !tags[tag] {
			t.Errorf("logRedactFields names %q, which is not a Config field — it can never match", tag)
		}
	}
	for tag := range logRedactExempt {
		if !tags[tag] {
			t.Errorf("logRedactExempt names %q, which is not a Config field", tag)
		}
	}
}

// ── Redaction ───────────────────────────────────────────────────────────────

// assertGone fails when a secret survived, and ALSO fails when the fixture
// never contained it — a redaction test that passes because the input was
// empty proves nothing.
func assertGone(t *testing.T, label, in, out, secret string) {
	t.Helper()
	if !strings.Contains(in, secret) {
		t.Fatalf("%s: fixture bug — the input never contained the secret", label)
	}
	if strings.Contains(out, secret) {
		t.Errorf("%s: secret survived redaction", label)
	}
}

func TestRedactsSharedManifestKeyNotInConfig(t *testing.T) {
	// The load-bearing case. This key comes from a remote gist and is in NOBODY's
	// config, so only the pattern pass can catch it.
	cfg := Config{}
	in := "[Manifest] using key " + fakeSharedKey + " for tidal.anandserver.cfd\n"
	out, n := redactSecrets(in, cfg)
	assertGone(t, "shared manifest key", in, out, fakeSharedKey)
	if n != 1 {
		t.Errorf("expected 1 redaction, got %d", n)
	}
	if !strings.Contains(out, "[REDACTED:api-key]") {
		t.Errorf("no redaction marker in output: %q", out)
	}
	// The surrounding line must survive so the log stays readable.
	if !strings.Contains(out, "tidal.anandserver.cfd") {
		t.Errorf("redaction ate the surrounding line: %q", out)
	}
}

func TestRedactsDeviceTokenByPattern(t *testing.T) {
	in := "GET /api/device/status Authorization: Bearer " + fakeDeviceToken + "\n"
	out, n := redactSecrets(in, Config{})
	assertGone(t, "device token", in, out, fakeDeviceToken)
	if n == 0 {
		t.Errorf("device token was not counted as a redaction")
	}
	if !strings.Contains(out, "Bearer") {
		t.Errorf("the word Bearer should survive: %q", out)
	}
}

func TestRedactsEveryConfiguredCredential(t *testing.T) {
	cfg := fullySeededConfig()
	secrets := configSecrets(cfg)
	if len(secrets) < 15 {
		t.Fatalf("expected the seeded config to yield most credentials, got %d", len(secrets))
	}

	var b strings.Builder
	for _, s := range secrets {
		b.WriteString("line mentioning " + s.value + " somewhere\n")
	}
	in := b.String()
	out, n := redactSecrets(in, cfg)

	for _, s := range secrets {
		assertGone(t, s.label, in, out, s.value)
	}
	if n < len(secrets) {
		t.Errorf("counted %d redactions for %d secrets", n, len(secrets))
	}
}

func TestShortPasswordIsNotGloballyReplaced(t *testing.T) {
	// A short password that happens to be a common word must NOT be blanked out
	// of every line — that would destroy the log's diagnostic value, and
	// passwords are never written to the log in the first place.
	cfg := Config{QobuzPassword: "download"}
	in := "Starting download of 12 tracks; download queue is ready\n"
	out, n := redactSecrets(in, cfg)
	if n != 0 {
		t.Errorf("short password should not have been redacted, count=%d", n)
	}
	if out != in {
		t.Errorf("log text was mangled by a short password:\n got: %q\nwant: %q", out, in)
	}
}

func TestLongPasswordIsRedacted(t *testing.T) {
	cfg := Config{QobuzPassword: "correct-horse-battery-staple"}
	in := "auth failed for correct-horse-battery-staple\n"
	out, _ := redactSecrets(in, cfg)
	assertGone(t, "long password", in, out, cfg.QobuzPassword)
}

func TestOverlappingSecretsReplaceLongestFirst(t *testing.T) {
	// A shorter secret that is a substring of a longer one must not chop the
	// longer one into a half-redacted fragment.
	//
	// The fields are chosen adversarially: the SHORT value sits in the field
	// whose label sorts FIRST alphabetically, so only the length ordering can
	// produce the correct result. An earlier version of this test put the long
	// value in the alphabetically-first field and therefore passed even with
	// the length comparison removed — the negative control caught it.
	long := "supersecrettokenvalue-with-suffix"
	short := "supersecrettokenvalue"
	cfg := Config{TidalAccessToken: long, DeezerBFSecret: short}
	in := "token=" + long + "\n"
	out, _ := redactSecrets(in, cfg)
	assertGone(t, "overlapping", in, out, short)
	if strings.Contains(out, "-with-suffix") {
		t.Errorf("the longer secret was only partly redacted: %q", out)
	}
}

func TestCleanLogIsLeftByteIdentical(t *testing.T) {
	cfg := fullySeededConfig()
	in := "[12:00:01] INFO      Resolving 18 tracks\n[12:00:04] SUCCESS   Added to library: Foo - Bar\n"
	out, n := redactSecrets(in, cfg)
	if n != 0 || out != in {
		t.Errorf("a log with no credentials must pass through untouched (count=%d)\n got: %q", n, out)
	}
}

func TestRedactionMarkerIsNotItselfRedacted(t *testing.T) {
	// The replacement text must not match either pattern, or a second pass
	// would corrupt it.
	out, _ := redactSecrets("[REDACTED:antra_api_key] [REDACTED:api-key]", Config{})
	if !strings.Contains(out, "[REDACTED:antra_api_key]") || !strings.Contains(out, "[REDACTED:api-key]") {
		t.Errorf("markers were altered: %q", out)
	}
}

// ── Header ──────────────────────────────────────────────────────────────────

func TestHeaderCarriesTheContextABugReportNeeds(t *testing.T) {
	cfg := fullySeededConfig()
	h := buildLogHeader(cfg, 412, time.Date(2026, 9, 10, 14, 23, 5, 0, time.UTC))

	for _, want := range []string{
		"Antra activity log",
		appVersion(),
		"lossless-24",
		"tidal, qobuz",
		"hifi, soulseek",
		"smart_dedup",
		`D:\Music`,
		"2026-09-10",
		"412",
	} {
		if !strings.Contains(h, want) {
			t.Errorf("header is missing %q:\n%s", want, h)
		}
	}
	if !strings.Contains(h, "Platform") {
		t.Errorf("header has no platform row")
	}
}

func TestHeaderNeverEchoesACredential(t *testing.T) {
	cfg := fullySeededConfig()
	h := buildLogHeader(cfg, 1, time.Now())
	for _, s := range configSecrets(cfg) {
		if strings.Contains(h, s.value) {
			t.Errorf("header leaked %s", s.label)
		}
	}
	// It must still say whether an account exists — presence is the useful bit.
	if !strings.Contains(h, "device token") || !strings.Contains(h, "supporter key") {
		t.Errorf("header should report account presence:\n%s", h)
	}
}

func TestAccountLabelStates(t *testing.T) {
	cases := []struct {
		name string
		cfg  Config
		want string
	}{
		{"none", Config{}, "none (free tier)"},
		{"key only", Config{AntraApiKey: fakePremiumKey}, "pasted supporter key"},
		{"token only", Config{AntraDeviceToken: fakeDeviceToken}, "device token (signed in)"},
		{"both", Config{AntraApiKey: fakePremiumKey, AntraDeviceToken: fakeDeviceToken}, "+"},
	}
	for _, c := range cases {
		if got := accountLabel(c.cfg); !strings.Contains(got, c.want) {
			t.Errorf("%s: got %q, want it to contain %q", c.name, got, c.want)
		}
	}
}

func TestBoolPtrLabelHonoursTheDefaultWhenUnset(t *testing.T) {
	// The *bool settings default to TRUE; an absent key must read "on", not
	// "off" — the whole reason those fields are pointers.
	if got := boolPtrLabel(nil, true); got != "on" {
		t.Errorf("unset *bool with default true: got %q", got)
	}
	f := false
	if got := boolPtrLabel(&f, true); got != "off" {
		t.Errorf("explicit false: got %q", got)
	}
}

// ── Path handling ───────────────────────────────────────────────────────────

func TestNormalizeLogTargetPath(t *testing.T) {
	cases := map[string]string{
		filepath.Join("a", "b", "antra-log"):     filepath.Join("a", "b", "antra-log") + ".txt",
		filepath.Join("a", "b", "antra-log.txt"): filepath.Join("a", "b", "antra-log.txt"),
		filepath.Join("a", "b", "Antra 2.0 run"): filepath.Join("a", "b", "Antra 2.0 run"),
	}
	for in, want := range cases {
		if got := normalizeLogTargetPath(in); got != want {
			t.Errorf("normalizeLogTargetPath(%q) = %q, want %q", in, got, want)
		}
	}
}

// ── The bytes that actually reach disk ──────────────────────────────────────

// Everything SaveActivityLog does except the native dialog and the write. The
// dialog cannot be driven headlessly, so this is the closest reachable point.
func TestComposeLogFileProducesASafeReadableFile(t *testing.T) {
	cfg := fullySeededConfig()
	// A realistic export: the shared gist key (absent from cfg), the user's own
	// pasted key (present in cfg), and ordinary lines that must survive intact.
	logText := strings.Join([]string{
		"[14:03:05] INFO      Building your music library",
		"[14:03:06] INFO      Manifest key " + fakeSharedKey + " accepted",
		"[14:03:07] INFO      Supporter key " + fakePremiumKey + " validated",
		"[14:03:09] SUCCESS   Added to library: Drake - Ran To Atlanta",
		"[14:04:12] ERROR     FAIL Blinding Lights - no source could find this track",
	}, "\n")

	body, redacted := composeLogFile(cfg, logText, 5, time.Date(2026, 9, 10, 14, 5, 0, 0, time.UTC))

	// Nothing sensitive.
	for _, secret := range []string{fakeSharedKey, fakePremiumKey, fakeDeviceToken} {
		if !strings.Contains(logText+cfg.AntraDeviceToken, secret) {
			t.Fatalf("fixture bug: %q never appeared in the input", secret)
		}
		if strings.Contains(body, secret) {
			t.Errorf("composed file leaked %q", secret)
		}
	}
	for _, s := range configSecrets(cfg) {
		if strings.Contains(body, s.value) {
			t.Errorf("composed file leaked %s", s.label)
		}
	}
	if redacted < 2 {
		t.Errorf("expected at least the two keys to be redacted, got %d", redacted)
	}

	// Still readable: header first, then the log, and the non-secret parts of
	// every line survive.
	if !strings.HasPrefix(body, strings.Repeat("=", 72)) {
		t.Errorf("file does not start with the header rule")
	}
	if strings.Index(body, "Antra activity log") > strings.Index(body, "Building your music library") {
		t.Errorf("header must come before the log body")
	}
	for _, want := range []string{
		"Building your music library",
		"Added to library: Drake - Ran To Atlanta",
		"FAIL Blinding Lights - no source could find this track",
		"Manifest key [REDACTED:api-key] accepted",
		"lossless-24",
	} {
		if !strings.Contains(body, want) {
			t.Errorf("composed file is missing %q", want)
		}
	}
	if !strings.HasSuffix(body, "\n") {
		t.Errorf("file should end with a newline")
	}
}

func TestComposedFileRoundTripsThroughDisk(t *testing.T) {
	cfg := Config{OutputFormat: "flac", AntraApiKey: fakePremiumKey}
	body, _ := composeLogFile(cfg, "[00:00:01] INFO      hello "+fakePremiumKey, 1, time.Now())

	target := normalizeLogTargetPath(filepath.Join(t.TempDir(), "antra-log"))
	if filepath.Ext(target) != ".txt" {
		t.Fatalf("extension was not appended: %q", target)
	}
	if err := os.WriteFile(target, []byte(body), 0o600); err != nil {
		t.Fatalf("write failed: %v", err)
	}
	read, err := os.ReadFile(target)
	if err != nil {
		t.Fatalf("read back failed: %v", err)
	}
	if string(read) != body {
		t.Errorf("file on disk differs from the composed body")
	}
	if strings.Contains(string(read), fakePremiumKey) {
		t.Errorf("the key reached disk")
	}
}

// ── The empty-log guard must not open a dialog ──────────────────────────────

func TestSaveActivityLogRefusesEmptyLogWithoutTouchingTheDialog(t *testing.T) {
	// a.ctx is nil here, so any call into the Wails runtime would panic. The
	// test passing at all is the proof that the guard returns first.
	app := &App{}
	out := app.SaveActivityLog("   \n  ", 0)
	var parsed map[string]interface{}
	if err := json.Unmarshal([]byte(out), &parsed); err != nil {
		t.Fatalf("guard did not return JSON: %q", out)
	}
	if _, ok := parsed["error"]; !ok {
		t.Errorf("expected an error for an empty log, got %q", out)
	}
}
