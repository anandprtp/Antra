package main

import (
	"encoding/base64"
	"encoding/json"
	"fmt"
	"io/fs"
	"math"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"sync"

	wailsRuntime "github.com/wailsapp/wails/v2/pkg/runtime"
)

var audioExtensions = map[string]bool{
	".flac": true,
	".mp3":  true,
	".m4a":  true,
	".mp4":  true,
	".aac":  true,
	".alac": true,
	".wav":  true,
	".wave": true,
	".aiff": true,
	".aif":  true,
	".ogg":  true,
	".opus": true,
}

// Regex patterns for ffmpeg audio analysis output.
var (
	reMaxVolume  = regexp.MustCompile(`max_volume:\s*([-\d.]+)\s*dB`)
	reMeanVolume = regexp.MustCompile(`mean_volume:\s*([-\d.]+)\s*dB`)
	reLufsI      = regexp.MustCompile(`\bI:\s*([-\d.]+)\s*LUFS`)
	reLufsLRA    = regexp.MustCompile(`\bLRA:\s*([-\d.]+)\s*LU`)
	reTruePeak   = regexp.MustCompile(`\bPeak:\s*([-\d.]+)\s*dBTP`)
)

// AudioStats holds audio quality measurements.
// A value of -999 means "not measured / unavailable".
type AudioStats struct {
	PeakDb     float64 `json:"peakDb"`
	RmsDb      float64 `json:"rmsDb"`
	TruePeakDb float64 `json:"truePeakDb"`
	LufsI      float64 `json:"lufsI"`
	LufsLRA    float64 `json:"lufsLRA"`
	CutoffHz   int     `json:"cutoffHz"`
}

// parseLastFloat finds the last regex match in text and returns it as float64.
// Returns (0, false) on no match, parse error, ±Inf, or NaN.
func parseLastFloat(re *regexp.Regexp, text string) (float64, bool) {
	matches := re.FindAllStringSubmatch(text, -1)
	if len(matches) == 0 {
		return 0, false
	}
	last := matches[len(matches)-1]
	v, err := strconv.ParseFloat(last[1], 64)
	if err != nil || math.IsInf(v, 0) || math.IsNaN(v) {
		return 0, false
	}
	return v, true
}

// runAudioStats measures peak, RMS, LUFS-I, LRA, and true peak.
func runAudioStats(filePath, ffmpegExe string) (*AudioStats, error) {
	cmd := exec.Command(
		resolveExe(ffmpegExe, "ffmpeg"),
		"-i", filePath,
		"-af", "volumedetect,ebur128=peak=true",
		"-f", "null", "-",
	)
	hideProcess(cmd)
	out, _ := cmd.CombinedOutput() // ffmpeg exits non-zero for -f null

	text := string(out)
	stats := &AudioStats{
		PeakDb:     -999,
		RmsDb:      -999,
		TruePeakDb: -999,
		LufsI:      -999,
		LufsLRA:    -999,
	}

	if v, ok := parseLastFloat(reMaxVolume, text); ok {
		stats.PeakDb = v
	}
	if v, ok := parseLastFloat(reMeanVolume, text); ok {
		stats.RmsDb = v
	}
	if v, ok := parseLastFloat(reLufsI, text); ok {
		stats.LufsI = v
	}
	if v, ok := parseLastFloat(reLufsLRA, text); ok {
		stats.LufsLRA = v
	}
	if v, ok := parseLastFloat(reTruePeak, text); ok {
		stats.TruePeakDb = v
	}

	return stats, nil
}

// measureRMSAboveFreq applies a highpass at freqHz to a 30-second window
// starting at startSec and returns the mean RMS level.
func measureRMSAboveFreq(filePath, ffmpegExe string, freqHz int, startSec float64) (float64, bool) {
	cmd := exec.Command(
		resolveExe(ffmpegExe, "ffmpeg"),
		"-ss", fmt.Sprintf("%.1f", startSec),
		"-i", filePath,
		"-t", "30",
		"-af", fmt.Sprintf("highpass=f=%d:poles=2,volumedetect", freqHz),
		"-f", "null", "-",
	)
	hideProcess(cmd)
	out, _ := cmd.CombinedOutput()
	return parseLastFloat(reMeanVolume, string(out))
}

// detectFrequencyCutoff estimates the highest frequency that still contains
// significant audio content (within 42 dB of the full-spectrum RMS baseline).
// Returns 0 when detection is unreliable (e.g., near-silent content).
func detectFrequencyCutoff(filePath, ffmpegExe string, durationSec float64) int {
	// Skip into the track to avoid silent intros
	startSec := durationSec * 0.1
	if startSec > 10 {
		startSec = 10
	}
	if durationSec < 10 {
		startSec = 0
	}

	// Baseline: full-spectrum RMS
	baseCmd := exec.Command(
		resolveExe(ffmpegExe, "ffmpeg"),
		"-ss", fmt.Sprintf("%.1f", startSec),
		"-i", filePath,
		"-t", "30",
		"-af", "volumedetect",
		"-f", "null", "-",
	)
	hideProcess(baseCmd)
	baseOut, _ := baseCmd.CombinedOutput()
	baselineRMS, ok := parseLastFloat(reMeanVolume, string(baseOut))
	if !ok || baselineRMS < -60 {
		return 0 // near-silent — unreliable
	}

	threshold := baselineRMS - 42.0

	// Probe frequencies in parallel (lowest → highest, so we check full range)
	freqs := []int{8000, 12000, 16000, 17000, 19000, 21000}
	type probeResult struct {
		freq int
		rms  float64
		ok   bool
	}
	results := make([]probeResult, len(freqs))
	var wg sync.WaitGroup
	for i, f := range freqs {
		wg.Add(1)
		go func(idx, freq int) {
			defer wg.Done()
			rms, ok := measureRMSAboveFreq(filePath, ffmpegExe, freq, startSec)
			results[idx] = probeResult{freq, rms, ok}
		}(i, f)
	}
	wg.Wait()

	// Highest frequency that still has content above the threshold
	cutoff := 0
	for _, r := range results {
		if r.ok && r.rms > threshold && r.freq > cutoff {
			cutoff = r.freq
		}
	}
	return cutoff
}

// extractDuration pulls the audio duration from a ffprobe result map.
func extractDuration(probe map[string]interface{}) float64 {
	if probe == nil {
		return 0
	}
	fmtMap, ok := probe["format"].(map[string]interface{})
	if !ok {
		return 0
	}
	durStr, ok := fmtMap["duration"].(string)
	if !ok {
		return 0
	}
	d, _ := strconv.ParseFloat(durStr, 64)
	return d
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

// AnalyzeAudio runs ffprobe for metadata, ffmpeg showspectrumpic for the
// spectrogram, and ffmpeg audio filters for quality statistics (peak, RMS,
// LUFS, LRA, true peak, frequency cutoff).
// Probe, spectrogram, and stats run concurrently; cutoff detection runs after
// the probe returns a duration.
func (a *App) AnalyzeAudio(filePath string) map[string]interface{} {
	result := make(map[string]interface{})

	a.mu.Lock()
	ffmpegExe := a.ffmpegExe
	ffprobeExe := a.ffprobeExe
	a.mu.Unlock()

	backend, backendErr := ensureBundledBackend()

	var (
		probe    map[string]interface{}
		probeErr error
		spec     string
		specErr  error
		stats    *AudioStats
	)

	var wg sync.WaitGroup
	wg.Add(3)

	// ── Probe ──────────────────────────────────────────────────────────────────
	go func() {
		defer wg.Done()
		if backendErr != nil {
			probe, probeErr = runFFProbe(filePath, ffprobeExe)
			return
		}
		probeCmd := exec.Command(backend, "--probe", filePath)
		hideProcess(probeCmd)
		out, err := probeCmd.Output()
		if err != nil {
			probeErr = err
			return
		}
		var p map[string]interface{}
		if jsonErr := json.Unmarshal(out, &p); jsonErr != nil {
			probeErr = fmt.Errorf("invalid probe JSON: %w", jsonErr)
			return
		}
		if errMsg, ok := p["error"].(string); ok {
			probeErr = fmt.Errorf("%s", errMsg)
		} else {
			probe = p
		}
	}()

	// ── Spectrogram ────────────────────────────────────────────────────────────
	go func() {
		defer wg.Done()
		if backendErr != nil {
			spec, specErr = generateSpectrogram(filePath, ffmpegExe)
			return
		}
		specCmd := exec.Command(backend, "--spectrogram", filePath)
		hideProcess(specCmd)
		out, err := specCmd.Output()
		if err != nil {
			specErr = err
			return
		}
		var specResult map[string]interface{}
		if jsonErr := json.Unmarshal(out, &specResult); jsonErr != nil {
			specErr = fmt.Errorf("invalid spectrogram JSON: %w", jsonErr)
			return
		}
		if errMsg, ok := specResult["error"].(string); ok {
			specErr = fmt.Errorf("%s", errMsg)
		} else if data, ok := specResult["data"].(string); ok {
			spec = "data:image/png;base64," + data
		}
	}()

	// ── Audio stats (always use cached ffmpegExe) ──────────────────────────────
	go func() {
		defer wg.Done()
		stats, _ = runAudioStats(filePath, ffmpegExe)
	}()

	wg.Wait()

	// ── Frequency cutoff detection ─────────────────────────────────────────────
	// Runs after the probe so we have a duration; uses the same ffmpegExe.
	if stats != nil {
		durationSec := extractDuration(probe)
		stats.CutoffHz = detectFrequencyCutoff(filePath, ffmpegExe, durationSec)
	}

	// ── Build result ───────────────────────────────────────────────────────────
	if probeErr != nil {
		result["probeError"] = probeErr.Error()
	} else if probe != nil {
		result["probe"] = probe
	}

	if specErr != nil {
		result["spectrogramError"] = specErr.Error()
	} else if spec != "" {
		result["spectrogram"] = spec
	}

	if stats != nil {
		result["stats"] = stats
	}

	return result
}

func (a *App) pickAudioFiles(title string) []string {
	files, err := wailsRuntime.OpenMultipleFilesDialog(a.ctx, wailsRuntime.OpenDialogOptions{
		Title: title,
		Filters: []wailsRuntime.FileFilter{
			{DisplayName: "Audio Files", Pattern: "*.flac;*.mp3;*.m4a;*.mp4;*.aac;*.alac;*.wav;*.wave;*.aiff;*.aif;*.ogg;*.opus"},
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

// PickAnalyzerFiles opens a multi-file picker filtered to audio files.
func (a *App) PickAnalyzerFiles() []string {
	return a.pickAudioFiles("Select Audio Files")
}

// PickImportFiles opens a multi-file picker for local library imports.
func (a *App) PickImportFiles() []string {
	return a.pickAudioFiles("Select Music Files to Import")
}

// PickImportFolder opens a folder picker for local library imports.
func (a *App) PickImportFolder() string {
	dir, err := wailsRuntime.OpenDirectoryDialog(a.ctx, wailsRuntime.OpenDialogOptions{
		Title: "Select Music Folder to Import",
	})
	if err != nil {
		return ""
	}
	return filepath.ToSlash(dir)
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

// ─── ffprobe ──────────────────────────────────────────────────────────────────

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

// ─── Spectrogram ──────────────────────────────────────────────────────────────

func generateSpectrogram(filePath, ffmpegExe string) (string, error) {
	tmpFile := filePath + ".__spec__.png"
	defer os.Remove(tmpFile)

	cmd := exec.Command(
		resolveExe(ffmpegExe, "ffmpeg"),
		"-y",
		"-i", filePath,
		"-lavfi", "showspectrumpic=s=1400x400:mode=combined:legend=1:color=viridis:scale=log:gain=4",
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
