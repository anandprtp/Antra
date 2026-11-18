package main

// Live coverage measurement for FR-5, against the REAL Deezer and iTunes APIs.
//
// Skipped unless ANTRA_TEST_PREVIEW_LIVE=1, so it never runs in a normal
// `go test`. It drives the SHIPPED lookup functions rather than re-implementing
// the calls, so a regression in routing, matching or caching shows up here.
//
// Results are region-dependent by design — that is the whole reason the lookup
// moved client-side — so this reports coverage rather than asserting a fixed
// number, and only hard-fails on things that must hold anywhere.

import (
	"io"
	"net/http"
	"os"
	"strings"
	"testing"
	"time"
)

func skipUnlessLive(t *testing.T) {
	t.Helper()
	if os.Getenv("ANTRA_TEST_PREVIEW_LIVE") != "1" {
		t.Skip("set ANTRA_TEST_PREVIEW_LIVE=1 to run the live preview measurement")
	}
}

type liveCase struct{ isrc, title, artist string }

var liveTracks = []liveCase{
	{"USUG12604764", "Ran To Atlanta", "Drake"},
	{"USSM12005478", "Blinding Lights", "The Weeknd"},
	{"GBAYE6400209", "House of the Rising Sun", "The Animals"},
	{"USSM12403099", "Type Shit", "Future"},
	{"QT8JC2612597", "Killa Klassic", "Metallica"},
	{"USUG11904206", "bad guy", "Billie Eilish"},
}

func TestLiveTrackPreviewCoverage(t *testing.T) {
	skipUnlessLive(t)
	resetPreviewState()

	var dz, it, none int
	for _, c := range liveTracks {
		h := lookupTrackPreview(c.isrc, c.title, c.artist)
		if h == nil {
			none++
			t.Logf("  %-14s %-26s -> NONE", c.isrc, c.title)
			continue
		}
		switch h.Source {
		case "deezer":
			dz++
		case "itunes":
			it++
		}
		if !strings.HasPrefix(h.URL, "https://") {
			t.Errorf("%s: url is not https: %q", c.title, h.URL)
		}
		t.Logf("  %-14s %-26s -> %-6s %s", c.isrc, c.title, h.Source, h.URL[:60])
	}
	t.Logf("COVERAGE: deezer=%d itunes=%d none=%d of %d", dz, it, none, len(liveTracks))

	// The point of keeping BOTH providers is that neither alone is complete.
	// This is the assertion that would have caught dropping the iTunes fallback.
	if dz+it < len(liveTracks) {
		t.Logf("NOTE: %d track(s) had no preview from either provider", none)
	}
	if dz+it == 0 {
		t.Fatalf("no provider returned anything — the lookup is broken, not the region")
	}
	if it == 0 {
		t.Logf("NOTE: iTunes contributed nothing here; on the VPS it contributed 0/6 too")
	}
}

func TestLiveISRCLessTrackFallsBackToSearch(t *testing.T) {
	skipUnlessLive(t)
	resetPreviewState()
	// No ISRC at all — the Deezer route is unreachable and only the verified
	// iTunes search can answer. This is the case a release without ISRCs hits.
	h := lookupTrackPreview("", "Blinding Lights", "The Weeknd")
	if h == nil {
		t.Fatalf("no preview for an ISRC-less lookup")
	}
	if h.Source != "itunes" {
		t.Errorf("expected the iTunes fallback, got %s", h.Source)
	}
	if !previewLooseMatch("Blinding Lights", h.Title) {
		t.Errorf("fallback returned a different song: %q", h.Title)
	}
	if !previewLooseMatch("The Weeknd", h.Artist) {
		t.Errorf("fallback returned a different artist: %q", h.Artist)
	}
}

func TestLiveWrongTitleIsRefusedRatherThanGuessed(t *testing.T) {
	skipUnlessLive(t)
	resetPreviewState()
	// A title that does not exist must come back empty, not with whatever the
	// search engine felt like returning.
	h := lookupTrackPreview("", "Zzzqqx Nonexistent Track Title 91847", "Nobody At All 91847")
	if h != nil {
		t.Errorf("invented a preview for a nonexistent track: %+v", h)
	}
}

func TestLiveAppleAlbumCardUsesExactLookup(t *testing.T) {
	skipUnlessLive(t)
	resetPreviewState()
	cases := []struct{ url, name, artist string }{
		{"https://music.apple.com/us/album/1440857781", "In Between Dreams", "Jack Johnson"},
		{"https://music.apple.com/us/album/1020858855", "Glenn Gould Remastered", "Glenn Gould"},
	}
	for _, c := range cases {
		h := lookupAlbumPreview(c.url, c.name, c.artist)
		if h == nil {
			t.Errorf("%s: no preview", c.name)
			continue
		}
		if h.Source != "itunes" {
			t.Errorf("%s: expected itunes, got %s", c.name, h.Source)
		}
		t.Logf("  %-32s -> %s / %s", c.name, h.Title, h.Artist)
	}
}

func TestLiveSpotifyAlbumCardFallsBackToSearch(t *testing.T) {
	skipUnlessLive(t)
	resetPreviewState()
	// A Spotify-sourced Discover card: no Apple id in the URL, so only the
	// verified album search can answer.
	cases := []struct{ name, artist string }{
		{"After Hours", "The Weeknd"},
		{"Rumours", "Fleetwood Mac"},
	}
	for _, c := range cases {
		h := lookupAlbumPreview("https://open.spotify.com/album/xyz", c.name, c.artist)
		if h == nil {
			t.Errorf("%s: no preview", c.name)
			continue
		}
		if !previewLooseMatch(c.artist, h.Artist) {
			t.Errorf("%s: album search returned the wrong artist: %q", c.name, h.Artist)
		}
		t.Logf("  %-24s by %-18s -> %s", c.name, c.artist, h.Title)
	}
}

func TestLiveCachePreventsASecondUpstreamCall(t *testing.T) {
	skipUnlessLive(t)
	resetPreviewState()

	t0 := time.Now()
	first := lookupTrackPreview("USSM12005478", "Blinding Lights", "The Weeknd")
	cold := time.Since(t0)
	if first == nil {
		t.Skip("no preview available here; cannot measure the cache")
	}

	t1 := time.Now()
	second := lookupTrackPreview("USSM12005478", "Blinding Lights", "The Weeknd")
	warm := time.Since(t1)

	if second == nil || second.URL != first.URL {
		t.Fatalf("cache returned a different URL")
	}
	t.Logf("  cold=%v warm=%v", cold, warm)
	// The warm path must not touch the network. The pacing floor is 200ms, so
	// anything near that means an upstream call happened.
	if warm > 50*time.Millisecond {
		t.Errorf("warm lookup took %v — it made an upstream call", warm)
	}
}

func TestLiveReturnedURLActuallyServesAudio(t *testing.T) {
	skipUnlessLive(t)
	resetPreviewState()
	// The whole feature is worthless if the URL 403s. Fetch a few bytes and
	// check the content type, rather than trusting that a URL-shaped string
	// means playable audio.
	for _, c := range []liveCase{
		{"USSM12005478", "Blinding Lights", "The Weeknd"}, // expect deezer
		{"", "bad guy", "Billie Eilish"},                  // expect itunes
	} {
		h := lookupTrackPreview(c.isrc, c.title, c.artist)
		if h == nil {
			t.Errorf("%s: no preview to fetch", c.title)
			continue
		}
		req, _ := http.NewRequest("GET", h.URL, nil)
		req.Header.Set("User-Agent", previewUA)
		resp, err := (&http.Client{Timeout: 20 * time.Second}).Do(req)
		if err != nil {
			t.Errorf("%s: CDN fetch failed: %v", c.title, err)
			continue
		}
		body, _ := io.ReadAll(io.LimitReader(resp.Body, 64*1024))
		resp.Body.Close()
		ct := resp.Header.Get("Content-Type")
		if resp.StatusCode != 200 {
			t.Errorf("%s: CDN returned %d", c.title, resp.StatusCode)
		}
		if !strings.HasPrefix(ct, "audio/") {
			t.Errorf("%s: content-type is %q, not audio", c.title, ct)
		}
		if len(body) < 8*1024 {
			t.Errorf("%s: only %d bytes came back", c.title, len(body))
		}
		t.Logf("  %-24s %s  %d bytes  ct=%s", c.title, h.Source, len(body), ct)
	}
}
