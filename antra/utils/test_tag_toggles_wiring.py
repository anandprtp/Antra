"""DMX-1 cross-file wiring.

`disabled_tags`, `date_format` and `write_id3v1` travel Svelte -> Go ->
config.json -> json_cli env -> load_config -> EngineConfig -> FileTagger.
Those five files share nothing but a string key, so a typo in any one of
them fails SILENTLY — the setting simply stops working. Each hop is asserted
here, and the two ends (the Svelte TAG_GROUPS list and TAG_FIELDS) are
compared directly, because a name that exists on only one side is a switch
that does nothing.

Run:  python -m antra.utils.test_tag_toggles_wiring
"""
from __future__ import annotations

import ast
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from antra.utils.tagger import DATE_FORMATS, TAG_FIELDS  # noqa: E402

FAILS: list[str] = []
CHECKS = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global CHECKS
    CHECKS += 1
    if not ok:
        FAILS.append("%s%s" % (label, (" — " + detail) if detail else ""))
        print("  FAIL  %s%s" % (label, (" — " + detail) if detail else ""))


def read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def main() -> int:
    print("\n[1] Svelte <-> tagger: the two ends of the toggle list")
    svelte = read("antra-wails/frontend/src/App.svelte")
    # Scoped to the END of the TAG_GROUPS literal, not "up to the next const":
    # the original slice ran to `const DATE_FORMATS`, so v2.0.0 DMX-2's option
    # tables — which sit between the two and share the `{ id, label }` shape —
    # were swept in and reported as ghost tag switches. Ending at the array's
    # own closing `];` cannot be broken by anything declared after it.
    block = svelte[svelte.index("const TAG_GROUPS"):]
    block = block[:block.index("\n  ];") + 5]
    ui_ids = re.findall(r"\{ id: '([a-z_]+)', label:", block)
    check("Svelte lists ids at all", bool(ui_ids), str(ui_ids))
    check("no duplicate ids in the UI", len(ui_ids) == len(set(ui_ids)), str(ui_ids))
    missing_ui = sorted(set(TAG_FIELDS) - set(ui_ids))
    check("every TAG_FIELDS id has a UI switch", not missing_ui, str(missing_ui))
    ghost = sorted(set(ui_ids) - set(TAG_FIELDS))
    check("no UI switch without a tagger field", not ghost,
          "%s would render a control that does nothing" % ghost)

    # Scoped to the DATE_FORMATS literal: the TAG_GROUPS entries above it
    # share the `{ id, label, note }` shape and would otherwise be swept in.
    fmt_block = svelte[svelte.index("const DATE_FORMATS"):]
    fmt_block = fmt_block[:fmt_block.index("];")]
    ui_fmts = re.findall(r"\{ id: '([a-z]+)',", fmt_block)
    check("UI offers exactly the tagger's date formats",
          set(ui_fmts) == set(DATE_FORMATS), "ui=%s tagger=%s" % (sorted(ui_fmts), sorted(DATE_FORMATS)))

    print("\n[2] Svelte state, guards and anchors")
    for key in ("disabled_tags", "date_format", "write_id3v1"):
        check("%s has an initial value" % key, re.search(r"\b%s:" % key, svelte) is not None)
    check("disabled_tags guarded as an array",
          "if (!Array.isArray(config.disabled_tags)) config.disabled_tags = [];" in svelte)
    check("write_id3v1 guarded to true (the *bool default trap)",
          "if (typeof config.write_id3v1 !== 'boolean') config.write_id3v1 = true;" in svelte)
    check("date_format guarded against an unknown value",
          "config.date_format = 'iso'" in svelte)
    for anchor in ("disabled_tags", "date_format", "write_id3v1"):
        check('data-setting="%s" anchor exists' % anchor,
              svelte.count('data-setting="%s"' % anchor) == 1,
              "found %d" % svelte.count('data-setting="%s"' % anchor))

    print("\n[3] settings catalog")
    catalog = read("antra-wails/frontend/src/lib/settings-catalog.ts")
    for anchor in ("disabled_tags", "date_format", "write_id3v1"):
        check("catalog indexes %s" % anchor, ("id: '%s'" % anchor) in catalog)

    print("\n[4] Go config struct")
    go = read("antra-wails/app_backend.go")
    check("Go: disabled_tags is a []string", 'DisabledTags        []string      `json:"disabled_tags,omitempty"`' in go)
    check("Go: date_format is a string", 'DateFormat          string        `json:"date_format,omitempty"`' in go)
    check("Go: write_id3v1 is a *bool (defaults to true)",
          'WriteId3v1          *bool         `json:"write_id3v1,omitempty"`' in go,
          "a plain bool would read false from every pre-DMX-1 config.json")

    print("\n[5] TypeScript model")
    ts = read("antra-wails/frontend/wailsjs/go/models.ts")
    for key in ("disabled_tags", "date_format", "write_id3v1"):
        check("models.ts declares %s" % key, ("%s?:" % key) in ts)
        check("models.ts assigns %s" % key, ('this.%s = source["%s"]' % (key, key)) in ts)

    print("\n[6] json_cli env mapping — set UNCONDITIONALLY (the BUG-23 trap)")
    cli = read("antra/json_cli.py")
    for env in ("DISABLED_TAGS", "DATE_FORMAT", "WRITE_ID3V1"):
        check("json_cli sets %s" % env, ('os.environ["%s"]' % env) in cli)
    # Not inside an `if key in settings` guard: a stale .env must not win.
    seg = cli[cli.index('os.environ["DISABLED_TAGS"]'):cli.index('os.environ["WRITE_ID3V1"]')]
    check("DISABLED_TAGS/DATE_FORMAT not behind an `in settings` guard",
          "in settings" not in seg, seg)

    print("\n[7] load_config reads the env")
    env_cases = [
        ({}, (), "iso", True),
        ({"DISABLED_TAGS": "lyrics,cover"}, ("lyrics", "cover"), "iso", True),
        ({"DISABLED_TAGS": " LYRICS , , cover "}, ("lyrics", "cover"), "iso", True),
        ({"DATE_FORMAT": "DMY"}, (), "dmy", True),
        ({"WRITE_ID3V1": "false"}, (), "iso", False),
    ]
    for env, want_tags, want_fmt, want_v1 in env_cases:
        code = (
            "import os,sys;sys.path.insert(0,%r)\n" % str(ROOT)
            + "".join("os.environ[%r]=%r\n" % (k, v) for k, v in env.items())
            + "from antra.core.config import load_config\n"
            + "c=load_config()\n"
            + "import json;print(json.dumps([list(c.disabled_tags),c.date_format,c.write_id3v1]))\n"
        )
        clean = {k: v for k, v in os.environ.items()
                 if k not in ("DISABLED_TAGS", "DATE_FORMAT", "WRITE_ID3V1")}
        out = subprocess.run([sys.executable, "-c", code], capture_output=True,
                             text=True, cwd=ROOT, env=clean)
        try:
            got = json.loads(out.stdout.strip().splitlines()[-1])
        except Exception:
            check("load_config %s" % env, False, out.stderr[-300:] or out.stdout[-300:])
            continue
        check("load_config %s -> tags" % (env or "defaults"), tuple(got[0]) == want_tags, str(got[0]))
        check("load_config %s -> date_format" % (env or "defaults"), got[1] == want_fmt, str(got[1]))
        check("load_config %s -> write_id3v1" % (env or "defaults"), got[2] == want_v1, str(got[2]))

    print("\n[8] EngineConfig -> FileTagger, on the real objects")
    src = read("antra/core/service.py")
    for key in ("disabled_tags", "date_format", "write_id3v1"):
        check("service.py forwards %s into EngineConfig" % key,
              ("%s=" % key) in src and ('getattr(cfg, "%s"' % key) in src)

    from antra.core.engine import DownloadEngine, EngineConfig
    from antra.utils.organizer import LibraryOrganizer

    with tempfile.TemporaryDirectory() as tmp:
        cfg = EngineConfig(disabled_tags=("lyrics", "cover"), date_format="mdy", write_id3v1=False)
        eng = DownloadEngine(
            resolver=None,  # unused by the constructor path under test
            organizer=LibraryOrganizer(tmp),
            config=cfg,
        )
        check("engine builds a tagger with the disabled set",
              eng.tagger.disabled_tags == frozenset({"lyrics", "cover"}),
              str(eng.tagger.disabled_tags))
        check("engine forwards date_format", eng.tagger.date_format == "mdy", eng.tagger.date_format)
        check("engine forwards write_id3v1", eng.tagger.write_id3v1 is False)
        # Regression: an EngineConfig that predates DMX-1 must behave as before.
        plain = DownloadEngine(resolver=None, organizer=LibraryOrganizer(tmp), config=EngineConfig())
        check("default EngineConfig disables nothing", plain.tagger.disabled_tags == frozenset())
        check("default EngineConfig is ISO", plain.tagger.date_format == "iso")
        check("default EngineConfig writes ID3v1", plain.tagger.write_id3v1 is True)

    print("\n[9] json_cli's mapping block, EXECUTED against real settings dicts")
    # The block is lifted out of json_cli.py and run, rather than read: this is
    # the one hop where a settings key becomes an env var, and a typo here is
    # silent. json_cli.py cannot be imported for this (its main() spawns a
    # download), so the statements are extracted by their marker comment.
    start = cli.index("            # v2.0.0 DMX-1 - per-tag write toggles.")
    end = cli.index('os.environ["WRITE_ID3V1"]')
    end = cli.index("\n", end) + 1
    block = "\n".join(line[12:] if line.startswith("            ") else line
                      for line in cli[start:end].split("\n"))
    check("extracted a non-trivial block", len(block) > 200, str(len(block)))

    def _art_str(settings, key, default):
        value = settings.get(key)
        value = "" if value is None else str(value).strip()
        return value or default

    def _art_bool(settings, key, default):
        value = settings.get(key)
        if value is None:
            value = default
        return "true" if value else "false"

    cases = [
        ({}, "", "iso", "true"),
        ({"disabled_tags": ["Lyrics", " cover ", ""]}, "lyrics,cover", "iso", "true"),
        ({"disabled_tags": "not-a-list"}, "", "iso", "true"),
        ({"date_format": " DMY "}, "", "DMY", "true"),
        ({"write_id3v1": False}, "", "iso", "false"),
    ]
    for settings, want_tags, want_fmt, want_v1 in cases:
        env: dict = {}
        scope = {
            "settings": settings,
            "os": type("os", (), {"environ": env})(),
            "_art_str": lambda k, d, _s=settings: _art_str(_s, k, d),
            "_art_bool": lambda k, d, _s=settings: _art_bool(_s, k, d),
        }
        exec(compile(block, "<json_cli DMX-1 block>", "exec"), scope)
        label = str(settings) if settings else "empty settings (a pre-DMX-1 config.json)"
        check("json_cli %s -> DISABLED_TAGS" % label, env.get("DISABLED_TAGS") == want_tags,
              repr(env.get("DISABLED_TAGS")))
        check("json_cli %s -> DATE_FORMAT" % label, env.get("DATE_FORMAT") == want_fmt,
              repr(env.get("DATE_FORMAT")))
        check("json_cli %s -> WRITE_ID3V1" % label, env.get("WRITE_ID3V1") == want_v1,
              repr(env.get("WRITE_ID3V1")))

    check("json_cli parses", isinstance(ast.parse(cli), ast.Module))

    print("\n%d checks, %d failed" % (CHECKS, len(FAILS)))
    for f in FAILS:
        print("  -", f)
    return 1 if FAILS else 0


if __name__ == "__main__":
    raise SystemExit(main())
