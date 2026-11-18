package main

import (
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"sync"
	"testing"
)

func TestWriteFileAtomic(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "config.json")
	if err := os.WriteFile(path, []byte("old"), 0o644); err != nil {
		t.Fatal(err)
	}

	var wg sync.WaitGroup
	for i := 0; i < 20; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			if err := writeFileAtomic(path, []byte(`{"k":"complete-value"}`), 0o600); err != nil {
				t.Error(err)
			}
		}()
	}
	wg.Wait()

	// Control: content is the full new value (never "old", never partial).
	if b, _ := os.ReadFile(path); string(b) != `{"k":"complete-value"}` {
		t.Errorf("unexpected content %q", b)
	}
	entries, _ := os.ReadDir(dir)
	if len(entries) != 1 {
		t.Errorf("temp files left behind: %d entries", len(entries))
	}
	if runtime.GOOS != "windows" {
		if fi, _ := os.Stat(path); fi.Mode().Perm() != 0o600 {
			t.Errorf("mode = %v, want 0600 (old 0644 file must be tightened)", fi.Mode().Perm())
		}
	}
	if err := writeFileAtomic(filepath.Join(dir, "missing", "x.json"), []byte("x"), 0o600); err == nil {
		t.Error("write into a missing folder must fail, not create it")
	}
}

func TestValidateBackendLinks(t *testing.T) {
	// Controls: ordinary links, an empty list and a local playlist path must pass.
	for _, ok := range [][]string{
		nil,
		{"https://open.spotify.com/album/abc"},
		{"https://music.apple.com/us/album/x/1?i=2", "https://www.deezer.com/en/album/3"},
		{`C:\Music\list.m3u`},
	} {
		if err := validateBackendLinks(ok); err != nil {
			t.Errorf("%v refused: %v", ok, err)
		}
	}
	for _, bad := range []string{"--export-path", "--config=evil.json", " -h", "-"} {
		if err := validateBackendLinks([]string{"https://open.spotify.com/album/abc", bad}); err == nil {
			t.Errorf("%q must be refused", bad)
		}
	}
}

// TestLinkEntryPointsRefuseFlagLikeInput covers the four App methods that build
// backend argv from a raw link WITHOUT going through resolveBackendCommand (so
// validateBackendLinks never ran on them): the Album Availability box and the
// Export button have no http-prefix filter in the frontend, and PreviewRelease /
// GetArtistDiscography can be reached with unfiltered data (card clicks, search
// results). Each must refuse a flag-like paste at the Go boundary instead of
// handing it to argparse, where it reads as another option.
func TestLinkEntryPointsRefuseFlagLikeInput(t *testing.T) {
	a := &App{}
	const bad = "--config=evil.json"

	if out := a.ExportPlaylist(bad, "csv", "x"); !strings.Contains(out, `"error"`) {
		t.Errorf("ExportPlaylist(%q) = %q, want an error", bad, out)
	}
	if out := a.GetArtistDiscography(bad); !strings.Contains(out, `"error"`) {
		t.Errorf("GetArtistDiscography(%q) = %q, want an error", bad, out)
	}
	if out := a.GetAlbumAvailability(bad); !strings.Contains(out, `"error"`) {
		t.Errorf("GetAlbumAvailability(%q) = %q, want an error", bad, out)
	}
	if out := a.PreviewRelease(bad); !strings.Contains(out, `"error"`) {
		t.Errorf("PreviewRelease(%q) = %q, want an error", bad, out)
	}
}
