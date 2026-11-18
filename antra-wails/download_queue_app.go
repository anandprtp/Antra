package main

// v2.0.0 SF-1 - App bindings for the persisted download queue. State and
// persistence live in download_queue.go; this file starts backend processes
// and tells the frontend what happened.

import (
	"context"
	"errors"
	"os"

	wailsRuntime "github.com/wailsapp/wails/v2/pkg/runtime"
)

func (a *App) dq() *downloadQueue {
	a.queueOnce.Do(func() {
		a.queue = newDownloadQueue(getAppDataDir())
		a.queue.load()
	})
	return a.queue
}

func (a *App) backendBusy() bool {
	a.mu.Lock()
	defer a.mu.Unlock()
	return a.activeCmd != nil
}

func (a *App) emitQueueChanged() {
	if a.ctx == nil {
		return
	}
	wailsRuntime.EventsEmit(a.ctx, "backend-event", map[string]interface{}{
		"type":  "queue_changed",
		"queue": a.dq().snapshot(),
	})
}

// GetQueue returns the current queue for rendering.
func (a *App) GetQueue() QueueSnapshot {
	return a.dq().snapshot()
}

// EnqueueDownload adds links as one job. When nothing is downloading it starts
// immediately - even if older restored jobs are still waiting for Resume, since
// a link the user just pasted is plainly meant to run now.
func (a *App) EnqueueDownload(urls []string, title string) (QueueEnqueueResult, error) {
	it, err := a.dq().addURLs(urls, title)
	if err != nil {
		return QueueEnqueueResult{}, err
	}
	return a.startOrWait(it)
}

// EnqueueImport adds an imported playlist as one job.
func (a *App) EnqueueImport(payload, title string, trackCount int) (QueueEnqueueResult, error) {
	it, err := a.dq().addImport(payload, title, trackCount)
	if err != nil {
		return QueueEnqueueResult{}, err
	}
	return a.startOrWait(it)
}

func (a *App) startOrWait(it QueueItem) (QueueEnqueueResult, error) {
	q := a.dq()
	if claimed, ok := q.claimByID(it.ID, a.backendBusy()); ok {
		if err := a.runQueueItem(claimed); err != nil {
			a.emitQueueChanged()
			return QueueEnqueueResult{ID: it.ID, Position: q.waitingPosition(it.ID)}, err
		}
		a.emitQueueChanged()
		return QueueEnqueueResult{ID: it.ID, Started: true}, nil
	}
	a.emitQueueChanged()
	return QueueEnqueueResult{ID: it.ID, Position: q.waitingPosition(it.ID)}, nil
}

// StartNextQueued runs the next waiting job. The frontend calls it when a
// process ends and no failed-track retries are pending, so retries always run
// before the next queued release. It is a no-op when paused, busy or empty.
func (a *App) StartNextQueued() bool {
	it, ok := a.dq().claimNext(a.backendBusy())
	if !ok {
		return false
	}
	err := a.runQueueItem(it)
	a.emitQueueChanged()
	return err == nil
}

// ResumeQueue unpauses and starts the next job if idle.
func (a *App) ResumeQueue() bool {
	a.dq().setPaused(false)
	started := a.StartNextQueued()
	if !started {
		a.emitQueueChanged()
	}
	return started
}

// RemoveQueueItem deletes a waiting job.
func (a *App) RemoveQueueItem(id string) error {
	err := a.dq().remove(id)
	a.emitQueueChanged()
	return err
}

// ClearQueue deletes every waiting job (the running one is untouched).
func (a *App) ClearQueue() {
	a.dq().clearWaiting()
	a.emitQueueChanged()
}

// SetQueueItemTitle replaces the link-derived title once the release name is
// known, so a restore prompt reads "ICEMAN" rather than a URL.
func (a *App) SetQueueItemTitle(id, title string) {
	if a.dq().setTitle(id, title) {
		a.emitQueueChanged()
	}
}

// runQueueItem starts the backend for a claimed job. On a start failure the
// job goes back to the head of the queue and the queue pauses.
func (a *App) runQueueItem(it QueueItem) error {
	q := a.dq()
	fail := func(err error) error {
		q.unclaim(it.ID)
		if a.ctx != nil {
			wailsRuntime.LogErrorf(a.ctx, "Queued download failed to start: %v", err)
		}
		return err
	}

	urls := it.URLs
	if kindHasPayload(it.Kind) {
		urls = []string{}
	}
	command, baseArgs, workDir, env, err := a.resolveBackendCommand(urls)
	if err != nil {
		return fail(err)
	}

	args := baseArgs
	tempPath := ""
	if kindHasPayload(it.Kind) {
		payload, rerr := q.readImportPayload(it.ID)
		if rerr != nil {
			return fail(errors.New("the queued track data is missing"))
		}
		// PV-1 - a selection carries full TrackMetadata, an import carries parsed
		// file rows; they go to different backend flags and must not be swapped.
		if it.Kind == QueueKindSelection {
			if tempPath, err = writeSelectionPayload(payload); err != nil {
				return fail(err)
			}
			args = selectionArgs(baseArgs, tempPath)
		} else {
			if tempPath, err = writeImportPayload(payload); err != nil {
				return fail(err)
			}
			args = importArgs(baseArgs, tempPath)
		}
	}

	a.mu.Lock()
	a.isStopping = false
	a.mu.Unlock()

	if a.ctx != nil {
		wailsRuntime.EventsEmit(a.ctx, "backend-event", map[string]interface{}{
			"type": "queue_job_started",
			"item": it,
		})
	}

	ctx, cancel := context.WithCancel(a.ctx)
	onEnd := func(status string) {
		a.mu.Lock()
		shuttingDown := a.shuttingDown
		a.mu.Unlock()
		q.finish(it.ID, status, shuttingDown)
		if !shuttingDown {
			a.emitQueueChanged()
		}
	}
	if err := a.startBackendProcess(ctx, cancel, command, args, workDir, env, onEnd); err != nil {
		if tempPath != "" {
			os.Remove(tempPath)
		}
		err = fail(err)
		// queue_job_started already told the frontend a job is running; end it
		// the same way a real process exit would, so the view does not hang.
		if a.ctx != nil {
			wailsRuntime.EventsEmit(a.ctx, "backend-event", map[string]interface{}{
				"type": "log", "level": "error", "message": "Library engine failed to start: " + err.Error(),
			})
			wailsRuntime.EventsEmit(a.ctx, "backend-event", map[string]interface{}{
				"type": "process_ended", "status": "failed",
			})
		}
		return err
	}
	return nil
}
