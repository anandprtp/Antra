package main

// Issue #72 — the network diagnostic.
//
// The detection that everything here turns on is "the peer answered with
// something that is not TLS" — OpenSSL calls it WRONG_VERSION_NUMBER, Go calls
// it tls.RecordHeaderError. That condition is REPRODUCED LOCALLY rather than
// asserted against a hand-written error string: a plain TCP listener that
// speaks HTTP is exactly what an intercepting middlebox looks like to the
// client, so `TestPlaintextPeerIsDetectedAsNotTLS` is the reported bug standing
// in front of us.
//
// Every detector test carries a control that must come out the OTHER way,
// because a `looksNotTLS` that simply returned true would pass all of them.

import (
	"context"
	"crypto/rand"
	"crypto/rsa"
	"crypto/tls"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/json"
	"errors"
	"math/big"
	"net"
	"strings"
	"testing"
	"time"
)

// ── local servers ────────────────────────────────────────────────────────────

// plaintextListener answers every connection with an HTTP response and never
// speaks TLS — a captive portal, a block page, or an http:// service behind an
// https:// URL.
func plaintextListener(t *testing.T) string {
	t.Helper()
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("listen: %v", err)
	}
	t.Cleanup(func() { _ = ln.Close() })
	go func() {
		for {
			c, err := ln.Accept()
			if err != nil {
				return
			}
			go func(c net.Conn) {
				defer c.Close()
				// Read the ClientHello first, then answer, then linger before
				// closing. The first version wrote and closed immediately,
				// which reached the client as a CONNECTION RESET rather than
				// as a non-TLS reply — Go reports that as `wsarecv: ... was
				// aborted`, not as a record-header error, and the test failed
				// for the wrong reason. A real interceptor answers and keeps
				// the socket up, which is what is reproduced here.
				buf := make([]byte, 1024)
				_ = c.SetReadDeadline(time.Now().Add(2 * time.Second))
				_, _ = c.Read(buf)
				_, _ = c.Write([]byte("HTTP/1.1 302 Found\r\nLocation: http://portal.example/\r\n\r\n"))
				time.Sleep(300 * time.Millisecond)
			}(c)
		}
	}()
	return ln.Addr().String()
}

func selfSignedCert(t *testing.T, name string) tls.Certificate {
	t.Helper()
	key, err := rsa.GenerateKey(rand.Reader, 2048)
	if err != nil {
		t.Fatalf("key: %v", err)
	}
	tmpl := x509.Certificate{
		SerialNumber: big.NewInt(1),
		Subject:      pkix.Name{CommonName: name},
		NotBefore:    time.Now().Add(-time.Hour),
		NotAfter:     time.Now().Add(time.Hour),
		DNSNames:     []string{name},
	}
	der, err := x509.CreateCertificate(rand.Reader, &tmpl, &tmpl, &key.PublicKey, key)
	if err != nil {
		t.Fatalf("cert: %v", err)
	}
	return tls.Certificate{Certificate: [][]byte{der}, PrivateKey: key}
}

// tlsListener speaks real TLS. It is the control: a genuine TLS peer that a
// client may still reject (untrusted certificate) must never be mistaken for a
// plaintext interceptor.
func tlsListener(t *testing.T) string {
	t.Helper()
	cert := selfSignedCert(t, "local.test")
	ln, err := tls.Listen("tcp", "127.0.0.1:0", &tls.Config{Certificates: []tls.Certificate{cert}})
	if err != nil {
		t.Fatalf("tls listen: %v", err)
	}
	t.Cleanup(func() { _ = ln.Close() })
	go func() {
		for {
			c, err := ln.Accept()
			if err != nil {
				return
			}
			go func(c net.Conn) {
				defer c.Close()
				_ = c.(*tls.Conn).Handshake()
			}(c)
		}
	}()
	return ln.Addr().String()
}

// rejectingTLSListener is a real TLS implementation that refuses the handshake
// with an alert. For the SNI control that must count as "a TLS peer answered".
func rejectingTLSListener(t *testing.T) string {
	t.Helper()
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("listen: %v", err)
	}
	t.Cleanup(func() { _ = ln.Close() })
	go func() {
		for {
			c, err := ln.Accept()
			if err != nil {
				return
			}
			go func(c net.Conn) {
				defer c.Close()
				sc := tls.Server(c, &tls.Config{
					GetConfigForClient: func(*tls.ClientHelloInfo) (*tls.Config, error) {
						return nil, errors.New("no configuration for this name")
					},
				})
				_ = sc.Handshake()
			}(c)
		}
	}()
	return ln.Addr().String()
}

func handshakeTo(t *testing.T, addr, serverName string) error {
	t.Helper()
	conn, err := net.DialTimeout("tcp", addr, 5*time.Second)
	if err != nil {
		t.Fatalf("dial %s: %v", addr, err)
	}
	defer conn.Close()
	_ = conn.SetDeadline(time.Now().Add(5 * time.Second))
	return tls.Client(conn, &tls.Config{ServerName: serverName}).Handshake()
}

// ── the detector, against real peers ─────────────────────────────────────────

func TestPlaintextPeerIsDetectedAsNotTLS(t *testing.T) {
	err := handshakeTo(t, plaintextListener(t), "anything.test")
	if err == nil {
		t.Fatal("handshake against a plaintext server unexpectedly succeeded")
	}
	if !looksNotTLS(err) {
		t.Errorf("looksNotTLS(%v) = false, want true — this is the reported failure", err)
	}
}

func TestRealTLSPeerIsNotReportedAsPlaintext(t *testing.T) {
	// CONTROL. The certificate is untrusted so the handshake still fails — but
	// it fails as TLS, and must not be reported as interception. Without this,
	// a looksNotTLS that always returned true would pass the test above.
	err := handshakeTo(t, tlsListener(t), "local.test")
	if err == nil {
		t.Fatal("expected an untrusted-certificate failure")
	}
	if looksNotTLS(err) {
		t.Errorf("a genuine TLS peer was reported as plaintext: %v", err)
	}
}

func TestTLSAlertCountsAsATLSAnswer(t *testing.T) {
	err := handshakeTo(t, rejectingTLSListener(t), "local.test")
	if err == nil {
		t.Fatal("expected the server to refuse")
	}
	if looksNotTLS(err) {
		t.Errorf("a TLS alert was reported as plaintext: %v", err)
	}
	if !isTLSAlert(err) {
		t.Errorf("isTLSAlert(%v) = false, want true", err)
	}
}

func TestLooksNotTLSIgnoresNil(t *testing.T) {
	if looksNotTLS(nil) || isTLSAlert(nil) {
		t.Error("nil must not be classified as a failure")
	}
}

// ── the SNI control ──────────────────────────────────────────────────────────

func TestSNIControlPlaintextPeerDoesNotSpeakTLS(t *testing.T) {
	c := runSNIControl(context.Background(), plaintextListener(t))
	if !c.Ran {
		t.Fatal("control did not run")
	}
	if c.SpokeTLS {
		t.Error("a plaintext peer was recorded as speaking TLS — this would flip the verdict from ip_filtered to sni_filtered")
	}
}

func TestSNIControlRefusingTLSPeerCountsAsTLS(t *testing.T) {
	// CONTROL, and the load-bearing half: Cloudflare will refuse a hostname it
	// does not serve on that address, and that refusal is the healthy answer.
	c := runSNIControl(context.Background(), rejectingTLSListener(t))
	if !c.SpokeTLS {
		t.Errorf("a refusing TLS peer was not counted as a TLS answer: %v", c.Error)
	}
}

func TestSNIControlSkippedWithNothingToDial(t *testing.T) {
	c := runSNIControl(context.Background(), sniControlAddr(""))
	if c.Ran || c.Skipped == "" {
		t.Errorf("expected a skip with a reason, got %+v", c)
	}
}

// ── the verdict ──────────────────────────────────────────────────────────────

func ok(label string) ProbeResult { return ProbeResult{Label: label, Stage: "ok", HTTPStatus: 200} }
func notTLS(label string) ProbeResult {
	return ProbeResult{Label: label, Stage: "tls", NotTLS: true, Error: "record header"}
}
func dnsFail(label string) ProbeResult {
	return ProbeResult{Label: label, Stage: "dns", Error: "no such host"}
}
func tcpFail(label string) ProbeResult {
	return ProbeResult{Label: label, Stage: "tcp", Error: "connection refused"}
}

func report(antra, controls []ProbeResult, sni SNIControl) DiagnosticReport {
	r := DiagnosticReport{Antra: antra, Controls: controls, SNI: sni}
	verdictFor(&r)
	return r
}

func TestVerdictAllHealthy(t *testing.T) {
	r := report([]ProbeResult{ok("a"), ok("b")}, []ProbeResult{ok("c")}, SNIControl{})
	if r.Verdict != "ok" {
		t.Errorf("verdict = %q, want ok", r.Verdict)
	}
}

func TestVerdictNoInternetBeatsEverythingElse(t *testing.T) {
	// The ordering is the design: with the controls down too, calling this a
	// block on Antra would send the user chasing the wrong thing.
	r := report(
		[]ProbeResult{notTLS("a"), notTLS("b")},
		[]ProbeResult{dnsFail("github"), dnsFail("cloudflare")},
		SNIControl{Ran: true, SpokeTLS: true},
	)
	if r.Verdict != "no_internet" {
		t.Errorf("verdict = %q, want no_internet", r.Verdict)
	}
	// CONTROL: with the controls healthy, the identical Antra failures give the
	// specific verdict — so the branch above is about the controls, not about
	// swallowing every failure.
	r = report(
		[]ProbeResult{notTLS("a"), notTLS("b")},
		[]ProbeResult{ok("github"), ok("cloudflare")},
		SNIControl{Ran: true, SpokeTLS: true},
	)
	if r.Verdict != "sni_filtered" {
		t.Errorf("with healthy controls, verdict = %q, want sni_filtered", r.Verdict)
	}
}

func TestVerdictSeparatesHostnameFilteringFromAddressFiltering(t *testing.T) {
	// This is the whole reason the SNI control exists: identical Antra
	// symptoms, and the ONLY difference is whether the same address answered
	// with TLS under a different name.
	antra := []ProbeResult{notTLS("tidal"), notTLS("qobuz")}
	controls := []ProbeResult{ok("github"), ok("cloudflare")}

	if got := report(antra, controls, SNIControl{Ran: true, SpokeTLS: true}).Verdict; got != "sni_filtered" {
		t.Errorf("same address answering under another name: verdict = %q, want sni_filtered", got)
	}
	if got := report(antra, controls, SNIControl{Ran: true, SpokeTLS: false}).Verdict; got != "ip_filtered" {
		t.Errorf("same address refusing every name: verdict = %q, want ip_filtered", got)
	}
	// With no control at all it must not guess between the two.
	if got := report(antra, controls, SNIControl{}).Verdict; got != "intercepted" {
		t.Errorf("no SNI control: verdict = %q, want the non-committal intercepted", got)
	}
}

func TestVerdictDNSAndRefusedConnections(t *testing.T) {
	controls := []ProbeResult{ok("github"), ok("cloudflare")}
	if got := report([]ProbeResult{dnsFail("a"), dnsFail("b")}, controls, SNIControl{}).Verdict; got != "dns_blocked" {
		t.Errorf("verdict = %q, want dns_blocked", got)
	}
	if got := report([]ProbeResult{tcpFail("a"), tcpFail("b")}, controls, SNIControl{}).Verdict; got != "connection_refused" {
		t.Errorf("verdict = %q, want connection_refused", got)
	}
	// One host down is a server problem, not a user problem, and must not be
	// reported as a block.
	if got := report([]ProbeResult{ok("a"), tcpFail("b")}, controls, SNIControl{}).Verdict; got != "partial" {
		t.Errorf("verdict = %q, want partial", got)
	}
}

func TestEveryVerdictCarriesAReadableSummary(t *testing.T) {
	controls := []ProbeResult{ok("github"), ok("cloudflare")}
	cases := []DiagnosticReport{
		report([]ProbeResult{ok("a")}, controls, SNIControl{}),
		report([]ProbeResult{notTLS("a")}, controls, SNIControl{Ran: true, SpokeTLS: true}),
		report([]ProbeResult{notTLS("a")}, controls, SNIControl{Ran: true}),
		report([]ProbeResult{dnsFail("a")}, controls, SNIControl{}),
		report([]ProbeResult{tcpFail("a")}, controls, SNIControl{}),
		report([]ProbeResult{notTLS("a")}, []ProbeResult{dnsFail("g"), dnsFail("c")}, SNIControl{}),
	}
	for _, r := range cases {
		if r.Verdict == "" || r.Summary == "" {
			t.Errorf("verdict %q has summary %q", r.Verdict, r.Summary)
		}
		if strings.Contains(r.Summary, "WRONG_VERSION") || strings.Contains(r.Summary, "tls.") {
			t.Errorf("summary leaks a raw error to the user: %q", r.Summary)
		}
	}
}

// ── HTTPS inspection ─────────────────────────────────────────────────────────

func issued(label, issuer string) ProbeResult {
	p := ok(label)
	p.CertIssuer = issuer
	return p
}

func TestInspectionDetectedWhenUnrelatedCAsCollapse(t *testing.T) {
	// api.github.com and www.cloudflare.com use different CAs in reality, so
	// one issuer across both means something re-signed them.
	got := detectInspection(
		[]ProbeResult{issued("github", "Kaspersky Personal Root"), issued("cloudflare", "Kaspersky Personal Root")},
		[]ProbeResult{issued("tidal", "Kaspersky Personal Root")},
	)
	if got != "Kaspersky Personal Root" {
		t.Errorf("detectInspection = %q, want the interceptor's name", got)
	}
}

func TestInspectionNotReportedForRealCAs(t *testing.T) {
	// CONTROL, and the regression this replaced: the first implementation was a
	// name allowlist, and a live run showed Cloudflare serving these hosts via
	// Google Trust Services intermediates "YE1"/"YE2" that were not on it — so a
	// healthy machine was reported as being intercepted.
	if got := detectInspection(
		[]ProbeResult{issued("github", "Sectigo Public Server Authentication CA DV E36"), issued("cloudflare", "WE1")},
		[]ProbeResult{issued("tidal", "YE2"), issued("apple", "YE2")},
	); got != "" {
		t.Errorf("healthy diverse CAs reported as inspection: %q", got)
	}
	// A single control cannot establish diversity, so it must stay silent
	// rather than guess.
	if got := detectInspection(
		[]ProbeResult{issued("github", "YE2")},
		[]ProbeResult{issued("tidal", "YE2")},
	); got != "" {
		t.Errorf("one control was enough to claim inspection: %q", got)
	}
	// Collapsed controls but a DIFFERENT Antra issuer is not a coherent
	// interception story either.
	if got := detectInspection(
		[]ProbeResult{issued("github", "Corp CA"), issued("cloudflare", "Corp CA")},
		[]ProbeResult{issued("tidal", "YE2")},
	); got != "" {
		t.Errorf("inconsistent issuers reported as inspection: %q", got)
	}
}

// ── the copied report ────────────────────────────────────────────────────────

func TestProxyCredentialsAreRedacted(t *testing.T) {
	got := redactProxy("http://alice:hunter2@proxy.corp:8080")
	if strings.Contains(got, "hunter2") || strings.Contains(got, "alice") {
		t.Errorf("credentials survived redaction: %q", got)
	}
	if !strings.Contains(got, "proxy.corp:8080") {
		t.Errorf("redaction lost the host, which is the useful part: %q", got)
	}
	// CONTROL: a proxy with no credentials must be reported verbatim.
	if got := redactProxy("http://proxy.corp:8080"); got != "http://proxy.corp:8080" {
		t.Errorf("credential-free proxy was altered: %q", got)
	}
}

func TestFormattedReportIsPasteableAndCarriesNoSecret(t *testing.T) {
	r := DiagnosticReport{
		GeneratedAt: "2026-09-26T00:00:00Z", OS: "linux", Arch: "amd64",
		Proxy: redactProxy("http://bob:s3cret@proxy.corp:8080"),
		Antra: []ProbeResult{{Label: "Tidal mirror", Host: "tidal.anandserver.cfd", Stage: "tls", NotTLS: true, Error: "record header error"}},
		Controls: []ProbeResult{
			{Label: "General internet (GitHub)", Host: "api.github.com", Stage: "ok", HTTPStatus: 200},
		},
		SNI: SNIControl{Ran: true, IP: "104.21.4.187:443", ServerName: sniControlName, SpokeTLS: true},
	}
	verdictFor(&r)
	blob, err := json.Marshal(r)
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	text := FormatDiagnosticReport(string(blob))

	for _, want := range []string{
		"Antra network diagnostic", "linux/amd64", "tidal.anandserver.cfd",
		"NOT-TLS", "api.github.com", sniControlName, r.Verdict,
	} {
		if !strings.Contains(text, want) {
			t.Errorf("report is missing %q:\n%s", want, text)
		}
	}
	if strings.Contains(text, "s3cret") {
		t.Error("the copied report carries a proxy password")
	}
	// The stack caveat must survive into the copied text: a clean Go result
	// does not prove the Python engine can connect.
	if !strings.Contains(text, "Python") {
		t.Error("the copied report drops the 'probed with Go's TLS stack' caveat")
	}
}

func TestFormatDiagnosticReportSurvivesGarbage(t *testing.T) {
	if got := FormatDiagnosticReport("not json at all"); got != "not json at all" {
		t.Errorf("unparseable input should be returned as-is, got %q", got)
	}
}

// ── sign-in error reporting (issue #72's literal title) ──────────────────────

func TestSignInFailureDescriptions(t *testing.T) {
	// Real errors from real peers, not hand-written strings.
	notTLSErr := handshakeTo(t, plaintextListener(t), "anything.test")
	if notTLSErr == nil {
		t.Fatal("expected the plaintext peer to break the handshake")
	}
	_, dnsErr := net.DefaultResolver.LookupHost(context.Background(),
		"no-such-host.antra-diagnostic-test.invalid")
	if dnsErr == nil {
		t.Skip("this resolver answers for .invalid, so there is no DNS failure to test")
	}

	interception := describeSignInFailure(notTLSErr)
	naming := describeSignInFailure(dnsErr)
	other := describeSignInFailure(errors.New("something else entirely"))

	// The defect was one message for every cause. Three causes, three answers.
	if interception == naming || naming == other || interception == other {
		t.Errorf("distinct failures share a message:\n  %q\n  %q\n  %q", interception, naming, other)
	}
	for _, m := range []string{interception, naming, other} {
		if m == "" {
			t.Error("an empty description reached the user")
		}
		// The old text sent people to check a connection that was working.
		if strings.Contains(strings.ToLower(m), "check your connection") {
			t.Errorf("the misleading original wording is back: %q", m)
		}
		// A raw transport error must not be pasted at the user either.
		if strings.Contains(m, "wsarecv") || strings.Contains(m, "lookup ") {
			t.Errorf("a raw error leaked into the user-facing text: %q", m)
		}
	}
	if !strings.Contains(strings.ToLower(naming), "dns") {
		t.Errorf("a name-resolution failure should name DNS: %q", naming)
	}
}
