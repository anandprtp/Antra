package main

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// Excel's "Unicode Text" save writes UTF-16LE with a BOM. Read as UTF-8 that is
// a NUL between every character, which the parser sees as zero rows.
func TestDecodePlaylistTextUTF16(t *testing.T) {
	le := []byte{0xFF, 0xFE, 'T', 0, 'i', 0, 't', 0, 'l', 0, 'e', 0, ',', 0, 0xE9, 0}
	if got := decodePlaylistText(le); got != "Title,é" {
		t.Errorf("UTF-16LE = %q, want %q", got, "Title,é")
	}
	be := []byte{0xFE, 0xFF, 0, 'A', 0, 'b'}
	if got := decodePlaylistText(be); got != "Ab" {
		t.Errorf("UTF-16BE = %q, want %q", got, "Ab")
	}
}

func TestDecodePlaylistTextUTF8(t *testing.T) {
	bom := append([]byte{0xEF, 0xBB, 0xBF}, []byte("Track Name,Artist")...)
	if got := decodePlaylistText(bom); got != string(bom) {
		t.Errorf("UTF-8 with BOM should pass through unchanged for the parser to strip, got %q", got)
	}
	latin1 := []byte("Caf\xe9,Artist")
	got := decodePlaylistText(latin1)
	if !strings.HasPrefix(got, "Caf") || !strings.HasSuffix(got, ",Artist") || !strings.Contains(got, "�") {
		t.Errorf("invalid UTF-8 should be repaired, not rejected: %q", got)
	}
}

func TestReadPlaylistFileRejectsFoldersAndHugeFiles(t *testing.T) {
	dir := t.TempDir()
	if _, err := readPlaylistFile(dir); err == nil {
		t.Error("a folder was accepted as a playlist file")
	}
	big := filepath.Join(dir, "big.csv")
	f, err := os.Create(big)
	if err != nil {
		t.Fatal(err)
	}
	if err := f.Truncate(maxPlaylistFileBytes + 1); err != nil {
		t.Fatal(err)
	}
	f.Close()
	if _, err := readPlaylistFile(big); err == nil || !strings.Contains(err.Error(), "MB") {
		t.Errorf("oversized file: err = %v, want a size message", err)
	}
	small := filepath.Join(dir, "ok.m3u")
	os.WriteFile(small, []byte("#EXTM3U\n#EXTINF:1,A - B\nb.flac\n"), 0o644)
	if got, err := readPlaylistFile(small); err != nil || !strings.HasPrefix(got, "#EXTM3U") {
		t.Errorf("small file: %q, %v", got, err)
	}
}

func TestWriteImportPayloadValidates(t *testing.T) {
	for _, bad := range []string{"", "   ", "[]", `[{"tracks":[1]}]`, "{broken", `{"name":"x"}`, `{"tracks":[]}`} {
		if p, err := writeImportPayload(bad); err == nil {
			os.Remove(p)
			t.Errorf("payload %q was accepted", bad)
		}
	}
	good := `  {"name":"Road Trip","tracks":[{"title":"A","artists":["B"]}]}  `
	p, err := writeImportPayload(good)
	if err != nil {
		t.Fatalf("valid payload rejected: %v", err)
	}
	defer os.Remove(p)
	data, err := os.ReadFile(p)
	if err != nil {
		t.Fatal(err)
	}
	if string(data) != strings.TrimSpace(good) {
		t.Errorf("file content = %q", data)
	}
	if !strings.HasPrefix(filepath.Base(p), "antra-import-") {
		t.Errorf("temp name %q is not recognisable", p)
	}
}

// The flag must be appended to whatever resolveBackendCommand returned, on both
// its return paths, without mutating the slice it was given.
func TestImportArgsAppendsWithoutMutating(t *testing.T) {
	base := make([]string, 2, 8)
	base[0], base[1] = "--config", "c.json"
	got := importArgs(base, "/tmp/p.json")
	want := []string{"--config", "c.json", "--import-json", "/tmp/p.json"}
	if strings.Join(got, "|") != strings.Join(want, "|") {
		t.Errorf("args = %v, want %v", got, want)
	}
	_ = append(base, "SENTINEL")
	if got[2] != "--import-json" {
		t.Error("importArgs shares the caller's backing array")
	}
}
