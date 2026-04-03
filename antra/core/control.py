"""
Cooperative download control helpers for future desktop frontends.

This currently supports pausing/resuming between tracks and cancelling
before the next track starts. It does not interrupt an in-flight download.
"""
import threading


class DownloadController:
    """Thread-safe pause/resume/cancel state for long-running downloads."""

    def __init__(self):
        self._resume_event = threading.Event()
        self._resume_event.set()
        self._cancel_event = threading.Event()

    def pause(self):
        self._resume_event.clear()

    def resume(self):
        self._resume_event.set()

    def cancel(self):
        self._cancel_event.set()
        self._resume_event.set()

    def wait_if_paused(self):
        self._resume_event.wait()

    def is_paused(self) -> bool:
        return not self._resume_event.is_set()

    def is_cancelled(self) -> bool:
        return self._cancel_event.is_set()
