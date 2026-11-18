package main

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

const testImportPayload = `{"name":"Mix","tracks":[{"title":"A","artists":["X"]}]}`

// reopen simulates an app restart: a fresh queue over the same directory.
func reopen(t *testing.T, dir string) *downloadQueue {
	t.Helper()
	q := newDownloadQueue(dir)
	q.load()
	return q
}

func mustAdd(t *testing.T, q *downloadQueue, urls ...string) QueueItem {
	t.Helper()
	it, err := q.addURLs(urls, "")
	if err != nil {
		t.Fatalf("addURLs: %v", err)
	}
	return it
}

func TestQueueFreshStartIsEmptyAndUnpaused(t *testing.T) {
	q := reopen(t, t.TempDir())
	s := q.snapshot()
	if len(s.Items) != 0 || s.Paused || s.Restored {
		t.Fatalf("fresh queue = %+v", s)
	}
}

func TestQueueClaimRunsInOrderAndOneAtATime(t *testing.T) {
	q := reopen(t, t.TempDir())
	a := mustAdd(t, q, "https://open.spotify.com/album/a")
	b := mustAdd(t, q, "https://open.spotify.com/album/b")

	got, ok := q.claimNext(false)
	if !ok || got.ID != a.ID {
		t.Fatalf("first claim = %v %v, want %s", got.ID, ok, a.ID)
	}
	if _, ok := q.claimNext(false); ok {
		t.Fatal("a second job was claimed while one is running")
	}
	q.finish(a.ID, "completed", false)
	got, ok = q.claimNext(false)
	if !ok || got.ID != b.ID {
		t.Fatalf("after finish claim = %v %v, want %s", got.ID, ok, b.ID)
	}
}

func TestQueueRefusesWhileBackendBusyWithRetry(t *testing.T) {
	q := reopen(t, t.TempDir())
	mustAdd(t, q, "https://open.spotify.com/album/a")
	if _, ok := q.claimNext(true); ok {
		t.Fatal("claimed a job while a failed-track retry holds the backend")
	}
	if _, ok := q.claimNext(false); !ok {
		t.Fatal("did not claim once the backend was free")
	}
}

// The case the feature exists for: the app closes mid-download.
func TestQueueRestartRestoresInterruptedJobPausedAndFirst(t *testing.T) {
	dir := t.TempDir()
	q := reopen(t, dir)
	a := mustAdd(t, q, "https://open.spotify.com/album/a")
	b := mustAdd(t, q, "https://open.spotify.com/album/b")
	// Run b ahead of a (a paste while idle), then shut down mid-download.
	if _, ok := q.claimByID(b.ID, false); !ok {
		t.Fatal("claimByID failed")
	}
	q.finish(b.ID, "failed", true) // shutdown kill: must be ignored

	r := reopen(t, dir)
	s := r.snapshot()
	if len(s.Items) != 2 {
		t.Fatalf("restored %d items, want 2", len(s.Items))
	}
	if !s.Paused || !s.Restored {
		t.Fatalf("restored queue must start paused+restored, got %+v", s)
	}
	if s.Items[0].ID != b.ID || !s.Items[0].Interrupted {
		t.Fatalf("interrupted job not first: %+v", s.Items)
	}
	if s.Items[1].ID != a.ID || s.Items[1].Interrupted {
		t.Fatalf("waiting job wrongly flagged: %+v", s.Items[1])
	}
	for _, it := range s.Items {
		if it.State != QueueStateQueued {
			t.Fatalf("restored item left in state %q", it.State)
		}
	}
	if s.RunningID != "" {
		t.Fatalf("restored queue claims a running job: %q", s.RunningID)
	}
	if _, ok := r.claimNext(false); ok {
		t.Fatal("restored queue auto-started without Resume")
	}
	r.setPaused(false)
	if got, ok := r.claimNext(false); !ok || got.ID != b.ID {
		t.Fatalf("Resume did not start the interrupted job first: %v %v", got.ID, ok)
	}
	if r.snapshot().Restored {
		t.Fatal("Restored flag survived Resume")
	}
}

// A hard crash never runs finish at all; the file still says running.
func TestQueueCrashWithoutFinishIsAlsoInterrupted(t *testing.T) {
	dir := t.TempDir()
	q := reopen(t, dir)
	a := mustAdd(t, q, "https://open.spotify.com/album/a")
	q.claimNext(false)
	r := reopen(t, dir)
	s := r.snapshot()
	if len(s.Items) != 1 || s.Items[0].ID != a.ID || !s.Items[0].Interrupted || !s.Paused {
		t.Fatalf("crash restore = %+v", s)
	}
}

func TestQueueFinishRemovesCompletedAndFailed(t *testing.T) {
	for _, status := range []string{"completed", "failed"} {
		dir := t.TempDir()
		q := reopen(t, dir)
		a := mustAdd(t, q, "https://open.spotify.com/album/a")
		mustAdd(t, q, "https://open.spotify.com/album/b")
		q.claimNext(false)
		q.finish(a.ID, status, false)
		s := reopen(t, dir).snapshot()
		if len(s.Items) != 1 || s.Items[0].ID == a.ID {
			t.Fatalf("%s: job not removed: %+v", status, s.Items)
		}
		if q.snapshot().Paused {
			t.Fatalf("%s must not pause the queue", status)
		}
	}
}

func TestQueueStopRemovesJobAndPauses(t *testing.T) {
	q := reopen(t, t.TempDir())
	a := mustAdd(t, q, "https://open.spotify.com/album/a")
	mustAdd(t, q, "https://open.spotify.com/album/b")
	q.claimNext(false)
	q.finish(a.ID, "cancelled", false)
	s := q.snapshot()
	if len(s.Items) != 1 || !s.Paused {
		t.Fatalf("Stop result = %+v", s)
	}
	if _, ok := q.claimNext(false); ok {
		t.Fatal("next job started after the user pressed Stop")
	}
}

func TestQueueStopOnLastJobLeavesQueueUnpaused(t *testing.T) {
	q := reopen(t, t.TempDir())
	a := mustAdd(t, q, "https://open.spotify.com/album/a")
	q.claimNext(false)
	q.finish(a.ID, "cancelled", false)
	if s := q.snapshot(); s.Paused || len(s.Items) != 0 {
		t.Fatalf("an empty queue must not stay paused: %+v", s)
	}
}

func TestQueueRemoveRefusesRunningJob(t *testing.T) {
	q := reopen(t, t.TempDir())
	a := mustAdd(t, q, "https://open.spotify.com/album/a")
	b := mustAdd(t, q, "https://open.spotify.com/album/b")
	q.claimNext(false)
	if err := q.remove(a.ID); err == nil {
		t.Fatal("removed the running job")
	}
	if err := q.remove(b.ID); err != nil {
		t.Fatalf("remove waiting: %v", err)
	}
	if err := q.remove(b.ID); err == nil {
		t.Fatal("removing twice did not error")
	}
}

func TestQueueClearWaitingKeepsRunning(t *testing.T) {
	q := reopen(t, t.TempDir())
	a := mustAdd(t, q, "https://open.spotify.com/album/a")
	mustAdd(t, q, "https://open.spotify.com/album/b")
	mustAdd(t, q, "https://open.spotify.com/album/c")
	q.claimNext(false)
	q.clearWaiting()
	s := q.snapshot()
	if len(s.Items) != 1 || s.Items[0].ID != a.ID || s.RunningID != a.ID {
		t.Fatalf("clearWaiting = %+v", s)
	}
}

func TestQueueURLValidationAndTitles(t *testing.T) {
	q := reopen(t, t.TempDir())
	if _, err := q.addURLs([]string{"", "  ", "javascript:alert(1)", "file:///etc/passwd"}, ""); err == nil {
		t.Fatal("accepted a job with no downloadable links")
	}
	it, err := q.addURLs([]string{" https://tidal.com/album/1?u ", "https://tidal.com/album/1?u", "apple-music://library/songs"}, "")
	if err != nil {
		t.Fatal(err)
	}
	if len(it.URLs) != 2 {
		t.Fatalf("dedupe/trim failed: %v", it.URLs)
	}
	if it.Title != "tidal.com/album/1 (+1 more)" {
		t.Fatalf("default title = %q", it.Title)
	}
	long, _ := q.addURLs([]string{"https://x.com/a"}, strings.Repeat("é", 500))
	if n := len([]rune(long.Title)); n != queueMaxTitleRunes {
		t.Fatalf("title clamp = %d runes", n)
	}
	if !q.setTitle(it.ID, "ICEMAN") || q.setTitle(it.ID, "ICEMAN") || q.setTitle(it.ID, "  ") {
		t.Fatal("setTitle change detection is wrong")
	}
}

func TestQueueImportPayloadSurvivesRestartAndIsCleanedUp(t *testing.T) {
	dir := t.TempDir()
	q := reopen(t, dir)
	if _, err := q.addImport(`{"tracks":[]}`, "Empty", 0); err == nil {
		t.Fatal("accepted an import with no tracks")
	}
	it, err := q.addImport(testImportPayload, "My Mix", 1)
	if err != nil {
		t.Fatal(err)
	}
	r := reopen(t, dir)
	got, err := r.readImportPayload(it.ID)
	if err != nil || got != testImportPayload {
		t.Fatalf("payload after restart = %q %v", got, err)
	}
	if err := r.remove(it.ID); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(r.importPayloadPath(it.ID)); !os.IsNotExist(err) {
		t.Fatal("removing an import left its payload file behind")
	}
}

func TestQueueDropsImportWhosePayloadVanished(t *testing.T) {
	dir := t.TempDir()
	q := reopen(t, dir)
	it, _ := q.addImport(testImportPayload, "My Mix", 1)
	os.Remove(q.importPayloadPath(it.ID))
	if s := reopen(t, dir).snapshot(); len(s.Items) != 0 || s.Paused {
		t.Fatalf("unrebuildable import restored: %+v", s)
	}
}

func TestQueuePrunesOrphanImportFiles(t *testing.T) {
	dir := t.TempDir()
	q := reopen(t, dir)
	os.MkdirAll(q.importsPath(), 0o755)
	orphan := filepath.Join(q.importsPath(), "deadbeef.json")
	os.WriteFile(orphan, []byte("{}"), 0o600)
	reopen(t, dir)
	if _, err := os.Stat(orphan); !os.IsNotExist(err) {
		t.Fatal("orphan import payload not pruned")
	}
}

// A hand-edited queue.json must not be able to aim a payload path elsewhere.
func TestQueueRejectsHostileIDsFromDisk(t *testing.T) {
	dir := t.TempDir()
	os.MkdirAll(dir, 0o755)
	f := queueFile{Version: 1, Items: []QueueItem{
		{ID: "../../evil", Kind: QueueKindURLs, URLs: []string{"https://x.com/a"}, State: "queued"},
		{ID: "ok1", Kind: "shell", URLs: []string{"https://x.com/a"}, State: "queued"},
		{ID: "ok2", Kind: QueueKindURLs, URLs: []string{"ftp://x"}, State: "queued"},
		{ID: "good", Kind: QueueKindURLs, URLs: []string{"https://x.com/a"}, State: "queued"},
	}}
	b, _ := json.Marshal(f)
	os.WriteFile(filepath.Join(dir, queueFileName), b, 0o644)
	s := reopen(t, dir).snapshot()
	if len(s.Items) != 1 || s.Items[0].ID != "good" {
		t.Fatalf("hostile entries survived: %+v", s.Items)
	}
	if _, err := newDownloadQueue(dir).readImportPayload("../../evil"); err == nil {
		t.Fatal("readImportPayload accepted a traversal id")
	}
}

func TestQueueCorruptFileIsSetAsideNotFatal(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, queueFileName)
	os.WriteFile(path, []byte(`{"version":1,"items":[{"id":`), 0o644)
	q := reopen(t, dir)
	if s := q.snapshot(); len(s.Items) != 0 {
		t.Fatalf("corrupt file produced items: %+v", s)
	}
	if _, err := os.Stat(path + ".corrupt"); err != nil {
		t.Fatal("corrupt queue was not preserved for inspection")
	}
	mustAdd(t, q, "https://x.com/a") // still usable
}

func TestQueueSaveIsAtomicLeavesNoTempFiles(t *testing.T) {
	dir := t.TempDir()
	q := reopen(t, dir)
	for i := 0; i < 5; i++ {
		mustAdd(t, q, "https://x.com/"+string(rune('a'+i)))
	}
	entries, _ := os.ReadDir(dir)
	for _, e := range entries {
		if strings.HasSuffix(e.Name(), ".tmp") {
			t.Fatalf("temp file left behind: %s", e.Name())
		}
	}
	raw, err := os.ReadFile(filepath.Join(dir, queueFileName))
	if err != nil {
		t.Fatal(err)
	}
	var f queueFile
	if err := json.Unmarshal(raw, &f); err != nil || len(f.Items) != 5 || f.Version != queueFileVersion {
		t.Fatalf("persisted file = %v items, version %d, err %v", len(f.Items), f.Version, err)
	}
}

func TestQueueUnclaimReturnsJobAndPauses(t *testing.T) {
	q := reopen(t, t.TempDir())
	a := mustAdd(t, q, "https://x.com/a")
	mustAdd(t, q, "https://x.com/b")
	q.claimNext(false)
	q.unclaim(a.ID)
	s := q.snapshot()
	if !s.Paused || s.RunningID != "" || s.Items[0].ID != a.ID || s.Items[0].State != QueueStateQueued {
		t.Fatalf("unclaim = %+v", s)
	}
}

func TestQueueWaitingPosition(t *testing.T) {
	q := reopen(t, t.TempDir())
	a := mustAdd(t, q, "https://x.com/a")
	b := mustAdd(t, q, "https://x.com/b")
	c := mustAdd(t, q, "https://x.com/c")
	q.claimNext(false)
	if q.waitingPosition(a.ID) != 0 || q.waitingPosition(b.ID) != 1 || q.waitingPosition(c.ID) != 2 {
		t.Fatal("waiting positions wrong")
	}
}

func TestQueueCapacity(t *testing.T) {
	q := reopen(t, t.TempDir())
	for i := 0; i < queueMaxItems; i++ {
		q.items = append(q.items, QueueItem{ID: newQueueID(), Kind: QueueKindURLs, URLs: []string{"https://x.com"}, State: "queued"})
	}
	if _, err := q.addURLs([]string{"https://x.com/over"}, ""); err == nil {
		t.Fatal("queue accepted a job beyond its cap")
	}
}
