package main

// v2.0.0 PV-1 — resolve a link and show it before anything is downloaded.
//
// Until now pasting a link started a download. The user saw the tracklist only
// as it was already being written to their library, so a wrong link, a deluxe
// edition they did not want, or a 25-track album they wanted two songs from all
// had to be stopped mid-flight and cleaned up by hand.
//
// The flow is now Preview → Download now. Preview runs the backend with
// --preview-url, which resolves metadata and exits without ever building a
// LibraryOrganizer, so a preview cannot touch a single file on disk.
//
// Why a cache file rather than re-resolving the link on Download now: resolving
// is the slow half (25–35 s for a Qobuz album, minutes for a large playlist).
// --preview-url writes every resolved TrackMetadata to a private temp file and
// the download reads it back, so the user pays that cost once. It also means the
// tracks that download are exactly the ones they were shown — a re-fetch could
// return a changed playlist and silently download something else.
//
// The webview never sees that file's path. It gets an opaque id and hands it
// back; the path lookup happens here. A path travelling through the webview
// would be an arbitrary-file argument to the Python backend.
//
// Everything here is named releasePreview*, not preview*: preview.go already
// owns that prefix for the unrelated 30-second audio previews (FR-5).

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"strings"
	"sync"
	"time"
)

// A preview resolves as slowly as the slowest metadata source. The one-shot
// commands beside this one (discography, library) use 30–60 s, which a large
// playlist would blow through; this is generous on purpose, and the frontend
// shows its existing skeleton for the whole wait.
const releasePreviewTimeout = 6 * time.Minute

// Caches are small (~1 KB per track) but a user who previews link after link
// without downloading would otherwise leave one temp file per preview behind
// for the rest of the session. Keeping the last few covers going back to an
// earlier preview; anything older is deleted.
const releasePreviewKeep = 8

type releasePreviewEntry struct {
	id   string
	path string
}

type releasePreviewStore struct {
	mu      sync.Mutex
	entries []releasePreviewEntry // oldest first
}

func (p *releasePreviewStore) add(id, path string) {
	p.mu.Lock()
	defer p.mu.Unlock()
	p.entries = append(p.entries, releasePreviewEntry{id: id, path: path})
	for len(p.entries) > releasePreviewKeep {
		_ = os.Remove(p.entries[0].path)
		p.entries = p.entries[1:]
	}
}

func (p *releasePreviewStore) path(id string) (string, bool) {
	p.mu.Lock()
	defer p.mu.Unlock()
	for _, e := range p.entries {
		if e.id == id {
			return e.path, true
		}
	}
	return "", false
}

func (p *releasePreviewStore) clear() {
	p.mu.Lock()
	defer p.mu.Unlock()
	for _, e := range p.entries {
		_ = os.Remove(e.path)
	}
	p.entries = nil
}

func (a *App) releasePreviews() *releasePreviewStore {
	a.releasePreviewOnce.Do(func() { a.releasePreviewStore = &releasePreviewStore{} })
	return a.releasePreviewStore
}

// PreviewRelease resolves one link's metadata and tracklist without downloading.
//
// Returns the preview object with a `preview_id` added, or {"error":"..."} —
// the same contract as GetArtistDiscography, so the frontend handles failure
// the way it already does everywhere else.
func (a *App) PreviewRelease(url string) string {
	url = strings.TrimSpace(url)
	if url == "" {
		return `{"error":"No link was given."}`
	}
	// Reachable with raw text: the Album Availability box and the Export button
	// have no http-prefix filter, and this is the backend boundary either way.
	if err := validateBackendLinks([]string{url}); err != nil {
		return releasePreviewErrorJSON(err.Error())
	}

	cacheFile, err := os.CreateTemp("", "antra-preview-*.json")
	if err != nil {
		return releasePreviewErrorJSON(fmt.Sprintf("could not create a preview cache file: %v", err))
	}
	cachePath := cacheFile.Name()
	cacheFile.Close()

	ctx, cancel := context.WithTimeout(context.Background(), releasePreviewTimeout)
	defer cancel()

	out, runErr := a.runReleasePreviewBackend(ctx, url, cachePath)
	if ctx.Err() == context.DeadlineExceeded {
		os.Remove(cachePath)
		return `{"error":"Timed out resolving that link."}`
	}
	if runErr != nil && len(bytes.TrimSpace(out)) == 0 {
		os.Remove(cachePath)
		return releasePreviewErrorJSON(runErr.Error())
	}

	data, perr := extractReleasePreview(out)
	if perr != nil {
		os.Remove(cachePath)
		return releasePreviewErrorJSON(perr.Error())
	}

	// A link with no preview (a podcast) wrote no cache and needs no id; hand the
	// verdict straight back so the frontend can queue it the old way.
	if unsupported, _ := data["unsupported"].(bool); unsupported {
		os.Remove(cachePath)
		encoded, merr := json.Marshal(data)
		if merr != nil {
			return releasePreviewErrorJSON(merr.Error())
		}
		return string(encoded)
	}

	// Without the cache the download would have nothing to read, so refuse the
	// preview rather than hand the frontend an id that cannot be downloaded.
	if st, serr := os.Stat(cachePath); serr != nil || st.Size() == 0 {
		os.Remove(cachePath)
		return `{"error":"The preview resolved but its track cache was not written."}`
	}

	id := newQueueID() // same alphabet validQueueID accepts
	a.releasePreviews().add(id, cachePath)
	data["preview_id"] = id

	encoded, merr := json.Marshal(data)
	if merr != nil {
		os.Remove(cachePath)
		return releasePreviewErrorJSON(merr.Error())
	}
	return string(encoded)
}

// runReleasePreviewBackend prefers the bundled binary and falls back to the
// source tree, exactly as the other one-shot commands do.
func (a *App) runReleasePreviewBackend(ctx context.Context, url, cachePath string) ([]byte, error) {
	args := []string{"--preview-url", url, "--preview-out", cachePath, "--config", getConfigPath()}

	if backend, err := ensureBundledBackend(); err == nil {
		cmd := exec.CommandContext(ctx, backend, args...)
		cmd.Env = append(os.Environ(), a.releasePreviewEnv()...)
		hideProcess(cmd)
		return cmd.Output()
	}

	pythonExe, _, workDir, env, err := a.resolveBackendCommand([]string{})
	if err != nil {
		return nil, err
	}
	cmd := exec.CommandContext(ctx, pythonExe, append([]string{"-m", "antra.json_cli"}, args...)...)
	cmd.Dir = workDir
	cmd.Env = env
	hideProcess(cmd)
	return cmd.Output()
}

// releasePreviewEnv carries the mirror key and endpoint overrides a metadata
// fetch needs. Tidal and Qobuz album metadata goes through the mirrors and 401s
// without the key — the bug fixed in 79d3513, which is exactly the shape of
// mistake a second spawn site invites.
func (a *App) releasePreviewEnv() []string {
	env := []string{"PYTHONUTF8=1"}
	if _, mirrorKey := a.supporterContext(); mirrorKey != "" {
		env = append(env, "ANTRA_MIRROR_API_KEY="+mirrorKey)
	}
	return append(env, endpointEnv()...)
}

// extractReleasePreview scans the backend's newline-JSON for the preview event.
// Scanning by line rather than parsing the whole blob is required, not tidiness:
// resolving a link logs progress to stdout first, so the output is several JSON
// documents and json.Unmarshal of the lot always fails.
func extractReleasePreview(out []byte) (map[string]interface{}, error) {
	var lastError string
	for _, line := range bytes.Split(out, []byte("\n")) {
		line = bytes.TrimSpace(line)
		if len(line) == 0 || line[0] != '{' {
			continue
		}
		var msg map[string]interface{}
		if err := json.Unmarshal(line, &msg); err != nil {
			continue
		}
		switch msg["type"] {
		case "preview_unsupported":
			// Not a failure: this link type has no preview (podcasts). The
			// frontend downloads it the old way rather than dead-ending.
			reason, _ := msg["reason"].(string)
			return map[string]interface{}{"unsupported": true, "reason": reason}, nil
		case "preview":
			data, ok := msg["data"].(map[string]interface{})
			if !ok {
				return nil, fmt.Errorf("the backend returned a preview with no data")
			}
			return data, nil
		case "error":
			if m, ok := msg["message"].(string); ok && m != "" {
				lastError = m
			}
		case "log":
			if msg["level"] == "error" {
				if m, ok := msg["message"].(string); ok && m != "" {
					lastError = m
				}
			}
		}
	}
	if lastError != "" {
		return nil, fmt.Errorf("%s", lastError)
	}
	return nil, fmt.Errorf("could not read that link")
}

func releasePreviewErrorJSON(msg string) string {
	b, err := json.Marshal(map[string]string{"error": msg})
	if err != nil {
		return `{"error":"unknown error"}`
	}
	return string(b)
}

// releasePreviewCache is the file --preview-url wrote: the link, the header the
// preview screen rendered, and every resolved track in full.
type releasePreviewCache struct {
	URL    string                   `json:"url"`
	Header map[string]interface{}   `json:"header"`
	Tracks []map[string]interface{} `json:"tracks"`
}

// EnqueuePreviewSelection queues the chosen tracks of a preview as one job.
//
// indices are positions in the previewed tracklist, which is the order the cache
// holds; the frontend has no other handle on a row. Out-of-range and duplicate
// indices are dropped rather than refused, so a stale checkbox cannot fail the
// whole download.
func (a *App) EnqueuePreviewSelection(previewID string, indices []int, title string) (QueueEnqueueResult, error) {
	cachePath, ok := a.releasePreviews().path(previewID)
	if !ok {
		return QueueEnqueueResult{}, fmt.Errorf("that preview is no longer available — load the link again")
	}
	raw, err := os.ReadFile(cachePath)
	if err != nil {
		return QueueEnqueueResult{}, fmt.Errorf("the previewed tracks could not be read — load the link again")
	}
	var cache releasePreviewCache
	if err := json.Unmarshal(raw, &cache); err != nil {
		return QueueEnqueueResult{}, fmt.Errorf("the previewed tracks could not be read — load the link again")
	}
	if len(cache.Tracks) == 0 {
		return QueueEnqueueResult{}, fmt.Errorf("that preview holds no tracks")
	}

	chosen := selectPreviewTracks(cache.Tracks, indices)
	if len(chosen) == 0 {
		return QueueEnqueueResult{}, fmt.Errorf("no tracks were selected")
	}

	payload, err := json.Marshal(releasePreviewCache{URL: cache.URL, Header: cache.Header, Tracks: chosen})
	if err != nil {
		return QueueEnqueueResult{}, err
	}
	if strings.TrimSpace(title) == "" {
		if t, ok := cache.Header["title"].(string); ok {
			title = t
		}
	}

	it, err := a.dq().addSelection(string(payload), title, len(chosen))
	if err != nil {
		return QueueEnqueueResult{}, err
	}
	return a.startOrWait(it)
}

// selectPreviewTracks keeps cache order and ignores anything out of range or
// repeated. An empty index list means the whole tracklist: that is what "nothing
// was deselected" means, and it saves the caller enumerating every row.
func selectPreviewTracks(tracks []map[string]interface{}, indices []int) []map[string]interface{} {
	if len(indices) == 0 {
		out := make([]map[string]interface{}, len(tracks))
		copy(out, tracks)
		return out
	}
	want := make(map[int]bool, len(indices))
	for _, i := range indices {
		if i >= 0 && i < len(tracks) {
			want[i] = true
		}
	}
	out := make([]map[string]interface{}, 0, len(want))
	for i, t := range tracks {
		if want[i] {
			out = append(out, t)
		}
	}
	return out
}

// selectionArgs points the backend at a selection payload.
func selectionArgs(baseArgs []string, path string) []string {
	args := append([]string{}, baseArgs...)
	return append(args, "--tracks-json", path)
}

// writeSelectionPayload writes the payload Python will read and delete. Shaped
// like writeImportPayload, and validated the same way: a job is not worth
// spawning a backend process for if its payload has no tracks.
func writeSelectionPayload(payload string) (string, error) {
	if err := validateSelectionPayload(payload); err != nil {
		return "", err
	}
	f, err := os.CreateTemp("", "antra-selection-*.json")
	if err != nil {
		return "", err
	}
	name := f.Name()
	if _, err := f.Write(bytes.TrimSpace([]byte(payload))); err != nil {
		f.Close()
		os.Remove(name)
		return "", err
	}
	if err := f.Close(); err != nil {
		os.Remove(name)
		return "", err
	}
	return name, nil
}

func validateSelectionPayload(payload string) error {
	trimmed := bytes.TrimSpace([]byte(payload))
	if len(trimmed) == 0 || trimmed[0] != '{' {
		return fmt.Errorf("selection payload is not a JSON object")
	}
	var probe struct {
		Tracks []json.RawMessage `json:"tracks"`
	}
	if err := json.Unmarshal(trimmed, &probe); err != nil {
		return fmt.Errorf("selection payload is not valid JSON: %w", err)
	}
	if len(probe.Tracks) == 0 {
		return fmt.Errorf("no tracks were selected")
	}
	return nil
}
