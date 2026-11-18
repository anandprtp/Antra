package main

// v2.0.0 PORT-3 - playlist-file importer, Go half.
//
// The file is parsed in the frontend (src/lib/importer.ts) so the user sees a
// preview before anything starts. Go does the two things the webview cannot do
// portably: open a native file picker, and hand a potentially 5,000-row
// tracklist to the Python backend. That hand-off goes through a temp file, not a
// command-line argument - Windows caps a command line at 32,767 characters, which
// a few hundred rows already exceed.

import (
	"bytes"
	"context"
	"encoding/binary"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"unicode/utf16"
	"unicode/utf8"

	wailsRuntime "github.com/wailsapp/wails/v2/pkg/runtime"
)

// A 5,000-row Exportify CSV measures well under 2 MB; this only stops someone
// pointing the picker at a video file and freezing the webview parsing it.
const maxPlaylistFileBytes = 20 << 20

func importJSON(v map[string]interface{}) string {
	b, err := json.Marshal(v)
	if err != nil {
		return `{"error":"could not encode result"}`
	}
	return string(b)
}

// PickPlaylistFile opens a native picker and returns {"name","text"},
// {"cancelled":true} or {"error"}.
func (a *App) PickPlaylistFile() string {
	path, err := wailsRuntime.OpenFileDialog(a.ctx, wailsRuntime.OpenDialogOptions{
		Title: "Import a playlist file",
		Filters: []wailsRuntime.FileFilter{
			{DisplayName: "Playlist files (CSV, M3U, XSPF, JSPF, JSON)", Pattern: "*.csv;*.tsv;*.m3u;*.m3u8;*.xspf;*.jspf;*.json;*.xml;*.txt"},
			{DisplayName: "All files", Pattern: "*.*"},
		},
	})
	if err != nil {
		return importJSON(map[string]interface{}{"error": err.Error()})
	}
	if strings.TrimSpace(path) == "" {
		return `{"cancelled":true}`
	}
	text, err := readPlaylistFile(path)
	if err != nil {
		return importJSON(map[string]interface{}{"error": err.Error()})
	}
	return importJSON(map[string]interface{}{"name": filepath.Base(path), "text": text})
}

func readPlaylistFile(path string) (string, error) {
	info, err := os.Stat(path)
	if err != nil {
		return "", err
	}
	if info.IsDir() {
		return "", errors.New("that is a folder, not a playlist file")
	}
	if info.Size() > maxPlaylistFileBytes {
		return "", fmt.Errorf("the file is %.1f MB; playlist files are expected to be under %d MB",
			float64(info.Size())/(1<<20), maxPlaylistFileBytes>>20)
	}
	raw, err := os.ReadFile(path)
	if err != nil {
		return "", err
	}
	return decodePlaylistText(raw), nil
}

// decodePlaylistText returns UTF-8. Excel's "Unicode Text" save and some Windows
// tools write UTF-16 with a BOM, which read as UTF-8 is a NUL between every
// character and parses as zero rows. A UTF-8 BOM is left for the parser, which
// strips it from the header. Invalid UTF-8 (a Latin-1 export) is repaired rather
// than rejected, so one accented byte cannot fail a whole file.
func decodePlaylistText(raw []byte) string {
	switch {
	case len(raw) >= 2 && raw[0] == 0xFF && raw[1] == 0xFE:
		return decodeUTF16(raw[2:], binary.LittleEndian)
	case len(raw) >= 2 && raw[0] == 0xFE && raw[1] == 0xFF:
		return decodeUTF16(raw[2:], binary.BigEndian)
	}
	if utf8.Valid(raw) {
		return string(raw)
	}
	return strings.ToValidUTF8(string(raw), "�")
}

func decodeUTF16(b []byte, order binary.ByteOrder) string {
	units := make([]uint16, 0, len(b)/2)
	for i := 0; i+1 < len(b); i += 2 {
		units = append(units, order.Uint16(b[i:]))
	}
	return string(utf16.Decode(units))
}

// writeImportPayload checks the payload is a JSON object carrying a non-empty
// "tracks" array, then writes it to a private temp file. Python re-validates
// every row; this only refuses to start a backend process for nothing.
func writeImportPayload(payload string) (string, error) {
	if err := validateImportPayload(payload); err != nil {
		return "", err
	}
	trimmed := bytes.TrimSpace([]byte(payload))
	f, err := os.CreateTemp("", "antra-import-*.json")
	if err != nil {
		return "", err
	}
	name := f.Name()
	if _, err := f.Write(trimmed); err != nil {
		f.Close()
		os.Remove(name)
		return "", err
	}
	if err := f.Close(); err != nil {
		os.Remove(name)
		return "", err
	}
	return name, nil
}

// validateImportPayload is shared with the SF-1 queue, which persists the
// payload and so must refuse a bad one before storing it.
func validateImportPayload(payload string) error {
	trimmed := bytes.TrimSpace([]byte(payload))
	var probe struct {
		Tracks []json.RawMessage `json:"tracks"`
	}
	if len(trimmed) == 0 || trimmed[0] != '{' {
		return errors.New("import payload is not a JSON object")
	}
	if err := json.Unmarshal(trimmed, &probe); err != nil {
		return fmt.Errorf("import payload is not valid JSON: %w", err)
	}
	if len(probe.Tracks) == 0 {
		return errors.New("the imported playlist has no tracks")
	}
	return nil
}

// StartImportDownload downloads an imported tracklist. The Python backend
// deletes the temp file once it has read it; it is removed here only when the
// process never starts.
func (a *App) StartImportDownload(payload string) error {
	path, err := writeImportPayload(payload)
	if err != nil {
		return err
	}

	wailsRuntime.LogInfof(a.ctx, "Starting playlist-file import")

	if cancel, cmd := a.detachActiveDownload(); cancel != nil || cmd != nil {
		if cancel != nil {
			cancel()
		}
		if err := killCommandTree(cmd); err != nil {
			wailsRuntime.LogWarningf(a.ctx, "Failed to stop previous library engine: %v", err)
		}
	}

	a.mu.Lock()
	a.isStopping = false
	a.mu.Unlock()

	ctx, cancel := context.WithCancel(a.ctx)
	// Same resolution as RetryTrackDownload: both the bundled-backend and the
	// dev-source return paths carry baseArgs, so the flag reaches either.
	command, baseArgs, workDir, env, err := a.resolveBackendCommand([]string{})
	if err != nil {
		cancel()
		os.Remove(path)
		wailsRuntime.LogErrorf(a.ctx, "%s", err.Error())
		return err
	}

	args := importArgs(baseArgs, path)
	if err := a.startBackendProcess(ctx, cancel, command, args, workDir, env, nil); err != nil {
		os.Remove(path)
		return err
	}
	return nil
}

func importArgs(baseArgs []string, path string) []string {
	args := append([]string{}, baseArgs...)
	return append(args, "--import-json", path)
}
