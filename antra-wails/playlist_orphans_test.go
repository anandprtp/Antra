package main

// issue #75. The property that matters here is not "the right number of files
// was deleted" — it is that a path the caller did not earn cannot be deleted.
// The webview supplies an opaque id and nothing else, so isDeletablePlaylistOrphan
// and the store's take-once semantics ARE the safety boundary.

import (
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
)

func TestIsDeletablePlaylistOrphan(t *testing.T) {
	root := t.TempDir()
	outside := t.TempDir()

	mk := func(dir, name string) string {
		p := filepath.Join(dir, name)
		if err := os.MkdirAll(filepath.Dir(p), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(p, []byte("x"), 0o644); err != nil {
			t.Fatal(err)
		}
		return p
	}

	inPlaylist := mk(root, filepath.Join("Liked Songs", "01 - A.flac"))
	inRoot := mk(root, "loose.mp3")
	notAudio := mk(root, filepath.Join("Liked Songs", "cover.jpg"))
	staging := mk(root, filepath.Join("Liked Songs", ".antra-renum-0.tmp"))
	elsewhere := mk(outside, "victim.flac")
	dir := filepath.Join(root, "Liked Songs")

	// A sibling directory whose name merely starts with the root's — the case a
	// string-prefix containment check would wrongly accept.
	sibling := mk(filepath.Dir(root), filepath.Base(root)+"-other/track.flac")

	cases := []struct {
		name string
		path string
		want bool
	}{
		{"a playlist file under the root", inPlaylist, true},
		{"a file directly in the root", inRoot, true},
		{"a non-audio file", notAudio, false},
		{"a renumber staging file", staging, false},
		{"a file outside the library", elsewhere, false},
		{"a sibling directory sharing the root's prefix", sibling, false},
		{"a directory", dir, false},
		{"the root itself", root, false},
		{"a path that does not exist", filepath.Join(root, "gone.flac"), false},
		{"an empty path", "", false},
		{"a traversal out of the root", filepath.Join(root, "..", filepath.Base(outside), "victim.flac"), false},
	}
	for _, c := range cases {
		if got := isDeletablePlaylistOrphan(c.path, root); got != c.want {
			t.Errorf("%s: got %v, want %v (%s)", c.name, got, c.want, c.path)
		}
	}

	// Control: the guard must not be refusing everything, which would make every
	// `false` above pass for the wrong reason.
	if !isDeletablePlaylistOrphan(inPlaylist, root) {
		t.Fatal("control: a legitimate orphan was refused")
	}

	// Extension matching is case-insensitive — Windows and macOS hand back
	// whatever case the file was created with.
	upper := mk(root, filepath.Join("Liked Songs", "02 - B.FLAC"))
	if !isDeletablePlaylistOrphan(upper, root) {
		t.Error("an upper-case extension was refused")
	}
}

func TestIsDeletablePlaylistOrphanRefusesSymlink(t *testing.T) {
	root := t.TempDir()
	outside := t.TempDir()
	target := filepath.Join(outside, "precious.flac")
	if err := os.WriteFile(target, []byte("x"), 0o644); err != nil {
		t.Fatal(err)
	}
	link := filepath.Join(root, "link.flac")
	if err := os.Symlink(target, link); err != nil {
		// Windows needs Developer Mode or elevation for symlinks. Skipping is
		// honest; the Lstat guard is still compiled and exercised elsewhere.
		t.Skipf("symlinks unavailable on %s: %v", runtime.GOOS, err)
	}
	if isDeletablePlaylistOrphan(link, root) {
		t.Error("a symlink pointing out of the library was accepted")
	}
	if _, err := os.Stat(target); err != nil {
		t.Errorf("the symlink target should be untouched: %v", err)
	}
}

func TestPlaylistOrphanStoreTakeIsOnce(t *testing.T) {
	s := &playlistOrphanStore{}
	s.add("abc", "Liked Songs", []string{"/a.flac", "/b.flac"})

	got, ok := s.take("abc")
	if !ok || len(got.paths) != 2 || got.playlist != "Liked Songs" {
		t.Fatalf("first take: %+v ok=%v", got, ok)
	}
	if _, ok := s.take("abc"); ok {
		t.Error("the same id was taken twice — a double-click would delete twice")
	}
	if _, ok := s.take("never-added"); ok {
		t.Error("an unknown id resolved to a set")
	}
}

func TestPlaylistOrphanStoreCopiesItsInput(t *testing.T) {
	s := &playlistOrphanStore{}
	paths := []string{"/a.flac"}
	s.add("id1", "P", paths)
	paths[0] = "/mutated.flac"
	got, _ := s.take("id1")
	if got.paths[0] != "/a.flac" {
		t.Errorf("the store aliased its caller's slice: %q", got.paths[0])
	}
}

func TestPlaylistOrphanStoreEvictsOldest(t *testing.T) {
	s := &playlistOrphanStore{}
	for i := 0; i < playlistOrphanKeep+3; i++ {
		s.add(string(rune('a'+i)), "P", []string{"/x.flac"})
	}
	if len(s.sets) != playlistOrphanKeep {
		t.Fatalf("kept %d sets, want %d", len(s.sets), playlistOrphanKeep)
	}
	if _, ok := s.take("a"); ok {
		t.Error("the oldest set survived eviction")
	}
	if _, ok := s.take(string(rune('a' + playlistOrphanKeep + 2))); !ok {
		t.Error("the newest set was evicted")
	}
}

func TestInterceptPlaylistOrphansHidesPaths(t *testing.T) {
	a := &App{}
	in := map[string]interface{}{
		"type":     "playlist_orphans",
		"playlist": "Liked Songs",
		"pruned":   false,
		"paths": []interface{}{
			filepath.Join("C:", "Music", "Liked Songs", "02 - Bravo.flac"),
			filepath.Join("C:", "Music", "Liked Songs", "09 - Delta.mp3"),
		},
	}
	out := a.interceptPlaylistOrphans(in)
	if out == nil {
		t.Fatal("a well-formed report was dropped")
	}
	if _, leaked := out["paths"]; leaked {
		t.Error("the outgoing payload still carries paths")
	}
	names, _ := out["names"].([]interface{})
	if len(names) != 2 || names[0] != "02 - Bravo.flac" {
		t.Errorf("names = %v", names)
	}
	// The strongest form of the claim: no value in the payload contains a
	// separator, so nothing resembling a path can reach the webview.
	for k, v := range out {
		if s, ok := v.(string); ok && strings.Contains(s, string(filepath.Separator)) {
			t.Errorf("%s leaks a path-like value: %q", k, s)
		}
	}
	if out["count"] != 2 {
		t.Errorf("count = %v", out["count"])
	}
	id, _ := out["orphan_id"].(string)
	if !validQueueID(id) {
		t.Fatalf("orphan_id %q is not a usable id", id)
	}
	if set, ok := a.playlistOrphans().take(id); !ok || len(set.paths) != 2 {
		t.Error("the paths were not retained under that id")
	}
}

func TestInterceptPlaylistOrphansAutoModeOffersNoDelete(t *testing.T) {
	a := &App{}
	out := a.interceptPlaylistOrphans(map[string]interface{}{
		"playlist": "P",
		"pruned":   true,
		"paths":    []interface{}{"/x/y.flac"},
	})
	if out == nil {
		t.Fatal("dropped")
	}
	if out["pruned"] != true {
		t.Error("pruned flag lost")
	}
	if _, has := out["orphan_id"]; has {
		// The files are already gone; a delete button for them would always fail.
		t.Error("an already-pruned report handed out a deletable id")
	}
}

func TestInterceptPlaylistOrphansDropsEmptyReports(t *testing.T) {
	a := &App{}
	for _, in := range []map[string]interface{}{
		{"playlist": "P"},
		{"playlist": "P", "paths": []interface{}{}},
		{"playlist": "P", "paths": "not-a-list"},
		{"playlist": "P", "paths": []interface{}{"", "  "}},
		{"playlist": "P", "paths": []interface{}{42}},
	} {
		if out := a.interceptPlaylistOrphans(in); out != nil {
			t.Errorf("%v produced %v, want nil so the caller drops it", in, out)
		}
	}
}

func TestDeletePlaylistOrphansRejectsUnknownID(t *testing.T) {
	a := &App{}
	for _, id := range []string{"", "not-an-id!", strings.Repeat("a", 100), "deadbeef"} {
		if _, err := a.DeletePlaylistOrphans(id); err == nil {
			t.Errorf("id %q was accepted", id)
		}
	}
}
