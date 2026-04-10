package main

import (
	"encoding/base64"
	"encoding/json"
	"fmt"
	"io/fs"
	"os"
	"os/exec"
	"path/filepath"
	"sort"
	"strings"

	wailsRuntime "github.com/wailsapp/wails/v2/pkg/runtime"
)

var audioExtensions = map[string]bool{
	".flac": true,
	".mp3":  true,
	".m4a":  true,
	".wav":  true,
	".aiff": true,
	".aif":  true,
	".ogg":  true,
}

// ScanFolder returns sorted audio file paths from a directory (non-recursive).
// Returns an empty slice (not nil) on error so the frontend always gets an array.
func (a *App) ScanFolder(folderPath string) []string {
	var files []string

	_ = filepath.WalkDir(folderPath, func(path string, d fs.DirEntry, err error) error {
		if err != nil {
			return nil
		}
		// Only recurse one level into the dropped folder itself
		if d.IsDir() && path != folderPath {
			rel, relErr := filepath.Rel(folderPath, path)
			if relErr == nil && strings.ContainsRune(rel, os.PathSeparator) {
				return fs.SkipDir
			}
		}
		if !d.IsDir() {
			ext := strings.ToLower(filepath.Ext(path))
			if audioExtensions[ext] {
				files = append(files, filepath.ToSlash(path))
			}
		}
		return nil
	})

	sort.Strings(files)
	if files == nil {
		return []string{}
	}
	return files
}

// AnalyzeAudio runs ffprobe for metadata and ffmpeg showspectrumpic for the
// spectrogram. Routes through the Python backend so the bundled ffmpeg inside
// _MEIPASS is available even on machines with no system ffmpeg.
func (a *App) AnalyzeAudio(filePath string) map[string]interface{} {
	result := make(map[string]interface{})

	backend, err := ensureBundledBackend()
	if err != nil {
		// Dev mode: call ffprobe/ffmpeg directly using cached paths
		a.mu.Lock()
		ffmpegExe := a.ffmpegExe
		ffprobeExe := a.ffprobeExe
		a.mu.Unlock()

		probe, err := runFFProbe(filePath, ffprobeExe)
		if err != nil {
			result["probeError"] = err.Error()
		} else {
			result["probe"] = probe
		}
		spec, err := generateSpectrogram(filePath, ffmpegExe)
		if err != nil {
			result["spectrogramError"] = err.Error()
		} else {
			result["spectrogram"] = spec
		}
		return result
	}

	// ── Probe ──────────────────────────────────────────────────────────────
	probeCmd := exec.Command(backend, "--probe", filePath)
	hideProcess(probeCmd)
	if out, err := probeCmd.Output(); err == nil {
		var probe map[string]interface{}
		if jsonErr := json.Unmarshal(out, &probe); jsonErr == nil {
			if errMsg, ok := probe["error"].(string); ok {
				result["probeError"] = errMsg
			} else {
				result["probe"] = probe
			}
		} else {
			result["probeError"] = "invalid probe JSON: " + jsonErr.Error()
		}
	} else {
		result["probeError"] = err.Error()
	}

	// ── Spectrogram ────────────────────────────────────────────────────────
	specCmd := exec.Command(backend, "--spectrogram", filePath)
	hideProcess(specCmd)
	if out, err := specCmd.Output(); err == nil {
		var specResult map[string]interface{}
		if jsonErr := json.Unmarshal(out, &specResult); jsonErr == nil {
			if errMsg, ok := specResult["error"].(string); ok {
				result["spectrogramError"] = errMsg
			} else if data, ok := specResult["data"].(string); ok {
				result["spectrogram"] = "data:image/png;base64," + data
			}
		} else {
			result["spectrogramError"] = "invalid spectrogram JSON: " + jsonErr.Error()
		}
	} else {
		result["spectrogramError"] = err.Error()
	}

	return result
}

// PickAnalyzerFiles opens a multi-file picker filtered to audio files.
func (a *App) PickAnalyzerFiles() []string {
	// Wails OpenMultipleFilesDialog expects a slice of FileFilter
	files, err := wailsRuntime.OpenMultipleFilesDialog(a.ctx, wailsRuntime.OpenDialogOptions{
		Title: "Select Audio Files",
		Filters: []wailsRuntime.FileFilter{
			{DisplayName: "Audio Files", Pattern: "*.flac;*.mp3;*.m4a;*.wav;*.aiff;*.aif;*.ogg"},
		},
	})
	if err != nil || len(files) == 0 {
		return []string{}
	}
	paths := make([]string, len(files))
	for i, f := range files {
		paths[i] = filepath.ToSlash(f)
	}
	sort.Strings(paths)
	return paths
}

// WriteFile writes raw bytes (base64-encoded) to a file path on disk.
// Used by the analyzer "Export All" to save PNGs directly to the chosen folder.
func (a *App) WriteFile(filePath string, base64Data string) error {
	data, err := base64.StdEncoding.DecodeString(base64Data)
	if err != nil {
		return fmt.Errorf("base64 decode: %w", err)
	}
	if err := os.MkdirAll(filepath.Dir(filePath), 0755); err != nil {
		return fmt.Errorf("mkdir: %w", err)
	}
	return os.WriteFile(filePath, data, 0644)
}

// ─── ffprobe ─────────────────────────────────────────────────────────────────

// resolveExe returns exePath if non-empty, otherwise falls back to name (looked up via PATH).
func resolveExe(exePath, name string) string {
	if exePath != "" {
		return exePath
	}
	return name
}

func runFFProbe(filePath, ffprobeExe string) (map[string]interface{}, error) {
	cmd := exec.Command(
		resolveExe(ffprobeExe, "ffprobe"),
		"-v", "quiet",
		"-print_format", "json",
		"-show_format",
		"-show_streams",
		"-select_streams", "a:0",
		filePath,
	)
	hideProcess(cmd)

	output, err := cmd.Output()
	if err != nil {
		return nil, fmt.Errorf("ffprobe: %w", err)
	}

	var data map[string]interface{}
	if err := json.Unmarshal(output, &data); err != nil {
		return nil, fmt.Errorf("ffprobe json: %w", err)
	}
	return data, nil
}

// ─── Spectrogram ─────────────────────────────────────────────────────────────

func generateSpectrogram(filePath, ffmpegExe string) (string, error) {
	tmpFile := filePath + ".__spec__.png"
	defer os.Remove(tmpFile)

	cmd := exec.Command(
		resolveExe(ffmpegExe, "ffmpeg"),
		"-y",
		"-i", filePath,
		"-lavfi", "showspectrumpic=s=1200x300:mode=combined:legend=0:color=viridis:scale=log:gain=4",
		"-frames:v", "1",
		tmpFile,
	)
	hideProcess(cmd)

	if out, err := cmd.CombinedOutput(); err != nil {
		return "", fmt.Errorf("ffmpeg: %w — %s", err, strings.TrimSpace(string(out)))
	}

	data, err := os.ReadFile(tmpFile)
	if err != nil {
		return "", fmt.Errorf("read spectrogram: %w", err)
	}

	return "data:image/png;base64," + base64.StdEncoding.EncodeToString(data), nil
}
