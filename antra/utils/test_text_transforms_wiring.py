"""v2.0.0 DMX-2 cross-file wiring.

The seven transforms travel:

  Svelte  ->  Go Config  ->  config.json  ->  json_cli env  ->  load_config
          ->  LibraryOrganizer  ->  migrate_legacy_templates  ->  FileTagger

Eight files that share nothing but a string key, so a typo in any one of them
fails SILENTLY: the setting simply stops working and nothing errors. Every hop
is asserted here, and the last one is asserted END TO END by writing a real
config.json, running the real json_cli env block against it, and reading the
value back out of a real FileTagger.

Run:  python -m antra.utils.test_text_transforms_wiring
"""
from __future__ import annotations

import ast
import json
import os
import pathlib
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from antra_shared.filename_prefs import (  # noqa: E402
    CASING_MODES,
    DEFAULT_FILENAME_PREFERENCES,
    FEATURED_MODES,
    MULTI_ARTIST_SEPARATORS,
)

FAILS: list[str] = []
CHECKS = 0

# The seven keys, with the default each layer must agree on.
KEYS: dict[str, object] = {
    "title_casing": "nothing",
    "artist_casing": "nothing",
    "remove_album_version": False,
    "remove_duplicate_artists": True,
    "featured_artists_mode": "nothing",
    "multi_artist_separator": "default",
    "album_various_artists": True,
}
# Go must use *bool for a boolean whose default is TRUE, or every config.json
# written before DMX-2 reads false and silently turns the setting off. That is
# the NotifyVolume / PreferExplicit / FR-2 trap, and it is why these two are
# listed separately from remove_album_version (whose default IS false).
GO_POINTER_BOOLS = ("remove_duplicate_artists", "album_various_artists")
GO_PLAIN_BOOLS = ("remove_album_version",)


def check(label: str, ok: bool, detail: str = "") -> None:
    global CHECKS
    CHECKS += 1
    if not ok:
        FAILS.append("%s%s" % (label, (" - " + detail) if detail else ""))
        print("  FAIL  %s%s" % (label, (" - " + detail) if detail else ""))


def read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def camel(key: str) -> str:
    return "".join(part.capitalize() for part in key.split("_"))


def main() -> int:
    print("\n[1] shared defaults")
    for key, want in KEYS.items():
        check("DEFAULT_FILENAME_PREFERENCES[%s] == %r" % (key, want),
              DEFAULT_FILENAME_PREFERENCES.get(key) == want,
              repr(DEFAULT_FILENAME_PREFERENCES.get(key)))

    print("\n[2] Go config struct")
    go = read("antra-wails/app_backend.go")
    for key in KEYS:
        check("Go json tag %s" % key, ('json:"%s,omitempty"' % key) in go)
        check("Go field %s" % camel(key), camel(key) in go)
    for key in GO_POINTER_BOOLS:
        # `*bool` on the same line as the tag — a plain bool here is the silent
        # default-flip, so it is asserted on the declaration, not the name.
        line = next((l for l in go.splitlines() if ('json:"%s,omitempty"' % key) in l), "")
        check("Go %s is *bool (defaults true)" % key, "*bool" in line, line.strip())
    for key in GO_PLAIN_BOOLS:
        line = next((l for l in go.splitlines() if ('json:"%s,omitempty"' % key) in l), "")
        check("Go %s is a plain bool (defaults false)" % key,
              "bool" in line and "*bool" not in line, line.strip())

    print("\n[3] generated TypeScript model")
    ts = read("antra-wails/frontend/wailsjs/go/models.ts")
    for key in KEYS:
        check("ts declares %s" % key, ("%s?:" % key) in ts)
        check("ts ctor assigns %s" % key, ('this.%s = source["%s"];' % (key, key)) in ts)

    print("\n[4] Svelte: init, guard, anchor, catalog")
    svelte = read("antra-wails/frontend/src/App.svelte")
    catalog = read("antra-wails/frontend/src/lib/settings-catalog.ts")
    for key in KEYS:
        check("Svelte config init has %s" % key, ("    %s:" % key) in svelte)
        # A control with no data-setting anchor is invisible to the command
        # palette; test_palette.cjs additionally pins the 1:1 with the catalog.
        check("exactly one data-setting=%s" % key,
              svelte.count('data-setting="%s"' % key) == 1,
              str(svelte.count('data-setting="%s"' % key)))
        check("catalog entry for %s" % key, ("id: '%s'" % key) in catalog)
        check("catalog surface is folder for %s" % key,
              ("id: '%s'" % key) in catalog and
              "surface: 'folder'" in catalog.split("id: '%s'" % key, 1)[1][:200])
    # The two true-by-default booleans must be guarded with a TYPE test, not a
    # falsy one: `if (!config.x) config.x = true` would flip a deliberately-off
    # setting back on at every launch.
    for key in ("remove_duplicate_artists", "album_various_artists"):
        check("Svelte guards %s by type, not truthiness" % key,
              ("typeof config.%s !== 'boolean'" % key) in svelte)
        check("Svelte does NOT falsy-guard %s" % key,
              ("if (!config.%s)" % key) not in svelte)
    # Every option id the UI can send must be a value Python accepts.
    for group, ids in (("CASING_OPTIONS", CASING_MODES),
                       ("FEATURED_OPTIONS", FEATURED_MODES),
                       ("SEPARATOR_OPTIONS", tuple(["default"] + sorted(MULTI_ARTIST_SEPARATORS)))):
        block = svelte.split("const %s = [" % group, 1)[1].split("];", 1)[0]
        ui = {seg.split("'", 1)[0] for seg in block.split("{ id: '")[1:]}
        check("%s matches Python exactly" % group, ui == set(ids),
              "ui=%r python=%r" % (sorted(ui), sorted(ids)))

    print("\n[5] json_cli env block is UNCONDITIONAL")
    cli = read("antra/json_cli.py")
    tree = ast.parse(cli)
    env_names = set()
    for node in ast.walk(tree):
        # os.environ["X"] = _art_*(...) — collect the keys written.
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            tgt = node.targets[0]
            if (isinstance(tgt, ast.Subscript)
                    and isinstance(tgt.value, ast.Attribute)
                    and tgt.value.attr == "environ"
                    and isinstance(tgt.slice, ast.Constant)):
                env_names.add(tgt.slice.value)
    for key in KEYS:
        check("json_cli writes %s" % key.upper(), key.upper() in env_names)
    # The BUG-23 trap: `if key in settings` lets a stale .env decide.
    for key in KEYS:
        check("%s is not behind an `in settings` guard" % key,
              ('if "%s" in settings' % key) not in cli)

    # The drift that made every transform inert in the shipped app: json_cli
    # carried FOUR byte-identical LibraryOrganizer constructions and only one
    # had the kwargs. Pinning "exactly one construction" is what stops a fifth
    # copy being added later and quietly bypassing the settings again.
    ctor_lines = [l for l in cli.splitlines() if "LibraryOrganizer(" in l]
    check("json_cli builds LibraryOrganizer in exactly one place",
          len(ctor_lines) == 1, repr(ctor_lines))
    call_lines = [l for l in cli.splitlines()
                  if "_make_organizer(cfg)" in l and not l.lstrip().startswith("def ")]
    check("every json_cli organizer goes through _make_organizer",
          len(call_lines) >= 3, repr(call_lines))

    print("\n[6] Config dataclass + load_config")
    cfgsrc = read("antra/core/config.py")
    import antra.core.config as cfgmod

    for key, want in KEYS.items():
        check("Config field %s" % key, ("    %s:" % key) in cfgsrc)
        check("load_config reads %s" % key.upper(), ('os.getenv("%s"' % key.upper()) in cfgsrc)
    saved = {k: os.environ.pop(k.upper(), None) for k in KEYS}
    try:
        cfg = cfgmod.load_config()
        for key, want in KEYS.items():
            check("load_config default %s == %r" % (key, want),
                  getattr(cfg, key) == want, repr(getattr(cfg, key)))
    finally:
        for k, v in saved.items():
            if v is not None:
                os.environ[k.upper()] = v

    print("\n[7] organizer -> tagger, end to end through a real config.json")
    # This is the hop that cannot be asserted from source: json_cli reads the
    # file, sets env, load_config reads env, service builds the organizer, and
    # the engine hands organizer.filename_preferences to the tagger.
    settings = {
        "download_path": tempfile.gettempdir(),
        "title_casing": "upper",
        "artist_casing": "lower",
        "remove_album_version": True,
        "remove_duplicate_artists": False,
        "featured_artists_mode": "move",
        "multi_artist_separator": "semicolon",
        "album_various_artists": False,
    }
    with tempfile.TemporaryDirectory() as td:
        cfg_path = os.path.join(td, "config.json")
        with open(cfg_path, "w", encoding="utf-8") as fh:
            json.dump(settings, fh)
        probe = (
            "import json, sys\n"
            # sys.path[0] is the SCRIPT's own directory, which is the temp dir
            # here rather than the repo root, so `import antra` has to be made
            # to work explicitly instead of relying on cwd.
            "sys.path.insert(0, %r)\n"
            # NO action flag: --probe and --spectrogram are handled and exit
            # BEFORE the config block, so passing one would skip the very hop
            # this case exists to exercise. With only --config, main() reaches
            # the env block and then exits for want of a URL, which is fine —
            # the environment is already set by then.
            "sys.argv = ['json_cli', '--config', %r]\n"
            "import antra.json_cli as jc\n"
            "try:\n"
            "    jc.main()\n"
            "except SystemExit:\n"
            "    pass\n"
            "import os\n"
            "from antra.core.config import load_config\n"
            "from antra.utils.tagger import FileTagger\n"
            "c = load_config()\n"
            "c.output_dir = %r\n"
            # Driven through json_cli's OWN `_make_organizer`, not a hand-built
            # LibraryOrganizer. An earlier cut of this case constructed the
            # organizer itself with the kwargs spelled out, so it passed while
            # the shipped app ignored every transform — json_cli had four
            # duplicated constructions and only one of them had been updated.
            # Testing the path the user takes is the whole point of section 7.
            "o = jc._make_organizer(c)\n"
            "t = FileTagger(filename_preferences=o.filename_preferences)\n"
            "keys = %r\n"
            "print('RESULT' + json.dumps({k: t.filename_preferences[k] for k in keys}))\n"
        ) % (str(ROOT), cfg_path, os.path.join(td, "lib"), list(KEYS))
        script = os.path.join(td, "probe.py")
        with open(script, "w", encoding="utf-8") as fh:
            fh.write(probe)
        env = dict(os.environ)
        for k in KEYS:
            env.pop(k.upper(), None)
        proc = subprocess.run([sys.executable, script], cwd=str(ROOT),
                              capture_output=True, text=True, errors="replace", env=env)
        line = next((l for l in (proc.stdout or "").splitlines()
                     if l.startswith("RESULT")), "")
        check("end-to-end probe produced a result", bool(line),
              ((proc.stdout or "") + (proc.stderr or ""))[-800:])
        if line:
            got = json.loads(line[len("RESULT"):])
            for key, want in settings.items():
                if key == "download_path":
                    continue
                check("config.json -> FileTagger: %s" % key, got.get(key) == want,
                      "got %r want %r" % (got.get(key), want))

    print("\n%d checks, %d failed" % (CHECKS, len(FAILS)))
    for f in FAILS:
        print("  - %s" % f)
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
