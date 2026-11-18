package main

import (
	"os"
	"path/filepath"
	"testing"
)

func TestCorruptConfigIsPreserved(t *testing.T) {
	dir := t.TempDir()
	t.Setenv("LOCALAPPDATA", dir)
	t.Setenv("XDG_DATA_HOME", dir)
	t.Setenv("HOME", dir)
	p := getConfigPath()
	if err := os.MkdirAll(filepath.Dir(p), 0o755); err != nil {
		t.Fatal(err)
	}
	bad := []byte(`{"download_path":"D:\Mine",`)
	if err := os.WriteFile(p, bad, 0o600); err != nil {
		t.Fatal(err)
	}
	(&App{}).GetConfig()
	got, err := os.ReadFile(p + ".corrupt")
	if err != nil || string(got) != string(bad) {
		t.Fatalf("corrupt config not preserved: %v %q", err, got)
	}
	// control: a valid config leaves no .corrupt file
	os.Remove(p + ".corrupt")
	os.WriteFile(p, []byte(`{"download_path":"D:/Mine"}`), 0o600)
	(&App{}).GetConfig()
	if _, err := os.Stat(p + ".corrupt"); err == nil {
		t.Fatal("valid config wrongly flagged corrupt")
	}
}
