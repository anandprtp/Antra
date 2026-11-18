package main

import (
	"encoding/base64"
	"encoding/json"
	"strings"
	"testing"
	"time"
)

func makeToken(exp int64) string {
	payload, _ := json.Marshal(map[string]any{"v": 1, "uid": 1, "usr": "u", "exp": exp})
	body := strings.TrimRight(base64.URLEncoding.EncodeToString(payload), "=")
	return "at_" + body + ".signature-not-checked-locally"
}

func TestDeviceTokenExpiry(t *testing.T) {
	want := time.Now().Add(20 * 24 * time.Hour).Unix()
	got, ok := deviceTokenExpiry(makeToken(want))
	if !ok || got != want {
		t.Fatalf("expiry: got %d ok=%v, want %d", got, ok, want)
	}
}

func TestDeviceTokenExpiryRejectsJunk(t *testing.T) {
	// Anything we cannot read must report "not ok" rather than a zero expiry
	// that would look like a token which expired in 1970 and trigger a
	// pointless network call on every launch.
	cases := map[string]string{
		"legacy key":       "ak_1234567890",
		"premium key":      "pk_1234567890",
		"empty":            "",
		"prefix only":      "at_",
		"no dot":           "at_abcdef",
		"bad base64":       "at_!!!!.sig",
		"not json":         "at_" + strings.TrimRight(base64.URLEncoding.EncodeToString([]byte("hello")), "=") + ".sig",
		"json without exp": "at_" + strings.TrimRight(base64.URLEncoding.EncodeToString([]byte(`{"uid":1}`)), "=") + ".sig",
	}
	for name, tok := range cases {
		if _, ok := deviceTokenExpiry(tok); ok {
			t.Errorf("%s: expected not-ok, got ok", name)
		}
	}
}

func TestRenewSkipsNonDeviceKeys(t *testing.T) {
	// A user on a legacy shared key must never be told to "sign in again".
	for _, tok := range []string{"ak_shared", "pk_premium", ""} {
		if _, ok := deviceTokenExpiry(tok); ok {
			t.Errorf("%q should not parse as a device token", tok)
		}
	}
}

func TestPaddingVariants(t *testing.T) {
	// base64url payloads land on every padding remainder depending on length;
	// all must decode.
	for i := 0; i < 6; i++ {
		exp := time.Now().Add(time.Duration(24*(i+1)) * time.Hour).Unix()
		payload, _ := json.Marshal(map[string]any{"exp": exp, "pad": strings.Repeat("x", i)})
		body := strings.TrimRight(base64.URLEncoding.EncodeToString(payload), "=")
		got, ok := deviceTokenExpiry("at_" + body + ".sig")
		if !ok || got != exp {
			t.Errorf("padding %d: got %d ok=%v want %d", i, got, ok, exp)
		}
	}
}

// ── Credential separation (v1.1.8 — device login demoted supporters) ─────────
//
// The bug these cover: the device token was written into AntraApiKey, which the
// Settings supporter-key box binds to. Signing in therefore destroyed the pasted
// key AND failed validation (a token is not a keys.json entry), so isSupporter
// went false and the user silently dropped to 1 worker with no FEAT-7 discount.

func TestStoredDeviceTokenPrefersOwnField(t *testing.T) {
	tok := makeToken(time.Now().Add(20 * 24 * time.Hour).Unix())
	cases := []struct {
		name string
		cfg  Config
		want string
	}{
		{"own field", Config{AntraDeviceToken: tok, AntraApiKey: "ak_supporter"}, tok},
		{"legacy field only", Config{AntraApiKey: tok}, tok},
		{"supporter key is not a token", Config{AntraApiKey: "ak_supporter"}, ""},
		{"nothing", Config{}, ""},
		{"both, own field wins", Config{AntraDeviceToken: tok, AntraApiKey: "at_other.sig"}, tok},
	}
	for _, c := range cases {
		if got := storedDeviceToken(c.cfg); got != c.want {
			t.Errorf("%s: got %q want %q", c.name, got, c.want)
		}
	}
}

func TestDeviceTierIsSupporter(t *testing.T) {
	for _, tier := range []string{"supporter", "premium", "admin", "PREMIUM", " Supporter "} {
		if !deviceTierIsSupporter(tier) {
			t.Errorf("%q should be supporter", tier)
		}
	}
	for _, tier := range []string{"", "free", "regular", "unknown"} {
		if deviceTierIsSupporter(tier) {
			t.Errorf("%q must NOT be supporter", tier)
		}
	}
}

func TestChooseCredential(t *testing.T) {
	const tok = "at_body.sig"
	const key = "ak_supporter"
	okKey := KeyInfoResult{Valid: true, IsSupporter: true, Reachable: true}
	badKey := KeyInfoResult{Valid: false, Reachable: true}
	unreachKey := KeyInfoResult{Valid: false, Reachable: false}

	supTok := DeviceAccountStatus{SignedIn: true, Valid: true, Reachable: true, IsSupporter: true, Tier: "premium"}
	freeTok := DeviceAccountStatus{SignedIn: true, Valid: true, Reachable: true, Tier: "free"}
	deadTok := DeviceAccountStatus{SignedIn: true, Valid: false, Reachable: true}
	unreachTok := DeviceAccountStatus{SignedIn: true, Reachable: false}

	cases := []struct {
		name          string
		token         string
		status        DeviceAccountStatus
		personal      string
		keyInfo       KeyInfoResult
		wantSupporter bool
		wantKey       string
		wantManifest  bool
	}{
		// THE REPORTED BUG: signed in, and a valid supporter key pasted. Before
		// the fix this returned (false, "") — 1 worker, Atmos locked.
		{"supporter token + supporter key", tok, supTok, key, okKey, true, tok, false},
		{"supporter key only", "", DeviceAccountStatus{}, key, okKey, true, key, false},
		{"supporter token only", tok, supTok, "", KeyInfoResult{}, true, tok, false},

		// A free website account must not cost someone their key's tier.
		{"free token + supporter key -> key wins", tok, freeTok, key, okKey, true, key, false},
		{"free token alone -> per-user auth at free tier", tok, freeTok, "", KeyInfoResult{}, false, tok, false},

		// A refused token must not authenticate anything; an unreachable server
		// must not sign anyone out.
		{"revoked token + supporter key", tok, deadTok, key, okKey, true, key, false},
		{"revoked token alone -> manifest check", tok, deadTok, "", KeyInfoResult{}, false, "", true},
		{"unreachable token kept, no tier claimed", tok, unreachTok, "", KeyInfoResult{}, false, tok, false},
		{"unreachable token + supporter key", tok, unreachTok, key, okKey, true, key, false},

		// Regression guards on the pre-existing behaviour.
		{"nothing configured -> manifest check", "", DeviceAccountStatus{}, "", KeyInfoResult{}, false, "", true},
		{"bad key -> no mirror key", "", DeviceAccountStatus{}, "ak_bogus", badKey, false, "", false},
		{"unverifiable key -> no mirror key", "", DeviceAccountStatus{}, key, unreachKey, false, "", false},
	}

	for _, c := range cases {
		sup, mk, manifest := chooseCredential(c.token, c.status, c.personal, c.keyInfo)
		if sup != c.wantSupporter || mk != c.wantKey || manifest != c.wantManifest {
			t.Errorf("%s: got (%v, %q, manifest=%v) want (%v, %q, manifest=%v)",
				c.name, sup, mk, manifest, c.wantSupporter, c.wantKey, c.wantManifest)
		}
	}
}

// ── Phase A: endpoints come from the account, not the public gist ────────────

// Hosts here must be real-shaped: F2 added an allowlist, so a placeholder
// like tidal.example is now dropped on purpose and would fail this plumbing
// test for an unrelated reason. Host policy itself is covered by
// TestIsTrustedAntraHost.
func TestEndpointEnvFrom(t *testing.T) {
	full := endpointManifest{Mirrors: endpointManifestMirrors{
		Tidal: "https://tidal.anandserver.cfd/", Qobuz: "https://qobuz.anandserver.cfd",
		Deezer: "https://deezer.anandserver.cfd", Amazon: "https://amazon.anandserver.cfd",
		Apple: "https://apple.anandserver.cfd",
	}}
	env := endpointEnvFrom(full)
	want := map[string]string{
		"TIDAL_MIRROR_URL":  "https://tidal.anandserver.cfd", // trailing slash trimmed
		"QOBUZ_MIRROR_URL":  "https://qobuz.anandserver.cfd",
		"DEEZER_MIRROR_URL": "https://deezer.anandserver.cfd",
		"APPLE_MIRRORS":     "https://apple.anandserver.cfd",
		"AMAZON_MIRRORS":    "https://amazon.anandserver.cfd",
	}
	got := map[string]string{}
	for _, kv := range env {
		k, v, _ := strings.Cut(kv, "=")
		got[k] = v
	}
	for k, v := range want {
		if got[k] != v {
			t.Errorf("%s: got %q want %q", k, got[k], v)
		}
	}
	// The backend must be told it needs no manifest, or it falls back to the
	// public gist — which is the whole thing being retired.
	if got["ANTRA_ENDPOINT_MANIFEST_DISABLED"] != "1" {
		t.Error("manifest fetch was not disabled for the backend")
	}
	// The per-user DEVICE TOKEN must never travel this way -- it goes as
	// ANTRA_MIRROR_API_KEY and nowhere else. The shared MANIFEST key is the
	// deliberate exception: with ANTRA_ENDPOINT_MANIFEST_DISABLED set, Python's
	// only other source for it is the on-disk cache, and when that was
	// unreadable it sent no X-API-Key at all and every Tidal/Qobuz metadata call
	// returned `401 API key required`.
	for k, v := range got {
		if k == "ANTRA_MANIFEST_API_KEY" {
			continue
		}
		if strings.HasPrefix(v, "ak_") || strings.HasPrefix(v, "at_") || strings.HasPrefix(v, "pk_") {
			t.Errorf("%s leaked a credential", k)
		}
	}

	// The manifest key must actually be handed over when there is one.
	withKey := endpointEnvFrom(endpointManifest{
		Mirrors: endpointManifestMirrors{Tidal: "https://tidal.anandserver.cfd"},
		ApiKey:  "ak_shared_example",
	})
	if !contains(withKey, "ANTRA_MANIFEST_API_KEY=ak_shared_example") {
		t.Errorf("manifest key was not exported: %v", withKey)
	}

	// ...and a key WITHOUT any URL must not make the backend believe it was
	// handed endpoints, or it would skip the manifest having resolved nothing.
	keyOnly := endpointEnvFrom(endpointManifest{ApiKey: "ak_shared_example"})
	if contains(keyOnly, "ANTRA_ENDPOINT_MANIFEST_DISABLED=1") {
		t.Errorf("disabled the manifest on a key-only payload: %v", keyOnly)
	}

	// Partial manifest: absent entries are omitted, not exported blank — a blank
	// would override a URL the backend could otherwise resolve for itself.
	partial := endpointEnvFrom(endpointManifest{Mirrors: endpointManifestMirrors{Tidal: "https://tidal.anandserver.cfd"}})
	joined := strings.Join(partial, " ")
	if !strings.Contains(joined, "TIDAL_MIRROR_URL=https://tidal.anandserver.cfd") {
		t.Error("partial manifest lost the one URL it had")
	}
	for _, absent := range []string{"QOBUZ_MIRROR_URL", "APPLE_MIRRORS", "AMAZON_MIRRORS", "DEEZER_MIRROR_URL"} {
		if strings.Contains(joined, absent) {
			t.Errorf("%s exported despite being empty", absent)
		}
	}

	// Empty manifest: export nothing at all, INCLUDING the disable flag —
	// otherwise the backend would be told "you have endpoints" when it has none
	// and would have no way to find any.
	if env := endpointEnvFrom(endpointManifest{}); len(env) != 0 {
		t.Errorf("empty manifest exported %v", env)
	}
}


func contains(env []string, want string) bool {
	for _, kv := range env {
		if kv == want {
			return true
		}
	}
	return false
}
