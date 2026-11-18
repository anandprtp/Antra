package main

import (
	"os"
	"path/filepath"
	"testing"
)

func mustSymlink(t *testing.T, target, link string) {
	t.Helper()
	// Not a junction fallback: Go 1.23+ no longer follows Windows junctions in EvalSymlinks,
	// so one would not exercise the code under test.
	if err := os.Symlink(target, link); err != nil {
		t.Skipf("cannot create symlinks here: %v", err)
	}
}

func TestResolveWritableTarget(t *testing.T) {
	granted := t.TempDir()
	other := t.TempDir()
	grants := []string{granted}

	// Control: the allowed case must succeed, or every refusal below proves nothing.
	if _, err := resolveWritableTarget(grants, filepath.Join(granted, "a.png")); err != nil {
		t.Fatalf("png in granted folder refused: %v", err)
	}
	if _, err := resolveWritableTarget(grants, filepath.Join(granted, "A.PNG")); err != nil {
		t.Fatalf("uppercase extension refused: %v", err)
	}

	refused := map[string]string{
		"relative path":      "a.png",
		"other folder":       filepath.Join(other, "a.png"),
		"non-png extension":  filepath.Join(granted, "a.exe"),
		"no extension":       filepath.Join(granted, "a"),
		"subfolder":          filepath.Join(granted, "sub", "a.png"),
		"dotdot out":         filepath.Join(granted, "..", filepath.Base(other), "a.png"),
		"no grants analogue": filepath.Join(granted, "a.png.exe"),
	}
	for name, p := range refused {
		if _, err := resolveWritableTarget(grants, p); err == nil {
			t.Errorf("%s: expected refusal for %q", name, p)
		}
	}
	if _, err := resolveWritableTarget(nil, filepath.Join(granted, "a.png")); err == nil {
		t.Error("with no grants every write must be refused")
	}
}

func TestResolveWritableTargetSymlinks(t *testing.T) {
	granted := t.TempDir()
	outside := t.TempDir()

	// A granted-folder entry that is a symlink to an outside folder.
	link := filepath.Join(granted, "link")
	mustSymlink(t, outside, link)
	if _, err := resolveWritableTarget([]string{granted}, filepath.Join(link, "a.png")); err == nil {
		t.Error("write through a symlinked subfolder must be refused")
	}

	// A symlinked .png pointing at a victim file must not be followed.
	victim := filepath.Join(outside, "victim.png")
	if err := os.WriteFile(victim, []byte("x"), 0644); err != nil {
		t.Fatal(err)
	}
	pngLink := filepath.Join(granted, "evil.png")
	mustSymlink(t, victim, pngLink)
	if _, err := resolveWritableTarget([]string{granted}, pngLink); err == nil {
		t.Error("write onto a symlinked file must be refused")
	}
}

func TestResolveLibraryPathSymlink(t *testing.T) {
	root := t.TempDir()
	outside := t.TempDir()
	if err := os.WriteFile(filepath.Join(outside, "secret.flac"), []byte("x"), 0644); err != nil {
		t.Fatal(err)
	}
	if err := os.MkdirAll(filepath.Join(root, "Artist"), 0755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(root, "Artist", "ok.flac"), []byte("x"), 0644); err != nil {
		t.Fatal(err)
	}

	// Controls: a real file, and a not-yet-existing path, still resolve.
	if _, err := resolveLibraryPath(root, "Artist/ok.flac"); err != nil {
		t.Fatalf("real file refused: %v", err)
	}
	if _, err := resolveLibraryPath(root, "Artist/new.flac"); err != nil {
		t.Fatalf("nonexistent path refused: %v", err)
	}
	if _, err := resolveLibraryPath(root, "../x.flac"); err == nil {
		t.Error("textual escape must still be refused")
	}

	mustSymlink(t, outside, filepath.Join(root, "escape"))
	if _, err := resolveLibraryPath(root, "escape/secret.flac"); err == nil {
		t.Error("symlink pointing outside the library must be refused")
	}

	// A symlink that stays inside the library is fine.
	mustSymlink(t, filepath.Join(root, "Artist"), filepath.Join(root, "alias"))
	if _, err := resolveLibraryPath(root, "alias/ok.flac"); err != nil {
		t.Errorf("in-library symlink refused: %v", err)
	}
}
