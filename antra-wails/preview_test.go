package main

// Tests for FR-5 (v2.0.0) — audio previews.
//
// The pure logic (matching gates, cache keys, signature parsing, album-id
// extraction, cache lifecycle) is tested here with no network. The real
// coverage measurement against Deezer and iTunes lives in
// preview_live_test.go, which is opt-in.

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"
)

func resetPreviewState() {
	previewMu.Lock()
	previewCache = map[string]previewEntry{}
	previewOrder = nil
	previewMu.Unlock()
	noteDzBlock(false)
}

// ── The gate that stops a stranger's audio reaching a row ───────────────────

func TestItunesPickSongRejectsWrongTitleOrArtist(t *testing.T) {
	res := []itResult{
		{Kind: "song", TrackName: "Bad Guy", ArtistName: "Tribute Players", PreviewURL: "u1"},
		{Kind: "song", TrackName: "Bad Guy (Karaoke)", ArtistName: "Billie Eilish", PreviewURL: "u2"},
		{Kind: "song", TrackName: "bad guy", ArtistName: "Billie Eilish", PreviewURL: "u3"},
	}
	got := itunesPickSong(res, "bad guy", "Billie Eilish")
	if got == nil {
		t.Fatalf("expected a match")
	}
	// u1 is the right title by the wrong artist; u2 normalises to the same
	// title (the bracket is stripped) so it is accepted on title AND artist —
	// which is correct behaviour for a 30-second preview and is asserted rather
	// than left ambiguous.
	if got.PreviewURL == "u1" {
		t.Errorf("accepted a tribute act: %s / %s", got.TrackName, got.ArtistName)
	}
	if got.ArtistName != "Billie Eilish" {
		t.Errorf("wrong artist accepted: %s", got.ArtistName)
	}
}

func TestItunesPickSongRefusesWhenNothingMatches(t *testing.T) {
	res := []itResult{
		{Kind: "song", TrackName: "Completely Different Song", ArtistName: "Someone Else", PreviewURL: "u"},
	}
	if got := itunesPickSong(res, "Blinding Lights", "The Weeknd"); got != nil {
		t.Errorf("should have refused, got %s / %s", got.TrackName, got.ArtistName)
	}
}

func TestItunesPickSongSkipsResultsWithNoPreview(t *testing.T) {
	res := []itResult{
		{Kind: "song", TrackName: "Blinding Lights", ArtistName: "The Weeknd", PreviewURL: ""},
		{Kind: "song", TrackName: "Blinding Lights", ArtistName: "The Weeknd", PreviewURL: "good"},
	}
	got := itunesPickSong(res, "Blinding Lights", "The Weeknd")
	if got == nil || got.PreviewURL != "good" {
		t.Errorf("should have skipped the preview-less hit, got %+v", got)
	}
}

func TestItunesPickSongWithNoArtistStillChecksTitle(t *testing.T) {
	res := []itResult{{Kind: "song", TrackName: "Some Other Track", ArtistName: "X", PreviewURL: "u"}}
	if itunesPickSong(res, "Blinding Lights", "") != nil {
		t.Errorf("an empty artist must not disable the title check")
	}
}

func TestPreviewLooseMatch(t *testing.T) {
	cases := []struct {
		a, b string
		want bool
	}{
		{"Don't Stop", "Dont Stop", true}, // apostrophes deleted, not spaced
		{"Blinding Lights", "blinding lights", true},
		{"Money", "Money (Remastered 2011)", true}, // bracket stripped
		{"Type Shit", "TYPE SHIT", true},
		{"Money", "Money For Nothing", true}, // containment — deliberate, it is a preview
		{"Money", "Sledgehammer", false},
		{"", "Anything", false},
		{"Anything", "", false},
	}
	for _, c := range cases {
		if got := previewLooseMatch(c.a, c.b); got != c.want {
			t.Errorf("previewLooseMatch(%q,%q) = %v, want %v", c.a, c.b, got, c.want)
		}
	}
}

// ── Apple album id extraction (the exact Discover route) ────────────────────

func TestAppleAlbumID(t *testing.T) {
	cases := map[string]string{
		"https://music.apple.com/us/album/1020858855":                "1020858855",
		"https://music.apple.com/gb/album/after-hours/1499385848":    "1499385848",
		"https://music.apple.com/in/album/some-name/694679421?i=123": "694679421",
		"https://open.spotify.com/album/4yP0hdKOZPNshxUOjY0cZj":      "",
		"https://music.apple.com/us/playlist/pl.abc123":              "",
		"": "",
	}
	for in, want := range cases {
		if got := appleAlbumID(in); got != want {
			t.Errorf("appleAlbumID(%q) = %q, want %q", in, got, want)
		}
	}
}

// ── Deezer signature ────────────────────────────────────────────────────────

func TestPreviewSigExp(t *testing.T) {
	u := "https://cdnt-preview.dzcdn.net/api/1/1/x.mp3?hdnea=exp=1789008604~acl=/api/1/1/x.mp3*"
	got := previewSigExp(u)
	if got.Unix() != 1789008604 {
		t.Errorf("got %v", got)
	}
	// iTunes URLs have no query string at all — zero means "never expires",
	// and the caller must not mistake that for "already expired".
	if !previewSigExp("https://audio-ssl.itunes.apple.com/x/mzaf_1.plus.aac.p.m4a").IsZero() {
		t.Errorf("an unsigned URL should yield the zero time")
	}
	if !previewSigExp("https://cdnt-preview.dzcdn.net/x.mp3?hdnea=exp=notanumber").IsZero() {
		t.Errorf("a malformed expiry should yield the zero time")
	}
}

// ── Cache keys ──────────────────────────────────────────────────────────────

func TestPreviewTrackKeyPrefersISRC(t *testing.T) {
	k := previewTrackKey("ussm12005478", "Blinding Lights", "The Weeknd")
	if k != "i:USSM12005478" {
		t.Errorf("ISRC key should win and be upper-cased, got %q", k)
	}
	// Same recording, different metadata spelling, same ISRC -> one entry.
	if previewTrackKey("USSM12005478", "blinding lights", "weeknd") != k {
		t.Errorf("ISRC key must not depend on title/artist")
	}
}

func TestPreviewTrackKeyFallsBackToTitleArtist(t *testing.T) {
	a := previewTrackKey("", "Blinding Lights", "The Weeknd")
	b := previewTrackKey("", "BLINDING LIGHTS", "the weeknd")
	if a == "" || a != b {
		t.Errorf("normalised title/artist key should be stable: %q vs %q", a, b)
	}
	if previewTrackKey("", "", "The Weeknd") != "" {
		t.Errorf("no ISRC and no title is not a lookupable key")
	}
}

func TestPreviewAlbumKeyPrefersAppleID(t *testing.T) {
	k := previewAlbumKey("https://music.apple.com/us/album/x/1499385848", "After Hours", "The Weeknd")
	if k != "aid:1499385848" {
		t.Errorf("got %q", k)
	}
	// Two same-named albums by different artists must not collide.
	x := previewAlbumKey("https://open.spotify.com/album/a", "Greatest Hits", "Queen")
	y := previewAlbumKey("https://open.spotify.com/album/b", "Greatest Hits", "ABBA")
	if x == y {
		t.Errorf("same album name by different artists collided: %q", x)
	}
}

// ── Cache lifecycle ─────────────────────────────────────────────────────────

func TestCachedHitHonoursTheSafetyMargin(t *testing.T) {
	resetPreviewState()
	// A Deezer URL with less than the safety margin left must NOT be served:
	// a dead signature returns 403 HTML and reads to the user as "preview is
	// broken".
	previewStoreHit("k", previewHit{URL: "u", Source: "deezer"},
		time.Now().Add(previewSigSafety/2))
	if hit, _ := previewCached("k"); hit != nil {
		t.Errorf("served a URL inside the safety margin")
	}
	resetPreviewState()
	previewStoreHit("k", previewHit{URL: "u", Source: "deezer"},
		time.Now().Add(previewSigTTL))
	hit, _ := previewCached("k")
	if hit == nil {
		t.Fatalf("a fresh signature should be served")
	}
	if hit.ExpiresIn <= 0 {
		t.Errorf("expires_in should be positive, got %d", hit.ExpiresIn)
	}
}

func TestItunesEntryNeverExpires(t *testing.T) {
	resetPreviewState()
	previewStoreHit("k", previewHit{URL: "u", Source: "itunes"}, time.Time{})
	hit, _ := previewCached("k")
	if hit == nil {
		t.Fatalf("an unsigned URL should stay usable")
	}
	if hit.ExpiresIn != 0 {
		t.Errorf("expires_in should be 0 (= no expiry), got %d", hit.ExpiresIn)
	}
}

func TestNegativeCacheIsBriefAndNotSticky(t *testing.T) {
	resetPreviewState()
	previewStoreMiss("k")
	if hit, neg := previewCached("k"); hit != nil || !neg {
		t.Errorf("a fresh miss should report negative")
	}
	// Force it stale — a miss must never become a lasting fact about the track.
	previewMu.Lock()
	e := previewCache["k"]
	e.until = time.Now().Add(-time.Second)
	previewCache["k"] = e
	previewMu.Unlock()
	if hit, neg := previewCached("k"); hit != nil || neg {
		t.Errorf("an expired miss must fall through to a fresh lookup")
	}
	if previewNegTTL > time.Hour {
		t.Errorf("negative TTL %v is far too long for a transient answer", previewNegTTL)
	}
}

func TestCacheEvictionIsBoundedAndPartial(t *testing.T) {
	resetPreviewState()
	for i := 0; i < previewMaxEntries+300; i++ {
		previewStoreHit(string(rune('a'+i%26))+strings.Repeat("x", i%50)+strconv.Itoa(i),
			previewHit{URL: "u", Source: "itunes"}, time.Time{})
	}
	previewMu.Lock()
	n := len(previewCache)
	previewMu.Unlock()
	if n > previewMaxEntries {
		t.Errorf("cache grew past its bound: %d", n)
	}
	if n == 0 {
		t.Errorf("eviction cleared everything — it must be partial, not a full wipe")
	}
}

func TestCacheIsConcurrencySafe(t *testing.T) {
	resetPreviewState()
	var wg sync.WaitGroup
	for i := 0; i < 40; i++ {
		wg.Add(1)
		go func(n int) {
			defer wg.Done()
			k := "k" + strconv.Itoa(n%7)
			previewStoreHit(k, previewHit{URL: "u", Source: "itunes"}, time.Time{})
			previewCached(k)
			previewCacheGet(k)
		}(i)
	}
	wg.Wait() // -race is what makes this meaningful
}

// ── Deezer edge-block backoff ───────────────────────────────────────────────

func TestDeezerBlockBackoffSkipsDoomedCalls(t *testing.T) {
	resetPreviewState()
	noteDzBlock(true)
	if !dzBlocked() {
		t.Fatalf("block was not recorded")
	}
	// While blocked, the Deezer route must short-circuit rather than pay for a
	// call that cannot succeed. A network call here would take seconds; this
	// returns instantly.
	start := time.Now()
	if got := deezerByISRC("USSM12005478"); got != nil {
		t.Errorf("returned a result while blocked")
	}
	if time.Since(start) > 100*time.Millisecond {
		t.Errorf("blocked path made a network call (%v)", time.Since(start))
	}
	noteDzBlock(false)
	if dzBlocked() {
		t.Errorf("block should clear when the edge answers again")
	}
}

// ── Content-type handling per provider ──────────────────────────────────────

// The bug this pins actually shipped in the first cut of preview.go: one shared
// "must be JSON" guard, correctly ported from the Deezer helper where a
// non-JSON body IS the edge-block signal, silently rejected every iTunes
// response — iTunes answers `text/javascript`. The Go path contributed 0/6
// while a direct probe from the same machine got 5/6, and every other unit test
// still passed.
func TestItunesTextJavascriptIsAccepted(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/javascript; charset=utf-8")
		_, _ = w.Write([]byte(`{"resultCount":1,"results":[{"kind":"song","trackName":"T","artistName":"A","previewUrl":"https://x/p.m4a"}]}`))
	}))
	defer srv.Close()

	var r itResponse
	if err := previewGetJSON(srv.URL, nil, &r, false); err != nil {
		t.Fatalf("text/javascript must be accepted when the flag is off: %v", err)
	}
	if len(r.Results) != 1 || r.Results[0].PreviewURL == "" {
		t.Errorf("body was not decoded: %+v", r)
	}
	// ...and the Deezer side must still reject it, because that is how an edge
	// block is detected there.
	if err := previewGetJSON(srv.URL, nil, &r, true); err == nil {
		t.Errorf("the Deezer guard must still reject a non-JSON content type")
	}
}

func TestDeezerNonJSONIsTreatedAsAnEdgeBlock(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/html")
		_, _ = w.Write([]byte("<html>Access Denied</html>"))
	}))
	defer srv.Close()
	var out map[string]any
	err := previewGetJSON(srv.URL, nil, &out, true)
	if err == nil || !strings.Contains(err.Error(), "non-json") {
		t.Errorf("expected a non-json error, got %v", err)
	}
}

// ── The CALL SITES must pass the right content-type flag ────────────────────
//
// TestItunesTextJavascriptIsAccepted proves the helper honours the flag; these
// prove the provider functions PASS the right one. The negative control caught
// that gap: flipping the flag at either call site left every other test green
// while reproducing the shipped bug exactly.

func TestItunesCallSitesAcceptTextJavascript(t *testing.T) {
	resetPreviewState()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		// The Content-Type iTunes really sends, measured 2026-09-10.
		w.Header().Set("Content-Type", "text/javascript; charset=utf-8")
		_, _ = w.Write([]byte(`{"resultCount":1,"results":[` +
			`{"wrapperType":"track","kind":"song","trackName":"Blinding Lights",` +
			`"artistName":"The Weeknd","collectionId":1,"previewUrl":"https://cdn/p.m4a"}]}`))
	}))
	defer srv.Close()

	old := itunesAPIBase
	itunesAPIBase = srv.URL
	defer func() { itunesAPIBase = old }()

	if got := itunesSearch(url.Values{"term": {"x"}}); len(got) != 1 {
		t.Fatalf("itunesSearch returned %d results — the call site rejects text/javascript", len(got))
	}
	if got := itunesLookup(url.Values{"id": {"1"}}); len(got) != 1 {
		t.Fatalf("itunesLookup returned %d results — the call site rejects text/javascript", len(got))
	}

	// And the whole track route must reach it.
	if h := lookupTrackPreview("", "Blinding Lights", "The Weeknd"); h == nil || h.Source != "itunes" {
		t.Errorf("the iTunes fallback did not fire: %+v", h)
	}
}

func TestDeezerCallSiteTreatsNonJSONAsABlock(t *testing.T) {
	resetPreviewState()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		// What an edge block looks like — HTML, not the API answering.
		w.Header().Set("Content-Type", "text/html")
		_, _ = w.Write([]byte("<html><title>Access Denied</title></html>"))
	}))
	defer srv.Close()

	old := deezerAPIBase
	deezerAPIBase = srv.URL
	defer func() { deezerAPIBase = old }()

	if got := deezerByISRC("USSM12005478"); got != nil {
		t.Errorf("an HTML body was accepted as a track answer: %+v", got)
	}
	if !dzBlocked() {
		t.Errorf("a non-JSON body must be recorded as an edge block so later previews skip it")
	}
	noteDzBlock(false)
}

// A JSON body from Deezer must of course still work — the guard must reject
// blocks, not the API.
func TestDeezerCallSiteAcceptsRealJSON(t *testing.T) {
	resetPreviewState()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json; charset=utf-8")
		_, _ = w.Write([]byte(`{"id":123,"title":"T","preview":"https://cdn/x.mp3?hdnea=exp=4102444800",` +
			`"artist":{"name":"A"}}`))
	}))
	defer srv.Close()

	old := deezerAPIBase
	deezerAPIBase = srv.URL
	defer func() { deezerAPIBase = old }()

	got := deezerByISRC("USSM12005478")
	if got == nil || got.Preview == "" {
		t.Fatalf("a valid JSON answer was rejected: %+v", got)
	}
	if dzBlocked() {
		t.Errorf("a valid answer must not be recorded as a block")
	}
}

// ── preview_volume must survive a round-trip, including 0 ──────────────────
//
// PreviewVolume is a *int for the reason the Config comments record: a plain
// int reads 0 (= silent) from any config.json written before FR-5, silently
// muting previews for every existing user. These pin both halves — an ABSENT
// key stays absent (so the frontend default applies), and an explicit 0 is
// preserved rather than being dropped by omitempty as if it were unset.

func TestPreviewVolumeRoundTrip(t *testing.T) {
	var cfg Config
	if err := json.Unmarshal([]byte(`{"download_path":"x"}`), &cfg); err != nil {
		t.Fatal(err)
	}
	if cfg.PreviewVolume != nil {
		t.Errorf("an absent key must stay nil so the 70%% default applies, got %v", *cfg.PreviewVolume)
	}

	for _, want := range []int{0, 25, 100} {
		v := want
		cfg.PreviewVolume = &v
		b, err := json.Marshal(cfg)
		if err != nil {
			t.Fatal(err)
		}
		if !strings.Contains(string(b), `"preview_volume":`+strconv.Itoa(want)) {
			t.Errorf("volume %d was not written to config.json", want)
		}
		var back Config
		if err := json.Unmarshal(b, &back); err != nil {
			t.Fatal(err)
		}
		if back.PreviewVolume == nil || *back.PreviewVolume != want {
			t.Errorf("volume %d did not survive the round-trip: %v", want, back.PreviewVolume)
		}
	}
}

// ── Binding guards ──────────────────────────────────────────────────────────

func TestBindingsRefuseEmptyInputWithoutNetwork(t *testing.T) {
	resetPreviewState()
	app := &App{}
	start := time.Now()
	if out := app.PreviewTrack("", "", ""); !strings.Contains(out, `"error"`) {
		t.Errorf("empty track input should error, got %s", out)
	}
	if out := app.PreviewAlbum("", "", ""); !strings.Contains(out, `"error"`) {
		t.Errorf("empty album input should error, got %s", out)
	}
	// Both guards must return before any HTTP happens — a.ctx is nil here, so
	// the test finishing at all is part of the proof.
	if time.Since(start) > 200*time.Millisecond {
		t.Errorf("guards made a network call (%v)", time.Since(start))
	}
}

func TestPreviewJSONShape(t *testing.T) {
	out := previewJSON(&previewHit{URL: "https://cdn/x.mp3", Source: "deezer",
		Title: "T", Artist: "A", ExpiresIn: 880})
	for _, want := range []string{`"url"`, `"source"`, `"deezer"`, `"expires_in"`, `880`} {
		if !strings.Contains(out, want) {
			t.Errorf("missing %s in %s", want, out)
		}
	}
	if !strings.Contains(previewJSON(nil), `"error"`) {
		t.Errorf("nil hit should render an error")
	}
}
