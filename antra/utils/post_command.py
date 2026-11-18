"""
v2.0.0 DMX-6 - post-download command hook.

Runs a user-supplied program after a download finishes: trigger a Navidrome or
Jellyfin rescan, hand the file to `beets`, sync a folder to a phone.

WHY THIS IS ITS OWN MODULE, AND WHY IT NEVER TOUCHES A SHELL
------------------------------------------------------------
The obvious implementation interpolates template values into one string and
hands it to a shell. That is a command-injection hole with a music library as
its input: a track legitimately titled "; rm -rf ~" is not hypothetical, and
neither is an album called "AC/DC $(whoami)". Track titles come from remote
catalogues we do not control, so they must be treated as hostile input.

The rule here is therefore structural rather than a sanitiser:

    1. The TEMPLATE is split into an argument vector ONCE, before any value is
       known (`split_template`).
    2. Token values are substituted INTO the already-split elements
       (`render_argv`), so a value can never introduce a new argument, a pipe,
       a redirect or a quote - whatever it contains, it stays exactly one argv
       element.
    3. The vector is executed with shell=False.

There is no point at which a track title is parsed as syntax. A sanitiser that
blocklists metacharacters would be the weaker design: it has to be right about
every shell on every platform, where this has to be right once.

The rest is guard rails, all of which the DMX-6 spec calls non-negotiable:
off by default, an explicit confirmation before the first run, the resolved
argv logged verbatim *before* it runs, and a timeout.
"""
from __future__ import annotations

import logging
import os
import re
import shlex
import signal
import subprocess
import sys
import tempfile
import threading
from dataclasses import dataclass
from typing import Optional, Sequence

logger = logging.getLogger(__name__)

# On Windows, don't flash a console window for the hook (same treatment the
# transcoder and slskd manager already give their subprocesses).
_SUBPROCESS_FLAGS: dict = {}
if sys.platform == "win32":
    _SUBPROCESS_FLAGS["creationflags"] = subprocess.CREATE_NO_WINDOW

# When the hook fires.
#   "track" - after every track that produced a NEW file
#   "batch" - once, after a playlist/album finishes
COMMAND_MODES = ("track", "batch")
DEFAULT_COMMAND_MODE = "track"

# Bounds for the timeout, in seconds. A hook is a side effect, not part of the
# download: it must never be able to hold a worker open indefinitely.
MIN_TIMEOUT = 1
MAX_TIMEOUT = 600
DEFAULT_TIMEOUT = 30

_TOKEN_RE = re.compile(r"\{([a-z_]+)\}")

# Tokens each mode knows about. A token valid in the other mode is still
# *known* (so it renders empty rather than warning); anything else is a typo
# and is reported once.
TRACK_TOKENS = (
    "path", "filename", "folder", "release_folder", "library",
    "title", "artist", "album", "album_artist", "year",
    "isrc", "track", "disc", "source", "format",
)
BATCH_TOKENS = (
    "library", "release_folder", "playlist",
    "count", "failed", "total",
)
ALL_TOKENS = tuple(sorted(set(TRACK_TOKENS) | set(BATCH_TOKENS)))


def normalize_command_mode(value: Optional[str]) -> str:
    """Clamp an arbitrary string to a known mode. Unknown -> the default."""
    v = (value or "").strip().lower()
    return v if v in COMMAND_MODES else DEFAULT_COMMAND_MODE


def normalize_timeout(value) -> int:
    """Clamp the timeout into [MIN_TIMEOUT, MAX_TIMEOUT]. Junk -> the default."""
    try:
        n = int(value)
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT
    return max(MIN_TIMEOUT, min(MAX_TIMEOUT, n))


def split_template(template: str) -> list[str]:
    """Split the command TEMPLATE into argv elements, before any value is known.

    Windows is split with posix=False and the outer quotes stripped by hand,
    because POSIX-mode shlex eats backslashes - it would turn
    "C:\\Program Files\\x.exe" into "C:Program Filesx.exe". Elsewhere
    posix=True is correct and gives normal escape handling.

    Note this only ever sees the template's own literal text. Token VALUES are
    substituted afterwards and are never passed through a splitter, which is
    what makes a title containing quotes or semicolons harmless.
    """
    text = (template or "").strip()
    if not text:
        return []
    if sys.platform == "win32":
        parts = shlex.split(text, posix=False)
        out: list[str] = []
        for p in parts:
            if len(p) >= 2 and p[0] == p[-1] and p[0] in ('"', "'"):
                p = p[1:-1]
            out.append(p)
        return out
    return shlex.split(text, posix=True)


def render_argv(parts: Sequence[str], context: dict) -> list[str]:
    """Substitute {token} values into already-split argv elements.

    An element that contained at least one token and resolves to the empty
    string is DROPPED rather than passed as an empty argument: "beets import
    {path}" with no path should not become `beets import ""`, which most tools
    read as the current directory. An element that merely *contains* a token
    ("--path={path}") keeps its literal part and is never dropped.
    """
    unknown: set[str] = set()
    out: list[str] = []
    for part in parts:
        had_token = False

        def _sub(m: "re.Match") -> str:
            nonlocal had_token
            had_token = True
            name = m.group(1)
            if name not in ALL_TOKENS:
                unknown.add(name)
                return ""
            value = context.get(name)
            return "" if value is None else str(value)

        rendered = _TOKEN_RE.sub(_sub, part)
        if had_token and rendered == "":
            continue
        out.append(rendered)
    if unknown:
        logger.warning(
            "  [HOOK]  Unknown template token(s) %s - rendered empty. Known tokens: %s",
            ", ".join("{" + n + "}" for n in sorted(unknown)),
            ", ".join("{" + n + "}" for n in ALL_TOKENS),
        )
    return out


# cmd.exe re-parses the command line of a .bat/.cmd file, so an argument
# containing a quote or one of these can break out and run a second command
# (measured: a title of `x" & echo pwned> f & "` did). shell=False does not
# protect a batch file, so such a hook is refused instead of run.
_BATCH_EXTS = (".bat", ".cmd")
_CMD_META = set('"&|<>^%\r\n')


def _batch_injection_risk(argv: Sequence[str]) -> bool:
    if sys.platform != "win32" or not argv:
        return False
    if os.path.splitext(argv[0])[1].lower() not in _BATCH_EXTS:
        return False
    return any(_CMD_META & set(a) for a in argv[1:])


def _kill_tree(proc: "subprocess.Popen") -> None:
    """Kill the hook AND anything it spawned; a lone kill() leaves grandchildren."""
    try:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                           capture_output=True, timeout=10, **_SUBPROCESS_FLAGS)
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except Exception:
        pass
    try:
        proc.kill()
    except Exception:
        pass


def _execute(argv, *, shell, cwd, timeout):
    """Run the hook; return an object with returncode/stdout/stderr.

    Output goes to temp files, not pipes: with pipes, Windows waits for EOF
    after killing a timed-out child, so a background process that inherited
    stdout held the run (and the serialising lock) open past `timeout`. On
    timeout the whole tree is killed and subprocess.TimeoutExpired is raised.
    """
    with tempfile.TemporaryFile() as fout, tempfile.TemporaryFile() as ferr:
        kw = dict(_SUBPROCESS_FLAGS)
        if sys.platform != "win32":
            kw["start_new_session"] = True  # own group so killpg reaches grandchildren
        proc = subprocess.Popen(argv, shell=shell, cwd=cwd, stdin=subprocess.DEVNULL,
                                stdout=fout, stderr=ferr, **kw)
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill_tree(proc)
            proc.wait()
            raise
        fout.seek(0)
        ferr.seek(0)
        proc.stdout = fout.read(65536).decode("utf-8", "replace")
        proc.stderr = ferr.read(65536).decode("utf-8", "replace")
        return proc


@dataclass
class PostCommandConfig:
    enabled: bool = False
    # The user must confirm once, in the UI, before anything is ever executed.
    # Enforced HERE and not only in the UI so a config.json hand-edited to
    # `enabled: true` without the acknowledgement stays inert.
    confirmed: bool = False
    command: str = ""
    mode: str = DEFAULT_COMMAND_MODE
    timeout: int = DEFAULT_TIMEOUT

    def __post_init__(self):
        self.mode = normalize_command_mode(self.mode)
        self.timeout = normalize_timeout(self.timeout)
        self.command = (self.command or "").strip()

    @property
    def active(self) -> bool:
        return bool(self.enabled and self.confirmed and self.command)


class PostCommandRunner:
    """Executes the hook. Every failure is contained: a hook can never fail a track.

    Runs are SERIALISED behind a lock. `download_playlist` uses up to
    `max_workers` threads, so track-mode hooks would otherwise fire
    concurrently - hammering a library-server rescan endpoint N-way in parallel
    is worse than a short queue, and the hook is out-of-band work that nothing
    is waiting on anyway. It also bounds how many child processes can exist.
    """

    def __init__(self, cfg: Optional[PostCommandConfig] = None, library_root: str = ""):
        self.cfg = cfg or PostCommandConfig()
        self.library_root = library_root or ""
        self._lock = threading.Lock()
        self._warned_unconfirmed = False

    # -- reasons to do nothing -------------------------------------------
    def _skip_reason(self, mode: str) -> Optional[str]:
        c = self.cfg
        if not c.enabled:
            return "disabled"
        if not c.command:
            return "no command set"
        if not c.confirmed:
            return (
                "not confirmed - enable it once in Settings so the command is "
                "acknowledged before anything runs"
            )
        if c.mode != mode:
            return "mode is " + repr(c.mode)
        return None

    def _run(self, context: dict, mode: str) -> Optional[int]:
        reason = self._skip_reason(mode)
        if reason:
            if reason.startswith("not confirmed") and not self._warned_unconfirmed:
                self._warned_unconfirmed = True
                logger.warning("  [HOOK]  Post-download command %s.", reason)
            return None

        try:
            argv = render_argv(split_template(self.cfg.command), context)
        except ValueError as e:  # e.g. an unbalanced quote typed into Settings
            logger.warning("  [HOOK]  Post-download command is malformed (%s) - not run.", e)
            return None
        if not argv:
            logger.warning("  [HOOK]  Post-download command resolved to nothing - not run.")
            return None

        if _batch_injection_risk(argv):
            logger.warning(
                "  [HOOK]  Not run: a batch file (.bat/.cmd) is re-parsed by cmd.exe and a value "
                "contains a character it would treat as syntax. Point the command at an .exe or a "
                "script interpreter instead."
            )
            return None

        cwd = self.library_root if os.path.isdir(self.library_root) else None

        with self._lock:
            # Logged VERBATIM, before it runs, as the spec requires - so a user
            # can see exactly what was executed, argument by argument.
            logger.info("  [HOOK]  Running: %s", " ".join(shlex.quote(a) for a in argv))
            try:
                proc = _execute(
                    argv,
                    shell=False,          # load-bearing - see module docstring
                    cwd=cwd,
                    timeout=self.cfg.timeout,
                )
            except subprocess.TimeoutExpired:
                logger.warning(
                    "  [HOOK]  Command timed out after %ss and was terminated.",
                    self.cfg.timeout,
                )
                return None
            except FileNotFoundError:
                logger.warning("  [HOOK]  Command not found: %s", argv[0])
                return None
            except OSError as e:
                logger.warning("  [HOOK]  Command could not be started: %s", e)
                return None
            except Exception as e:  # never let a hook fail a track
                logger.warning("  [HOOK]  Command failed to run: %s", e)
                return None

        self._log_output(proc)
        if proc.returncode != 0:
            logger.warning("  [HOOK]  Command exited with status %s.", proc.returncode)
        return proc.returncode

    @staticmethod
    def _log_output(proc) -> None:
        for stream, label in ((proc.stdout, "out"), (proc.stderr, "err")):
            if not stream:
                continue
            lines = [ln for ln in stream.splitlines() if ln.strip()][:5]
            for ln in lines:
                logger.info("  [HOOK]  %s: %s", label, ln[:300])

    # -- public entry points ---------------------------------------------
    def run_for_track(self, *, file_path: str, track, source: str = "",
                      release_folder: str = "") -> Optional[int]:
        ext = os.path.splitext(file_path)[1].lstrip(".").lower()
        ctx = {
            "path": file_path,
            "filename": os.path.basename(file_path),
            "folder": os.path.dirname(file_path),
            "release_folder": release_folder or os.path.dirname(file_path),
            "library": self.library_root,
            "title": getattr(track, "title", "") or "",
            "artist": getattr(track, "artist_string", "") or "",
            "album": getattr(track, "album", "") or "",
            # TrackMetadata has no album_artist_string; album_artists is a list
            # and falls back to the track artists, which is the same chain the
            # organizer's {album_artist} token uses.
            "album_artist": ", ".join(getattr(track, "album_artists", None) or [])
                            or (getattr(track, "artist_string", "") or ""),
            "year": getattr(track, "release_year", "") or "",
            "isrc": getattr(track, "isrc", "") or "",
            "track": getattr(track, "track_number", "") or "",
            "disc": getattr(track, "disc_number", "") or "",
            "source": source or "",
            "format": ext,
        }
        return self._run(ctx, "track")

    def run_for_batch(self, *, downloaded: int, failed: int, total: int,
                      playlist_name: str = "", release_folder: str = "") -> Optional[int]:
        ctx = {
            "library": self.library_root,
            "release_folder": release_folder or "",
            "playlist": playlist_name or "",
            "count": downloaded,
            "failed": failed,
            "total": total,
        }
        return self._run(ctx, "batch")
