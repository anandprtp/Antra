package main

import "testing"

// F2 — the device token travels in X-API-Key to hosts the PUBLIC gist manifest
// supplies. isTrustedAntraHost is the allowlist that decides who may receive
// it, so these cases are the security boundary, not cosmetics.

func TestIsTrustedAntraHost(t *testing.T) {
	trusted := []string{
		"https://antra.hoshi.cfd",
		"https://tidal.anandserver.cfd",
		"https://qobuz.anandserver.cfd",
		"https://deezer.anandserver.cfd",
		"https://amazon.anandserver.cfd",
		"https://apple.anandserver.cfd",
		"https://apple.anandserver.cfd/",
		"  https://tidal.anandserver.cfd  ", // manifests carry stray whitespace
		"https://TIDAL.ANANDSERVER.CFD",     // hosts are case-insensitive
		"https://tidal.anandserver.cfd:8443/api",
		// Go parses userinfo off the LAST "@", so the connection really does
		// go to the trusted host. Pinned so nobody "hardens" it into a refusal.
		"https://user:pw@evil.com@tidal.anandserver.cfd",
	}
	for _, u := range trusted {
		if !isTrustedAntraHost(u) {
			t.Errorf("must be trusted, was refused: %q", u)
		}
	}

	refused := map[string]string{
		"https://evil.example.com":         "an unrelated host",
		"https://anandserver.cfd.evil.com": "suffix smuggled into another domain",
		"https://evil.com/anandserver.cfd": "our domain only in the path",
		"https://antra.hoshi.cfd.evil.com": "login host smuggled into another domain",
		"http://tidal.anandserver.cfd":     "plaintext http — the token must never go in clear",
		"https://xanandserver.cfd":         "missing the dot before the suffix",
		"https://anandserver.cfd":          "bare apex is not a mirror",
		"ftp://tidal.anandserver.cfd":      "wrong scheme",
		"tidal.anandserver.cfd":            "no scheme at all",
		"":                                 "empty",
		"   ":                              "whitespace only",
		"://[":                             "unparseable",
	}
	for u, why := range refused {
		if isTrustedAntraHost(u) {
			t.Errorf("must be refused (%s): %q", why, u)
		}
	}
}

// A manifest that names a host we do not trust must not cause that URL to be
// exported to the Python backend, because the backend is handed the account
// credential alongside these URLs.
func TestEndpointEnvFromDropsUntrustedHosts(t *testing.T) {
	var m endpointManifest
	m.Mirrors.Tidal = "https://tidal.anandserver.cfd"
	m.Mirrors.Qobuz = "https://attacker.example.com" // poisoned entry
	m.Mirrors.Deezer = "http://deezer.anandserver.cfd"
	m.ApiKey = "ak_" + "0123456789abcdef0123456789abcdef01234567"

	env := endpointEnvFrom(m)

	has := func(prefix string) bool {
		for _, e := range env {
			if len(e) >= len(prefix) && e[:len(prefix)] == prefix {
				return true
			}
		}
		return false
	}

	// Control first: a trusted entry must survive, or "dropped" proves nothing.
	if !has("TIDAL_MIRROR_URL=https://tidal.anandserver.cfd") {
		t.Fatalf("trusted mirror was dropped — the filter is too aggressive: %v", env)
	}
	if has("QOBUZ_MIRROR_URL=") {
		t.Errorf("an attacker-supplied host was exported to the backend: %v", env)
	}
	if has("DEEZER_MIRROR_URL=") {
		t.Errorf("a plaintext http mirror was exported to the backend: %v", env)
	}
}

// With every mirror poisoned, nothing is exported — and crucially the backend
// is NOT told the manifest already supplied endpoints, or it would skip its own
// resolution and end up with no mirrors at all.
func TestEndpointEnvFromFullyPoisonedManifest(t *testing.T) {
	var m endpointManifest
	m.Mirrors.Tidal = "https://evil.example.com"
	m.Mirrors.Qobuz = "https://also-evil.example.com"

	for _, e := range endpointEnvFrom(m) {
		if len(e) >= len("ANTRA_ENDPOINT_MANIFEST_DISABLED") &&
			e[:len("ANTRA_ENDPOINT_MANIFEST_DISABLED")] == "ANTRA_ENDPOINT_MANIFEST_DISABLED" {
			t.Fatalf("backend told to stop resolving endpoints although none were exported: %v", e)
		}
		if len(e) >= len("_MIRROR_URL") {
			t.Errorf("untrusted host exported: %v", e)
		}
	}
}
