package main

// Issue #58 - song/album search. The Go side does one thing worth testing:
// it normalises `kind` and `source` before they reach the backend.
//
// That is not cosmetic. --search-kind and --search-source are argparse
// `choices=`, so an unknown value makes json_cli exit 2 with the reason on
// stderr - which cmd.Output() discards, leaving the user with a bare
// "exit status 2" and no way to tell what went wrong.

import (
	"strings"
	"testing"
)

// argValue reads the value of a `--flag=value` element.
func argValue(args []string, flag string) string {
	for _, a := range args {
		if strings.HasPrefix(a, flag+"=") {
			return strings.TrimPrefix(a, flag+"=")
		}
	}
	return ""
}

func argAfter(args []string, flag string) string {
	for i, a := range args {
		if a == flag && i+1 < len(args) {
			return args[i+1]
		}
	}
	return ""
}

func TestSearchCatalogArgsPassesValidValuesThrough(t *testing.T) {
	for _, kind := range []string{"song", "album", "artist"} {
		for _, source := range []string{"apple", "spotify"} {
			args := searchCatalogArgs("queen", kind, source, "C:/cfg.json")
			if got := argAfter(args, "--search-kind"); got != kind {
				t.Errorf("kind %q was rewritten to %q", kind, got)
			}
			if got := argAfter(args, "--search-source"); got != source {
				t.Errorf("source %q was rewritten to %q", source, got)
			}
		}
	}
}

func TestSearchCatalogArgsNormalisesUnknownValues(t *testing.T) {
	// Empty, misspelled, wrong case and an injected flag all have to land on a
	// value argparse accepts.
	for _, bad := range []string{"", "songs", "SONG", "track", "--help", "tidal"} {
		args := searchCatalogArgs("q", bad, bad, "cfg")
		if got := argAfter(args, "--search-kind"); got != "song" {
			t.Errorf("kind %q normalised to %q, want song", bad, got)
		}
		if got := argAfter(args, "--search-source"); got != "apple" {
			t.Errorf("source %q normalised to %q, want apple", bad, got)
		}
	}
}

func TestSearchCatalogArgsShape(t *testing.T) {
	args := searchCatalogArgs("bohemian rhapsody", "song", "apple", "C:/Users/x/config.json")

	if got := argValue(args, "--search-catalog"); got != "bohemian rhapsody" {
		t.Errorf("query = %q", got)
	}
	if got := argAfter(args, "--config"); got != "C:/Users/x/config.json" {
		t.Errorf("config = %q", got)
	}
	// A query with a space stays in ONE argv element. Only bare flags are
	// checked: the query rides inside `--search-catalog=...`, which contains a
	// space on purpose and is exactly what must not be split.
	for _, a := range args {
		if strings.HasPrefix(a, "--") && !strings.Contains(a, "=") && strings.Contains(a, " ") {
			t.Errorf("bare flag %q carries embedded whitespace", a)
		}
	}
	if len(args) != 7 {
		t.Errorf("argv = %v (%d elements), want 7", args, len(args))
	}

	// A query that begins with a dash has to stay INSIDE its own argv element.
	// As two elements argparse reads it as the next flag and the CLI exits 2
	// with "expected one argument" - measured against the real CLI, which is
	// what this assertion is standing in for.
	args = searchCatalogArgs("--config", "song", "apple", "cfg")
	if got := argValue(args, "--search-catalog"); got != "--config" {
		t.Errorf("flag-shaped query = %q, want it passed through verbatim", got)
	}
	n := 0
	for _, a := range args {
		if a == "--config" {
			n++
		}
	}
	// Exactly one: the real flag. Two would mean the query escaped its element
	// and argparse would see a second --config with no value after it.
	if n != 1 {
		t.Fatalf("--config appears %d times, want 1: %v", n, args)
	}
	// CONTROL: the real --config= pair is still there and still points at the
	// config path, so the check above is not passing because the flag vanished.
	if got := argAfter(args, "--config"); got != "cfg" {
		t.Errorf("config = %q, want cfg", got)
	}
}
