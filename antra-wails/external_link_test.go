package main

import (
	"strings"
	"testing"
)

// AUDIT-DESKTOP-15: URLs that arrive from a server (the remote support gist, the
// sign-in response) end up in BrowserOpenURL, which hands them to the OS. Only a
// plain https URL to a real host may get that far.

func TestIsSafeExternalLink(t *testing.T) {
	ok := []string{
		"https://patreon.com/AntraVerse",
		"https://www.patreon.com/c/AntraVerse?x=1#y",
		"https://antra.hoshi.cfd/device?code=ABCD-EFGH",
	}
	for _, u := range ok { // controls: ordinary links pass
		if !isSafeExternalLink(u) {
			t.Errorf("%q should be allowed", u)
		}
	}
	bad := map[string]string{
		"empty":             "",
		"http":              "http://patreon.com/x",
		"file":              "file:///C:/Windows/System32/calc.exe",
		"smb":               "smb://evil/share",
		"javascript":        "javascript:alert(1)",
		"custom handler":    "ms-msdt:/id PCWDiagnostic",
		"data":              "data:text/html,<script>1</script>",
		"no host":           "https:///path",
		"userinfo trick":    "https://patreon.com@evil.example/x",
		"userinfo password": "https://user:pw@patreon.com/x",
		"relative":          "/patreon",
		"scheme-relative":   "//evil.example/x",
		"control char":      "https://patreon.com/x\r\nSet-Cookie: a=b",
		"space in host":     "https://pat reon.com/x",
		"uppercase scheme":  "HTTP://patreon.com",
	}
	for name, u := range bad {
		if isSafeExternalLink(u) {
			t.Errorf("%s (%q) must be rejected", name, u)
		}
	}
	if !isSafeExternalLink("HTTPS://Patreon.com/x") { // scheme/host case is not a security signal
		t.Error("an upper-case https scheme should still pass")
	}
}

func TestMergeSupportStatusRejectsUnsafeLinks(t *testing.T) {
	base := SupportStatus{Link: "https://patreon.com/AntraVerse"}
	for _, u := range []string{"file:///etc/passwd", "ms-msdt:/id x", "http://patreon.com/x", "https://a@b.example/"} {
		if got := mergeSupportStatus(base, supportStatusPatch{Link: u}).Link; got != base.Link {
			t.Errorf("unsafe override %q replaced the default: %q", u, got)
		}
	}
	if got := mergeSupportStatus(base, supportStatusPatch{Link: "https://example.org/give"}).Link; got != "https://example.org/give" {
		t.Errorf("control: a safe override must still apply, got %q", got)
	}
}

func TestVerificationURLMustBeTrusted(t *testing.T) {
	if verificationURLProblem("https://antra.hoshi.cfd/device?code=X") != "" {
		t.Error("control: the real sign-in page must pass")
	}
	if verificationURLProblem("") != "" {
		t.Error("an absent verification_url is tolerated (the UI shows the code)")
	}
	for _, u := range []string{"https://evil.example/device", "file:///C:/x", "http://antra.hoshi.cfd/x", "javascript:1",
		"https://antra.hoshi.cfd.evil.example/x"} {
		if verificationURLProblem(u) == "" {
			t.Errorf("%q must be refused", u)
		}
	}
}

func TestParseDeviceCodeRefusesForeignVerificationURL(t *testing.T) {
	good := `{"device_code":"d","user_code":"ABCD","verification_url":"https://antra.hoshi.cfd/device?code=ABCD","expires_in":600,"interval":5}`
	got := parseDeviceCode(strings.NewReader(good))
	if got.Error != "" || got.UserCode != "ABCD" || got.VerificationURL == "" {
		t.Errorf("control: a normal reply must pass through untouched: %+v", got)
	}
	for _, u := range []string{"https://evil.example/device", "file:///C:/x", "ms-msdt:/id x", "http://antra.hoshi.cfd/x"} {
		got := parseDeviceCode(strings.NewReader(`{"user_code":"ABCD","verification_url":"` + u + `"}`))
		if got.Error == "" || got.VerificationURL != "" {
			t.Errorf("%q must be refused with an error and no URL: %+v", u, got)
		}
	}
	if got := parseDeviceCode(strings.NewReader("not json")); got.Error == "" {
		t.Error("garbage must still be an error")
	}
}
