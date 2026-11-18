package main

// Desktop device-code login (v1.1.8 FEAT-8).
//
// The app never sees a password. It asks the website for a short code, opens the
// browser, and polls until the user approves — then stores a long-lived,
// individually revocable device token in the OS config dir.
//
// Device code was chosen over a loopback redirect because it needs no localhost
// listener, so it cannot be broken by a firewall, an occupied port, or a
// locked-down machine — the failure modes hardest to support remotely.

import (
	"bytes"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"net"
	"net/http"
	"net/url"
	"strings"
	"sync"
	"time"
)

const deviceAuthBase = "https://antra.hoshi.cfd"

type DeviceCodeResult struct {
	DeviceCode      string `json:"device_code"`
	UserCode        string `json:"user_code"`
	VerificationURL string `json:"verification_url"`
	ExpiresIn       int    `json:"expires_in"`
	Interval        int    `json:"interval"`
	Error           string `json:"error,omitempty"`
	// CanDiagnose marks a failure the network diagnostic can say something
	// useful about, so the UI only offers it when it would actually help.
	CanDiagnose bool `json:"can_diagnose,omitempty"`
}

// describeSignInFailure turns a transport error into something a user can act
// on, and logs the original so a bug report carries it (issue #72).
//
// The wording deliberately avoids "check your connection": in every report so
// far the connection was fine and the problem was the path to these specific
// hosts. Saying the wrong thing confidently is what kept this issue open.
func describeSignInFailure(err error) string {
	log.Printf("[SignIn] could not reach %s: %v", deviceAuthBase, err)
	switch {
	case looksNotTLS(err):
		return "Something on your network replied instead of the Antra server, so the secure connection could not be set up. " +
			"This is usually an ISP filter, a proxy, or a Wi-Fi sign-in page - a VPN normally fixes it. Run the connection diagnostic for details."
	case isDNSFailure(err):
		return "The Antra server's address could not be looked up. Your DNS provider may be blocking it - " +
			"switching DNS to 1.1.1.1 or 8.8.8.8, or turning on a VPN, normally fixes this."
	case isTimeout(err):
		return "The Antra server did not answer in time. If this keeps happening, run the connection diagnostic."
	}
	return "Could not open a connection to the Antra servers. Other sites may work fine while these are blocked - run the connection diagnostic to find out which."
}

func isDNSFailure(err error) bool {
	var dnsErr *net.DNSError
	return errors.As(err, &dnsErr)
}

func isTimeout(err error) bool {
	var ne net.Error
	return errors.As(err, &ne) && ne.Timeout()
}

type DeviceTokenResult struct {
	Status   string `json:"status"` // pending | approved | error
	Token    string `json:"token,omitempty"`
	Username string `json:"username,omitempty"`
	Tier     string `json:"tier,omitempty"`
	Error    string `json:"error,omitempty"`
}

// isTrustedAntraHost reports whether a base URL may be given the device token.
//
// The login endpoints use the hardcoded deviceAuthBase, but /api/device/status
// and /api/device/renew are called against mirror URLs that come from the
// PUBLIC gist manifest — and the token travels in X-API-Key. Without this check
// anyone who could edit that gist (i.e. anyone who took the GitHub account)
// could repoint the mirrors at their own server and harvest every user's token
// on the next status poll.
//
// Failing the check withholds the credential; it does not disable the mirror.
// A self-hosted endpoint therefore still serves downloads, it just never sees
// an Antra account token it has no use for.
func isTrustedAntraHost(raw string) bool {
	u, err := url.Parse(strings.TrimSpace(raw))
	if err != nil || u.Scheme != "https" {
		return false
	}
	host := strings.ToLower(u.Hostname())
	return host == "antra.hoshi.cfd" || strings.HasSuffix(host, ".anandserver.cfd")
}

func deviceAuthURL(path string) string {
	base := strings.TrimRight(deviceAuthBase, "/")
	return base + path
}

// StartDeviceLogin asks the website for a device code and returns it so the UI
// can show the short user code and open the browser.
func (a *App) StartDeviceLogin() DeviceCodeResult {
	client := &http.Client{Timeout: 20 * time.Second}
	req, err := http.NewRequest(http.MethodPost, deviceAuthURL("/api/device/code"), bytes.NewReader([]byte("{}")))
	if err != nil {
		return DeviceCodeResult{Error: err.Error()}
	}
	req.Header.Set("Content-Type", "application/json")
	resp, err := client.Do(req)
	if err != nil {
		// Issue #72. This used to answer "Could not reach the Antra servers.
		// Check your connection." and throw `err` away - which is the title of
		// the bug report, and it was doubly unhelpful: the user's connection is
		// usually fine (other sites work, and a VPN fixes it instantly), and
		// the one fact that would have identified the cause was discarded, so
		// the first reply on the issue had to be "have you tried a VPN?".
		return DeviceCodeResult{Error: describeSignInFailure(err), CanDiagnose: true}
	}
	defer resp.Body.Close()
	if resp.StatusCode != 200 {
		return DeviceCodeResult{Error: fmt.Sprintf("Server returned %d.", resp.StatusCode)}
	}
	return parseDeviceCode(resp.Body)
}

// parseDeviceCode decodes the /api/device/code reply and refuses a
// verification_url that is not an https page on an Antra host: the UI hands it
// straight to the OS browser.
func parseDeviceCode(body io.Reader) DeviceCodeResult {
	var out DeviceCodeResult
	if err := json.NewDecoder(body).Decode(&out); err != nil {
		return DeviceCodeResult{Error: "Unexpected response from server."}
	}
	if msg := verificationURLProblem(out.VerificationURL); msg != "" {
		log.Printf("[SignIn] refusing verification_url %q", out.VerificationURL)
		return DeviceCodeResult{Error: msg}
	}
	return out
}

// PollDeviceLogin checks once whether the user has approved the code. The
// frontend drives the polling loop so it can show progress and allow cancelling;
// the server enforces its own minimum interval and answers 429 "slow_down" if
// called too fast, which is surfaced as still-pending rather than an error.
func (a *App) PollDeviceLogin(deviceCode string) DeviceTokenResult {
	client := &http.Client{Timeout: 20 * time.Second}
	body, _ := json.Marshal(map[string]string{
		"device_code": deviceCode,
		"device_name": "Antra Desktop",
	})
	req, err := http.NewRequest(http.MethodPost, deviceAuthURL("/api/device/token"), bytes.NewReader(body))
	if err != nil {
		return DeviceTokenResult{Status: "error", Error: err.Error()}
	}
	req.Header.Set("Content-Type", "application/json")
	resp, err := client.Do(req)
	if err != nil {
		return DeviceTokenResult{Status: "pending"} // transient — keep polling
	}
	defer resp.Body.Close()

	if resp.StatusCode == 429 {
		return DeviceTokenResult{Status: "pending"} // polled too fast; back off
	}
	if resp.StatusCode == 404 {
		return DeviceTokenResult{Status: "error", Error: "Login request expired. Please start again."}
	}
	if resp.StatusCode != 200 {
		return DeviceTokenResult{Status: "error", Error: fmt.Sprintf("Server returned %d.", resp.StatusCode)}
	}

	var out DeviceTokenResult
	if err := json.NewDecoder(resp.Body).Decode(&out); err != nil {
		return DeviceTokenResult{Status: "error", Error: "Unexpected response from server."}
	}
	if out.Status == "approved" && out.Token != "" {
		// Persist into the token's OWN field. It used to be written to
		// AntraApiKey, which the Settings supporter-key box binds to — so
		// signing in destroyed the pasted key and then failed to validate,
		// demoting a supporter to free tier. The two are separate credentials
		// and must never share a field.
		cfg := a.GetConfig()
		cfg.AntraDeviceToken = out.Token
		if err := a.SaveConfig(cfg); err != nil {
			return DeviceTokenResult{Status: "error", Error: "Signed in, but the token could not be saved: " + err.Error()}
		}
		invalidateDeviceStatus()
		invalidateEndpointManifest()
	}
	return out
}

// storedDeviceToken returns the device token, tolerating installs written before
// it had its own field.
func storedDeviceToken(cfg Config) string {
	if t := strings.TrimSpace(cfg.AntraDeviceToken); strings.HasPrefix(t, "at_") {
		return t
	}
	if t := strings.TrimSpace(cfg.AntraApiKey); strings.HasPrefix(t, "at_") {
		return t
	}
	return ""
}

// SignOutDevice clears the stored device token. It also clears a token left in
// the old shared field, so signing out cannot leave a stale one behind — but it
// never touches a real ak_/pk_ supporter key.
func (a *App) SignOutDevice() string {
	cfg := a.GetConfig()
	changed := false
	if strings.TrimSpace(cfg.AntraDeviceToken) != "" {
		cfg.AntraDeviceToken = ""
		changed = true
	}
	if strings.HasPrefix(strings.TrimSpace(cfg.AntraApiKey), "at_") {
		cfg.AntraApiKey = ""
		changed = true
	}
	if changed {
		if err := a.SaveConfig(cfg); err != nil {
			return err.Error()
		}
		invalidateDeviceStatus()
		invalidateEndpointManifest()
	}
	return ""
}

// ── Account status (v1.1.8 FEAT-8 / FEAT-12) ────────────────────────────────

// DeviceAccountStatus is what the UI needs to describe the signed-in account.
// Reachable distinguishes "the server says this token is dead" from "we could
// not ask" — the same distinction KeyInfoResult draws for BUG-5, and for the
// same reason: an outage must never look like a revoked session.
type DeviceAccountStatus struct {
	SignedIn    bool   `json:"signed_in"`
	Valid       bool   `json:"valid"`
	Reachable   bool   `json:"reachable"`
	IsSupporter bool   `json:"is_supporter"`
	Tier        string `json:"tier,omitempty"`
	Username    string `json:"username,omitempty"`
	ExpiresAt   int64  `json:"expires_at,omitempty"`
	ShouldRenew bool   `json:"should_renew,omitempty"`
	Error       string `json:"error,omitempty"`
}

// mirrorBases returns up to `limit` mirror base URLs from the manifest. Any
// mirror can answer the device endpoints, so trying several means one being
// down does not cost the user their session display.
func mirrorBases(limit int) []string {
	manifest := loadEndpointManifest()
	var out []string
	for _, base := range []string{
		manifest.Mirrors.Tidal, manifest.Mirrors.Qobuz, manifest.Mirrors.Amazon,
		manifest.Mirrors.Deezer, manifest.Mirrors.Apple,
	} {
		base = strings.TrimRight(strings.TrimSpace(base), "/")
		if base == "" {
			continue
		}
		out = append(out, base)
		if limit > 0 && len(out) >= limit {
			break
		}
	}
	return out
}

// deviceTierIsSupporter maps the token's tier claim to supporter status. Kept in
// one place so the UI, the worker count and the mirror-key choice cannot drift.
func deviceTierIsSupporter(tier string) bool {
	switch strings.ToLower(strings.TrimSpace(tier)) {
	case "supporter", "premium", "admin":
		return true
	}
	return false
}

// The status is cached briefly because supporterContext() consults it on every
// download start, and an unreachable mirror pool would otherwise add its full
// timeout budget to the start of every job.
var (
	deviceStatusMu    sync.Mutex
	deviceStatusVal   DeviceAccountStatus
	deviceStatusToken string
	deviceStatusAt    time.Time
)

const deviceStatusTTL = 60 * time.Second

// invalidateDeviceStatus drops the cache so the next read measures again. Called
// whenever the stored token changes.
func invalidateDeviceStatus() {
	deviceStatusMu.Lock()
	deviceStatusToken = ""
	deviceStatusAt = time.Time{}
	deviceStatusMu.Unlock()
}

// GetDeviceAccountStatus asks a mirror whether the stored device token is still
// valid and what tier it carries, cached for deviceStatusTTL.
//
// The tier is read from the SERVER's verification, never from a local decode of
// the payload: the payload is only base64, so trusting it client-side would let
// anyone unlock supporter-gated UI by editing one field. The local decode in
// deviceTokenExpiry is used solely to decide whether to bother asking.
func (a *App) GetDeviceAccountStatus() DeviceAccountStatus {
	token := storedDeviceToken(a.GetConfig())

	deviceStatusMu.Lock()
	if deviceStatusToken == token && time.Since(deviceStatusAt) < deviceStatusTTL {
		cached := deviceStatusVal
		deviceStatusMu.Unlock()
		return cached
	}
	deviceStatusMu.Unlock()

	res := a.fetchDeviceAccountStatus(token)

	deviceStatusMu.Lock()
	deviceStatusVal, deviceStatusToken, deviceStatusAt = res, token, time.Now()
	deviceStatusMu.Unlock()
	return res
}

func (a *App) fetchDeviceAccountStatus(token string) DeviceAccountStatus {
	if token == "" {
		return DeviceAccountStatus{SignedIn: false, Reachable: true}
	}

	// Two mirrors, not five: one being down must not cost the session display,
	// but the whole probe still has to fit inside a download's patience.
	bases := mirrorBases(2)
	if len(bases) == 0 {
		return DeviceAccountStatus{SignedIn: true, Reachable: false,
			Error: "No mirror endpoints are known yet."}
	}

	client := &http.Client{Timeout: 6 * time.Second}
	lastErr := ""
	for _, base := range bases {
		if !isTrustedAntraHost(base) {
			lastErr = "No trusted Antra endpoint is available."
			continue
		}
		req, err := http.NewRequest(http.MethodGet, base+"/api/device/status", nil)
		if err != nil {
			continue
		}
		req.Header.Set("X-API-Key", token)
		resp, err := client.Do(req)
		if err != nil {
			lastErr = "Could not reach the Antra servers."
			continue
		}
		var res DeviceAccountStatus
		done := func() bool {
			defer resp.Body.Close()
			if resp.StatusCode != 200 {
				// 404 means this mirror has no device router deployed yet; any
				// other non-200 is a server-side problem. Either way, ask the
				// next mirror rather than declaring the session dead.
				lastErr = fmt.Sprintf("Server returned %d.", resp.StatusCode)
				return false
			}
			var body struct {
				Valid       bool   `json:"valid"`
				ShouldRenew bool   `json:"should_renew"`
				ExpiresAt   int64  `json:"expires_at"`
				Tier        string `json:"tier"`
				Username    string `json:"username"`
			}
			if json.NewDecoder(resp.Body).Decode(&body) != nil {
				lastErr = "Unexpected response from server."
				return false
			}
			res = DeviceAccountStatus{
				SignedIn:    true,
				Valid:       body.Valid,
				Reachable:   true,
				IsSupporter: body.Valid && deviceTierIsSupporter(body.Tier),
				Tier:        body.Tier,
				Username:    body.Username,
				ExpiresAt:   body.ExpiresAt,
				ShouldRenew: body.ShouldRenew,
			}
			return true
		}()
		if done {
			return res
		}
	}
	return DeviceAccountStatus{SignedIn: true, Reachable: false, Error: lastErr}
}

// ── Token renewal (v1.1.8 FEAT-12) ──────────────────────────────────────────
// Renewal deliberately goes to the MIRRORS, not the website. Device tokens are
// stateless HMAC and the mirrors already hold the signing secret, so they can
// re-mint one with no website involvement. That keeps the web app off the
// steady-state path: after first login the app keeps working even if the site
// is down for weeks. Without this, every token simply lapsed after 30 days and
// the only way back was the browser flow.

// DeviceRenewResult reports the outcome of a renewal attempt.
type DeviceRenewResult struct {
	Status    string `json:"status"` // renewed | current | reauth | unavailable | none
	Token     string `json:"token,omitempty"`
	ExpiresAt int64  `json:"expires_at,omitempty"`
	Tier      string `json:"tier,omitempty"`
	Username  string `json:"username,omitempty"`
	DaysLeft  int    `json:"days_left,omitempty"`
	Error     string `json:"error,omitempty"`
}

// deviceTokenExpiry reads the expiry out of a token locally. The payload is
// only base64 — no secret is needed to READ it, and nothing is trusted on the
// strength of it: this decides whether to bother asking, never whether the
// token is valid. That question belongs to the server.
func deviceTokenExpiry(token string) (int64, bool) {
	token = strings.TrimSpace(token)
	if !strings.HasPrefix(token, "at_") {
		return 0, false
	}
	body, _, ok := strings.Cut(token[len("at_"):], ".")
	if !ok || body == "" {
		return 0, false
	}
	if pad := len(body) % 4; pad != 0 {
		body += strings.Repeat("=", 4-pad)
	}
	raw, err := base64.URLEncoding.DecodeString(body)
	if err != nil {
		return 0, false
	}
	var payload struct {
		Exp int64 `json:"exp"`
	}
	if err := json.Unmarshal(raw, &payload); err != nil || payload.Exp == 0 {
		return 0, false
	}
	return payload.Exp, true
}

// RenewDeviceTokenIfNeeded refreshes the stored token when it is close to
// expiry. Safe to call on every launch: it returns "none"/"current" without
// touching the network when there is nothing to do.
func (a *App) RenewDeviceTokenIfNeeded() DeviceRenewResult {
	cfg := a.GetConfig()
	token := storedDeviceToken(cfg)
	if token == "" {
		// A legacy ak_/pk_ key or no key at all — nothing to renew.
		return DeviceRenewResult{Status: "none"}
	}

	exp, ok := deviceTokenExpiry(token)
	if !ok {
		return DeviceRenewResult{Status: "reauth", Error: "Stored token is unreadable."}
	}
	daysLeft := int(time.Until(time.Unix(exp, 0)).Hours() / 24)
	// Server-side window is 7 days; ask a little earlier so a user who opens the
	// app rarely still gets renewed before it lapses.
	if time.Until(time.Unix(exp, 0)) > 10*24*time.Hour {
		return DeviceRenewResult{Status: "current", ExpiresAt: exp, DaysLeft: daysLeft}
	}

	// Any mirror can renew, so gather several from the manifest.
	endpoints := mirrorBases(0)
	if len(endpoints) == 0 {
		return DeviceRenewResult{Status: "unavailable", ExpiresAt: exp, DaysLeft: daysLeft,
			Error: "No mirror endpoints are known yet."}
	}

	client := &http.Client{Timeout: 15 * time.Second}
	var lastErr string
	// Any mirror can renew, so try several: a single one being down must not
	// cost the user their session.
	for i, base := range endpoints {
		if i >= 4 {
			break
		}
		base = strings.TrimRight(strings.TrimSpace(base), "/")
		if base == "" || !isTrustedAntraHost(base) {
			continue
		}
		req, err := http.NewRequest(http.MethodPost, base+"/api/device/renew", bytes.NewReader([]byte("{}")))
		if err != nil {
			continue
		}
		req.Header.Set("Content-Type", "application/json")
		req.Header.Set("X-API-Key", token)
		resp, err := client.Do(req)
		if err != nil {
			lastErr = err.Error()
			continue
		}
		func() {
			defer resp.Body.Close()
			switch {
			case resp.StatusCode == 200:
				var out struct {
					Token     string `json:"token"`
					ExpiresAt int64  `json:"expires_at"`
					Tier      string `json:"tier"`
					Username  string `json:"username"`
				}
				if json.NewDecoder(resp.Body).Decode(&out) == nil && strings.HasPrefix(out.Token, "at_") {
					cfg.AntraDeviceToken = out.Token
					if strings.HasPrefix(strings.TrimSpace(cfg.AntraApiKey), "at_") {
						cfg.AntraApiKey = "" // clear a token left in the old shared field
					}
					if err := a.SaveConfig(cfg); err != nil {
						lastErr = "renewed but could not save: " + err.Error()
						return
					}
					invalidateDeviceStatus()
					invalidateEndpointManifest()
					lastErr = ""
					exp = out.ExpiresAt
					daysLeft = int(time.Until(time.Unix(out.ExpiresAt, 0)).Hours() / 24)
					lastErr = "\x00ok:" + out.Tier + ":" + out.Username
				}
			case resp.StatusCode == 401:
				// The server positively rejected it — revoked, too old, or the
				// renewal chain has run its course. Only this means "log in again".
				lastErr = "\x00reauth"
			default:
				// 5xx or anything else is a server-side problem. Keep the token:
				// signing the user out over a transient outage is the exact
				// failure this feature exists to prevent.
				lastErr = "server returned " + resp.Status
			}
		}()
		if strings.HasPrefix(lastErr, "\x00") {
			break
		}
	}

	switch {
	case strings.HasPrefix(lastErr, "\x00ok:"):
		parts := strings.SplitN(strings.TrimPrefix(lastErr, "\x00ok:"), ":", 2)
		res := DeviceRenewResult{Status: "renewed", ExpiresAt: exp, DaysLeft: daysLeft}
		if len(parts) == 2 {
			res.Tier, res.Username = parts[0], parts[1]
		}
		return res
	case lastErr == "\x00reauth":
		return DeviceRenewResult{Status: "reauth", ExpiresAt: exp, DaysLeft: daysLeft,
			Error: "Your session has ended. Please sign in again."}
	default:
		return DeviceRenewResult{Status: "unavailable", ExpiresAt: exp, DaysLeft: daysLeft, Error: lastErr}
	}
}
