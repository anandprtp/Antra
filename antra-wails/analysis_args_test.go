package main

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
)

// inTempCwd runs fn with the working directory set to a fresh temp dir.
func inTempCwd(t *testing.T, fn func(dir string)) {
	t.Helper()
	old, _ := os.Getwd()
	dir := t.TempDir()
	if err := os.Chdir(dir); err != nil {
		t.Fatal(err)
	}
	defer os.Chdir(old)
	fn(dir)
}

// guardBlocked reports whether the binding returned before reaching the ffmpeg
// path resolver (which would then have spent the Once).
func guardBlocked(a *App) bool {
	ran := false
	a.ffmpegOnce.Do(func() { ran = true })
	return ran
}

// AUDIT-DESKTOP-9: the webview-callable analysis bindings must refuse paths that
// ffprobe would read as an option, or that are not regular audio files.

func TestValidateAnalysisFile(t *testing.T) {
	dir := t.TempDir()
	ok := filepath.Join(dir, "song.flac")
	opus := filepath.Join(dir, "song.opus")
	txt := filepath.Join(dir, "notes.txt")
	for _, p := range []string{ok, opus, txt} {
		if err := os.WriteFile(p, []byte("x"), 0o600); err != nil {
			t.Fatal(err)
		}
	}
	sub := filepath.Join(dir, "dir.flac") // a directory with an audio name
	if err := os.Mkdir(sub, 0o700); err != nil {
		t.Fatal(err)
	}

	for _, p := range []string{ok, opus} { // controls: real audio files pass
		if _, err := validateAnalysisFile(p); err != nil {
			t.Errorf("%s should be accepted: %v", p, err)
		}
	}
	bad := map[string]string{
		"empty":                "",
		"leading dash":         "-i",
		"dash relative flac":   "-show_streams.flac",
		"relative":             "song.flac",
		"nul byte":             ok + "\x00.txt",
		"not audio":            txt,
		"directory named flac": sub,
		"missing":              filepath.Join(dir, "nope.flac"),
	}
	for name, p := range bad {
		if _, err := validateAnalysisFile(p); err == nil {
			t.Errorf("%s (%q) must be rejected", name, p)
		}
	}
}

func TestAnalysisBindingsRefuseBadPaths(t *testing.T) {
	a := &App{}
	if got := a.ScanFolder("-i"); len(got) != 0 {
		t.Errorf("ScanFolder(relative) = %v", got)
	}
	if got := a.ScanFolder(filepath.Join(t.TempDir(), "missing")); got == nil || len(got) != 0 {
		t.Errorf("ScanFolder(missing) must be an empty non-nil slice, got %#v", got)
	}
	if res := a.AnalyzeAudio("-i"); res["probeError"] == nil || len(res) != 1 {
		t.Errorf("AnalyzeAudio(bad) = %v", res)
	}
	var tl TrackLyrics
	if err := json.Unmarshal([]byte(a.GetTrackLyrics("-i")), &tl); err != nil || len(tl.Lines) != 0 {
		t.Errorf("GetTrackLyrics(bad) = %v / %v", tl, err)
	}

	// control: a real folder is still scanned
	dir := t.TempDir()
	f := filepath.Join(dir, "a.flac")
	if err := os.WriteFile(f, []byte("x"), 0o600); err != nil {
		t.Fatal(err)
	}
	if got := a.ScanFolder(dir); len(got) != 1 {
		t.Errorf("ScanFolder(real dir) = %v, want 1 file", got)
	}
}

// Each guard is isolated by a case only it rejects: a RELATIVE path to a file
// or folder that really exists (every other check would pass it).
func TestAnalysisGuardsIsolated(t *testing.T) {
	inTempCwd(t, func(dir string) {
		if err := os.WriteFile("-evil.flac", []byte("x"), 0o600); err != nil {
			t.Fatal(err)
		}
		if _, err := validateAnalysisFile("-evil.flac"); err == nil {
			t.Error("relative audio file named like an option must be rejected")
		}
		a := &App{}
		if got := a.ScanFolder("."); len(got) != 0 {
			t.Errorf("ScanFolder(relative) must refuse, got %v", got)
		}
		a = &App{}
		_ = a.GetTrackLyrics("-evil.flac")
		if !guardBlocked(a) {
			t.Error("GetTrackLyrics reached ffprobe for a rejected path")
		}
		a = &App{}
		_ = a.AnalyzeAudio("-evil.flac")
		if !guardBlocked(a) {
			t.Error("AnalyzeAudio reached ffprobe for a rejected path")
		}
		// control: with an absolute real path the binding DOES proceed past the guard
		a = &App{}
		_ = a.GetTrackLyrics(filepath.Join(dir, "-evil.flac"))
		if guardBlocked(a) {
			t.Error("control: a valid absolute path must pass the guard")
		}
	})
}

func TestSearchArtistsArgsDashQuery(t *testing.T) {
	for _, q := range []string{"-M-", "--config", "-", "Drake"} {
		args := searchArtistsArgs(q, "apple", "cfg.json")
		if args[0] != "--search-artists="+q {
			t.Errorf("query %q must travel inside one --flag=value element, got %q", q, args[0])
		}
		want := []string{"--search-source", "apple", "--config", "cfg.json"}
		if len(args) != 1+len(want) {
			t.Fatalf("argv = %q", args)
		}
		for i, w := range want {
			if args[1+i] != w {
				t.Errorf("query %q leaked into the fixed arguments: %q", q, args)
			}
		}
	}
}
