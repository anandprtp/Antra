package main

import (
	"os"
	"path/filepath"
	"testing"
)

func touchAudio(t *testing.T, path string) {
	t.Helper()
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte("x"), 0o644); err != nil {
		t.Fatal(err)
	}
}

// v2.0.0 DMX-4: an album split into CD1/ and CD2/ is ONE release in
// Downloaded Music, in both the flat and the standard layout.
func TestDiscFoldersAreOneRelease(t *testing.T) {
	root := t.TempDir()
	// flat layout: root/Album/CD<n>
	touchAudio(t, filepath.Join(root, "Box (1973)", "CD1", "01 - A.flac"))
	touchAudio(t, filepath.Join(root, "Box (1973)", "CD2", "01 - B.flac"))
	// standard layout: root/Artist/Album/CD<n>
	touchAudio(t, filepath.Join(root, "The Beatles", "1967-1970 (1973)", "CD1", "05 - Hey Jude.flac"))
	touchAudio(t, filepath.Join(root, "The Beatles", "1967-1970 (1973)", "CD2", "05 - Let It Be.flac"))
	// ordinary artist with two albums must still be two releases
	touchAudio(t, filepath.Join(root, "Drake", "Album A", "01.flac"))
	touchAudio(t, filepath.Join(root, "Drake", "Album B", "01.flac"))

	got := map[string]bool{}
	for _, d := range collectRootAlbumReleaseDirs(root) {
		rel, _ := filepath.Rel(root, d)
		got[filepath.ToSlash(rel)] = true
	}
	want := []string{"Box (1973)", "The Beatles/1967-1970 (1973)", "Drake/Album A", "Drake/Album B"}
	for _, w := range want {
		if !got[w] {
			t.Errorf("missing release %q; got %v", w, got)
		}
	}
	for g := range got {
		if filepath.Base(g) == "CD1" || filepath.Base(g) == "CD2" {
			t.Errorf("disc folder listed as its own release: %q", g)
		}
	}
	if len(got) != len(want) {
		t.Errorf("got %d releases, want %d: %v", len(got), len(want), got)
	}

	// legacy Albums/Artist/Album/CD<n> — artist dir with only disc folders under an album
	legacy := filepath.Join(root, "Albums")
	touchAudio(t, filepath.Join(legacy, "X", "Y", "CD1", "1.flac"))
	touchAudio(t, filepath.Join(legacy, "X", "Y", "CD2", "1.flac"))
	lg := collectLegacyAlbumReleaseDirs(legacy, "standard")
	if len(lg) != 1 || filepath.Base(lg[0]) != "Y" {
		t.Errorf("legacy layout: want [Y], got %v", lg)
	}
}

func TestAllDiscFolders(t *testing.T) {
	cases := []struct {
		in   []string
		want bool
	}{
		{[]string{"/a/CD1", "/a/CD2"}, true},
		{[]string{"/a/CD1", "/a/Bonus"}, false},
		{[]string{"/a/cd1"}, false},
		{nil, false},
	}
	for _, c := range cases {
		if got := allDiscFolders(c.in); got != c.want {
			t.Errorf("allDiscFolders(%v) = %v, want %v", c.in, got, c.want)
		}
	}
}

// Path order puts CD10/ before CD2/; disc/track tags must win when all present.
func TestSortReleaseTracks(t *testing.T) {
	tracks := []libraryReleaseTrack{
		{FileName: "CD10/01", DiscNumber: 10, TrackNumber: 1},
		{FileName: "CD1/02", DiscNumber: 1, TrackNumber: 2},
		{FileName: "CD2/01", DiscNumber: 2, TrackNumber: 1},
		{FileName: "CD1/01", DiscNumber: 1, TrackNumber: 1},
	}
	sortReleaseTracks(tracks)
	order := []string{"CD1/01", "CD1/02", "CD2/01", "CD10/01"}
	for i, w := range order {
		if tracks[i].FileName != w {
			t.Fatalf("position %d: got %s want %s (%v)", i, tracks[i].FileName, w, tracks)
		}
	}
	// a single untagged track leaves path order untouched
	partial := []libraryReleaseTrack{
		{FileName: "b", DiscNumber: 2, TrackNumber: 1},
		{FileName: "a", DiscNumber: 0, TrackNumber: 1},
	}
	sortReleaseTracks(partial)
	if partial[0].FileName != "b" {
		t.Errorf("partial tags reordered: %v", partial)
	}
}
