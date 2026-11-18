package main

// v2.0.0 SF-1 - persisted download queue.
//
// Before this the desktop had no queue at all: StartDownload killed whatever was
// running and started the new job, so clicking a My Library card or a
// discography selection mid-download silently threw the running download away,
// and closing the app lost the job outright.
//
// The queue lives in Go, not the webview, because Go owns the backend process
// lifecycle and is the only side that is still alive to record "this job was
// interrupted" when the window closes. It is persisted as an atomically-written
// JSON file next to config.json / history.json rather than SQLite: that is how
// the Go side already stores state, and a pure-Go SQLite driver would add
// megabytes to the binary for a table of a handful of rows.
//
// Restore semantics differ from the website's queue on purpose. The website has
// to fear double-charging a server quota on resubmit; the desktop has no
// server-side job. What makes resuming safe here is library dedup: re-running an
// interrupted link skips every track already on disk and fetches only the rest.
// Even so, nothing is resumed automatically - a restored queue starts PAUSED and
// the user chooses Resume or Discard, because starting network activity on
// launch without being asked is the wrong default.

import (
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"time"
)

const (
	queueFileName      = "queue.json"
	queueImportsDir    = "queue-imports"
	queueFileVersion   = 1
	queueMaxItems      = 500
	queueMaxURLsPerJob = 1000
	queueMaxTitleRunes = 200

	QueueKindURLs   = "urls"
	QueueKindImport = "import"
	// PV-1 - the chosen tracks of a previewed release, stored as full
	// TrackMetadata rather than a link so the download does not re-resolve it.
	QueueKindSelection = "selection"

	QueueStateQueued  = "queued"
	QueueStateRunning = "running"
)

// QueueItem is one download job waiting for, or holding, the backend process.
type QueueItem struct {
	ID          string   `json:"id"`
	Kind        string   `json:"kind"`
	URLs        []string `json:"urls,omitempty"`
	Title       string   `json:"title"`
	TrackCount  int      `json:"track_count,omitempty"`
	State       string   `json:"state"`
	AddedAt     string   `json:"added_at"`
	Interrupted bool     `json:"interrupted,omitempty"`
}

// QueueSnapshot is what the frontend renders.
type QueueSnapshot struct {
	Items     []QueueItem `json:"items"`
	Paused    bool        `json:"paused"`
	RunningID string      `json:"running_id"`
	// Restored is true while the queue still holds jobs recovered from a
	// previous session that the user has not yet resumed or discarded.
	Restored bool `json:"restored"`
}

// QueueEnqueueResult tells the caller whether the job started or is waiting.
type QueueEnqueueResult struct {
	ID       string `json:"id"`
	Started  bool   `json:"started"`
	Position int    `json:"position"` // 1-based among waiting jobs; 0 when started
}

type queueFile struct {
	Version int         `json:"version"`
	Items   []QueueItem `json:"items"`
}

// downloadQueue holds state and persistence only. Starting processes is the
// App's job (queue_app.go), which keeps this type testable in a temp dir.
type downloadQueue struct {
	mu        sync.Mutex
	dir       string
	items     []QueueItem
	paused    bool
	restored  bool
	runningID string
	loaded    bool
}

func newDownloadQueue(dir string) *downloadQueue {
	return &downloadQueue{dir: dir}
}

func (q *downloadQueue) filePath() string   { return filepath.Join(q.dir, queueFileName) }
func (q *downloadQueue) importsPath() string { return filepath.Join(q.dir, queueImportsDir) }

func (q *downloadQueue) importPayloadPath(id string) string {
	return filepath.Join(q.importsPath(), id+".json")
}

// kindHasPayload reports whether a job stores a JSON payload beside the queue
// that must be written, read back and deleted with it. Both payload kinds share
// one directory and one naming rule; a kind missing from here leaks its file.
func kindHasPayload(kind string) bool {
	return kind == QueueKindImport || kind == QueueKindSelection
}

func newQueueID() string {
	b := make([]byte, 8)
	if _, err := rand.Read(b); err != nil {
		return fmt.Sprintf("q%d", time.Now().UnixNano())
	}
	return hex.EncodeToString(b)
}

// validQueueID guards every id that is later joined into a file path, so a
// hand-edited queue.json cannot point an import payload outside its folder.
func validQueueID(id string) bool {
	if len(id) == 0 || len(id) > 64 {
		return false
	}
	for _, r := range id {
		if !((r >= '0' && r <= '9') || (r >= 'a' && r <= 'z') || (r >= 'A' && r <= 'Z')) {
			return false
		}
	}
	return true
}

func clampTitle(s string) string {
	s = strings.TrimSpace(strings.ToValidUTF8(s, ""))
	if r := []rune(s); len(r) > queueMaxTitleRunes {
		return string(r[:queueMaxTitleRunes])
	}
	return s
}

// normalizeQueueURLs mirrors the frontend filter in startDownload.
func normalizeQueueURLs(urls []string) []string {
	out := make([]string, 0, len(urls))
	seen := map[string]bool{}
	for _, u := range urls {
		u = strings.TrimSpace(u)
		if !(strings.HasPrefix(u, "http://") || strings.HasPrefix(u, "https://") || strings.HasPrefix(u, "apple-music://")) {
			continue
		}
		if seen[u] {
			continue
		}
		seen[u] = true
		out = append(out, u)
		if len(out) >= queueMaxURLsPerJob {
			break
		}
	}
	return out
}

func defaultURLTitle(urls []string) string {
	if len(urls) == 0 {
		return "Download"
	}
	first := urls[0]
	if len(urls) > 1 {
		return fmt.Sprintf("%s (+%d more)", shortURL(first), len(urls)-1)
	}
	return shortURL(first)
}

func shortURL(u string) string {
	s := u
	for _, p := range []string{"https://", "http://", "www."} {
		s = strings.TrimPrefix(s, p)
	}
	if i := strings.IndexAny(s, "?#"); i >= 0 {
		s = s[:i]
	}
	return clampTitle(s)
}

// load reads the persisted queue once. Any job found is, by definition, from a
// session that ended before finishing it: every item is reset to queued, the
// one that was running is flagged interrupted and moved to the front, and the
// queue starts paused.
func (q *downloadQueue) load() {
	q.mu.Lock()
	defer q.mu.Unlock()
	if q.loaded {
		return
	}
	q.loaded = true

	raw, err := os.ReadFile(q.filePath())
	if err != nil {
		q.pruneOrphanImportsLocked() // no queue means no import payload is wanted
		return
	}
	var f queueFile
	if err := json.Unmarshal(raw, &f); err != nil {
		// Keep the evidence rather than overwriting it on the next save.
		_ = os.Rename(q.filePath(), q.filePath()+".corrupt")
		q.pruneOrphanImportsLocked()
		return
	}

	var interrupted, rest []QueueItem
	for _, it := range f.Items {
		if !validQueueID(it.ID) {
			continue
		}
		it.Title = clampTitle(it.Title)
		switch it.Kind {
		case QueueKindURLs:
			it.URLs = normalizeQueueURLs(it.URLs)
			if len(it.URLs) == 0 {
				continue
			}
		case QueueKindImport, QueueKindSelection:
			it.URLs = nil
			if _, err := os.Stat(q.importPayloadPath(it.ID)); err != nil {
				continue // payload is gone; the job cannot be rebuilt
			}
		default:
			continue
		}
		if it.State == QueueStateRunning {
			it.Interrupted = true
		}
		it.State = QueueStateQueued
		if it.Interrupted {
			interrupted = append(interrupted, it)
		} else {
			rest = append(rest, it)
		}
	}
	q.items = append(interrupted, rest...)
	if len(q.items) > queueMaxItems {
		q.items = q.items[:queueMaxItems]
	}
	if len(q.items) > 0 {
		q.paused = true
		q.restored = true
	}
	q.saveLocked()
	q.pruneOrphanImportsLocked()
}

// saveLocked writes via temp file + rename so a crash mid-write can never leave
// a truncated queue.json. Failure is logged by the caller's absence of state
// only - a queue that cannot persist still works for this session.
func (q *downloadQueue) saveLocked() error {
	if err := os.MkdirAll(q.dir, 0o755); err != nil {
		return err
	}
	data, err := json.MarshalIndent(queueFile{Version: queueFileVersion, Items: q.items}, "", "  ")
	if err != nil {
		return err
	}
	tmp, err := os.CreateTemp(q.dir, "queue-*.json.tmp")
	if err != nil {
		return err
	}
	name := tmp.Name()
	if _, err := tmp.Write(data); err != nil {
		tmp.Close()
		os.Remove(name)
		return err
	}
	if err := tmp.Sync(); err != nil {
		tmp.Close()
		os.Remove(name)
		return err
	}
	if err := tmp.Close(); err != nil {
		os.Remove(name)
		return err
	}
	if err := os.Rename(name, q.filePath()); err != nil {
		os.Remove(name)
		return err
	}
	return nil
}

func (q *downloadQueue) pruneOrphanImportsLocked() {
	entries, err := os.ReadDir(q.importsPath())
	if err != nil {
		return
	}
	keep := map[string]bool{}
	for _, it := range q.items {
		if kindHasPayload(it.Kind) {
			keep[it.ID+".json"] = true
		}
	}
	for _, e := range entries {
		if !e.IsDir() && !keep[e.Name()] {
			_ = os.Remove(filepath.Join(q.importsPath(), e.Name()))
		}
	}
}

func (q *downloadQueue) snapshotLocked() QueueSnapshot {
	items := make([]QueueItem, len(q.items))
	copy(items, q.items)
	return QueueSnapshot{Items: items, Paused: q.paused, RunningID: q.runningID, Restored: q.restored}
}

func (q *downloadQueue) snapshot() QueueSnapshot {
	q.load()
	q.mu.Lock()
	defer q.mu.Unlock()
	return q.snapshotLocked()
}

func (q *downloadQueue) addLocked(it QueueItem) error {
	if len(q.items) >= queueMaxItems {
		return fmt.Errorf("the download queue is full (%d jobs)", queueMaxItems)
	}
	q.items = append(q.items, it)
	return q.saveLocked()
}

func (q *downloadQueue) addURLs(urls []string, title string) (QueueItem, error) {
	q.load()
	clean := normalizeQueueURLs(urls)
	if len(clean) == 0 {
		return QueueItem{}, errors.New("no downloadable links were given")
	}
	title = clampTitle(title)
	if title == "" {
		title = defaultURLTitle(clean)
	}
	it := QueueItem{
		ID: newQueueID(), Kind: QueueKindURLs, URLs: clean, Title: title,
		State: QueueStateQueued, AddedAt: time.Now().UTC().Format(time.RFC3339),
	}
	q.mu.Lock()
	defer q.mu.Unlock()
	if err := q.addLocked(it); err != nil {
		return QueueItem{}, err
	}
	return it, nil
}

// addImport stores the payload beside the queue so an import survives a
// restart; the temp file Python reads is written fresh at start time.
func (q *downloadQueue) addImport(payload, title string, trackCount int) (QueueItem, error) {
	q.load()
	if err := validateImportPayload(payload); err != nil {
		return QueueItem{}, err
	}
	title = clampTitle(title)
	if title == "" {
		title = "Imported playlist"
	}
	it := QueueItem{
		ID: newQueueID(), Kind: QueueKindImport, Title: title, TrackCount: trackCount,
		State: QueueStateQueued, AddedAt: time.Now().UTC().Format(time.RFC3339),
	}
	if err := os.MkdirAll(q.importsPath(), 0o755); err != nil {
		return QueueItem{}, err
	}
	if err := os.WriteFile(q.importPayloadPath(it.ID), []byte(strings.TrimSpace(payload)), 0o600); err != nil {
		return QueueItem{}, err
	}
	q.mu.Lock()
	defer q.mu.Unlock()
	if err := q.addLocked(it); err != nil {
		os.Remove(q.importPayloadPath(it.ID))
		return QueueItem{}, err
	}
	return it, nil
}

// addSelection stores a previewed selection the same way addImport stores an
// imported file: beside the queue, so pressing Download now and then closing the
// app still leaves a job that can be resumed. Reusing the import payload
// directory is deliberate - one place to prune, one naming rule.
func (q *downloadQueue) addSelection(payload, title string, trackCount int) (QueueItem, error) {
	q.load()
	if err := validateSelectionPayload(payload); err != nil {
		return QueueItem{}, err
	}
	title = clampTitle(title)
	if title == "" {
		title = "Selected tracks"
	}
	it := QueueItem{
		ID: newQueueID(), Kind: QueueKindSelection, Title: title, TrackCount: trackCount,
		State: QueueStateQueued, AddedAt: time.Now().UTC().Format(time.RFC3339),
	}
	if err := os.MkdirAll(q.importsPath(), 0o755); err != nil {
		return QueueItem{}, err
	}
	if err := os.WriteFile(q.importPayloadPath(it.ID), []byte(strings.TrimSpace(payload)), 0o600); err != nil {
		return QueueItem{}, err
	}
	q.mu.Lock()
	defer q.mu.Unlock()
	if err := q.addLocked(it); err != nil {
		os.Remove(q.importPayloadPath(it.ID))
		return QueueItem{}, err
	}
	return it, nil
}

func (q *downloadQueue) readImportPayload(id string) (string, error) {
	if !validQueueID(id) {
		return "", errors.New("invalid queue id")
	}
	b, err := os.ReadFile(q.importPayloadPath(id))
	if err != nil {
		return "", err
	}
	return string(b), nil
}

func (q *downloadQueue) indexLocked(id string) int {
	for i, it := range q.items {
		if it.ID == id {
			return i
		}
	}
	return -1
}

func (q *downloadQueue) removeLocked(id string) bool {
	i := q.indexLocked(id)
	if i < 0 {
		return false
	}
	it := q.items[i]
	q.items = append(q.items[:i], q.items[i+1:]...)
	if kindHasPayload(it.Kind) && validQueueID(it.ID) {
		_ = os.Remove(q.importPayloadPath(it.ID))
	}
	if q.runningID == id {
		q.runningID = ""
	}
	q.clearRestoredIfDoneLocked()
	return true
}

func (q *downloadQueue) clearRestoredIfDoneLocked() {
	if !q.restored {
		return
	}
	for _, it := range q.items {
		if it.Interrupted {
			return
		}
	}
	q.restored = false
}

// remove deletes a waiting job. The running job is refused: Stop is the way to
// end that, and it must go through process cancellation.
func (q *downloadQueue) remove(id string) error {
	q.load()
	q.mu.Lock()
	defer q.mu.Unlock()
	if id == q.runningID {
		return errors.New("that download is running - use Stop instead")
	}
	if !q.removeLocked(id) {
		return errors.New("that download is no longer in the queue")
	}
	return q.saveLocked()
}

// clearWaiting removes every job except the running one.
func (q *downloadQueue) clearWaiting() {
	q.load()
	q.mu.Lock()
	defer q.mu.Unlock()
	var keep []QueueItem
	for _, it := range q.items {
		if it.ID == q.runningID {
			keep = append(keep, it)
			continue
		}
		if kindHasPayload(it.Kind) && validQueueID(it.ID) {
			_ = os.Remove(q.importPayloadPath(it.ID))
		}
	}
	q.items = keep
	q.restored = false
	if len(q.items) == 0 {
		q.paused = false
	}
	q.saveLocked()
}

func (q *downloadQueue) setPaused(p bool) {
	q.load()
	q.mu.Lock()
	defer q.mu.Unlock()
	q.paused = p
	if !p {
		q.restored = false
	}
}

// claimNext marks the first waiting job running and returns it. It refuses when
// paused, when a queue job is already running, or when the caller says the
// backend is busy with something else (a failed-track retry).
func (q *downloadQueue) claimNext(backendBusy bool) (QueueItem, bool) {
	q.load()
	q.mu.Lock()
	defer q.mu.Unlock()
	if q.paused || q.runningID != "" || backendBusy {
		return QueueItem{}, false
	}
	return q.claimLocked(0)
}

// claimByID starts one specific job regardless of pause - used when the user
// adds a job while the queue is idle but holds paused leftovers.
func (q *downloadQueue) claimByID(id string, backendBusy bool) (QueueItem, bool) {
	q.load()
	q.mu.Lock()
	defer q.mu.Unlock()
	if q.runningID != "" || backendBusy {
		return QueueItem{}, false
	}
	i := q.indexLocked(id)
	if i < 0 {
		return QueueItem{}, false
	}
	return q.claimLocked(i)
}

func (q *downloadQueue) claimLocked(i int) (QueueItem, bool) {
	for ; i < len(q.items); i++ {
		if q.items[i].State == QueueStateQueued {
			q.items[i].State = QueueStateRunning
			q.runningID = q.items[i].ID
			q.saveLocked()
			return q.items[i], true
		}
	}
	return QueueItem{}, false
}

// unclaim puts a job that failed to START back at the head of the queue and
// pauses, so a broken backend cannot burn through every waiting job.
func (q *downloadQueue) unclaim(id string) {
	q.mu.Lock()
	defer q.mu.Unlock()
	if i := q.indexLocked(id); i >= 0 {
		q.items[i].State = QueueStateQueued
	}
	if q.runningID == id {
		q.runningID = ""
	}
	q.paused = true
	q.saveLocked()
}

// finish records how the running job's process ended. A job that completed,
// failed or was stopped by the user leaves the queue; Stop also pauses, since
// someone pressing Stop means "stop", not "start the next one". When the app
// itself is shutting down the item is deliberately kept, still marked running,
// so the next launch sees it as interrupted.
func (q *downloadQueue) finish(id, status string, shuttingDown bool) {
	q.mu.Lock()
	defer q.mu.Unlock()
	if id == "" || shuttingDown {
		return
	}
	q.removeLocked(id)
	if status == "cancelled" {
		q.paused = true
	}
	if len(q.items) == 0 {
		q.paused = false
		q.restored = false
	}
	q.saveLocked()
}

func (q *downloadQueue) setTitle(id, title string) bool {
	title = clampTitle(title)
	if title == "" {
		return false
	}
	q.mu.Lock()
	defer q.mu.Unlock()
	i := q.indexLocked(id)
	if i < 0 || q.items[i].Title == title {
		return false
	}
	q.items[i].Title = title
	q.saveLocked()
	return true
}

func (q *downloadQueue) waitingPosition(id string) int {
	q.mu.Lock()
	defer q.mu.Unlock()
	pos := 0
	for _, it := range q.items {
		if it.State == QueueStateQueued {
			pos++
			if it.ID == id {
				return pos
			}
		}
	}
	return 0
}
