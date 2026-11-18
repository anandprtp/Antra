package main

import (
	"encoding/json"
	"strings"
	"testing"
)

// parseExportOutput has to pull one event out of newline-delimited JSON that is
// preceded by log lines. Unmarshalling the whole buffer - which the older
// one-shot bindings do - fails here, so this pins the line-scanning behaviour.
func TestParseExportOutputPicksTheEventPastLogLines(t *testing.T) {
	out := []byte(strings.Join([]string{
		`{"type":"log","level":"info","message":"[Config] Filename format: default"}`,
		`{"type":"log","level":"info","message":"[Sources] apple ready"}`,
		`{"type":"playlist_exported","path":"C:\\x\\a.csv","format":"csv","track_count":14,` +
			`"columns":["Track Name"],"omitted_columns":["Genres"],"bytes":2456}`,
	}, "\n"))

	got := parseExportOutput(out)
	if got == "" {
		t.Fatal("no result parsed")
	}
	var m map[string]interface{}
	if err := json.Unmarshal([]byte(got), &m); err != nil {
		t.Fatalf("result is not valid JSON: %v (%s)", err, got)
	}
	if m["type"] != "playlist_exported" {
		t.Errorf("type = %v, want playlist_exported", m["type"])
	}
	if m["track_count"].(float64) != 14 {
		t.Errorf("track_count = %v, want 14", m["track_count"])
	}
	if m["path"] != `C:\x\a.csv` {
		t.Errorf("path = %v, want the windows path unmangled", m["path"])
	}
	cols, ok := m["omitted_columns"].([]interface{})
	if !ok || len(cols) != 1 || cols[0] != "Genres" {
		t.Errorf("omitted_columns = %v, want [Genres]", m["omitted_columns"])
	}
}

func TestParseExportOutputSurfacesBackendErrors(t *testing.T) {
	out := []byte(`{"type":"log","message":"starting"}` + "\n" +
		`{"type":"error","message":"No tracks were found at that link."}`)
	got := parseExportOutput(out)
	if !strings.Contains(got, `"error"`) {
		t.Fatalf("error not surfaced: %s", got)
	}
	if !strings.Contains(got, "No tracks were found") {
		t.Errorf("error message lost: %s", got)
	}
}

// A quote in the backend message must not produce invalid JSON.
func TestParseExportOutputEscapesQuotesInErrors(t *testing.T) {
	out := []byte(`{"type":"error","message":"bad \"quoted\" thing"}`)
	got := parseExportOutput(out)
	var m map[string]interface{}
	if err := json.Unmarshal([]byte(got), &m); err != nil {
		t.Fatalf("quote in message broke the JSON: %v (%s)", err, got)
	}
}

// Empty string means "nothing usable" so the caller can fall back to the
// process error rather than reporting a bogus success.
func TestParseExportOutputReturnsEmptyWhenNothingUsable(t *testing.T) {
	for _, in := range []string{"", "not json at all", `{"type":"log","message":"x"}`} {
		if got := parseExportOutput([]byte(in)); got != "" {
			t.Errorf("parseExportOutput(%q) = %q, want empty", in, got)
		}
	}
}

func TestExportFormatTablesAgree(t *testing.T) {
	for fmtName := range exportFormatExtensions {
		if _, ok := exportFormatFilters[fmtName]; !ok {
			t.Errorf("format %q has an extension but no dialog filter", fmtName)
		}
	}
	for fmtName := range exportFormatFilters {
		if _, ok := exportFormatExtensions[fmtName]; !ok {
			t.Errorf("format %q has a filter but no extension", fmtName)
		}
	}
	// Must stay in step with EXPORT_EXTENSIONS in antra/utils/playlist_export.py.
	want := map[string]string{
		"csv": ".csv", "m3u8": ".m3u8", "xspf": ".xspf", "json": ".json", "txt": ".txt",
	}
	for k, v := range want {
		if exportFormatExtensions[k] != v {
			t.Errorf("extension for %q = %q, want %q", k, exportFormatExtensions[k], v)
		}
	}
	if len(exportFormatExtensions) != len(want) {
		t.Errorf("format count = %d, want %d", len(exportFormatExtensions), len(want))
	}
}
