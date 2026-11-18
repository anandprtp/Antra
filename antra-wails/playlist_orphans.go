package main

// issue #75 — files left behind in a playlist folder.
//
// When a playlist loses a song, or when a run renumbers the folder, the file
// that used to be there is not removed by anything. The Python engine reports
// them at the end of a COMPLETE playlist run; this file decides what the user
// can do about them.
//
// The webview never sees a filesystem path. It gets an opaque id plus the
// basenames, and hands the id back to DeletePlaylistOrphans. The same rule PV-1
// established for its preview cache applies with more force here, because the
// call on the other end of it deletes files: a path travelling through the
// webview would be an arbitrary-file argument to os.Remove.
//
// Nothing here deletes on its own. The engine deletes only in
// playlist_prune_orphans="auto" mode, on the Python side, and then reports what
// it removed with pruned=true so the frontend states it rather than offering a
// button.

import (
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"sync"
)

// A user who syncs several playlists in one run leaves one set each. Keeping a
// handful means the panel for an earlier playlist still works after a later one
// reports; anything older is dropped, and dropping a set only loses the ability
// to delete through the UI — the files are untouched.
const playlistOrphanKeep = 8

type playlistOrphanSet struct {
	id       string
	playlist string
	paths    []string
}

type playlistOrphanStore struct {
	mu   sync.Mutex
	sets []playlistOrphanSet // oldest first
}

func (s *playlistOrphanStore) add(id, playlist string, paths []string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	kept := make([]string, len(paths))
	copy(kept, paths)
	s.sets = append(s.sets, playlistOrphanSet{id: id, playlist: playlist, paths: kept})
	for len(s.sets) > playlistOrphanKeep {
		s.sets = s.sets[1:]
	}
}

// take returns a set AND removes it, so a double-click cannot run the deletion
// twice. The second call reports that the set is gone rather than silently
// succeeding against files that are already deleted.
func (s *playlistOrphanStore) take(id string) (playlistOrphanSet, bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	for i, set := range s.sets {
		if set.id == id {
			s.sets = append(s.sets[:i], s.sets[i+1:]...)
			return set, true
		}
	}
	return playlistOrphanSet{}, false
}

func (a *App) playlistOrphans() *playlistOrphanStore {
	a.playlistOrphanOnce.Do(func() { a.playlistOrphanStore = &playlistOrphanStore{} })
	return a.playlistOrphanStore
}

// interceptPlaylistOrphans converts the backend's playlist_orphans line into the
// payload the webview is allowed to see: an id, the count and the basenames.
//
// It returns nil when the line names no usable file, and the caller then drops
// the event entirely rather than relaying the original — this is the one message
// in the stream that carries raw paths, so "relay unchanged" is not an option.
func (a *App) interceptPlaylistOrphans(payload map[string]interface{}) map[string]interface{} {
	raw, ok := payload["paths"].([]interface{})
	if !ok || len(raw) == 0 {
		return nil
	}
	paths := make([]string, 0, len(raw))
	names := make([]interface{}, 0, len(raw))
	for _, v := range raw {
		p, ok := v.(string)
		if !ok || strings.TrimSpace(p) == "" {
			continue
		}
		paths = append(paths, p)
		names = append(names, filepath.Base(p))
	}
	if len(paths) == 0 {
		return nil
	}

	playlist, _ := payload["playlist"].(string)
	pruned, _ := payload["pruned"].(bool)

	out := map[string]interface{}{
		"type":     "playlist_orphans",
		"playlist": playlist,
		"count":    len(paths),
		"names":    names,
		"pruned":   pruned,
	}
	// An "auto" run already deleted them. Handing out an id would offer a
	// delete button for files that no longer exist.
	if !pruned {
		id := newQueueID()
		a.playlistOrphans().add(id, playlist, paths)
		out["orphan_id"] = id
	}
	return out
}

// DeletePlaylistOrphansResult is what the frontend renders after the user
// confirms. Failed is non-zero when some files could not be removed — a file
// open in a media player is the common case — and the user can retry by
// re-running the playlist.
type DeletePlaylistOrphansResult struct {
	Deleted int    `json:"deleted"`
	Failed  int    `json:"failed"`
	Message string `json:"message"`
}

// DeletePlaylistOrphans removes the files of one reported set.
//
// Every path is re-checked here even though Python produced it. The store is the
// only thing between an id from the webview and os.Remove, so the guards are
// deliberately paranoid: inside the configured library root, a supported audio
// extension, and a regular file. A path failing any of them is counted as failed
// and left alone rather than silently skipped.
func (a *App) DeletePlaylistOrphans(orphanID string) (DeletePlaylistOrphansResult, error) {
	if !validQueueID(orphanID) {
		return DeletePlaylistOrphansResult{}, fmt.Errorf("that list of leftover files is no longer available")
	}
	set, ok := a.playlistOrphans().take(orphanID)
	if !ok {
		return DeletePlaylistOrphansResult{}, fmt.Errorf("that list of leftover files is no longer available")
	}

	root := strings.TrimSpace(a.GetConfig().DownloadPath)
	if root == "" {
		return DeletePlaylistOrphansResult{}, fmt.Errorf("no music library folder is configured")
	}
	rootAbs, err := filepath.Abs(root)
	if err != nil {
		return DeletePlaylistOrphansResult{}, fmt.Errorf("could not resolve the music library folder: %w", err)
	}

	var result DeletePlaylistOrphansResult
	for _, p := range set.paths {
		if !isDeletablePlaylistOrphan(p, rootAbs) {
			result.Failed++
			continue
		}
		if err := os.Remove(p); err != nil {
			result.Failed++
			continue
		}
		result.Deleted++
	}

	result.Message = fmt.Sprintf("Removed %d leftover file(s) from %s.", result.Deleted, set.playlist)
	if result.Failed > 0 {
		result.Message = fmt.Sprintf(
			"Removed %d leftover file(s) from %s; %d could not be removed.",
			result.Deleted, set.playlist, result.Failed,
		)
	}
	return result, nil
}

// isDeletablePlaylistOrphan is the whole safety boundary, kept as one testable
// function rather than inlined conditions.
func isDeletablePlaylistOrphan(path, rootAbs string) bool {
	if strings.TrimSpace(path) == "" {
		return false
	}
	abs, err := filepath.Abs(path)
	if err != nil {
		return false
	}
	if !isAudioExtension(filepath.Ext(abs)) {
		return false
	}
	// filepath.Rel is the containment check: a "..\" first element means the
	// path climbed out of the root. Comparing string prefixes instead would
	// accept "C:\Music-other" for a root of "C:\Music".
	rel, err := filepath.Rel(rootAbs, abs)
	if err != nil {
		return false
	}
	if rel == "." || rel == ".." || strings.HasPrefix(rel, ".."+string(filepath.Separator)) {
		return false
	}
	// Lstat, not Stat: a symlink must never be followed out of the library.
	info, err := os.Lstat(abs)
	if err != nil || !info.Mode().IsRegular() {
		return false
	}
	return true
}

// Must stay in step with SUPPORTED_AUDIO_EXTENSIONS in antra/utils/organizer.py.
// A format missing here is refused, never deleted by accident, so drifting apart
// fails safe.
func isAudioExtension(ext string) bool {
	switch strings.ToLower(ext) {
	case ".flac", ".mp3", ".aac", ".m4a", ".mp4", ".opus":
		return true
	}
	return false
}
