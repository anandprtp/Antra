package main

// v2.0.0 PV-1 tests. These cover the two pieces that decide whether the RIGHT
// tracks download: reading the preview event out of a noisy stdout, and mapping
// the user's checkbox indices onto cache rows.

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
)

func TestExtractReleasePreviewSkipsLogLines(t *testing.T) {
	// Exactly what the backend emitted in a live run: two log lines first. A
	// whole-blob json.Unmarshal fails on this, which is why the scan exists.
	out := []byte(`{"type": "log", "level": "info", "message": "[Config] Output format: lossless"}
{"type": "log", "level": "info", "message": "[Proxy] retrying with the built-in metadata key"}
{"type": "preview", "data": {"title": "Scorpion", "track_count": 25}}
`)
	data, err := extractReleasePreview(out)
	if err != nil {
		t.Fatalf("expected the preview to be found, got %v", err)
	}
	if data["title"] != "Scorpion" {
		t.Fatalf("wrong preview returned: %v", data)
	}
}

func TestExtractReleasePreviewSurfacesBackendError(t *testing.T) {
	out := []byte(`{"type": "log", "level": "info", "message": "resolving"}
{"type": "error", "message": "No tracks were found at that link."}
`)
	_, err := extractReleasePreview(out)
	if err == nil {
		t.Fatal("expected an error")
	}
	if err.Error() != "No tracks were found at that link." {
		t.Fatalf("the backend's message was not surfaced: %q", err.Error())
	}
}

// A podcast link has no preview. It must come back as a verdict the frontend can
// act on (queue it the old way), NOT as an error — otherwise previewing a podcast
// is a dead end. This is the exact line the built backend emits.
func TestExtractReleasePreviewUnsupported(t *testing.T) {
	out := []byte(`{"type": "preview_unsupported", "reason": "Podcast episodes and shows are downloaded without a preview."}`)
	data, err := extractReleasePreview(out)
	if err != nil {
		t.Fatalf("unsupported must not be an error: %v", err)
	}
	if unsupported, _ := data["unsupported"].(bool); !unsupported {
		t.Fatalf("the unsupported flag did not survive: %v", data)
	}
	if data["reason"] == "" {
		t.Fatal("the reason was dropped")
	}
}

func TestExtractReleasePreviewNoOutput(t *testing.T) {
	if _, err := extractReleasePreview(nil); err == nil {
		t.Fatal("empty output must be an error, not an empty preview")
	}
}

func rows(n int) []map[string]interface{} {
	out := make([]map[string]interface{}, n)
	for i := range out {
		out[i] = map[string]interface{}{"title": string(rune('a' + i))}
	}
	return out
}

func titles(rs []map[string]interface{}) string {
	s := ""
	for _, r := range rs {
		s += r["title"].(string)
	}
	return s
}

func TestSelectPreviewTracks(t *testing.T) {
	all := rows(5) // a b c d e
	cases := []struct {
		name    string
		indices []int
		want    string
	}{
		{"empty means everything", nil, "abcde"},
		{"a subset", []int{1, 3}, "bd"},
		// The order must come from the cache, not from the caller: the download
		// has to run in tracklist order whatever order the boxes were ticked in.
		{"cache order wins", []int{3, 0, 1}, "abd"},
		{"duplicates collapse", []int{2, 2, 2}, "c"},
		{"out of range is ignored", []int{0, 99, -1, 4}, "ae"},
		{"all out of range selects nothing", []int{7, 8}, ""},
	}
	for _, c := range cases {
		got := titles(selectPreviewTracks(all, c.indices))
		if got != c.want {
			t.Errorf("%s: got %q want %q", c.name, got, c.want)
		}
	}
}

func TestValidateSelectionPayload(t *testing.T) {
	if err := validateSelectionPayload(`{"tracks":[{"title":"x"}]}`); err != nil {
		t.Fatalf("a valid payload was refused: %v", err)
	}
	for _, bad := range []string{"", "[]", "{}", `{"tracks":[]}`, `{"tracks":`} {
		if err := validateSelectionPayload(bad); err == nil {
			t.Errorf("payload %q should have been refused", bad)
		}
	}
}

// A selection job must round-trip through the queue file the way an import does,
// including surviving a reload — that is what makes Download now safe to press
// and then close the app.
func TestQueueSelectionSurvivesReload(t *testing.T) {
	dir := t.TempDir()
	q := newDownloadQueue(dir)
	payload := `{"url":"https://open.spotify.com/album/x","header":{"title":"Scorpion"},"tracks":[{"title":"Nonstop"}]}`
	it, err := q.addSelection(payload, "Scorpion", 1)
	if err != nil {
		t.Fatalf("addSelection: %v", err)
	}
	if it.Kind != QueueKindSelection {
		t.Fatalf("wrong kind %q", it.Kind)
	}

	q2 := newDownloadQueue(dir)
	q2.load()
	snap := q2.snapshot()
	if len(snap.Items) != 1 {
		t.Fatalf("the selection did not survive the reload: %+v", snap.Items)
	}
	if snap.Items[0].Kind != QueueKindSelection || snap.Items[0].TrackCount != 1 {
		t.Fatalf("reloaded item lost its shape: %+v", snap.Items[0])
	}
	got, err := q2.readImportPayload(snap.Items[0].ID)
	if err != nil {
		t.Fatalf("payload unreadable after reload: %v", err)
	}
	var probe map[string]interface{}
	if err := json.Unmarshal([]byte(got), &probe); err != nil || probe["url"] == "" {
		t.Fatalf("payload came back wrong: %q", got)
	}
}

// Negative control for the kindHasPayload plumbing: removing a selection job must
// delete its payload file. Before kindHasPayload existed, the import-only check
// left one orphan JSON per selection behind.
func TestRemovingSelectionDeletesPayload(t *testing.T) {
	dir := t.TempDir()
	q := newDownloadQueue(dir)
	it, err := q.addSelection(`{"tracks":[{"title":"x"}]}`, "sel", 1)
	if err != nil {
		t.Fatalf("addSelection: %v", err)
	}
	path := filepath.Join(dir, queueImportsDir, it.ID+".json")
	if _, err := os.Stat(path); err != nil {
		t.Fatalf("payload was never written: %v", err)
	}
	if err := q.remove(it.ID); err != nil {
		t.Fatalf("remove: %v", err)
	}
	if _, err := os.Stat(path); !os.IsNotExist(err) {
		t.Fatalf("payload file was left behind at %s", path)
	}
}
