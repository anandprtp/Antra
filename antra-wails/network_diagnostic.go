package main

// Issue #72 - "Could not reach the antra servers. Check your connection."
//
// A handful of users (all reports so far from the US) cannot reach ANY Antra
// host: sign-in fails outright, and with a supporter key pasted instead, every
// mirror fails with
//
//	SSLError(SSLError(1, '[SSL: WRONG_VERSION_NUMBER] wrong version number'))
//
// while the one source that is NOT behind *.anandserver.cfd downloads fine.
// Turning on a VPN fixes it completely.
//
// WRONG_VERSION_NUMBER does not mean "server down". It means the bytes that
// came back on port 443 were not TLS at all, so something on the path answered
// instead of the mirror. The existing curl_cffi fallback - a real Chrome TLS
// fingerprint - was tried in that report and ALSO failed, which rules out
// fingerprint-based blocking and leaves hostname (SNI) or destination-IP
// filtering. We cannot tell those two apart from a machine that is not affected,
// and that is exactly what this file exists to settle.
//
// The probe deliberately lives in Go rather than in the Python backend:
//   - the reported symptom is sign-in, which never reaches Python at all;
//   - it has to work when the backend itself cannot start;
//   - Go reports "the peer did not speak TLS" as a distinct, typed error
//     (tls.RecordHeaderError), which is precisely OpenSSL's
//     WRONG_VERSION_NUMBER and is the signal the whole verdict turns on.
//
// Its one limitation is stated in the report it produces: Go's TLS stack is not
// Python's, so a host that passes here can still fail in the backend. The
// verdict is about the network path, not about any one library.

import (
	"context"
	"crypto/tls"
	"encoding/json"
	"errors"
	"fmt"
	"net"
	"net/http"
	"net/url"
	"os"
	"runtime"
	"sort"
	"strings"
	"time"
)

const (
	probeTimeout = 8 * time.Second

	// Reached only by the SNI control, and only as a name in a ClientHello -
	// no request is ever sent to it. A reputable, long-established domain is
	// the point: a filter that is looking at hostnames has no reason to object
	// to this one.
	sniControlName = "www.cloudflare.com"
)

// antraHosts is every hostname the app needs. Kept as a literal rather than
// read from the endpoint manifest on purpose: when this runs, fetching the
// manifest is one of the things that may have failed.
var antraHosts = []struct{ label, host string }{
	{"Sign-in / website", "antra.hoshi.cfd"},
	{"Tidal mirror", "tidal.anandserver.cfd"},
	{"Qobuz mirror", "qobuz.anandserver.cfd"},
	{"Deezer mirror", "deezer.anandserver.cfd"},
	{"Amazon mirror", "amazon.anandserver.cfd"},
	{"Apple mirror", "apple.anandserver.cfd"},
}

// controlHosts are reachable for everyone. They separate "this machine has no
// working internet" from "this machine cannot reach Antra specifically", which
// is the first fork in the verdict and the one users get wrong.
var controlHosts = []struct{ label, host string }{
	{"General internet (GitHub)", "api.github.com"},
	{"Cloudflare-hosted control", "www.cloudflare.com"},
}

// ProbeResult is one host, taken as far as it gets.
type ProbeResult struct {
	Label string   `json:"label"`
	Host  string   `json:"host"`
	IPs   []string `json:"ips,omitempty"`
	// Stage is how far it got: dns | tcp | tls | http | ok.
	Stage string `json:"stage"`
	Error string `json:"error,omitempty"`
	// NotTLS is the finding this whole file is built around: the peer answered
	// with something that is not a TLS record. Python reports the same
	// condition as WRONG_VERSION_NUMBER.
	NotTLS      bool   `json:"not_tls,omitempty"`
	TLSVersion  string `json:"tls_version,omitempty"`
	CertIssuer  string `json:"cert_issuer,omitempty"`
	CertSubject string `json:"cert_subject,omitempty"`
	HTTPStatus  int    `json:"http_status,omitempty"`
	ElapsedMS   int64  `json:"elapsed_ms"`
}

// SNIControl is the experiment that separates the two live hypotheses. It
// dials an Antra IP - the same address that just failed - and offers a
// reputable hostname in the ClientHello instead of the Antra one.
//
//	answered with TLS  => the route to that IP is fine; the filter is reacting
//	                      to the NAME, so it is SNI/TLD filtering.
//	answered with junk  => the address itself is intercepted, whatever name is
//	  or nothing           asked for, so it is IP-range filtering.
//
// A TLS *alert* (Cloudflare refusing a hostname that is not configured on that
// IP) counts as "answered with TLS" and is the expected healthy outcome: it
// proves a real TLS peer is on the other end.
type SNIControl struct {
	Ran        bool   `json:"ran"`
	IP         string `json:"ip,omitempty"`
	ServerName string `json:"server_name,omitempty"`
	// SpokeTLS is true when the peer produced a well-formed TLS response,
	// including a handshake alert.
	SpokeTLS bool   `json:"spoke_tls"`
	Error    string `json:"error,omitempty"`
	Skipped  string `json:"skipped,omitempty"`
}

// DiagnosticReport is what the UI renders and what the user copies into an
// issue. Deliberately carries no credential of any kind: hostnames, IPs, error
// strings and timings only.
type DiagnosticReport struct {
	GeneratedAt string        `json:"generated_at"`
	OS          string        `json:"os"`
	Arch        string        `json:"arch"`
	Proxy       string        `json:"proxy,omitempty"`
	Antra       []ProbeResult `json:"antra"`
	Controls    []ProbeResult `json:"controls"`
	SNI         SNIControl    `json:"sni_control"`
	// Verdict is a stable key for the UI; Summary is the sentence a user reads.
	Verdict string `json:"verdict"`
	Summary string `json:"summary"`
	Advice  string `json:"advice,omitempty"`
	// Text is the same report rendered for the Copy button. It travels inside
	// the payload so the webview has no second copy of the formatter to drift
	// from, and so what the user pastes is exactly what the app measured.
	Text string `json:"text,omitempty"`
}

// looksNotTLS reports whether an error means "the peer did not speak TLS".
//
// tls.RecordHeaderError is Go's typed form of it and is the reliable signal.
// The string checks are a backstop for wrapped errors, and are matched on
// Go's own wording rather than on anything a server controls.
func looksNotTLS(err error) bool {
	if err == nil {
		return false
	}
	var rhe tls.RecordHeaderError
	if errors.As(err, &rhe) {
		return true
	}
	s := strings.ToLower(err.Error())
	return strings.Contains(s, "first record does not look like a tls handshake") ||
		strings.Contains(s, "record header") ||
		strings.Contains(s, "wrong version number")
}

// isTLSAlert reports whether the peer answered with a real TLS alert - a
// refusal, but from an actual TLS implementation. For the SNI control that is
// a PASS: it proves the path to that address carries TLS.
func isTLSAlert(err error) bool {
	if err == nil {
		return false
	}
	var ae tls.AlertError
	if errors.As(err, &ae) {
		return true
	}
	s := strings.ToLower(err.Error())
	// Go words a peer's alert as `remote error: tls: <description>`. The first
	// version of this listed descriptions ("handshake failure", "unrecognized
	// name", ...) and missed the one a real refusing server actually sent —
	// `remote error: tls: internal error` — which made the SNI control read a
	// healthy TLS peer as an interceptor and would have turned an sni_filtered
	// verdict into ip_filtered. The PREFIX is the signal: whatever follows it,
	// the peer implemented TLS well enough to send an alert.
	if strings.HasPrefix(s, "remote error: tls:") {
		return true
	}
	return strings.Contains(s, "tls: ") &&
		(strings.Contains(s, "alert") ||
			strings.Contains(s, "handshake failure") ||
			strings.Contains(s, "unrecognized name") ||
			strings.Contains(s, "certificate"))
}

func probeHost(ctx context.Context, label, host string) ProbeResult {
	started := time.Now()
	r := ProbeResult{Label: label, Host: host, Stage: "dns"}
	defer func() { r.ElapsedMS = time.Since(started).Milliseconds() }()

	ips, err := net.DefaultResolver.LookupHost(ctx, host)
	if err != nil {
		r.Error = err.Error()
		return r
	}
	sort.Strings(ips)
	r.IPs = ips

	r.Stage = "tcp"
	d := &net.Dialer{Timeout: probeTimeout}
	conn, err := d.DialContext(ctx, "tcp", net.JoinHostPort(host, "443"))
	if err != nil {
		r.Error = err.Error()
		return r
	}

	r.Stage = "tls"
	_ = conn.SetDeadline(time.Now().Add(probeTimeout))
	tc := tls.Client(conn, &tls.Config{ServerName: host})
	if err := tc.HandshakeContext(ctx); err != nil {
		r.Error = err.Error()
		r.NotTLS = looksNotTLS(err)
		_ = conn.Close()
		return r
	}
	st := tc.ConnectionState()
	r.TLSVersion = tlsVersionName(st.Version)
	if len(st.PeerCertificates) > 0 {
		r.CertIssuer = st.PeerCertificates[0].Issuer.CommonName
		r.CertSubject = st.PeerCertificates[0].Subject.CommonName
	}
	_ = tc.Close()

	// The handshake is the finding; the request is only confirmation, and a
	// non-200 from a healthy mirror must not read as a network failure.
	r.Stage = "http"
	client := &http.Client{Timeout: probeTimeout}
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, "https://"+host+"/", nil)
	if err != nil {
		r.Error = err.Error()
		return r
	}
	resp, err := client.Do(req)
	if err != nil {
		r.Error = err.Error()
		r.NotTLS = r.NotTLS || looksNotTLS(err)
		return r
	}
	defer resp.Body.Close()
	r.HTTPStatus = resp.StatusCode
	r.Stage = "ok"
	return r
}

func tlsVersionName(v uint16) string {
	switch v {
	case tls.VersionTLS13:
		return "TLS 1.3"
	case tls.VersionTLS12:
		return "TLS 1.2"
	case tls.VersionTLS11:
		return "TLS 1.1"
	case tls.VersionTLS10:
		return "TLS 1.0"
	}
	return fmt.Sprintf("0x%04x", v)
}

// runSNIControl dials `ip` directly and offers sniControlName in the
// ClientHello. See the SNIControl doc comment for what each outcome means.
// addr is host:port so tests can point it at a local listener; production
// always passes port 443.
func runSNIControl(ctx context.Context, addr string) SNIControl {
	c := SNIControl{IP: addr, ServerName: sniControlName}
	if addr == "" {
		c.Skipped = "no Antra host resolved to an address, so there was nothing to dial"
		return c
	}
	c.Ran = true
	d := &net.Dialer{Timeout: probeTimeout}
	conn, err := d.DialContext(ctx, "tcp", addr)
	if err != nil {
		c.Error = err.Error()
		return c
	}
	defer conn.Close()
	_ = conn.SetDeadline(time.Now().Add(probeTimeout))
	tc := tls.Client(conn, &tls.Config{ServerName: sniControlName})
	err = tc.HandshakeContext(ctx)
	if err == nil {
		c.SpokeTLS = true
		return c
	}
	c.Error = err.Error()
	// An alert is a refusal FROM a TLS peer, which is what we are testing for.
	c.SpokeTLS = isTLSAlert(err) && !looksNotTLS(err)
	return c
}

// sniControlAddr turns a bare address into something dialable, or "" when
// there is nothing to dial.
func sniControlAddr(ip string) string {
	if ip == "" {
		return ""
	}
	return net.JoinHostPort(ip, "443")
}

func proxySummary() string {
	var out []string
	for _, k := range []string{"HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"} {
		if v := strings.TrimSpace(os.Getenv(k)); v != "" {
			out = append(out, k+"="+redactProxy(v))
		}
	}
	// Covers a proxy Go picks up that is not in the obvious variables.
	if u, err := http.ProxyFromEnvironment(&http.Request{
		URL: &url.URL{Scheme: "https", Host: "antra.hoshi.cfd"},
	}); err == nil && u != nil && len(out) == 0 {
		out = append(out, "effective="+redactProxy(u.String()))
	}
	return strings.Join(out, ", ")
}

// redactProxy strips any credentials embedded in a proxy URL. The report is
// meant to be pasted into a public issue.
func redactProxy(raw string) string {
	u, err := url.Parse(raw)
	if err != nil || u.User == nil {
		return raw
	}
	u.User = url.User("***")
	return u.String()
}

// verdictFor turns the measurements into one sentence. The ORDER of these
// branches is the whole design: each one is only reached when the cheaper,
// more-likely explanation above it has been ruled out.
func verdictFor(r *DiagnosticReport) {
	antraOK, antraNotTLS, antraDNSFail, antraTCPFail := 0, 0, 0, 0
	for _, p := range r.Antra {
		switch {
		case p.Stage == "ok":
			antraOK++
		case p.NotTLS:
			antraNotTLS++
		case p.Stage == "dns":
			antraDNSFail++
		case p.Stage == "tcp":
			antraTCPFail++
		}
	}
	mitm := detectInspection(r.Controls, r.Antra)
	controlsOK := 0
	for _, p := range r.Controls {
		if p.Stage == "ok" {
			controlsOK++
		}
	}

	switch {
	case antraOK == len(r.Antra):
		r.Verdict = "ok"
		r.Summary = "Every Antra server was reachable from this machine."
		r.Advice = "If downloads are still failing, the cause is not the network path to the servers — please attach the activity log to the issue."

	case controlsOK == 0:
		// Nothing at all works. Saying "Antra is blocked" here would be wrong
		// and would send the user chasing the wrong thing.
		r.Verdict = "no_internet"
		r.Summary = "This machine could not reach anything, including GitHub — so this is a general connectivity problem, not something specific to Antra."
		r.Advice = "Check that you are online, then run this again."

	case mitm != "":
		r.Verdict = "https_inspection"
		r.Summary = fmt.Sprintf("HTTPS traffic on this machine is being inspected by %q rather than served by the real certificate.", mitm)
		r.Advice = "That is usually antivirus HTTPS scanning, a corporate firewall, or a proxy. Exempt the Antra domains from HTTPS inspection, or use a different network."

	case antraDNSFail == len(r.Antra):
		r.Verdict = "dns_blocked"
		r.Summary = "None of the Antra hostnames could be looked up, while other sites resolved fine — your DNS resolver is refusing them."
		r.Advice = "Switch DNS to 1.1.1.1 or 8.8.8.8, or turn off any DNS-level filtering, and try again."

	case antraNotTLS > 0 && r.SNI.Ran && r.SNI.SpokeTLS:
		// The decisive case, and the one matching every report so far.
		r.Verdict = "sni_filtered"
		r.Summary = "Something on your network is blocking connections by hostname: the same server address answers normally under a different name, but refuses the Antra ones."
		r.Advice = "This is usually an ISP filter, a router's \"security\" feature, or a school/workplace network. A VPN fixes it, and so does encrypted DNS (DoH) in some cases. It is not a problem with the Antra servers."

	case antraNotTLS > 0 && r.SNI.Ran && !r.SNI.SpokeTLS:
		r.Verdict = "ip_filtered"
		r.Summary = "Connections to the Antra servers' addresses are being intercepted whatever hostname is asked for, so the block is on the address range rather than on the Antra names."
		r.Advice = "A VPN fixes this. If you are on a corporate or campus network, the block is likely deliberate and worth raising with whoever runs it."

	case antraNotTLS > 0:
		r.Verdict = "intercepted"
		r.Summary = "The Antra servers answered with something that is not HTTPS, which means a device on your network replied instead of the server."
		r.Advice = "A VPN, proxy, firewall, or public Wi-Fi sign-in page is the usual cause. Try another network or turn a VPN on."

	// Only reached once interception has been ruled out, and that order is
	// deliberate. A mix of working and failing hosts normally means one mirror
	// is down — but NOT when the failures carry the interception signature:
	// the sign-in host is on a different domain from the five mirrors, so a
	// filter can hit one and miss the other and still leave some hosts fine.
	// Reporting that as "one mirror is down" would send the user to the status
	// page instead of to a VPN.
	case antraOK > 0:
		r.Verdict = "partial"
		r.Summary = fmt.Sprintf("%d of %d Antra servers were reachable — the rest failed.", antraOK, len(r.Antra))
		r.Advice = "That pattern usually means one mirror is genuinely down rather than a problem on your side. Please attach this report to the issue."

	case antraTCPFail > 0:
		r.Verdict = "connection_refused"
		r.Summary = "The Antra servers resolved but would not accept a connection, while other sites did."
		r.Advice = "A firewall is the usual cause. Try another network or turn a VPN on."

	default:
		r.Verdict = "unreachable"
		r.Summary = "The Antra servers could not be reached, though the rest of the internet works from this machine."
		r.Advice = "A VPN usually fixes this. Please attach this report to the issue."
	}
}

// detectInspection reports the issuer re-signing traffic, or "" when nothing is.
//
// The first version of this was an allowlist of known-good CA names, and a live
// run against the real hosts immediately proved it wrong: Cloudflare currently
// serves these domains with Google Trust Services intermediates named "YE1" and
// "YE2", which were not in the list, so a perfectly healthy machine was reported
// as being under HTTPS inspection. Telling people their antivirus is
// intercepting them when it is not is a worse failure than saying nothing, and
// the list would have needed editing every time a CA rotated an intermediate.
//
// The allowlist-free signal is CA DIVERSITY. api.github.com and
// www.cloudflare.com are served by genuinely different certificate authorities
// (measured live 2026-09-26: Sectigo and Google Trust Services respectively).
// An interception proxy re-signs everything with its OWN single CA, so under
// inspection those two collapse to one issuer. That is what is tested: it needs
// no knowledge of which CAs are legitimate, and it cannot be broken by a
// rotation.
//
// It needs at least two controls with observed issuers, and it deliberately
// reports nothing when it has fewer - an unproven guess here is worse than
// silence.
func detectInspection(controls []ProbeResult, antra []ProbeResult) string {
	seen := map[string]bool{}
	observed := 0
	for _, p := range controls {
		if p.CertIssuer != "" {
			seen[strings.ToLower(p.CertIssuer)] = true
			observed++
		}
	}
	// Two controls that agree is the evidence; one control agreeing with itself
	// is not evidence of anything, and the first version of this accepted it.
	if observed < 2 || len(seen) != 1 {
		return ""
	}
	var only string
	for k := range seen {
		only = k
	}
	// Unrelated CAs collapsed into one. Confirm the Antra hosts carry the same
	// issuer before naming it, so a single odd control cannot trigger this.
	for _, p := range antra {
		if p.CertIssuer != "" && strings.ToLower(p.CertIssuer) != only {
			return ""
		}
	}
	for _, p := range controls {
		if p.CertIssuer != "" {
			return p.CertIssuer
		}
	}
	return ""
}

// DiagnoseNetwork probes every Antra host plus controls and returns the report
// as JSON. Read-only: it sends no credential, and the only request it makes is
// a GET of each host's root.
func (a *App) DiagnoseNetwork() string {
	ctx, cancel := context.WithTimeout(context.Background(), 90*time.Second)
	defer cancel()

	r := DiagnosticReport{
		GeneratedAt: time.Now().UTC().Format(time.RFC3339),
		OS:          runtime.GOOS,
		Arch:        runtime.GOARCH,
		Proxy:       proxySummary(),
	}
	for _, h := range antraHosts {
		r.Antra = append(r.Antra, probeHost(ctx, h.label, h.host))
	}
	for _, h := range controlHosts {
		r.Controls = append(r.Controls, probeHost(ctx, h.label, h.host))
	}

	// Dial the first Antra address we managed to resolve, preferring one that
	// actually failed - that is the address the verdict is about.
	ip := ""
	for _, p := range r.Antra {
		if len(p.IPs) > 0 && p.Stage != "ok" {
			ip = p.IPs[0]
			break
		}
	}
	if ip == "" {
		for _, p := range r.Antra {
			if len(p.IPs) > 0 {
				ip = p.IPs[0]
				break
			}
		}
	}
	r.SNI = runSNIControl(ctx, sniControlAddr(ip))

	verdictFor(&r)
	r.Text = renderReport(&r)
	out, err := json.Marshal(r)
	if err != nil {
		return `{"verdict":"error","summary":"Could not build the report."}`
	}
	return string(out)
}

// FormatDiagnosticReport renders a report as the plain text the Copy button
// puts on the clipboard: readable in a GitHub issue, and carrying no secret.
func FormatDiagnosticReport(jsonBlob string) string {
	var r DiagnosticReport
	if err := json.Unmarshal([]byte(jsonBlob), &r); err != nil {
		return jsonBlob
	}
	return renderReport(&r)
}

func renderReport(rp *DiagnosticReport) string {
	r := *rp
	var b strings.Builder
	fmt.Fprintf(&b, "Antra network diagnostic — %s\n", r.GeneratedAt)
	fmt.Fprintf(&b, "Platform: %s/%s\n", r.OS, r.Arch)
	if r.Proxy != "" {
		fmt.Fprintf(&b, "Proxy: %s\n", r.Proxy)
	}
	fmt.Fprintf(&b, "Verdict: %s — %s\n\n", r.Verdict, r.Summary)

	write := func(title string, ps []ProbeResult) {
		fmt.Fprintf(&b, "%s\n", title)
		for _, p := range ps {
			line := fmt.Sprintf("  %-28s %-26s %-5s", p.Label, p.Host, p.Stage)
			if p.HTTPStatus != 0 {
				line += fmt.Sprintf(" HTTP %d", p.HTTPStatus)
			}
			if p.NotTLS {
				line += " NOT-TLS"
			}
			if p.CertIssuer != "" {
				line += " cert=" + p.CertIssuer
			}
			if p.Error != "" {
				line += " err=" + truncate(p.Error, 120)
			}
			fmt.Fprintf(&b, "%s\n", line)
		}
		fmt.Fprintln(&b)
	}
	write("Antra hosts:", r.Antra)
	write("Controls:", r.Controls)

	fmt.Fprintf(&b, "SNI control (same address, different hostname):\n")
	switch {
	case !r.SNI.Ran:
		fmt.Fprintf(&b, "  skipped — %s\n", r.SNI.Skipped)
	case r.SNI.SpokeTLS:
		fmt.Fprintf(&b, "  %s as %q: answered with TLS\n", r.SNI.IP, r.SNI.ServerName)
	default:
		fmt.Fprintf(&b, "  %s as %q: did NOT answer with TLS (%s)\n",
			r.SNI.IP, r.SNI.ServerName, truncate(r.SNI.Error, 120))
	}
	// Stated in the report itself so nobody over-reads a clean result.
	fmt.Fprintf(&b, "\nNote: probed with Go's TLS stack, which is not the one the download\n"+
		"engine (Python) uses. A host that passes here can still fail there.\n")
	return b.String()
}

func truncate(s string, n int) string {
	if len(s) <= n {
		return s
	}
	return s[:n] + "…"
}
