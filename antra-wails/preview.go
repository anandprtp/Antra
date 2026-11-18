package main

// FR-5 (v2.0.0) — 30-second audio previews.
//
// Ported from Antra-Web's preview.js + /api/preview, but the provider split is
// DELIBERATELY DIFFERENT, because measurement from a desktop connection
// inverted two of the web build's findings (2026-09-10, Indian residential IP):
//
//   * Deezer by ISRC        4/6   (GBAYE6400209 and USUG11904206 return an
//                                  entry with no preview — exactly the region
//                                  skew the web build documented)
//   * Deezer /search        0/6   — and 0/5 for albums. Not a syntax problem:
//                                  every shape (plain, advanced, /search/track,
//                                  /search/album) returns HTTP 200, valid JSON,
//                                  no API error, and zero results.
//   * iTunes /search        5/6   — and it covered BOTH ISRCs Deezer missed.
//   * iTunes /lookup        6/6   previewable tracks for an album id.
//
// So the web build's decision to reject the iTunes fallback was right for the
// VPS (403/429 there) and wrong for desktop: here iTunes is what takes coverage
// from 4/6 to 6/6, and it is the ONLY route for a Discover album card, since
// Deezer search returns nothing to search with.
//
// The lookup runs in Go, not the Python backend. Spawning the ~70 MB
// PyInstaller binary per preview click would cost seconds; an in-process GET
// costs ~500 ms. The web version used Python only because the web app *is* a
// Python server.
//
// Audio itself is fetched by the webview straight from the provider's CDN —
// never through Antra and never through our own mirrors. A mirror serves the
// WHOLE track: using one for a preview would burn a real request against our
// Tidal/Qobuz/Amazon accounts and hand the client a 40 MB FLAC to play 30
// seconds of.

import (
	"encoding/json"
	"fmt"
	"net/http"
	"net/url"
	"regexp"
	"strconv"
	"strings"
	"sync"
	"time"
)

const (
	previewUA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 " +
		"(KHTML, like Gecko) Chrome/124.0"
	previewHTTPTimeout = 8 * time.Second

	// Deezer signs its CDN URL with hdnea=exp=<unix>; measured at 900 s.
	// Anything closer to the edge than this is re-fetched rather than handed
	// out, because a dead signature returns 403 HTML and would present to the
	// user as "preview is broken".
	previewSigSafety = 120 * time.Second
	previewSigTTL    = 900 * time.Second

	// A miss is never cached as a lasting fact — the same definitive-vs-transient
	// split this project draws for the Qobuz preview cache and the Tidal
	// negative cache.
	previewNegTTL = 5 * time.Minute

	previewMaxEntries = 2000

	// Click-initiated, one at a time, so the sustained rate is ~1 call per
	// click. This only exists to stop a user click-dragging down a tracklist
	// from bursting: measured, 25 rapid iTunes calls in 16 s were all served,
	// but the VPS has been 403'd by iTunes under burst before.
	previewMinInterval = 200 * time.Millisecond
)

var previewClient = &http.Client{Timeout: previewHTTPTimeout}

// Base URLs are variables, not literals, so the tests can drive the REAL
// provider functions against a server that reproduces each API's actual
// Content-Type. Without that, the per-provider content-type decision is only
// reachable by passing the flag by hand, and a call site could flip it while
// every test still passed — which is exactly how the iTunes fallback silently
// contributed 0/6 in the first cut.
var (
	deezerAPIBase = "https://api.deezer.com"
	itunesAPIBase = "https://itunes.apple.com"
)

// ── Cache ───────────────────────────────────────────────────────────────────

type previewEntry struct {
	url    string
	source string
	title  string
	artist string
	// exp is the moment the signed URL dies. ZERO means "does not expire":
	// measured, an iTunes preview URL carries no query string at all, so there
	// is no signature to go stale.
	exp   time.Time
	miss  bool
	until time.Time // negative-cache expiry
}

var (
	previewMu    sync.Mutex
	previewCache = map[string]previewEntry{}
	previewOrder []string // insertion order, for oldest-first eviction

	previewLastCall time.Time
	previewCallMu   sync.Mutex
)

func previewCacheGet(key string) (previewEntry, bool) {
	previewMu.Lock()
	defer previewMu.Unlock()
	e, ok := previewCache[key]
	return e, ok
}

func previewCachePut(key string, e previewEntry) {
	previewMu.Lock()
	defer previewMu.Unlock()
	if _, exists := previewCache[key]; !exists {
		previewOrder = append(previewOrder, key)
	}
	previewCache[key] = e
	if len(previewCache) > previewMaxEntries {
		// Partial oldest-first eviction, not a full clear: a full clear would
		// leave a busy session permanently cache-cold.
		drop := len(previewCache) / 10
		for i := 0; i < drop && i < len(previewOrder); i++ {
			delete(previewCache, previewOrder[i])
		}
		previewOrder = previewOrder[min(drop, len(previewOrder)):]
	}
}

func min(a, b int) int {
	if a < b {
		return a
	}
	return b
}

// previewPace serialises upstream calls with a small floor between them.
func previewPace() {
	previewCallMu.Lock()
	defer previewCallMu.Unlock()
	if wait := previewMinInterval - time.Since(previewLastCall); wait > 0 {
		time.Sleep(wait)
	}
	previewLastCall = time.Now()
}

// ── HTTP ────────────────────────────────────────────────────────────────────

// Remember a Deezer edge block briefly so every preview does not pay for a
// doomed call, but re-probe often enough to notice it lifting. Deezer's API
// always answers JSON, so a NON-JSON body is definitionally the edge block
// documented in deezer_proxy.py — not an answer about the track.
var (
	dzBlockedUntil time.Time
	dzBlockMu      sync.Mutex
)

func dzBlocked() bool {
	dzBlockMu.Lock()
	defer dzBlockMu.Unlock()
	return time.Now().Before(dzBlockedUntil)
}

func noteDzBlock(blocked bool) {
	dzBlockMu.Lock()
	defer dzBlockMu.Unlock()
	if blocked {
		dzBlockedUntil = time.Now().Add(10 * time.Minute)
	} else {
		dzBlockedUntil = time.Time{}
	}
}

// previewGetJSON fetches and decodes a JSON body.
//
// `requireJSONContentType` is NOT cosmetic and is deliberately per-provider.
// For Deezer it is the edge-block detector: that API always answers
// application/json, so a non-JSON body means the edge refused us rather than
// answering about the track. For iTunes it must be OFF — measured 2026-09-10,
// itunes.apple.com/search and /lookup both answer
// `Content-Type: text/javascript; charset=utf-8`, so requiring JSON there
// rejects every single response. That is exactly what happened when this
// helper was first written with one shared check: the iTunes fallback
// contributed 0/6 through Go while a direct probe from the same machine got
// 5/6, and every unit test still passed.
func previewGetJSON(endpoint string, params url.Values, out interface{}, requireJSONContentType bool) error {
	previewPace()
	full := endpoint
	if len(params) > 0 {
		full += "?" + params.Encode()
	}
	req, err := http.NewRequest("GET", full, nil)
	if err != nil {
		return err
	}
	req.Header.Set("User-Agent", previewUA)
	resp, err := previewClient.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode != 200 {
		return fmt.Errorf("http %d", resp.StatusCode)
	}
	if requireJSONContentType &&
		!strings.Contains(strings.ToLower(resp.Header.Get("Content-Type")), "json") {
		return fmt.Errorf("non-json response")
	}
	return json.NewDecoder(resp.Body).Decode(out)
}

// ── Deezer ──────────────────────────────────────────────────────────────────

type dzTrack struct {
	ID      json.Number `json:"id"`
	Title   string      `json:"title"`
	Preview string      `json:"preview"`
	Artist  struct {
		Name string `json:"name"`
	} `json:"artist"`
	Error map[string]interface{} `json:"error"`
}

var reHdnea = regexp.MustCompile(`[?&]hdnea=exp=(\d+)`)

// previewSigExp reads the unix expiry out of a Deezer CDN URL, or zero when the
// shape ever changes — in which case the caller falls back to a nominal TTL
// rather than treating the URL as immortal.
func previewSigExp(u string) time.Time {
	m := reHdnea.FindStringSubmatch(u)
	if m == nil {
		return time.Time{}
	}
	n, err := strconv.ParseInt(m[1], 10, 64)
	if err != nil {
		return time.Time{}
	}
	return time.Unix(n, 0)
}

func deezerByISRC(isrc string) *dzTrack {
	if isrc == "" || dzBlocked() {
		return nil
	}
	var t dzTrack
	if err := previewGetJSON(deezerAPIBase+"/track/isrc:"+url.PathEscape(isrc), nil, &t, true); err != nil {
		if strings.Contains(err.Error(), "non-json") {
			noteDzBlock(true)
		}
		return nil
	}
	noteDzBlock(false)
	if t.Error != nil || t.Preview == "" {
		return nil
	}
	return &t
}

func deezerByID(id string) *dzTrack {
	if id == "" || dzBlocked() {
		return nil
	}
	var t dzTrack
	if err := previewGetJSON(deezerAPIBase+"/track/"+url.PathEscape(id), nil, &t, true); err != nil {
		return nil
	}
	if t.Error != nil || t.Preview == "" {
		return nil
	}
	return &t
}

// ── iTunes ──────────────────────────────────────────────────────────────────

type itResult struct {
	WrapperType    string `json:"wrapperType"`
	Kind           string `json:"kind"`
	TrackName      string `json:"trackName"`
	ArtistName     string `json:"artistName"`
	CollectionName string `json:"collectionName"`
	CollectionID   int64  `json:"collectionId"`
	PreviewURL     string `json:"previewUrl"`
}

type itResponse struct {
	ResultCount int        `json:"resultCount"`
	Results     []itResult `json:"results"`
}

func itunesSearch(params url.Values) []itResult {
	var r itResponse
	if err := previewGetJSON(itunesAPIBase+"/search", params, &r, false); err != nil {
		return nil
	}
	return r.Results
}

func itunesLookup(params url.Values) []itResult {
	var r itResponse
	if err := previewGetJSON(itunesAPIBase+"/lookup", params, &r, false); err != nil {
		return nil
	}
	return r.Results
}

// ── Matching ────────────────────────────────────────────────────────────────

var (
	reBracketed = regexp.MustCompile(`\([^)]*\)|\[[^\]]*\]`)
	reApostroph = regexp.MustCompile("[’'`]")
	reNonAlnum  = regexp.MustCompile(`[^a-z0-9]+`)
)

// previewNorm is deliberately NOT the resolver's matcher: this gates a
// 30-second preview, not what gets written to disk, and importing the
// download-path matcher would couple a cosmetic feature to BUG-1's acceptance
// logic. Apostrophes are DELETED rather than spaced, because catalogues
// disagree on "Don't" vs "Dont" and spacing makes those two never match.
func previewNorm(s string) string {
	s = reBracketed.ReplaceAllString(strings.ToLower(s), " ")
	s = reApostroph.ReplaceAllString(s, "")
	return strings.TrimSpace(reNonAlnum.ReplaceAllString(s, " "))
}

func previewLooseMatch(a, b string) bool {
	na, nb := previewNorm(a), previewNorm(b)
	if na == "" || nb == "" {
		return false
	}
	return na == nb || strings.Contains(na, nb) || strings.Contains(nb, na)
}

// itunesPickSong verifies BOTH title and artist before accepting a search hit.
//
// The web build checked only the title, because it passed the artist inside
// Deezer's query so the API had already constrained it. iTunes takes one free
// -text `term` and happily returns covers and tribute acts, so an unverified
// first hit would attach a stranger's audio to the row — the BUG-1 failure mode
// in a new place.
func itunesPickSong(res []itResult, title, artist string) *itResult {
	for i := range res {
		r := &res[i]
		if r.PreviewURL == "" {
			continue
		}
		if !previewLooseMatch(title, r.TrackName) {
			continue
		}
		if artist != "" && !previewLooseMatch(artist, r.ArtistName) {
			continue
		}
		return r
	}
	return nil
}

// ── Lookup ──────────────────────────────────────────────────────────────────

type previewHit struct {
	URL       string
	Source    string
	Title     string
	Artist    string
	ExpiresIn int
}

func previewFromEntry(e previewEntry) previewHit {
	exp := 0
	if !e.exp.IsZero() {
		exp = int(time.Until(e.exp).Seconds())
		if exp < 0 {
			exp = 0
		}
	}
	return previewHit{URL: e.url, Source: e.source, Title: e.title,
		Artist: e.artist, ExpiresIn: exp}
}

// previewCached returns a usable cached hit, or nil. It also reports whether a
// live negative entry says "asked recently, and there is none".
func previewCached(key string) (*previewHit, bool) {
	e, ok := previewCacheGet(key)
	if !ok {
		return nil, false
	}
	now := time.Now()
	if e.miss {
		if now.Before(e.until) {
			return nil, true // negative still valid
		}
		return nil, false
	}
	// exp zero == never expires (iTunes). Otherwise honour the safety margin.
	if e.exp.IsZero() || e.exp.Sub(now) > previewSigSafety {
		h := previewFromEntry(e)
		return &h, false
	}
	return nil, false
}

func previewStoreMiss(key string) {
	previewCachePut(key, previewEntry{miss: true, until: time.Now().Add(previewNegTTL)})
}

func previewStoreHit(key string, h previewHit, exp time.Time) {
	previewCachePut(key, previewEntry{
		url: h.URL, source: h.Source, title: h.Title, artist: h.Artist, exp: exp,
	})
}

// lookupTrackPreview resolves one tracklist row.
//
// Deezer-by-ISRC is tried FIRST even though iTunes scored higher overall,
// because an ISRC is an identification rather than a match: it cannot attach
// the wrong recording to the row. iTunes search is a fuzzy match and needs the
// title+artist gate above, so it is the fallback rather than the primary.
func lookupTrackPreview(isrc, title, artist string) *previewHit {
	key := previewTrackKey(isrc, title, artist)
	if key == "" {
		return nil
	}
	if hit, negative := previewCached(key); hit != nil || negative {
		return hit
	}

	if t := deezerByISRC(isrc); t != nil {
		exp := previewSigExp(t.Preview)
		if exp.IsZero() {
			exp = time.Now().Add(previewSigTTL)
		}
		h := previewHit{URL: t.Preview, Source: "deezer",
			Title: t.Title, Artist: t.Artist.Name}
		previewStoreHit(key, h, exp)
		h.ExpiresIn = int(time.Until(exp).Seconds())
		return &h
	}

	if title != "" {
		term := strings.TrimSpace(artist + " " + title)
		res := itunesSearch(url.Values{
			"term": {term}, "media": {"music"}, "entity": {"song"}, "limit": {"5"},
		})
		if r := itunesPickSong(res, title, artist); r != nil {
			h := previewHit{URL: r.PreviewURL, Source: "itunes",
				Title: r.TrackName, Artist: r.ArtistName}
			// iTunes URLs carry no query string, so there is no signature and
			// nothing to expire — measured 2026-09-10.
			previewStoreHit(key, h, time.Time{})
			return &h
		}
	}

	previewStoreMiss(key)
	return nil
}

var reAppleAlbumID = regexp.MustCompile(`music\.apple\.com/[^/]+/album/(?:[^/]+/)?(\d+)`)

// appleAlbumID pulls the catalog album id out of a Discover card's URL. Apple
// discovery builds these as music.apple.com/{storefront}/album/{id}, and that
// id IS iTunes' collectionId — so the album route can be an exact lookup rather
// than a fuzzy search.
func appleAlbumID(u string) string {
	if m := reAppleAlbumID.FindStringSubmatch(u); m != nil {
		return m[1]
	}
	return ""
}

// lookupAlbumPreview resolves a Discover album card to a representative track.
//
// Deezer has NO usable route here: its /search returns zero results for every
// query shape from a desktop connection (measured 0/5 for albums), and an album
// card carries no ISRC to identify with.
func lookupAlbumPreview(pageURL, name, artist string) *previewHit {
	key := previewAlbumKey(pageURL, name, artist)
	if key == "" {
		return nil
	}
	if hit, negative := previewCached(key); hit != nil || negative {
		return hit
	}

	// 1. Exact: an Apple-sourced card carries the collectionId.
	if id := appleAlbumID(pageURL); id != "" {
		if r := firstPreviewableSong(itunesLookup(url.Values{
			"id": {id}, "entity": {"song"}, "limit": {"12"},
		})); r != nil {
			return previewAlbumHit(key, r)
		}
	}

	// 2. Fallback: a Spotify-sourced card has no Apple id, so search for the
	//    album, then look up its songs. Verified on the returned album's own
	//    name+artist so a same-titled release by someone else is not accepted.
	if name != "" {
		albums := itunesSearch(url.Values{
			"term": {strings.TrimSpace(artist + " " + name)}, "media": {"music"},
			"entity": {"album"}, "limit": {"5"},
		})
		for i := range albums {
			al := &albums[i]
			if !previewLooseMatch(name, al.CollectionName) {
				continue
			}
			if artist != "" && !previewLooseMatch(artist, al.ArtistName) {
				continue
			}
			if r := firstPreviewableSong(itunesLookup(url.Values{
				"id":     {strconv.FormatInt(al.CollectionID, 10)},
				"entity": {"song"}, "limit": {"12"},
			})); r != nil {
				return previewAlbumHit(key, r)
			}
			break // the top verified album is the answer; do not walk the rest
		}
	}

	previewStoreMiss(key)
	return nil
}

func firstPreviewableSong(res []itResult) *itResult {
	for i := range res {
		if res[i].Kind == "song" && res[i].PreviewURL != "" {
			return &res[i]
		}
	}
	return nil
}

func previewAlbumHit(key string, r *itResult) *previewHit {
	h := previewHit{URL: r.PreviewURL, Source: "itunes",
		Title: r.TrackName, Artist: r.ArtistName}
	previewStoreHit(key, h, time.Time{})
	return &h
}

func previewTrackKey(isrc, title, artist string) string {
	if s := strings.TrimSpace(isrc); s != "" {
		return "i:" + strings.ToUpper(s)
	}
	nt, na := previewNorm(title), previewNorm(artist)
	if nt == "" {
		return ""
	}
	return "t:" + nt + "|" + na
}

func previewAlbumKey(pageURL, name, artist string) string {
	if id := appleAlbumID(pageURL); id != "" {
		return "aid:" + id
	}
	nn, na := previewNorm(name), previewNorm(artist)
	if nn == "" {
		return ""
	}
	return "a:" + nn + "|" + na
}

// ── Bindings ────────────────────────────────────────────────────────────────

func previewJSON(h *previewHit) string {
	if h == nil {
		return `{"error":"No preview available for this track."}`
	}
	out, _ := json.Marshal(map[string]interface{}{
		"url":        h.URL,
		"source":     h.Source,
		"title":      h.Title,
		"artist":     h.Artist,
		"expires_in": h.ExpiresIn,
	})
	return string(out)
}

// PreviewTrack returns a 30-second preview URL for one tracklist row.
// Returns {"url":...,"source":...,"expires_in":N} or {"error":"..."}.
func (a *App) PreviewTrack(isrc, title, artist string) string {
	if strings.TrimSpace(isrc) == "" && strings.TrimSpace(title) == "" {
		return `{"error":"No track information to look up."}`
	}
	return previewJSON(lookupTrackPreview(
		strings.TrimSpace(isrc), strings.TrimSpace(title), strings.TrimSpace(artist)))
}

// PreviewAlbum returns a preview URL representing a Discover album card — the
// first track of the album that has one.
func (a *App) PreviewAlbum(pageURL, name, artist string) string {
	if strings.TrimSpace(name) == "" && appleAlbumID(pageURL) == "" {
		return `{"error":"No album information to look up."}`
	}
	return previewJSON(lookupAlbumPreview(
		strings.TrimSpace(pageURL), strings.TrimSpace(name), strings.TrimSpace(artist)))
}
