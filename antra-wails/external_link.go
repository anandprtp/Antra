package main

import (
	"net/url"
	"strings"
)

// isSafeExternalLink reports whether a server-supplied URL may be handed to the
// OS browser (BrowserOpenURL). Anything that reaches the OS opener is resolved
// by the system's protocol handlers, so file:, smb:, javascript: and the
// registered custom schemes (ms-msdt: and friends) are all reachable through it.
// Only a plain https URL to a real host qualifies; embedded credentials
// ("https://patreon.com@evil.example/") are refused because they make the
// visible host differ from the real one, and whitespace/control characters are
// refused outright rather than trimmed (the caller opens the string it passed).
func isSafeExternalLink(raw string) bool {
	if raw == "" {
		return false
	}
	for _, r := range raw {
		if r <= 0x20 || r == 0x7f {
			return false
		}
	}
	u, err := url.Parse(raw)
	if err != nil || !strings.EqualFold(u.Scheme, "https") {
		return false
	}
	return u.User == nil && u.Hostname() != ""
}

// verificationURLProblem checks the sign-in page the server asks us to open. It
// must be https on an Antra host - the same allowlist that guards where the
// device token may be sent (AUTH-F2). An absent URL is tolerated: the UI still
// shows the user code.
func verificationURLProblem(raw string) string {
	if raw == "" {
		return ""
	}
	if !isSafeExternalLink(raw) || !isTrustedAntraHost(raw) {
		return "The sign-in server returned an unexpected address, so the browser was not opened. Please try again or update Antra."
	}
	return ""
}
