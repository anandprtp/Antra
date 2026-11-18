"""
v2.0.0 DMX-6 — negative control for test_post_command.py.

A green suite proves nothing until it goes red against broken code. This
reverts each load-bearing property in place, re-runs the suite, and asserts it
FAILS — then restores every file byte-identically.

The sabotages are chosen to be the mistakes that would actually be made:
shell interpolation of the template, dropping a gate, dropping the timeout,
firing the hook before the overwrite backup is cleaned up.

Run:  python antra/utils/test_post_command_negctl.py
"""
from __future__ import annotations

import hashlib
import io
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MOD = os.path.join(ROOT, "antra", "utils", "post_command.py")
ENG = os.path.join(ROOT, "antra", "core", "engine.py")
CLI = os.path.join(ROOT, "antra", "json_cli.py")
SUITE = os.path.join(ROOT, "antra", "utils", "test_post_command.py")


def read(p):
    return io.open(p, encoding="utf-8", newline="").read()


def write(p, s):
    io.open(p, "w", encoding="utf-8", newline="").write(s)


def sha(p):
    return hashlib.sha256(io.open(p, "rb").read()).hexdigest()


def suite_passes():
    r = subprocess.run([sys.executable, SUITE], capture_output=True, text=True,
                       cwd=ROOT)
    return r.returncode == 0


# (label, file, old, new)
SABOTAGES = [
    # 1. The shell-injection hole itself.
    ("shell=True (the injection hole)", MOD,
     "                    shell=False,          # load-bearing - see module docstring",
     "                    shell=True,"),

    # 2. Re-split the rendered argv — the other way to reintroduce injection.
    ("render_argv re-splits its own output", MOD,
     "    return out\n\n\n# cmd.exe re-parses",
     "    return shlex.split(\" \".join(out))\n\n\n# cmd.exe re-parses"),

    # 3/4/5. Each gate, removed one at a time.
    ("confirmation gate removed", MOD,
     "        if not c.confirmed:",
     "        if False:"),
    ("enabled gate removed", MOD,
     "        if not c.enabled:",
     "        if False:"),
    ("mode gate removed", MOD,
     "        if c.mode != mode:",
     "        if False:"),

    # 6. Empty bare tokens passed as empty args instead of dropped.
    ("empty bare token no longer dropped", MOD,
     "        if had_token and rendered == \"\":\n            continue",
     "        if False:\n            continue"),

    # 7. Timeout not passed to the child.
    ("timeout not passed to subprocess", MOD,
     "                    timeout=self.cfg.timeout,",
     ""),

    # 8. Timeout clamp removed, so 0 or junk reaches subprocess.
    ("timeout clamp removed", MOD,
     "    return max(MIN_TIMEOUT, min(MAX_TIMEOUT, n))",
     "    return n"),

    # 9. Serialisation lock removed — parallel workers overlap.
    ("serialisation lock removed", MOD,
     "        with self._lock:\n            # Logged VERBATIM",
     "        if True:\n            # Logged VERBATIM"),

    # 10. The COMPLETED gate in the engine — a SKIPPED track would fire it.
    ("engine COMPLETED gate removed", ENG,
     "        if result.status == DownloadStatus.COMPLETED and result.file_path:",
     "        if result.file_path:"),

    # 11. Hook moved ahead of the overwrite cleanup.
    ("hook fires before _finish_overwrite_stash", ENG,
     "        self._finish_overwrite_stash(\r\n"
     "            stash, success=result.status == DownloadStatus.COMPLETED\r\n"
     "        )\r\n",
     "        pass\r\n"),

    # 12. json_cli made conditional — the BUG-23 trap.
    ("json_cli POST_COMMAND made conditional", CLI,
     '            os.environ["POST_COMMAND_ENABLED"] = _art_bool("post_command_enabled", False)',
     '            if "post_command_enabled" in settings:\n'
     '                os.environ["POST_COMMAND_ENABLED"] = _art_bool("post_command_enabled", False)'),
]


def main():
    before = {p: sha(p) for p in (MOD, ENG, CLI)}

    print("Control run (unmodified) — the suite must PASS:")
    if not suite_passes():
        print("  X suite does not pass before any sabotage; aborting")
        return 1
    print("  OK\n")

    caught = 0
    missed = []
    for label, path, old, new in SABOTAGES:
        src = read(path)
        if src.count(old) != 1:
            missed.append(f"{label}  (anchor matched {src.count(old)}x — sabotage invalid)")
            print(f"  ?? {label}: anchor matched {src.count(old)}x, skipped")
            continue
        write(path, src.replace(old, new, 1))
        try:
            failed = not suite_passes()
        finally:
            write(path, src)
            assert sha(path) == before[path], f"restore failed for {path}"
        if failed:
            caught += 1
            print(f"  CAUGHT   {label}")
        else:
            missed.append(label)
            print(f"  MISSED   {label}")

    print("\nControl run (restored) — the suite must PASS again:")
    okk = suite_passes()
    print("  OK" if okk else "  X suite does not pass after restore")

    for p in (MOD, ENG, CLI):
        assert sha(p) == before[p], f"{p} not restored byte-identically"
    print("All files restored byte-identically.")

    print("\n" + "=" * 62)
    print(f"negative control: {caught}/{len(SABOTAGES)} detected")
    if missed:
        print("MISSED:")
        for m in missed:
            print("  - " + m)
    print("=" * 62)
    return 0 if (caught == len(SABOTAGES) and okk) else 1


if __name__ == "__main__":
    sys.exit(main())
