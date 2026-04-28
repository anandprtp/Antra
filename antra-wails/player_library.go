package main

import (
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io/fs"
	"math/rand"
	"mime"
	"net"
	"net/http"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"time"
)

type libraryReleaseSummary struct {
	Kind         string `json:"kind"`
	RelativePath string `json:"relative_path"`
	Title        string `json:"title"`
	Artist       string `json:"artist,omitempty"`
	Year         string `json:"year,omitempty"`
	TrackCount   int    `json:"track_count"`
	ArtworkURL   string `json:"artwork_url,omitempty"`
}

type libraryReleaseTrack struct {
	Title           string  `json:"title"`
	Artist          string  `json:"artist,omitempty"`
	Album           string  `json:"album,omitempty"`
	FileName        string  `json:"file_name"`
	FilePath        string  `json:"file_path"`
	DiscNumber      int     `json:"disc_number,omitempty"`
	TrackNumber     int     `json:"track_number,omitempty"`
	DurationSeconds float64 `json:"duration_seconds,omitempty"`
	Codec           string  `json:"codec,omitempty"`
	AudioURL        string  `json:"audio_url"`
}

type libraryReleaseDetail struct {
	Kind         string                `json:"kind"`
	RelativePath string                `json:"relative_path"`
	Title        string                `json:"title"`
	Artist       string                `json:"artist,omitempty"`
	Year         string                `json:"year,omitempty"`
	ArtworkURL   string                `json:"artwork_url,omitempty"`
	TrackCount   int                   `json:"track_count"`
	Tracks       []libraryReleaseTrack `json:"tracks"`
}

type libraryPayload struct {
	Albums    []libraryReleaseSummary `json:"albums"`
	Playlists []libraryReleaseSummary `json:"playlists"`
}

var (
	playerAudioExtensions = map[string]bool{
		".flac": true,
		".m4a":  true,
		".mp3":  true,
		".wav":  true,
		".aif":  true,
		".aiff": true,
		".ogg":  true,
		".opus": true,
		".aac":  true,
		".alac": true,
	}
	playerImageExtensions = map[string]bool{
		".jpg":  true,
		".jpeg": true,
		".png":  true,
		".webp": true,
		".gif":  true,
	}
	preferredArtworkNames = []string{
		"cover", "folder", "front", "artwork", "album", "thumb",
	}
)

func (a *App) GetDownloadedMusicLibrary() string {
	cfg := a.GetConfig()
	root := strings.TrimSpace(cfg.DownloadPath)
	if root == "" {
		return `{"albums":[],"playlists":[],"error":"Download path not configured"}`
	}

	_ = a.ensureMediaServer()
	payload := libraryPayload{
		Albums:    a.scanReleaseSummaries(root, "album", cfg.FolderStructure),
		Playlists: a.scanReleaseSummaries(root, "playlist", cfg.FolderStructure),
	}

	data, err := json.Marshal(payload)
	if err != nil {
		return `{"albums":[],"playlists":[],"error":"Failed to encode library"}`
	}
	return string(data)
}

func (a *App) GetDownloadedRelease(relativePath string) string {
	cfg := a.GetConfig()
	root := strings.TrimSpace(cfg.DownloadPath)
	if root == "" {
		return `{"error":"Download path not configured"}`
	}

	_ = a.ensureMediaServer()
	absolutePath, err := resolveLibraryPath(root, relativePath)
	if err != nil {
		return fmt.Sprintf(`{"error":%q}`, err.Error())
	}

	info, err := os.Stat(absolutePath)
	if err != nil || !info.IsDir() {
		return `{"error":"Release folder not found"}`
	}

	kind := inferReleaseKind(relativePath)
	title, artist, year := inferReleaseNames(absolutePath, kind, root)
	trackPaths := collectAudioFiles(absolutePath)
	sort.Strings(trackPaths)

	tracks := make([]libraryReleaseTrack, 0, len(trackPaths))
	for _, trackPath := range trackPaths {
		track := libraryReleaseTrack{
			FileName: filepath.Base(trackPath),
			FilePath: filepath.ToSlash(trackPath),
			AudioURL: a.mediaURL("audio", trackPath),
			Artist:   artist,
			Album:    title,
		}
		applyTrackFallbackMetadata(&track)
		applyTrackProbeMetadata(&track, a.ffprobeExe)
		tracks = append(tracks, track)
	}

	detail := libraryReleaseDetail{
		Kind:         kind,
		RelativePath: filepath.ToSlash(relativePath),
		Title:        title,
		Artist:       artist,
		Year:         year,
		TrackCount:   len(tracks),
		Tracks:       tracks,
	}
	detail.ArtworkURL = a.artworkURLForRelease(absolutePath, trackPaths)

	data, err := json.Marshal(detail)
	if err != nil {
		return `{"error":"Failed to encode release"}`
	}
	return string(data)
}

func (a *App) ensureMediaServer() string {
	a.mu.Lock()
	if a.mediaBaseURL != "" {
		base := a.mediaBaseURL
		a.mu.Unlock()
		return base
	}
	a.mu.Unlock()

	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		return ""
	}

	token := make([]byte, 12)
	if _, err := rand.New(rand.NewSource(time.Now().UnixNano())).Read(token); err != nil {
		copy(token, []byte(fmt.Sprintf("%d", time.Now().UnixNano())))
	}
	tokenHex := hex.EncodeToString(token)

	mux := http.NewServeMux()
	mux.HandleFunc("/media/audio", a.handleAudioMedia)
	mux.HandleFunc("/media/art", a.handleArtworkMedia)
	mux.HandleFunc("/media/embedded-art", a.handleEmbeddedArtworkMedia)
	server := &http.Server{
		Handler:           mux,
		ReadHeaderTimeout: 5 * time.Second,
	}
	baseURL := "http://" + listener.Addr().String()

	a.mu.Lock()
	if a.mediaBaseURL == "" {
		a.mediaServer = server
		a.mediaBaseURL = baseURL
		a.mediaToken = tokenHex
		go func() {
			_ = server.Serve(listener)
		}()
	}
	base := a.mediaBaseURL
	a.mu.Unlock()

	if base != baseURL {
		_ = server.Close()
	}
	return base
}

func (a *App) handleAudioMedia(w http.ResponseWriter, r *http.Request) {
	a.serveLibraryFile(w, r, true)
}

func (a *App) handleArtworkMedia(w http.ResponseWriter, r *http.Request) {
	a.serveLibraryFile(w, r, false)
}

func (a *App) handleEmbeddedArtworkMedia(w http.ResponseWriter, r *http.Request) {
	cfg := a.GetConfig()
	root := strings.TrimSpace(cfg.DownloadPath)
	if root == "" {
		http.Error(w, "library not configured", http.StatusServiceUnavailable)
		return
	}

	a.mu.Lock()
	expectedToken := a.mediaToken
	a.mu.Unlock()
	if expectedToken == "" || r.URL.Query().Get("token") != expectedToken {
		http.Error(w, "forbidden", http.StatusForbidden)
		return
	}

	target := r.URL.Query().Get("path")
	absolutePath, err := resolveLibraryPath(root, target)
	if err != nil {
		http.Error(w, "invalid path", http.StatusBadRequest)
		return
	}
	if !isAudioFile(absolutePath) {
		http.Error(w, "unsupported audio file", http.StatusBadRequest)
		return
	}

	imagePath, err := a.extractEmbeddedArtwork(absolutePath)
	if err != nil || imagePath == "" {
		http.NotFound(w, r)
		return
	}

	file, err := os.Open(imagePath)
	if err != nil {
		http.NotFound(w, r)
		return
	}
	defer file.Close()

	info, err := file.Stat()
	if err != nil || info.IsDir() {
		http.NotFound(w, r)
		return
	}

	w.Header().Set("Content-Type", "image/png")
	w.Header().Set("Cache-Control", "no-store")
	http.ServeContent(w, r, info.Name(), info.ModTime(), file)
}

func (a *App) serveLibraryFile(w http.ResponseWriter, r *http.Request, expectAudio bool) {
	cfg := a.GetConfig()
	root := strings.TrimSpace(cfg.DownloadPath)
	if root == "" {
		http.Error(w, "library not configured", http.StatusServiceUnavailable)
		return
	}

	a.mu.Lock()
	expectedToken := a.mediaToken
	a.mu.Unlock()
	if expectedToken == "" || r.URL.Query().Get("token") != expectedToken {
		http.Error(w, "forbidden", http.StatusForbidden)
		return
	}

	target := r.URL.Query().Get("path")
	absolutePath, err := resolveLibraryPath(root, target)
	if err != nil {
		http.Error(w, "invalid path", http.StatusBadRequest)
		return
	}

	if expectAudio && !isAudioFile(absolutePath) {
		http.Error(w, "unsupported audio file", http.StatusBadRequest)
		return
	}
	if !expectAudio && !isImageFile(absolutePath) {
		http.Error(w, "unsupported image file", http.StatusBadRequest)
		return
	}

	file, err := os.Open(absolutePath)
	if err != nil {
		http.NotFound(w, r)
		return
	}
	defer file.Close()

	info, err := file.Stat()
	if err != nil || info.IsDir() {
		http.NotFound(w, r)
		return
	}

	contentType := mime.TypeByExtension(strings.ToLower(filepath.Ext(absolutePath)))
	if contentType != "" {
		w.Header().Set("Content-Type", contentType)
	}
	w.Header().Set("Cache-Control", "no-store")
	w.Header().Set("Accept-Ranges", "bytes")
	http.ServeContent(w, r, info.Name(), info.ModTime(), file)
}

func (a *App) scanReleaseSummaries(root, kind, folderStructure string) []libraryReleaseSummary {
	var releaseDirs []string
	switch kind {
	case "playlist":
		releaseDirs = collectTopLevelReleaseDirs(filepath.Join(root, "Playlists"))
	default:
		releaseDirs = collectAlbumReleaseDirs(filepath.Join(root, "Albums"), folderStructure)
	}

	results := make([]libraryReleaseSummary, 0, len(releaseDirs))
	for _, releaseDir := range releaseDirs {
		title, artist, year := inferReleaseNames(releaseDir, kind, root)
		trackPaths := collectAudioFiles(releaseDir)
		trackCount := len(trackPaths)
		if trackCount == 0 {
			continue
		}

		rel, err := filepath.Rel(root, releaseDir)
		if err != nil {
			continue
		}
		summary := libraryReleaseSummary{
			Kind:         kind,
			RelativePath: filepath.ToSlash(rel),
			Title:        title,
			Artist:       artist,
			Year:         year,
			TrackCount:   trackCount,
		}
		summary.ArtworkURL = a.artworkURLForRelease(releaseDir, trackPaths)
		results = append(results, summary)
	}

	sort.Slice(results, func(i, j int) bool {
		if results[i].Artist != results[j].Artist {
			return strings.ToLower(results[i].Artist) < strings.ToLower(results[j].Artist)
		}
		return strings.ToLower(results[i].Title) < strings.ToLower(results[j].Title)
	})
	return results
}

func (a *App) artworkURLForRelease(releaseDir string, trackPaths []string) string {
	if art := findArtworkFile(releaseDir); art != "" {
		return a.mediaURL("art", art)
	}
	if len(trackPaths) > 0 {
		return a.mediaURL("embedded-art", trackPaths[0])
	}
	return ""
}

func collectAlbumReleaseDirs(albumsRoot, folderStructure string) []string {
	if !dirExists(albumsRoot) {
		return nil
	}

	if folderStructure == "flat" {
		return collectTopLevelReleaseDirs(albumsRoot)
	}

	var releases []string
	artistDirs, err := os.ReadDir(albumsRoot)
	if err != nil {
		return nil
	}
	for _, artistDir := range artistDirs {
		if !artistDir.IsDir() {
			continue
		}
		fullArtistDir := filepath.Join(albumsRoot, artistDir.Name())
		childReleases := collectTopLevelReleaseDirs(fullArtistDir)
		if len(childReleases) == 0 && hasAudioFilesRecursive(fullArtistDir) {
			releases = append(releases, fullArtistDir)
			continue
		}
		releases = append(releases, childReleases...)
	}
	return releases
}

func collectTopLevelReleaseDirs(root string) []string {
	if !dirExists(root) {
		return nil
	}

	entries, err := os.ReadDir(root)
	if err != nil {
		return nil
	}

	var releases []string
	for _, entry := range entries {
		if !entry.IsDir() {
			continue
		}
		fullPath := filepath.Join(root, entry.Name())
		if hasAudioFilesRecursive(fullPath) {
			releases = append(releases, fullPath)
		}
	}
	return releases
}

func collectAudioFiles(root string) []string {
	var files []string
	filepath.WalkDir(root, func(path string, d fs.DirEntry, err error) error {
		if err != nil || d == nil || d.IsDir() {
			return nil
		}
		if isAudioFile(path) {
			files = append(files, path)
		}
		return nil
	})
	return files
}

func hasAudioFilesRecursive(root string) bool {
	found := false
	filepath.WalkDir(root, func(path string, d fs.DirEntry, err error) error {
		if err != nil || d == nil || d.IsDir() {
			return nil
		}
		if isAudioFile(path) {
			found = true
			return fs.SkipAll
		}
		return nil
	})
	return found
}

func inferReleaseNames(releaseDir, kind, root string) (string, string, string) {
	title := filepath.Base(releaseDir)
	artist := ""
	if kind == "album" {
		parent := filepath.Base(filepath.Dir(releaseDir))
		if !strings.EqualFold(parent, "Albums") {
			artist = parent
		}
	}
	year := ""
	if idx := strings.LastIndex(title, "("); idx >= 0 && strings.HasSuffix(title, ")") {
		candidate := strings.TrimSuffix(strings.TrimSpace(title[idx+1:]), ")")
		if len(candidate) == 4 && allDigits(candidate) {
			year = candidate
			title = strings.TrimSpace(title[:idx])
		}
	}
	if kind == "playlist" && artist == "" {
		artist = "Playlist"
	}
	if title == "" {
		title = filepath.Base(releaseDir)
	}
	if rel, err := filepath.Rel(root, releaseDir); err == nil && rel == "." {
		title = "Music"
	}
	return title, artist, year
}

func inferReleaseKind(relativePath string) string {
	parts := strings.Split(strings.ToLower(filepath.ToSlash(relativePath)), "/")
	if len(parts) > 0 && parts[0] == "playlists" {
		return "playlist"
	}
	return "album"
}

func findArtworkFile(dir string) string {
	entries, err := os.ReadDir(dir)
	if err != nil {
		return ""
	}

	for _, baseName := range preferredArtworkNames {
		for _, entry := range entries {
			if entry.IsDir() {
				continue
			}
			name := strings.TrimSuffix(strings.ToLower(entry.Name()), filepath.Ext(entry.Name()))
			if name == baseName && isImageFile(entry.Name()) {
				return filepath.Join(dir, entry.Name())
			}
		}
	}
	for _, entry := range entries {
		if entry.IsDir() {
			continue
		}
		if isImageFile(entry.Name()) {
			return filepath.Join(dir, entry.Name())
		}
	}
	return ""
}

func (a *App) extractEmbeddedArtwork(audioPath string) (string, error) {
	cacheDir := filepath.Join(getAppDataDir(), "player_art_cache")
	if err := os.MkdirAll(cacheDir, 0755); err != nil {
		return "", err
	}

	sum := hex.EncodeToString([]byte(strings.ToLower(audioPath)))
	if len(sum) > 48 {
		sum = sum[:48]
	}
	outputPath := filepath.Join(cacheDir, sum+".png")
	if info, err := os.Stat(outputPath); err == nil && !info.IsDir() && info.Size() > 0 {
		return outputPath, nil
	}

	cmd := exec.Command(
		resolveExe(a.ffmpegExe, "ffmpeg"),
		"-y",
		"-i", audioPath,
		"-an",
		"-map", "0:v:0",
		"-frames:v", "1",
		outputPath,
	)
	hideProcess(cmd)
	if out, err := cmd.CombinedOutput(); err != nil {
		_ = os.Remove(outputPath)
		return "", fmt.Errorf("ffmpeg artwork extract: %w (%s)", err, strings.TrimSpace(string(out)))
	}
	return outputPath, nil
}

func (a *App) mediaURL(kind, absolutePath string) string {
	a.mu.Lock()
	baseURL := a.mediaBaseURL
	token := a.mediaToken
	a.mu.Unlock()
	if baseURL == "" || absolutePath == "" {
		return ""
	}
	values := url.Values{}
	values.Set("path", absolutePath)
	values.Set("token", token)
	return fmt.Sprintf("%s/media/%s?%s", baseURL, kind, values.Encode())
}

func resolveLibraryPath(root, requested string) (string, error) {
	rootAbs, err := filepath.Abs(root)
	if err != nil {
		return "", err
	}

	if requested == "" {
		return "", fmt.Errorf("empty path")
	}

	requested = filepath.Clean(filepath.FromSlash(requested))
	target := requested
	if !filepath.IsAbs(target) {
		target = filepath.Join(rootAbs, requested)
	}
	targetAbs, err := filepath.Abs(target)
	if err != nil {
		return "", err
	}

	rel, err := filepath.Rel(rootAbs, targetAbs)
	if err != nil || rel == ".." || strings.HasPrefix(rel, ".."+string(filepath.Separator)) {
		return "", fmt.Errorf("path escapes library root")
	}
	return targetAbs, nil
}

func applyTrackFallbackMetadata(track *libraryReleaseTrack) {
	base := strings.TrimSuffix(track.FileName, filepath.Ext(track.FileName))
	base = strings.TrimSpace(base)
	if parts := strings.SplitN(base, " - ", 2); len(parts) == 2 {
		left := strings.TrimSpace(parts[0])
		right := strings.TrimSpace(parts[1])
		if len(left) >= 2 && allDigits(left[:2]) {
			track.Title = right
			num, _ := strconv.Atoi(left)
			if num >= 100 {
				track.DiscNumber = num / 100
				track.TrackNumber = num % 100
			} else {
				track.TrackNumber = num
			}
			return
		}
	}
	track.Title = base
}

func applyTrackProbeMetadata(track *libraryReleaseTrack, ffprobeExe string) {
	probe, err := runFFProbe(track.FilePath, ffprobeExe)
	if err != nil {
		return
	}

	streams, _ := probe["streams"].([]interface{})
	if len(streams) > 0 {
		if stream, ok := streams[0].(map[string]interface{}); ok {
			if codec, ok := stream["codec_name"].(string); ok {
				track.Codec = strings.ToUpper(codec)
			}
			if disc := getProbeTag(stream, "disc", "DISC", "discnumber", "DISCNUMBER"); disc != "" && track.DiscNumber == 0 {
				track.DiscNumber = parseTagNumber(disc)
			}
			if number := getProbeTag(stream, "track", "TRACK", "tracknumber", "TRACKNUMBER"); number != "" && track.TrackNumber == 0 {
				track.TrackNumber = parseTagNumber(number)
			}
		}
	}

	format, _ := probe["format"].(map[string]interface{})
	if format != nil {
		if duration, ok := format["duration"].(string); ok {
			if seconds, err := strconv.ParseFloat(duration, 64); err == nil {
				track.DurationSeconds = seconds
			}
		}
		if track.Title == "" {
			if title := getProbeTag(format, "title", "TITLE"); title != "" {
				track.Title = title
			}
		} else if title := getProbeTag(format, "title", "TITLE"); title != "" {
			track.Title = title
		}
		if artist := getProbeTag(format, "artist", "ARTIST", "album_artist", "ALBUM_ARTIST"); artist != "" {
			track.Artist = artist
		}
		if album := getProbeTag(format, "album", "ALBUM"); album != "" {
			track.Album = album
		}
		if disc := getProbeTag(format, "disc", "DISC", "discnumber", "DISCNUMBER"); disc != "" && track.DiscNumber == 0 {
			track.DiscNumber = parseTagNumber(disc)
		}
		if number := getProbeTag(format, "track", "TRACK", "tracknumber", "TRACKNUMBER"); number != "" && track.TrackNumber == 0 {
			track.TrackNumber = parseTagNumber(number)
		}
	}

	if track.Title == "" {
		track.Title = strings.TrimSuffix(track.FileName, filepath.Ext(track.FileName))
	}
}

func getProbeTag(section map[string]interface{}, keys ...string) string {
	tags, ok := section["tags"].(map[string]interface{})
	if !ok {
		return ""
	}
	for _, key := range keys {
		if value, ok := tags[key]; ok {
			if text := strings.TrimSpace(fmt.Sprintf("%v", value)); text != "" {
				return text
			}
		}
	}
	return ""
}

func parseTagNumber(value string) int {
	head := strings.TrimSpace(strings.SplitN(value, "/", 2)[0])
	n, _ := strconv.Atoi(head)
	return n
}

func isAudioFile(path string) bool {
	return playerAudioExtensions[strings.ToLower(filepath.Ext(path))]
}

func isImageFile(path string) bool {
	return playerImageExtensions[strings.ToLower(filepath.Ext(path))]
}

func dirExists(path string) bool {
	info, err := os.Stat(path)
	return err == nil && info.IsDir()
}

func allDigits(value string) bool {
	for _, r := range value {
		if r < '0' || r > '9' {
			return false
		}
	}
	return value != ""
}
